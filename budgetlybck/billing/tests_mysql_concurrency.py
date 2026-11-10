"""Opt-in real-MySQL concurrency tests for the Paystack billing workflow.

These are deliberately skipped unless a disposable MySQL/MariaDB Django test
database is configured and explicitly enabled. SQLite does not exercise
``SELECT ... FOR UPDATE`` semantics and must never be used to claim coverage.
"""

import hashlib
import hmac
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest import SkipTest
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import close_old_connections, connection
from django.test import TransactionTestCase, override_settings
from rest_framework.test import APIRequestFactory, force_authenticate

from billing.models import (LifetimeAccessEntitlement, LifetimeAccessOffer,
                            EntitlementRestorationDecision, PaystackPurchaseAttempt,
                            PaystackWebhookEvent)
from billing.services.access import apply_verification
from billing.services.dispute_restoration import (DisputeRestorationError,
                                                  execute_restoration)
from billing.views import PaystackWebhookView, PurchaseInitializeView


RUN_MYSQL_CONCURRENCY = (
    connection.vendor == 'mysql'
    and os.getenv('BILLING_RUN_MYSQL_CONCURRENCY_TESTS') == '1'
)


@override_settings(
    DEBUG=True,
    PAYSTACK_MODE='test',
    PAYSTACK_SECRET_KEY='sk_test_concurrency',
    PAYSTACK_WEBHOOK_SIGNING_SECRET='whsec_concurrency',
    PAYSTACK_CALLBACK_URL='http://localhost:8000/api/billing/paystack/callback/',
    FRONTEND_PAYMENT_RETURN_URL='http://localhost:5173/payment/complete',
)
class MySQLPaystackConcurrencyTests(TransactionTestCase):
    """Run only with ``BILLING_RUN_MYSQL_CONCURRENCY_TESTS=1`` on MySQL."""

    databases = {'default'}

    @classmethod
    def setUpClass(cls):
        if not RUN_MYSQL_CONCURRENCY:
            raise SkipTest(
                'Requires disposable MySQL/MariaDB and BILLING_RUN_MYSQL_CONCURRENCY_TESTS=1.'
            )
        super().setUpClass()

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            'mysql-payer', 'mysql-payer@example.com', 'SafePassword123!'
        )
        self.offer = LifetimeAccessOffer.objects.create(
            name='MySQL concurrency offer', price_minor=105000, currency='KES', is_active=True
        )
        self.factory = APIRequestFactory()

    def create_attempt(self):
        return PaystackPurchaseAttempt.objects.create(
            user=self.user,
            offer=self.offer,
            expected_amount_minor=105000,
            expected_currency='KES',
        )

    @staticmethod
    def provider_data(reference):
        return {
            'id': 998877,
            'reference': reference,
            'status': 'success',
            'amount': 105000,
            'currency': 'KES',
        }

    def run_workers(self, *workers):
        barrier = threading.Barrier(len(workers))

        def wrapped_worker(worker):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return worker()
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(wrapped_worker, worker) for worker in workers]
            return [future.result(timeout=20) for future in futures]

    def test_parallel_verification_creates_exactly_one_entitlement(self):
        attempt = self.create_attempt()

        worker = lambda: apply_verification(attempt.reference, self.provider_data(attempt.reference))[2]
        results = self.run_workers(worker, worker)

        self.assertEqual(results.count(True), 1)
        self.assertEqual(LifetimeAccessEntitlement.objects.filter(user=self.user).count(), 1)
        self.assertEqual(LifetimeAccessEntitlement.objects.get(user=self.user).provider_mode, 'test')
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, PaystackPurchaseAttempt.SUCCESSFUL)

    @patch('billing.views.initialize_transaction')
    def test_parallel_initialization_creates_one_checkout_reference(self, initialize):
        initialize.side_effect = lambda **kwargs: {
            'reference': kwargs['reference'],
            'authorization_url': 'https://checkout.test/mysql',
        }

        def initialize_request():
            user = get_user_model().objects.get(pk=self.user.pk)
            request = self.factory.post('/api/billing/purchases/initialize/', {}, format='json')
            force_authenticate(request, user=user)
            return PurchaseInitializeView.as_view()(request).status_code

        statuses = self.run_workers(initialize_request, initialize_request)

        self.assertCountEqual(statuses, [201, 409])
        self.assertEqual(PaystackPurchaseAttempt.objects.filter(user=self.user).count(), 1)

    def test_manual_verification_and_webhook_converge_on_one_entitlement(self):
        attempt = self.create_attempt()
        event = {
            'event': 'charge.success',
            'data': {'id': 998877, 'reference': attempt.reference},
        }
        raw = json.dumps(event, separators=(',', ':')).encode('utf-8')
        timestamp = int(time.time())
        signature = hmac.new(
            b'whsec_concurrency', f'{timestamp}.'.encode('ascii') + raw, hashlib.sha512
        ).hexdigest()

        def manual_verify():
            return apply_verification(attempt.reference, self.provider_data(attempt.reference))[2]

        def webhook_verify():
            request = self.factory.post(
                '/api/billing/paystack/webhook/', raw, content_type='application/json',
                HTTP_PAYSTACK_SIGNATURE=f't={timestamp},v1=whk_test:{signature}',
            )
            with patch(
                'billing.views.verify_and_activate_purchase',
                side_effect=lambda reference: apply_verification(reference, self.provider_data(reference)),
            ):
                return PaystackWebhookView.as_view()(request).status_code

        results = self.run_workers(manual_verify, webhook_verify)

        self.assertIn(True, results)
        self.assertIn(200, results)
        self.assertEqual(LifetimeAccessEntitlement.objects.filter(user=self.user).count(), 1)
        self.assertEqual(PaystackWebhookEvent.objects.count(), 1)

    def test_parallel_restoration_refuses_ambiguous_provider_outcome(self):
        self.attempt = self.create_attempt()
        self.attempt.status = PaystackPurchaseAttempt.SUCCESSFUL
        self.attempt.provider_transaction_id = '998877'
        self.attempt.provider_metadata = {'dispute_review_required': True}
        self.attempt.save(update_fields=['status', 'provider_transaction_id', 'provider_metadata'])
        entitlement = LifetimeAccessEntitlement.objects.create(
            user=self.user,
            source=LifetimeAccessEntitlement.PAYSTACK_PURCHASE,
            activating_purchase=self.attempt,
            status=LifetimeAccessEntitlement.CHARGEBACK_SUSPENDED,
        )
        User = get_user_model()
        approver = User.objects.create_superuser('mysql-approver', 'mysql-approver@example.com', 'SafePassword123!')
        executor_one = User.objects.create_superuser('mysql-executor-one', 'mysql-executor-one@example.com', 'SafePassword123!')
        executor_two = User.objects.create_superuser('mysql-executor-two', 'mysql-executor-two@example.com', 'SafePassword123!')
        decision = EntitlementRestorationDecision.objects.create(
            purchase=self.attempt,
            purchase_reference=self.attempt.reference,
            entitlement=entitlement,
            user=self.user,
            provider_transaction_id='998877',
            approved_by=approver,
            approved_by_identifier=f'{approver.pk}:{approver.username}',
            evidence_reference='MYSQL-CASE-1',
            reason='Disposable MySQL concurrency test.',
            previous_status=LifetimeAccessEntitlement.CHARGEBACK_SUSPENDED,
        )
        disputes = [{
            'status': 'resolved',
            'resolution': 'declined',
            'refund_amount': None,
            'transaction': {'id': 998877, 'reference': self.attempt.reference},
        }]

        def execute(executor):
            try:
                execute_restoration(
                    decision_id=decision.decision_id,
                    operator=executor,
                    provider_data=self.provider_data(self.attempt.reference),
                    disputes=disputes,
                )
                return 'executed'
            except DisputeRestorationError:
                return 'rejected'

        results = self.run_workers(lambda: execute(executor_one), lambda: execute(executor_two))

        self.assertCountEqual(results, ['rejected', 'rejected'])
        decision.refresh_from_db()
        entitlement.refresh_from_db()
        self.assertEqual(decision.state, EntitlementRestorationDecision.APPROVED)
        self.assertEqual(entitlement.status, LifetimeAccessEntitlement.CHARGEBACK_SUSPENDED)
