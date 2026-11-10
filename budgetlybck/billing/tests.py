from datetime import datetime, timedelta, timezone as datetime_timezone
import hashlib
import hmac
import importlib
from io import StringIO
import json
import os
import time
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.apps import apps
from django.core.exceptions import ValidationError
from django.core.management import CommandError, call_command
from django.db import IntegrityError
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase, APIRequestFactory, force_authenticate

from budget.models import Account, Category, Transaction
from budgetlybck.settings import env_positive_int_set
from .models import (LifetimeAccessEntitlement, LifetimeAccessOffer,
                     EntitlementRestorationDecision, PaystackPurchaseAttempt,
                     PaystackWebhookEvent)
from .permissions import has_lifetime_access
from .services.paystack import (PaystackConfigurationError, PaystackUnavailableError,
                                get_config, initialize_transaction,
                                webhook_signature_valid)
from .services.dispute_restoration import (DisputeRestorationError,
                                           approve_restoration,
                                           execute_restoration)
from .services.access import (PurchaseVerificationError, apply_reversal,
                              apply_verification)
from .views import PaystackWebhookView, PurchaseInitializeView


User = get_user_model()


class BillingModelTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('billing-user', 'billing@example.com', 'SafePassword123!')
        self.offer = LifetimeAccessOffer.objects.create(
            name='Budgetly Lifetime Access',
            description='Lifetime access',
            price_minor=105000,
            currency='KES',
            is_active=True,
        )

    def attempt(self, **overrides):
        values = {
            'user': self.user,
            'offer': self.offer,
            'expected_amount_minor': 105000,
            'expected_currency': 'KES',
        }
        values.update(overrides)
        return PaystackPurchaseAttempt.objects.create(**values)

    def test_offer_uses_confirmed_kes_price_and_only_one_active_offer(self):
        self.assertEqual((self.offer.price_minor, self.offer.currency), (105000, 'KES'))
        with self.assertRaises(ValidationError):
            LifetimeAccessOffer.objects.create(
                name='Another offer', price_minor=105000, currency='KES', is_active=True
            )
        with self.assertRaises(ValidationError):
            LifetimeAccessOffer.objects.create(name='USD', price_minor=105000, currency='USD')

    def test_purchase_reference_and_provider_transaction_id_are_unique(self):
        first = self.attempt(provider_transaction_id='paystack-123')
        other_user = User.objects.create_user('billing-user-2', 'billing2@example.com', 'SafePassword123!')
        with self.assertRaises(ValidationError):
            self.attempt(user=other_user, provider_transaction_id='paystack-123')
        self.assertTrue(first.reference.startswith('bgt_'))
        self.assertNotEqual(first.reference, self.attempt().reference)

    def test_attempt_snapshot_must_match_offer(self):
        with self.assertRaises(ValidationError):
            self.attempt(expected_amount_minor=100000)
        attempt = self.attempt()
        self.offer.price_minor = 110000
        with self.assertRaises(ValidationError):
            self.offer.save()
        self.assertEqual(attempt.expected_amount_minor, 105000)

    def test_only_one_entitlement_and_safe_status_transitions(self):
        entitlement, created = LifetimeAccessEntitlement.grant_legacy_access(self.user)
        self.assertTrue(created)
        self.assertTrue(has_lifetime_access(self.user))
        same, created_again = LifetimeAccessEntitlement.grant_legacy_access(self.user)
        self.assertFalse(created_again)
        self.assertEqual(same.pk, entitlement.pk)
        entitlement.transition_to(LifetimeAccessEntitlement.CHARGEBACK_SUSPENDED)
        self.assertFalse(has_lifetime_access(self.user))
        entitlement.transition_to(LifetimeAccessEntitlement.ACTIVE)
        entitlement.transition_to(LifetimeAccessEntitlement.REFUNDED)
        with self.assertRaises(ValidationError):
            entitlement.transition_to(LifetimeAccessEntitlement.ACTIVE)

    def test_webhook_deduplication_key_is_unique(self):
        PaystackWebhookEvent.objects.create(
            deduplication_key='charge.success:reference', event_type='charge.success', payload_digest='a' * 64
        )
        with self.assertRaises(IntegrityError):
            PaystackWebhookEvent.objects.create(
                deduplication_key='charge.success:reference', event_type='charge.success', payload_digest='b' * 64
            )

    def test_legacy_grant_does_not_change_financial_records(self):
        account = Account.objects.create(user=self.user, name='Cash', account_type='cash', currency='KES')
        category = Category.objects.create(user=self.user, name='Food', category_type='expense')
        transaction = Transaction.objects.create(
            user=self.user, account=account, category=category, transaction_type='expense',
            description='Lunch', amount='10.00', transaction_date='2026-01-01'
        )

        LifetimeAccessEntitlement.grant_legacy_access(self.user)

        self.assertTrue(Transaction.objects.filter(pk=transaction.pk).exists())
        account.refresh_from_db()
        self.assertEqual(str(account.current_balance), '-10.00')


@override_settings(PAYSTACK_MODE='live')
class PaymentReconciliationReportTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('reconcile-user', 'reconcile@example.com', 'SafePassword123!')
        self.offer = LifetimeAccessOffer.objects.create(
            name='Reconciliation offer', price_minor=105000, currency='KES', is_active=True,
        )

    def attempt(self, mode='live', **overrides):
        values = {
            'user': self.user,
            'offer': self.offer,
            'expected_amount_minor': 105000,
            'expected_currency': 'KES',
            'provider_mode': mode,
        }
        values.update(overrides)
        return PaystackPurchaseAttempt.objects.create(**values)

    def entitlement(self, attempt, status_value):
        return LifetimeAccessEntitlement.objects.create(
            user=attempt.user,
            source=LifetimeAccessEntitlement.PAYSTACK_PURCHASE,
            activating_purchase=attempt,
            provider_mode=attempt.provider_mode,
            status=status_value,
        )

    def test_live_report_is_mode_scoped_and_lists_manual_findings_without_mutation(self):
        missing = self.attempt(status=PaystackPurchaseAttempt.SUCCESSFUL)
        suspended_user = User.objects.create_user('reconcile-suspended', 'reconcile-suspended@example.com', 'SafePassword123!')
        suspended = self.attempt(user=suspended_user, status=PaystackPurchaseAttempt.SUCCESSFUL)
        self.entitlement(suspended, LifetimeAccessEntitlement.CHARGEBACK_SUSPENDED)
        refunded_user = User.objects.create_user('reconcile-refunded', 'reconcile-refunded@example.com', 'SafePassword123!')
        refunded = self.attempt(user=refunded_user, status=PaystackPurchaseAttempt.SUCCESSFUL)
        self.entitlement(refunded, LifetimeAccessEntitlement.REFUNDED)
        deleted = self.attempt(status=PaystackPurchaseAttempt.SUCCESSFUL, user=None)
        stale = self.attempt(status=PaystackPurchaseAttempt.PENDING)
        PaystackPurchaseAttempt.objects.filter(pk=stale.pk).update(
            created_at=timezone.now() - timedelta(minutes=61)
        )
        review = self.attempt(provider_metadata={
            'refund_review_required': True,
            'dispute_review_required': True,
        })
        test_attempt = self.attempt('test', status=PaystackPurchaseAttempt.SUCCESSFUL)
        historical = self.attempt()
        PaystackPurchaseAttempt.objects.filter(pk=historical.pk).update(provider_mode=None)
        PaystackWebhookEvent.objects.create(
            deduplication_key='live:charge.success:failed', event_type='charge.success',
            provider_mode='live', transaction_reference=missing.reference,
            payload_digest='a' * 64, processing_status=PaystackWebhookEvent.FAILED,
            error_category='verification_failed',
        )
        PaystackWebhookEvent.objects.create(
            deduplication_key='test:charge.success:failed', event_type='charge.success',
            provider_mode='test', transaction_reference=test_attempt.reference,
            payload_digest='b' * 64, processing_status=PaystackWebhookEvent.FAILED,
            error_category='verification_failed',
        )

        output = StringIO()
        call_command(
            'payment_reconciliation_report', '--mode', 'live',
            '--stale-after-minutes', '60', stdout=output,
        )
        report = output.getvalue()

        self.assertIn('Requested provider mode: live', report)
        self.assertIn('Configured Paystack mode: live', report)
        self.assertIn(missing.reference, report)
        self.assertIn(suspended.reference, report)
        self.assertIn(refunded.reference, report)
        self.assertIn(deleted.reference, report)
        self.assertIn(stale.reference, report)
        self.assertIn(review.reference, report)
        self.assertIn(historical.reference, report)
        self.assertIn('verification_failed: 1', report)
        self.assertNotIn(test_attempt.reference, report)

        missing.refresh_from_db()
        self.assertEqual(missing.status, PaystackPurchaseAttempt.SUCCESSFUL)
        self.assertFalse(LifetimeAccessEntitlement.objects.filter(activating_purchase=missing).exists())

    def test_mode_is_required_and_stale_threshold_must_be_positive(self):
        with self.assertRaises(CommandError):
            call_command('payment_reconciliation_report')
        with self.assertRaises(CommandError):
            call_command('payment_reconciliation_report', '--mode', 'live', '--stale-after-minutes', '0')


@override_settings(BILLING_ENFORCE_LIFETIME_ACCESS=True)
class LifetimeAccessEnforcementTests(APITestCase):
    """Server-side access gating for every financial API family."""

    def setUp(self):
        self.user = User.objects.create_user('unpaid', 'unpaid@example.com', 'SafePassword123!')
        self.other_user = User.objects.create_user('paid-other', 'other@example.com', 'SafePassword123!')
        self.account = Account.objects.create(user=self.user, name='Cash', account_type='cash', currency='KES')
        self.other_account = Account.objects.create(user=self.other_user, name='Other cash', account_type='cash', currency='KES')
        self.category = Category.objects.create(name='Food', category_type='expense')
        self.client.force_authenticate(self.user)

    def assert_payment_required(self, response):
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data, {
            'detail': 'Lifetime access is required for financial features.',
            'code': 'payment_required',
        })

    def grant_access(self, user=None):
        return LifetimeAccessEntitlement.grant_legacy_access(user or self.user)[0]

    def test_disabled_by_default_preserves_financial_access(self):
        with self.settings(BILLING_ENFORCE_LIFETIME_ACCESS=False):
            response = self.client.get('/api/accounts/')
        self.assertEqual(response.status_code, 200)

    def test_unpaid_user_is_denied_financial_reads_and_writes(self):
        for response in (
            self.client.get('/api/categories/'),
            self.client.get('/api/accounts/'),
            self.client.get('/api/transactions/'),
            self.client.get('/api/transactions/summary/'),
            self.client.get('/api/transfers/'),
            self.client.get('/api/loans/'),
            self.client.get('/api/loan-repayments/'),
            self.client.get('/api/budgets/'),
            self.client.get('/api/savings/goals/'),
            self.client.get('/api/savings/contributions/'),
            self.client.get('/api/savings/withdrawals/'),
            self.client.get('/api/savings/plans/'),
            self.client.get('/api/dashboard/summary/'),
            self.client.get('/api/exchange-rates/?from=KES&to=USD'),
            self.client.post('/api/accounts/', {'name': 'Blocked', 'account_type': 'cash', 'currency': 'KES'}, format='json'),
            self.client.patch(f'/api/accounts/{self.account.pk}/', {'name': 'Blocked'}, format='json'),
            self.client.delete(f'/api/accounts/{self.account.pk}/'),
        ):
            self.assert_payment_required(response)

    def test_active_entitlement_allows_financial_access_but_ownership_still_applies(self):
        self.grant_access()

        self.assertEqual(self.client.get('/api/accounts/').status_code, 200)
        self.assertEqual(self.client.get('/api/dashboard/summary/').status_code, 200)
        self.assertEqual(self.client.get('/api/savings/goals/').status_code, 200)
        self.assertEqual(self.client.post('/api/accounts/', {
            'name': 'Wallet', 'account_type': 'cash', 'currency': 'KES',
        }, format='json').status_code, 201)
        self.assertEqual(self.client.get(f'/api/accounts/{self.other_account.pk}/').status_code, 404)

    def test_non_active_entitlement_statuses_are_denied(self):
        entitlement = self.grant_access()
        for status_value in (
            LifetimeAccessEntitlement.REVOKED,
            LifetimeAccessEntitlement.REFUNDED,
            LifetimeAccessEntitlement.CHARGEBACK_SUSPENDED,
        ):
            entitlement.status = status_value
            entitlement.save(update_fields=['status'])
            self.assert_payment_required(self.client.get('/api/accounts/'))

    def test_staff_is_not_exempt_from_financial_payment_requirement(self):
        self.user.is_staff = True
        self.user.save(update_fields=['is_staff'])

        self.assert_payment_required(self.client.get('/api/accounts/'))

    def test_billing_and_account_management_remain_payment_exempt(self):
        offer = LifetimeAccessOffer.objects.create(
            name='Lifetime offer', price_minor=105000, currency='KES', is_active=True,
        )
        attempt = PaystackPurchaseAttempt.objects.create(
            user=self.user, offer=offer, expected_amount_minor=105000,
            expected_currency='KES', status=PaystackPurchaseAttempt.PENDING,
        )

        self.assertEqual(self.client.get('/api/billing/access/').status_code, 200)
        self.assertEqual(self.client.get(f'/api/billing/purchases/{attempt.reference}/').status_code, 200)
        self.assertEqual(self.client.get('/api/auth/profile/').status_code, 200)
        self.assertEqual(self.client.get('/api/auth/legal-status/').status_code, 200)
        self.assertEqual(
            self.client.delete('/api/auth/delete-account/', {'password': 'wrong-password'}, format='json').status_code,
            400,
        )

    def test_unauthenticated_financial_request_is_401_not_payment_required(self):
        self.client.force_authenticate(user=None)
        response = self.client.get('/api/accounts/')
        self.assertEqual(response.status_code, 401)


@override_settings(BILLING_ENFORCE_LIFETIME_ACCESS=True, PAYSTACK_MODE='test')
class PaymentEnvironmentIsolationTests(APITestCase):
    """Test and Live billing facts must never activate each other."""

    live_settings = {
        'PAYSTACK_MODE': 'live',
        'PAYSTACK_LIVE_SECRET_KEY': 'sk_live_environment_test',
        'PAYSTACK_LIVE_WEBHOOK_SIGNING_SECRET': 'whsec_live_environment_test',
        'PAYSTACK_CALLBACK_URL': 'https://api.example.test/api/billing/paystack/callback/',
        'FRONTEND_PAYMENT_RETURN_URL': 'https://app.example.test/payment/complete',
    }

    def setUp(self):
        self.user = User.objects.create_user('environment-user', 'environment@example.com', 'SafePassword123!')
        self.account = Account.objects.create(user=self.user, name='Cash', account_type='cash', currency='KES')
        self.offer = LifetimeAccessOffer.objects.create(
            name='Environment offer', price_minor=105000, currency='KES', is_active=True,
        )
        self.client.force_authenticate(self.user)

    def attempt(self, mode, **overrides):
        values = {
            'user': self.user,
            'offer': self.offer,
            'expected_amount_minor': 105000,
            'expected_currency': 'KES',
            'provider_mode': mode,
        }
        values.update(overrides)
        return PaystackPurchaseAttempt.objects.create(**values)

    @staticmethod
    def provider_data(attempt, provider_id):
        return {
            'id': provider_id,
            'reference': attempt.reference,
            'status': 'success',
            'amount': 105000,
            'currency': 'KES',
        }

    def test_test_and_live_entitlements_are_distinct_and_access_is_mode_specific(self):
        test_attempt = self.attempt('test')
        apply_verification(test_attempt.reference, self.provider_data(test_attempt, 1001))
        self.assertTrue(has_lifetime_access(self.user))
        self.assertEqual(self.client.get('/api/accounts/').status_code, 200)

        with self.settings(**self.live_settings):
            self.assertFalse(has_lifetime_access(self.user))
            self.assertEqual(self.client.get('/api/accounts/').status_code, 403)
            with self.assertRaises(PurchaseVerificationError):
                apply_verification(test_attempt.reference, self.provider_data(test_attempt, 1001))

            live_attempt = self.attempt('live')
            apply_verification(live_attempt.reference, self.provider_data(live_attempt, 1001))
            self.assertTrue(has_lifetime_access(self.user))
            self.assertEqual(self.client.get('/api/accounts/').status_code, 200)

        self.assertTrue(has_lifetime_access(self.user))
        self.assertEqual(
            set(LifetimeAccessEntitlement.objects.filter(user=self.user).values_list('provider_mode', flat=True)),
            {'test', 'live'},
        )

    def test_test_pending_attempt_does_not_block_live_checkout(self):
        self.attempt('test', status=PaystackPurchaseAttempt.PENDING)
        with self.settings(**{
            **self.live_settings,
            'BILLING_LIVE_CHECKOUT_ENABLED': True,
            'BILLING_LIVE_CHECKOUT_ALLOWED_USER_IDS': frozenset({self.user.pk}),
        }):
            with patch('billing.views.initialize_transaction') as initialize:
                initialize.side_effect = lambda **kwargs: {
                    'reference': kwargs['reference'], 'authorization_url': 'https://checkout.example.test/live',
                }
                response = self.client.post('/api/billing/purchases/initialize/', {}, format='json')

        self.assertEqual(response.status_code, 201)
        created = PaystackPurchaseAttempt.objects.get(reference=response.data['reference'])
        self.assertEqual(created.provider_mode, 'live')

    def test_cross_mode_webhook_and_refund_cannot_mutate_test_purchase(self):
        test_attempt = self.attempt('test', status=PaystackPurchaseAttempt.SUCCESSFUL, provider_transaction_id='1002')
        entitlement = LifetimeAccessEntitlement.objects.create(
            user=self.user,
            source=LifetimeAccessEntitlement.PAYSTACK_PURCHASE,
            activating_purchase=test_attempt,
            provider_mode='test',
        )
        with self.settings(**self.live_settings):
            with self.assertRaises(PurchaseVerificationError):
                apply_reversal(test_attempt.reference, 'refund.processed', {
                    'id': 9, 'amount': '105000', 'currency': 'KES',
                })

            event = {'event': 'charge.success', 'data': {'id': 1002, 'reference': test_attempt.reference}}
            raw, signature = self.signed_live_webhook(event)
            with patch(
                'billing.views.verify_and_activate_purchase',
                side_effect=lambda reference: apply_verification(
                    reference, self.provider_data(test_attempt, 1002),
                ),
            ):
                response = self.client.post(
                    '/api/billing/paystack/webhook/', raw, content_type='application/json',
                    HTTP_PAYSTACK_SIGNATURE=signature,
                )

        test_attempt.refresh_from_db()
        entitlement.refresh_from_db()
        event = PaystackWebhookEvent.objects.get()
        self.assertEqual(response.status_code, 200)
        self.assertEqual((event.provider_mode, event.processing_status), ('live', PaystackWebhookEvent.FAILED))
        self.assertEqual(test_attempt.status, PaystackPurchaseAttempt.SUCCESSFUL)
        self.assertEqual(entitlement.status, LifetimeAccessEntitlement.ACTIVE)

    def test_unclassified_historical_purchase_never_grants_access(self):
        historical = self.attempt('test')
        PaystackPurchaseAttempt.objects.filter(pk=historical.pk).update(provider_mode=None)
        historical.refresh_from_db()

        with self.assertRaises(PurchaseVerificationError):
            apply_verification(historical.reference, self.provider_data(historical, 1003))
        self.assertFalse(has_lifetime_access(self.user))

    @staticmethod
    def signed_live_webhook(event):
        raw = json.dumps(event, separators=(',', ':')).encode('utf-8')
        timestamp = int(time.time())
        digest = hmac.new(
            b'whsec_live_environment_test', f'{timestamp}.'.encode('ascii') + raw, hashlib.sha512,
        ).hexdigest()
        return raw, f't={timestamp},v1=whk_live:{digest}'


@override_settings(
    PAYSTACK_MODE='live',
    PAYSTACK_LIVE_SECRET_KEY='sk_live_checkout_test',
    PAYSTACK_LIVE_WEBHOOK_SIGNING_SECRET='whsec_live_checkout_test',
    PAYSTACK_CALLBACK_URL='https://api.example.test/api/billing/paystack/callback/',
    FRONTEND_PAYMENT_RETURN_URL='https://app.example.test/payment/complete',
)
class LiveCheckoutRestrictionTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user('live-checkout', 'live-checkout@example.com', 'SafePassword123!')
        self.other_user = User.objects.create_user('other-live-checkout', 'other-live-checkout@example.com', 'SafePassword123!')
        self.offer = LifetimeAccessOffer.objects.create(
            name='Controlled live offer', price_minor=105000, currency='KES', is_active=True,
        )
        self.client.force_authenticate(self.user)

    def assert_restricted(self, response):
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data, {
            'detail': 'Live checkout is not available for this account.',
            'code': 'live_checkout_restricted',
        })

    @patch('billing.views.initialize_transaction')
    def test_disabled_live_checkout_rejects_before_paystack(self, initialize):
        with self.settings(
            BILLING_LIVE_CHECKOUT_ENABLED=False,
            BILLING_LIVE_CHECKOUT_ALLOWED_USER_IDS=frozenset({self.user.pk}),
        ):
            response = self.client.post('/api/billing/purchases/initialize/', {}, format='json')
        self.assert_restricted(response)
        initialize.assert_not_called()
        self.assertFalse(PaystackPurchaseAttempt.objects.exists())

    @patch('billing.views.initialize_transaction')
    def test_nonempty_live_checkout_allowlist_restricts_to_authenticated_user_id(self, initialize):
        with self.settings(
            BILLING_LIVE_CHECKOUT_ENABLED=True,
            BILLING_LIVE_CHECKOUT_ALLOWED_USER_IDS=frozenset({self.other_user.pk}),
        ):
            response = self.client.post('/api/billing/purchases/initialize/', {'email': self.other_user.email}, format='json')
        self.assert_restricted(response)
        initialize.assert_not_called()

    @patch('billing.views.initialize_transaction')
    def test_invalid_allowlist_fails_closed(self, initialize):
        with self.settings(
            BILLING_LIVE_CHECKOUT_ENABLED=True,
            BILLING_LIVE_CHECKOUT_ALLOWED_USER_IDS=None,
        ):
            response = self.client.post('/api/billing/purchases/initialize/', {}, format='json')
        self.assert_restricted(response)
        initialize.assert_not_called()

    @patch('billing.views.initialize_transaction')
    def test_enabled_live_checkout_without_allowlist_allows_authenticated_unpaid_user(self, initialize):
        initialize.side_effect = lambda **kwargs: {
            'reference': kwargs['reference'], 'authorization_url': 'https://checkout.example.test/live',
        }
        with self.settings(
            BILLING_LIVE_CHECKOUT_ENABLED=True,
            BILLING_LIVE_CHECKOUT_ALLOWED_USER_IDS=frozenset(),
        ):
            response = self.client.post('/api/billing/purchases/initialize/', {}, format='json')
        self.assertEqual(response.status_code, 201)
        self.assertEqual(PaystackPurchaseAttempt.objects.get().user_id, self.user.pk)

    def test_invalid_allowlist_values_are_rejected_by_settings_parser(self):
        with patch.dict(os.environ, {'BILLING_LIVE_CHECKOUT_ALLOWED_USER_IDS': ''}):
            self.assertEqual(env_positive_int_set('BILLING_LIVE_CHECKOUT_ALLOWED_USER_IDS'), frozenset())
        with patch.dict(os.environ, {'BILLING_LIVE_CHECKOUT_ALLOWED_USER_IDS': '5,not-an-id'}):
            self.assertIsNone(env_positive_int_set('BILLING_LIVE_CHECKOUT_ALLOWED_USER_IDS'))
        with patch.dict(os.environ, {'BILLING_LIVE_CHECKOUT_ALLOWED_USER_IDS': '0'}):
            self.assertIsNone(env_positive_int_set('BILLING_LIVE_CHECKOUT_ALLOWED_USER_IDS'))

    @patch('billing.views.initialize_transaction')
    def test_authorized_live_user_can_initialize_checkout(self, initialize):
        initialize.side_effect = lambda **kwargs: {
            'reference': kwargs['reference'], 'authorization_url': 'https://checkout.example.test/live',
        }
        with self.settings(
            BILLING_LIVE_CHECKOUT_ENABLED=True,
            BILLING_LIVE_CHECKOUT_ALLOWED_USER_IDS=frozenset({self.user.pk}),
        ):
            response = self.client.post('/api/billing/purchases/initialize/', {}, format='json')
        self.assertEqual(response.status_code, 201)
        self.assertEqual(PaystackPurchaseAttempt.objects.get().provider_mode, 'live')

    @patch('billing.views.verify_and_activate_purchase')
    def test_disabled_checkout_does_not_block_verification(self, verify):
        attempt = PaystackPurchaseAttempt.objects.create(
            user=self.user, offer=self.offer, expected_amount_minor=105000,
            expected_currency='KES', provider_mode='live', status=PaystackPurchaseAttempt.PENDING,
        )
        verify.return_value = (attempt, None, False)
        with self.settings(
            BILLING_LIVE_CHECKOUT_ENABLED=False,
            BILLING_LIVE_CHECKOUT_ALLOWED_USER_IDS=frozenset(),
        ):
            response = self.client.post(f'/api/billing/purchases/{attempt.reference}/verify/', {}, format='json')
        self.assertEqual(response.status_code, 200)
        verify.assert_called_once_with(attempt.reference)

    @patch('billing.views.initialize_transaction')
    def test_test_mode_checkout_is_unaffected(self, initialize):
        initialize.side_effect = lambda **kwargs: {
            'reference': kwargs['reference'], 'authorization_url': 'https://checkout.example.test/test',
        }
        with self.settings(
            PAYSTACK_MODE='test',
            PAYSTACK_TEST_SECRET_KEY='sk_test_checkout_test',
            PAYSTACK_TEST_WEBHOOK_SIGNING_SECRET='whsec_test_checkout_test',
            PAYSTACK_CALLBACK_URL='http://localhost:8000/api/billing/paystack/callback/',
            FRONTEND_PAYMENT_RETURN_URL='http://localhost:5173/payment/complete',
            BILLING_LIVE_CHECKOUT_ENABLED=False,
            BILLING_LIVE_CHECKOUT_ALLOWED_USER_IDS=None,
        ):
            response = self.client.post('/api/billing/purchases/initialize/', {}, format='json')
        self.assertEqual(response.status_code, 201)
        self.assertEqual(PaystackPurchaseAttempt.objects.get().provider_mode, 'test')


class PaymentEnvironmentMigrationTests(TestCase):
    """Exercise the guarded historical Test-mode classification step."""

    def setUp(self):
        self.user = User.objects.create_user('historical-mode', 'historical-mode@example.com', 'SafePassword123!')
        self.offer = LifetimeAccessOffer.objects.create(
            name='Historical mode offer', price_minor=105000, currency='KES', is_active=True,
        )
        self.attempt = PaystackPurchaseAttempt.objects.create(
            user=self.user, offer=self.offer, expected_amount_minor=105000,
            expected_currency='KES', status=PaystackPurchaseAttempt.SUCCESSFUL,
            provider_transaction_id='historical-provider-id', provider_mode='test',
        )
        self.entitlement = LifetimeAccessEntitlement.objects.create(
            user=self.user, source=LifetimeAccessEntitlement.PAYSTACK_PURCHASE,
            activating_purchase=self.attempt, provider_mode='test',
        )
        self.webhook = PaystackWebhookEvent.objects.create(
            deduplication_key='test:charge.success:historical', event_type='charge.success',
            payload_digest='a' * 64, provider_mode='test',
        )
        operator = User.objects.create_user('historical-operator', 'historical-operator@example.com', 'SafePassword123!', is_staff=True)
        self.decision = EntitlementRestorationDecision.objects.create(
            purchase=self.attempt, purchase_reference=self.attempt.reference,
            entitlement=self.entitlement, user=self.user,
            provider_transaction_id='historical-provider-id', provider_mode='test',
            approved_by=operator, approved_by_identifier=f'{operator.pk}:{operator.username}',
            evidence_reference='HISTORICAL-TEST', reason='Historical Test-mode fixture.',
            previous_status=LifetimeAccessEntitlement.ACTIVE,
        )
        PaystackPurchaseAttempt.objects.filter(pk=self.attempt.pk).update(provider_mode=None)
        LifetimeAccessEntitlement.objects.filter(pk=self.entitlement.pk).update(provider_mode=None)
        PaystackWebhookEvent.objects.filter(pk=self.webhook.pk).update(provider_mode=None)
        EntitlementRestorationDecision.objects.filter(pk=self.decision.pk).update(provider_mode=None)
        self.migration = importlib.import_module('billing.migrations.0005_payment_environment_isolation')

    def test_unconfirmed_historical_records_remain_unclassified(self):
        with patch.dict(os.environ, {'BILLING_CONFIRM_HISTORICAL_PAYSTACK_TEST_ONLY': ''}):
            self.migration.classify_historical_paystack_records(apps)

        self.attempt.refresh_from_db()
        self.entitlement.refresh_from_db()
        self.webhook.refresh_from_db()
        self.decision.refresh_from_db()
        self.assertEqual(
            (self.attempt.provider_mode, self.entitlement.provider_mode, self.webhook.provider_mode, self.decision.provider_mode),
            (None, None, None, None),
        )
        self.assertFalse(has_lifetime_access(self.user))

    def test_explicit_test_only_confirmation_classifies_related_historical_records(self):
        with patch.dict(os.environ, {'BILLING_CONFIRM_HISTORICAL_PAYSTACK_TEST_ONLY': 'yes'}):
            self.migration.classify_historical_paystack_records(apps)

        self.attempt.refresh_from_db()
        self.entitlement.refresh_from_db()
        self.webhook.refresh_from_db()
        self.decision.refresh_from_db()
        self.assertEqual(
            (self.attempt.provider_mode, self.entitlement.provider_mode, self.webhook.provider_mode, self.decision.provider_mode),
            ('test', 'test', 'test', 'test'),
        )
        self.assertTrue(has_lifetime_access(self.user))


class BillingManagementCommandTests(TestCase):
    cutoff = '2026-01-01T00:00:00+00:00'

    def setUp(self):
        self.early = User.objects.create_user('early', 'early@example.com', 'SafePassword123!')
        self.boundary = User.objects.create_user('boundary', 'boundary@example.com', 'SafePassword123!')
        self.late = User.objects.create_user('late', 'late@example.com', 'SafePassword123!')
        User.objects.filter(pk=self.early.pk).update(date_joined=datetime(2025, 12, 31, 23, 59, tzinfo=datetime_timezone.utc))
        User.objects.filter(pk=self.boundary.pk).update(date_joined=datetime(2026, 1, 1, 0, 0, tzinfo=datetime_timezone.utc))
        User.objects.filter(pk=self.late.pk).update(date_joined=datetime(2026, 1, 1, 0, 1, tzinfo=datetime_timezone.utc))

    def test_seed_offer_is_idempotent_and_uses_configured_price(self):
        output = StringIO()
        call_command('seed_lifetime_offer', stdout=output)
        call_command('seed_lifetime_offer', stdout=output)
        offer = LifetimeAccessOffer.objects.get()
        self.assertEqual((offer.price_minor, offer.currency, offer.is_active), (105000, 'KES', True))
        self.assertEqual(LifetimeAccessOffer.objects.count(), 1)

    def test_founding_grant_dry_run_cutoff_and_rerun(self):
        output = StringIO()
        call_command('grant_founding_users', '--cutoff', self.cutoff, '--dry-run', stdout=output)
        self.assertIn('Eligible users: 1; already entitled: 0; proposed grants: 1.', output.getvalue())
        self.assertFalse(LifetimeAccessEntitlement.objects.exists())

        call_command('grant_founding_users', '--cutoff', self.cutoff, stdout=StringIO())
        call_command('grant_founding_users', '--cutoff', self.cutoff, stdout=StringIO())

        entitlement = LifetimeAccessEntitlement.objects.get(user=self.early)
        self.assertEqual((entitlement.source, entitlement.status), ('legacy_grant', 'active'))
        self.assertFalse(LifetimeAccessEntitlement.objects.filter(user=self.boundary).exists())
        self.assertFalse(LifetimeAccessEntitlement.objects.filter(user=self.late).exists())
        self.assertEqual(LifetimeAccessEntitlement.objects.filter(user=self.early).count(), 1)

    def test_founding_grant_never_overwrites_a_paid_entitlement(self):
        offer = LifetimeAccessOffer.objects.create(name='Offer', price_minor=105000, currency='KES')
        purchase = PaystackPurchaseAttempt.objects.create(
            user=self.early, offer=offer, expected_amount_minor=105000, expected_currency='KES',
            status=PaystackPurchaseAttempt.SUCCESSFUL,
        )
        entitlement = LifetimeAccessEntitlement.objects.create(
            user=self.early, source=LifetimeAccessEntitlement.PAYSTACK_PURCHASE,
            activating_purchase=purchase,
        )

        call_command('grant_founding_users', '--cutoff', self.cutoff, stdout=StringIO())

        entitlement.refresh_from_db()
        self.assertEqual(entitlement.source, LifetimeAccessEntitlement.PAYSTACK_PURCHASE)
        self.assertEqual(entitlement.activating_purchase_id, purchase.id)


class DisputeRestorationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('disputed-user', 'disputed@example.com', 'SafePassword123!')
        self.approver = User.objects.create_user('approver', 'approver@example.com', 'SafePassword123!', is_staff=True)
        self.executor = User.objects.create_user('executor', 'executor@example.com', 'SafePassword123!', is_staff=True)
        self.unauthorized = User.objects.create_user('unauthorized', 'unauthorized@example.com', 'SafePassword123!', is_staff=True)
        self.approver.user_permissions.add(Permission.objects.get(codename='approve_dispute_restoration'))
        self.executor.user_permissions.add(Permission.objects.get(codename='restore_disputed_access'))
        self.offer = LifetimeAccessOffer.objects.create(name='Dispute offer', price_minor=105000, currency='KES')
        self.attempt = PaystackPurchaseAttempt.objects.create(
            user=self.user,
            offer=self.offer,
            expected_amount_minor=105000,
            expected_currency='KES',
            status=PaystackPurchaseAttempt.SUCCESSFUL,
            provider_transaction_id='445566',
            provider_metadata={'dispute_review_required': True},
        )
        self.entitlement = LifetimeAccessEntitlement.objects.create(
            user=self.user,
            source=LifetimeAccessEntitlement.PAYSTACK_PURCHASE,
            activating_purchase=self.attempt,
            status=LifetimeAccessEntitlement.CHARGEBACK_SUSPENDED,
        )

    def provider_data(self, **overrides):
        data = {
            'id': 445566,
            'reference': self.attempt.reference,
            'status': 'success',
            'amount': 105000,
            'currency': 'KES',
        }
        data.update(overrides)
        return data

    def resolved_disputes(self, **overrides):
        dispute = {
            'status': 'resolved',
            'resolution': 'declined',
            'refund_amount': None,
            'transaction': {'id': 445566, 'reference': self.attempt.reference},
        }
        dispute.update(overrides)
        return [dispute]

    def approve(self, **overrides):
        values = {
            'purchase_reference': self.attempt.reference,
            'operator': self.approver,
            'evidence_reference': 'CASE-1001',
            'reason': 'Paystack dispute record shows a declined resolution.',
            'provider_data': self.provider_data(),
        }
        values.update(overrides)
        return approve_restoration(**values)

    def test_resolved_declined_dispute_is_ambiguous_and_fails_closed(self):
        decision = self.approve()

        with self.assertRaisesRegex(DisputeRestorationError, 'cannot establish a favorable final dispute outcome'):
            execute_restoration(
                decision_id=decision.decision_id,
                operator=self.executor,
                provider_data=self.provider_data(),
                disputes=self.resolved_disputes(),
            )

        self.entitlement.refresh_from_db()
        decision.refresh_from_db()
        self.assertEqual(self.entitlement.status, LifetimeAccessEntitlement.CHARGEBACK_SUSPENDED)
        self.assertEqual(decision.state, EntitlementRestorationDecision.APPROVED)
        self.assertIsNone(decision.executed_at)

    def test_invalid_evidence_and_unauthorized_operators_cannot_approve_or_execute(self):
        with self.assertRaises(DisputeRestorationError):
            self.approve(evidence_reference='')
        with self.assertRaises(DisputeRestorationError):
            self.approve(operator=self.unauthorized)
        decision = self.approve()
        with self.assertRaises(DisputeRestorationError):
            execute_restoration(
                decision_id=decision.decision_id,
                operator=self.unauthorized,
                provider_data=self.provider_data(),
                disputes=self.resolved_disputes(),
            )
        self.assertEqual(EntitlementRestorationDecision.objects.count(), 1)
        self.entitlement.refresh_from_db()
        self.assertEqual(self.entitlement.status, LifetimeAccessEntitlement.CHARGEBACK_SUSPENDED)

    def test_unresolved_or_refunded_disputes_cannot_restore_access(self):
        decision = self.approve()
        with self.assertRaises(DisputeRestorationError):
            execute_restoration(
                decision_id=decision.decision_id,
                operator=self.executor,
                provider_data=self.provider_data(),
                disputes=self.resolved_disputes(status='pending'),
            )
        self.attempt.provider_metadata['refunds'] = [{'amount': 105000}]
        self.attempt.save(update_fields=['provider_metadata'])
        with self.assertRaises(DisputeRestorationError):
            execute_restoration(
                decision_id=decision.decision_id,
                operator=self.executor,
                provider_data=self.provider_data(),
                disputes=self.resolved_disputes(),
            )
        self.entitlement.refresh_from_db()
        self.assertEqual(self.entitlement.status, LifetimeAccessEntitlement.CHARGEBACK_SUSPENDED)

    def test_non_successful_payment_cannot_receive_a_restoration_approval(self):
        with self.assertRaises(PurchaseVerificationError):
            self.approve(provider_data=self.provider_data(status='pending'))

        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.status, PaystackPurchaseAttempt.SUCCESSFUL)
        self.assertFalse(EntitlementRestorationDecision.objects.exists())

    def test_decision_is_unique_immutable_and_rolls_back_if_execution_record_cannot_save(self):
        decision = self.approve()
        with self.assertRaises(DisputeRestorationError):
            self.approve()
        decision.reason = 'changed'
        with self.assertRaises(ValidationError):
            decision.save()
        decision.refresh_from_db()
        with patch('billing.services.dispute_restoration.dispute_outcome_allows_restoration', return_value=True):
            with patch.object(EntitlementRestorationDecision, 'save', side_effect=ValidationError('audit write failed')):
                with self.assertRaises(ValidationError):
                    execute_restoration(
                        decision_id=decision.decision_id,
                        operator=self.executor,
                        provider_data=self.provider_data(),
                        disputes=self.resolved_disputes(),
                    )
        self.entitlement.refresh_from_db()
        decision.refresh_from_db()
        self.assertEqual(self.entitlement.status, LifetimeAccessEntitlement.CHARGEBACK_SUSPENDED)
        self.assertEqual(decision.state, EntitlementRestorationDecision.APPROVED)

    def test_deleted_account_prevents_execution(self):
        decision = self.approve()
        self.user.delete()
        with self.assertRaises(DisputeRestorationError):
            execute_restoration(
                decision_id=decision.decision_id,
                operator=self.executor,
                provider_data=self.provider_data(),
                disputes=self.resolved_disputes(),
            )

    @patch('billing.management.commands.approve_dispute_restoration.verify_transaction')
    @patch('billing.management.commands.restore_disputed_access.verify_transaction')
    @patch('billing.management.commands.restore_disputed_access.fetch_transaction_disputes')
    @patch('billing.management.commands.restore_disputed_access.getpass.getpass', return_value='SafePassword123!')
    @patch('billing.management.commands.approve_dispute_restoration.getpass.getpass', return_value='SafePassword123!')
    def test_two_person_management_commands_refuse_ambiguous_provider_outcome(self, approve_password, restore_password, fetch_disputes, restore_verify, approve_verify):
        approve_verify.return_value = self.provider_data()
        restore_verify.return_value = self.provider_data()
        fetch_disputes.return_value = self.resolved_disputes()

        with self.assertRaises(CommandError):
            call_command(
                'approve_dispute_restoration',
                '--purchase-reference', self.attempt.reference,
                '--operator-email', self.approver.email,
                '--evidence-reference', 'CASE-1002',
                '--reason', 'Approved after provider review.',
            )
        call_command(
            'approve_dispute_restoration',
            '--purchase-reference', self.attempt.reference,
            '--operator-email', self.approver.email,
            '--evidence-reference', 'CASE-1002',
            '--reason', 'Approved after provider review.',
            '--confirm',
        )
        decision = EntitlementRestorationDecision.objects.get()
        with self.assertRaises(CommandError):
            call_command(
                'restore_disputed_access',
                '--decision-id', str(decision.decision_id),
                '--operator-email', self.approver.email,
                '--confirm',
            )
        with self.assertRaises(CommandError):
            call_command(
                'restore_disputed_access',
                '--decision-id', str(decision.decision_id),
                '--operator-email', self.executor.email,
                '--confirm',
            )
        decision.refresh_from_db()
        self.entitlement.refresh_from_db()
        self.assertEqual(decision.state, EntitlementRestorationDecision.APPROVED)
        self.assertEqual(self.entitlement.status, LifetimeAccessEntitlement.CHARGEBACK_SUSPENDED)


@override_settings(
    DEBUG=True,
    DEBUG_PROPAGATE_EXCEPTIONS=True,
    PAYSTACK_MODE='test',
    PAYSTACK_SECRET_KEY='sk_test_example',
    PAYSTACK_WEBHOOK_SIGNING_SECRET='whsec_test_secret',
    PAYSTACK_CALLBACK_URL='http://localhost:8000/api/billing/paystack/callback/',
    FRONTEND_PAYMENT_RETURN_URL='http://localhost:5173/payment/complete',
)
class PaystackPhaseTwoTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user('payer', 'payer@example.com', 'SafePassword123!')
        self.other_user = User.objects.create_user('other-payer', 'other-payer@example.com', 'SafePassword123!')
        self.offer = LifetimeAccessOffer.objects.create(
            name='Budgetly Lifetime Access', price_minor=105000, currency='KES', is_active=True
        )
        self.client.force_authenticate(user=self.user)

    def attempt(self, **overrides):
        values = {
            'user': self.user, 'offer': self.offer,
            'expected_amount_minor': 105000, 'expected_currency': 'KES',
        }
        values.update(overrides)
        return PaystackPurchaseAttempt.objects.create(**values)

    def provider_data(self, attempt, **overrides):
        data = {
            'id': 987654, 'reference': attempt.reference, 'status': 'success',
            'amount': 105000, 'currency': 'KES', 'channel': 'card', 'gateway_response': 'Successful',
        }
        data.update(overrides)
        return data

    def signed_webhook(self, event, timestamp=None):
        raw = json.dumps(event, separators=(',', ':')).encode('utf-8')
        timestamp = int(time.time()) if timestamp is None else timestamp
        digest = hmac.new(b'whsec_test_secret', f'{timestamp}.'.encode('ascii') + raw, hashlib.sha512).hexdigest()
        return raw, f't={timestamp},v1=whk_test:{digest}'

    @patch('billing.views.initialize_transaction')
    def test_initialize_uses_server_offer_and_ignores_client_price(self, initialize):
        initialize.side_effect = lambda **kwargs: {'reference': kwargs['reference'], 'authorization_url': 'https://checkout.test/one'}

        request = APIRequestFactory().post('/api/billing/purchases/initialize/', {'amount': 1, 'currency': 'USD'}, format='json')
        force_authenticate(request, user=self.user)
        response = PurchaseInitializeView.as_view()(request)

        self.assertEqual(response.status_code, 201)
        attempt = PaystackPurchaseAttempt.objects.get(user=self.user)
        self.assertEqual((attempt.expected_amount_minor, attempt.expected_currency), (105000, 'KES'))
        self.assertEqual(response.data['reference'], attempt.reference)
        self.assertEqual(initialize.call_args.kwargs['amount_minor'], 105000)
        self.assertEqual(initialize.call_args.kwargs['email'], self.user.email)

    @override_settings(PAYSTACK_SECRET_KEY='')
    def test_initialize_reports_missing_configuration(self):
        request = APIRequestFactory().post('/api/billing/purchases/initialize/', {}, format='json')
        force_authenticate(request, user=self.user)
        response = PurchaseInitializeView.as_view()(request)
        self.assertEqual(response.status_code, 503)
        self.assertFalse(PaystackPurchaseAttempt.objects.exists())

    @patch('billing.views.initialize_transaction', side_effect=PaystackUnavailableError('timeout'))
    def test_ambiguous_initialization_stays_pending_and_blocks_a_second_checkout(self, initialize):
        first = self.client.post('/api/billing/purchases/initialize/', {}, format='json')
        attempt = PaystackPurchaseAttempt.objects.get(user=self.user)
        second = self.client.post('/api/billing/purchases/initialize/', {}, format='json')

        self.assertEqual((first.status_code, second.status_code), (503, 409))
        self.assertEqual(attempt.status, PaystackPurchaseAttempt.PENDING)
        self.assertEqual(initialize.call_count, 1)

    @patch('billing.views.initialize_transaction')
    def test_missing_or_mismatched_active_offer_is_a_safe_configuration_error(self, initialize):
        self.offer.delete()

        response = self.client.post('/api/billing/purchases/initialize/', {}, format='json')

        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        initialize.assert_not_called()

    @patch('billing.views.initialize_transaction')
    def test_active_user_cannot_initialize_another_purchase(self, initialize):
        LifetimeAccessEntitlement.grant_legacy_access(self.user)
        response = self.client.post('/api/billing/purchases/initialize/', {}, format='json')
        self.assertEqual(response.status_code, 409)
        initialize.assert_not_called()

    @patch('billing.services.payments.verify_transaction')
    def test_verified_success_activates_once_and_is_idempotent(self, verify):
        attempt = self.attempt()
        verify.return_value = self.provider_data(attempt)

        first = self.client.post(f'/api/billing/purchases/{attempt.reference}/verify/', {}, format='json')
        second = self.client.post(f'/api/billing/purchases/{attempt.reference}/verify/', {}, format='json')

        self.assertEqual((first.status_code, second.status_code), (200, 200))
        self.assertTrue(first.data['activated'])
        self.assertFalse(second.data['activated'])
        self.assertEqual(LifetimeAccessEntitlement.objects.filter(user=self.user).count(), 1)
        attempt.refresh_from_db()
        self.assertEqual((attempt.status, attempt.provider_transaction_id), ('successful', '987654'))

    @patch('billing.services.payments.verify_transaction')
    def test_pending_and_mismatched_transactions_never_activate_access(self, verify):
        pending = self.attempt()
        verify.return_value = self.provider_data(pending, status='pending')
        pending_response = self.client.post(f'/api/billing/purchases/{pending.reference}/verify/', {}, format='json')
        self.assertEqual(pending_response.status_code, 200)
        self.assertFalse(pending_response.data['has_lifetime_access'])
        self.assertEqual(PaystackPurchaseAttempt.objects.get(pk=pending.pk).status, 'pending')

        mismatch = self.attempt(status=PaystackPurchaseAttempt.FAILED)
        verify.return_value = self.provider_data(mismatch, amount=100000)
        mismatch_response = self.client.post(f'/api/billing/purchases/{mismatch.reference}/verify/', {}, format='json')
        self.assertEqual(mismatch_response.status_code, 400)
        self.assertFalse(LifetimeAccessEntitlement.objects.filter(user=self.user).exists())

    @patch('billing.services.payments.verify_transaction')
    def test_verification_cannot_downgrade_a_confirmed_purchase(self, verify):
        attempt = self.attempt(status=PaystackPurchaseAttempt.SUCCESSFUL)
        entitlement = LifetimeAccessEntitlement.objects.create(
            user=self.user,
            source=LifetimeAccessEntitlement.PAYSTACK_PURCHASE,
            activating_purchase=attempt,
        )
        verify.return_value = self.provider_data(attempt, status='failed')

        response = self.client.post(f'/api/billing/purchases/{attempt.reference}/verify/', {}, format='json')

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        attempt.refresh_from_db()
        entitlement.refresh_from_db()
        self.assertEqual(attempt.status, PaystackPurchaseAttempt.SUCCESSFUL)
        self.assertEqual(entitlement.status, LifetimeAccessEntitlement.ACTIVE)

    @patch('billing.services.payments.verify_transaction')
    def test_verification_requires_a_stable_provider_transaction_id(self, verify):
        attempt = self.attempt()
        verify.return_value = self.provider_data(attempt, id=None)

        missing_id = self.client.post(f'/api/billing/purchases/{attempt.reference}/verify/', {}, format='json')

        self.assertEqual(missing_id.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(LifetimeAccessEntitlement.objects.filter(user=self.user).exists())

        verify.return_value = self.provider_data(attempt, id=987654)
        self.assertEqual(
            self.client.post(f'/api/billing/purchases/{attempt.reference}/verify/', {}, format='json').status_code,
            status.HTTP_200_OK,
        )
        verify.return_value = self.provider_data(attempt, id=987655)
        changed_id = self.client.post(f'/api/billing/purchases/{attempt.reference}/verify/', {}, format='json')

        self.assertEqual(changed_id.status_code, status.HTTP_400_BAD_REQUEST)
        attempt.refresh_from_db()
        self.assertEqual(attempt.provider_transaction_id, '987654')

    def test_purchase_detail_is_owner_only_and_access_reports_pending(self):
        attempt = self.attempt()
        access = self.client.get('/api/billing/access/')
        self.client.force_authenticate(user=self.other_user)
        foreign = self.client.get(f'/api/billing/purchases/{attempt.reference}/')

        self.assertEqual(access.data['purchase_required'], True)
        self.assertEqual(foreign.status_code, 404)

    @patch('billing.services.payments.verify_transaction')
    def test_founding_entitlement_is_not_overwritten_by_payment_verification(self, verify):
        entitlement, _ = LifetimeAccessEntitlement.grant_legacy_access(self.user)
        attempt = self.attempt()
        verify.return_value = self.provider_data(attempt)

        response = self.client.post(f'/api/billing/purchases/{attempt.reference}/verify/', {}, format='json')

        entitlement.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(entitlement.source, LifetimeAccessEntitlement.LEGACY_GRANT)
        self.assertIsNone(entitlement.activating_purchase_id)

    @patch('billing.views.verify_and_activate_purchase')
    def test_webhook_signature_duplicate_and_provider_failure_handling(self, activate):
        attempt = self.attempt()
        event = {'event': 'charge.success', 'data': {'id': 77, 'reference': attempt.reference}}
        raw, signature = self.signed_webhook(event)
        first = self.client.post('/api/billing/paystack/webhook/', raw, content_type='application/json', HTTP_PAYSTACK_SIGNATURE=signature)
        second = self.client.post('/api/billing/paystack/webhook/', raw, content_type='application/json', HTTP_PAYSTACK_SIGNATURE=signature)

        self.assertEqual((first.status_code, second.status_code), (200, 200))
        self.assertEqual(activate.call_count, 1)
        self.assertEqual(PaystackWebhookEvent.objects.count(), 1)

        failed_event = {'event': 'charge.success', 'data': {'id': 78, 'reference': attempt.reference}}
        failed_raw, failed_signature = self.signed_webhook(failed_event)
        activate.side_effect = PaystackUnavailableError('offline')
        unavailable = self.client.post('/api/billing/paystack/webhook/', failed_raw, content_type='application/json', HTTP_PAYSTACK_SIGNATURE=failed_signature)
        self.assertEqual(unavailable.status_code, 503)
        self.assertEqual(PaystackWebhookEvent.objects.get(transaction_reference=attempt.reference, deduplication_key__endswith='78').error_category, 'provider_unavailable')

    def test_webhook_rejects_invalid_and_replayed_timestamped_signatures(self):
        raw = b'{"event":"charge.success","data":{"reference":"unknown"}}'
        invalid = self.client.post('/api/billing/paystack/webhook/', raw, content_type='application/json', HTTP_PAYSTACK_SIGNATURE='t=1,v1=bad')
        _, old_signature = self.signed_webhook({'event': 'charge.success', 'data': {'reference': 'unknown'}}, timestamp=int(time.time()) - 301)
        replay = self.client.post('/api/billing/paystack/webhook/', raw, content_type='application/json', HTTP_PAYSTACK_SIGNATURE=old_signature)
        self.assertEqual((invalid.status_code, replay.status_code), (401, 401))
        self.assertFalse(PaystackWebhookEvent.objects.exists())

    def test_webhook_rejects_missing_signature_and_malformed_signed_payload(self):
        missing = self.client.post(
            '/api/billing/paystack/webhook/', b'{}', content_type='application/json'
        )
        raw = b'not-json'
        timestamp = int(time.time())
        digest = hmac.new(
            b'whsec_test_secret', f'{timestamp}.'.encode('ascii') + raw, hashlib.sha512
        ).hexdigest()
        malformed = self.client.post(
            '/api/billing/paystack/webhook/', raw, content_type='application/json',
            HTTP_PAYSTACK_SIGNATURE=f't={timestamp},v1=whk_test:{digest}',
        )

        self.assertEqual((missing.status_code, malformed.status_code), (401, 400))
        self.assertFalse(PaystackWebhookEvent.objects.exists())

    def test_signed_refund_without_transaction_reference_is_not_processed(self):
        event = {'event': 'refund.processed', 'data': {'id': 96, 'amount': '105000', 'currency': 'KES'}}
        raw, signature = self.signed_webhook(event)

        response = self.client.post('/api/billing/paystack/webhook/', raw, content_type='application/json', HTTP_PAYSTACK_SIGNATURE=signature)

        self.assertEqual(response.status_code, 200)
        webhook_event = PaystackWebhookEvent.objects.get()
        self.assertEqual(webhook_event.processing_status, PaystackWebhookEvent.FAILED)
        self.assertEqual(webhook_event.error_category, 'verification_failed')

    def test_refund_and_chargeback_events_move_paid_access_to_review_states(self):
        attempt = self.attempt(status=PaystackPurchaseAttempt.SUCCESSFUL)
        entitlement = LifetimeAccessEntitlement.objects.create(
            user=self.user, source=LifetimeAccessEntitlement.PAYSTACK_PURCHASE, activating_purchase=attempt
        )
        refund = {
            'event': 'refund.processed',
            'data': {
                'id': 90,
                'transaction_reference': attempt.reference,
                'amount': '105000',
                'currency': 'KES',
            },
        }
        raw, signature = self.signed_webhook(refund)
        response = self.client.post('/api/billing/paystack/webhook/', raw, content_type='application/json', HTTP_PAYSTACK_SIGNATURE=signature)
        entitlement.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(entitlement.status, LifetimeAccessEntitlement.REFUNDED)

        attempt.refresh_from_db()
        self.assertEqual(attempt.status, PaystackPurchaseAttempt.REVERSED)
        self.assertFalse(attempt.provider_metadata.get('refund_review_required', False))

        chargeback_attempt = self.attempt(user=self.other_user, status=PaystackPurchaseAttempt.SUCCESSFUL)
        chargeback_entitlement = LifetimeAccessEntitlement.objects.create(
            user=self.other_user, source=LifetimeAccessEntitlement.PAYSTACK_PURCHASE,
            activating_purchase=chargeback_attempt,
        )
        dispute = {'event': 'charge.dispute.create', 'data': {'id': 91, 'reference': chargeback_attempt.reference}}
        dispute_raw, dispute_signature = self.signed_webhook(dispute)
        dispute_response = self.client.post('/api/billing/paystack/webhook/', dispute_raw, content_type='application/json', HTTP_PAYSTACK_SIGNATURE=dispute_signature)
        chargeback_entitlement.refresh_from_db()
        self.assertEqual(dispute_response.status_code, 200)
        self.assertEqual(chargeback_entitlement.status, LifetimeAccessEntitlement.CHARGEBACK_SUSPENDED)

    def test_partial_refund_is_retained_without_revoking_lifetime_access(self):
        attempt = self.attempt(status=PaystackPurchaseAttempt.SUCCESSFUL)
        entitlement = LifetimeAccessEntitlement.objects.create(
            user=self.user,
            source=LifetimeAccessEntitlement.PAYSTACK_PURCHASE,
            activating_purchase=attempt,
        )
        refund = {
            'event': 'refund.processed',
            'data': {
                'id': 92,
                'transaction_reference': attempt.reference,
                'amount': '50000',
                'currency': 'KES',
            },
        }
        raw, signature = self.signed_webhook(refund)

        response = self.client.post('/api/billing/paystack/webhook/', raw, content_type='application/json', HTTP_PAYSTACK_SIGNATURE=signature)

        self.assertEqual(response.status_code, 200)
        attempt.refresh_from_db()
        entitlement.refresh_from_db()
        self.assertEqual(attempt.status, PaystackPurchaseAttempt.SUCCESSFUL)
        self.assertEqual(entitlement.status, LifetimeAccessEntitlement.ACTIVE)
        self.assertEqual(attempt.provider_metadata['refunds'][0]['amount'], 50000)
        self.assertTrue(attempt.provider_metadata['refund_review_required'])

    def test_mismatched_refund_currency_is_retained_for_reconciliation_without_access_change(self):
        attempt = self.attempt(status=PaystackPurchaseAttempt.SUCCESSFUL)
        entitlement = LifetimeAccessEntitlement.objects.create(
            user=self.user,
            source=LifetimeAccessEntitlement.PAYSTACK_PURCHASE,
            activating_purchase=attempt,
        )
        refund = {
            'event': 'refund.processed',
            'data': {
                'id': 95,
                'transaction_reference': attempt.reference,
                'amount': '105000',
                'currency': 'USD',
            },
        }
        raw, signature = self.signed_webhook(refund)

        response = self.client.post('/api/billing/paystack/webhook/', raw, content_type='application/json', HTTP_PAYSTACK_SIGNATURE=signature)

        self.assertEqual(response.status_code, 200)
        attempt.refresh_from_db()
        entitlement.refresh_from_db()
        event = PaystackWebhookEvent.objects.get(transaction_reference=attempt.reference)
        self.assertEqual(event.processing_status, PaystackWebhookEvent.FAILED)
        self.assertEqual(attempt.status, PaystackPurchaseAttempt.SUCCESSFUL)
        self.assertEqual(entitlement.status, LifetimeAccessEntitlement.ACTIVE)

    def test_dispute_resolution_does_not_automatically_restore_access(self):
        attempt = self.attempt(status=PaystackPurchaseAttempt.SUCCESSFUL)
        entitlement = LifetimeAccessEntitlement.objects.create(
            user=self.user,
            source=LifetimeAccessEntitlement.PAYSTACK_PURCHASE,
            activating_purchase=attempt,
            status=LifetimeAccessEntitlement.CHARGEBACK_SUSPENDED,
        )
        dispute = {
            'event': 'charge.dispute.resolve',
            'data': {'id': 93, 'transaction_reference': attempt.reference},
        }
        raw, signature = self.signed_webhook(dispute)

        response = self.client.post('/api/billing/paystack/webhook/', raw, content_type='application/json', HTTP_PAYSTACK_SIGNATURE=signature)

        self.assertEqual(response.status_code, 200)
        attempt.refresh_from_db()
        entitlement.refresh_from_db()
        self.assertEqual(attempt.status, PaystackPurchaseAttempt.SUCCESSFUL)
        self.assertEqual(entitlement.status, LifetimeAccessEntitlement.CHARGEBACK_SUSPENDED)
        self.assertTrue(attempt.provider_metadata['dispute_review_required'])

    @patch('billing.services.payments.verify_transaction')
    def test_dispute_before_delayed_success_creates_suspended_entitlement(self, verify):
        attempt = self.attempt(status=PaystackPurchaseAttempt.PENDING)
        dispute = {
            'event': 'charge.dispute.create',
            'data': {'id': 94, 'transaction_reference': attempt.reference},
        }
        raw, signature = self.signed_webhook(dispute)
        dispute_response = self.client.post(
            '/api/billing/paystack/webhook/', raw,
            content_type='application/json', HTTP_PAYSTACK_SIGNATURE=signature,
        )
        verify.return_value = self.provider_data(attempt)

        verification_response = self.client.post(
            f'/api/billing/purchases/{attempt.reference}/verify/', {}, format='json'
        )

        self.assertEqual(dispute_response.status_code, 200)
        self.assertEqual(verification_response.status_code, 200)
        entitlement = LifetimeAccessEntitlement.objects.get(user=self.user)
        self.assertEqual(entitlement.status, LifetimeAccessEntitlement.CHARGEBACK_SUSPENDED)
        self.assertFalse(verification_response.data['has_lifetime_access'])


@override_settings(DEBUG=True, PAYSTACK_MODE='test', PAYSTACK_TEST_SECRET_KEY='sk_test_example', PAYSTACK_TEST_WEBHOOK_SIGNING_SECRET='whsec_test_secret', PAYSTACK_CALLBACK_URL='http://localhost:8000/api/billing/paystack/callback/')
class PaystackServiceTests(TestCase):
    @override_settings(
        DEBUG=False,
        PAYSTACK_MODE='live',
        PAYSTACK_LIVE_SECRET_KEY='sk_live_example',
        PAYSTACK_LIVE_WEBHOOK_SIGNING_SECRET='whsec_live_secret',
        PAYSTACK_CALLBACK_URL='https://api.example.test/api/billing/paystack/callback/',
        FRONTEND_PAYMENT_RETURN_URL='https://app.example.test/payment/complete',
    )
    def test_live_configuration_requires_matching_key_signing_secret_and_https_callback(self):
        self.assertEqual(get_config().secret_key, 'sk_live_example')

        with self.settings(PAYSTACK_LIVE_SECRET_KEY='sk_test_wrong'):
            with self.assertRaises(PaystackConfigurationError):
                get_config()
        with self.settings(PAYSTACK_LIVE_SECRET_KEY='sk_live_example', PAYSTACK_LIVE_WEBHOOK_SIGNING_SECRET=''):
            with self.assertRaises(PaystackConfigurationError):
                get_config()
        with self.settings(PAYSTACK_LIVE_SECRET_KEY='sk_live_example', PAYSTACK_LIVE_WEBHOOK_SIGNING_SECRET='whsec_live_secret', PAYSTACK_CALLBACK_URL='http://api.example.test/callback/'):
            with self.assertRaises(PaystackConfigurationError):
                get_config()
        with self.settings(PAYSTACK_LIVE_SECRET_KEY='sk_live_example', PAYSTACK_LIVE_WEBHOOK_SIGNING_SECRET='whsec_live_secret', PAYSTACK_CALLBACK_URL='https://api.example.test/callback/', FRONTEND_PAYMENT_RETURN_URL='http://app.example.test/payment/complete'):
            with self.assertRaises(PaystackConfigurationError):
                get_config()

    @override_settings(
        PAYSTACK_MODE='live',
        PAYSTACK_SECRET_KEY='sk_test_legacy_only',
        PAYSTACK_WEBHOOK_SIGNING_SECRET='whsec_test_legacy_only',
        PAYSTACK_TEST_SECRET_KEY='sk_test_isolated',
        PAYSTACK_TEST_WEBHOOK_SIGNING_SECRET='whsec_test_isolated',
        PAYSTACK_LIVE_SECRET_KEY='sk_live_selected',
        PAYSTACK_LIVE_WEBHOOK_SIGNING_SECRET='whsec_live_selected',
        PAYSTACK_CALLBACK_URL='https://api.example.test/api/billing/paystack/callback/',
        FRONTEND_PAYMENT_RETURN_URL='https://app.example.test/payment/complete',
    )
    def test_live_mode_selects_only_live_credentials_when_both_sets_exist(self):
        self.assertEqual(get_config().secret_key, 'sk_live_selected')

        raw = b'{"event":"charge.success"}'
        timestamp = int(time.time())
        signature = hmac.new(
            b'whsec_live_selected', f'{timestamp}.'.encode('ascii') + raw, hashlib.sha512
        ).hexdigest()
        self.assertTrue(webhook_signature_valid(raw, {'Paystack-Signature': f't={timestamp},v1={signature}'}))

    @override_settings(
        PAYSTACK_MODE='live',
        PAYSTACK_SECRET_KEY='sk_test_legacy_only',
        PAYSTACK_WEBHOOK_SIGNING_SECRET='whsec_test_legacy_only',
        PAYSTACK_LIVE_SECRET_KEY='',
        PAYSTACK_LIVE_WEBHOOK_SIGNING_SECRET='',
        PAYSTACK_CALLBACK_URL='https://api.example.test/api/billing/paystack/callback/',
        FRONTEND_PAYMENT_RETURN_URL='https://app.example.test/payment/complete',
    )
    def test_live_mode_never_falls_back_to_legacy_or_test_credentials(self):
        with self.assertRaises(PaystackConfigurationError):
            get_config()
        self.assertFalse(webhook_signature_valid(b'{}', {'Paystack-Signature': 't=1,v1=invalid'}))

    @patch('billing.services.paystack.requests.request')
    def test_service_uses_timeout_and_handles_provider_response(self, request):
        response = Mock(ok=True, status_code=200)
        response.json.return_value = {'status': True, 'data': {'reference': 'bgt_reference', 'authorization_url': 'https://checkout.test'}}
        request.return_value = response
        result = initialize_transaction(
            email='payer@example.com', amount_minor=105000, reference='bgt_reference',
            callback_url='http://localhost:8000/api/billing/paystack/callback/', metadata={'purchase_reference': 'bgt_reference'},
        )
        self.assertEqual(result['reference'], 'bgt_reference')
        self.assertEqual(request.call_args.kwargs['timeout'], (3.05, 10.0))

    @patch('billing.services.paystack.requests.request')
    def test_service_turns_provider_timeout_into_safe_error(self, request):
        request.side_effect = __import__('requests').Timeout()
        with self.assertRaises(PaystackUnavailableError):
            initialize_transaction(
                email='payer@example.com', amount_minor=105000, reference='bgt_reference',
                callback_url='http://localhost:8000/api/billing/paystack/callback/', metadata={},
            )

    @patch('billing.services.paystack.requests.request')
    def test_service_turns_provider_5xx_into_safe_unavailable_error(self, request):
        response = Mock(ok=False, status_code=502)
        request.return_value = response

        with self.assertRaises(PaystackUnavailableError):
            initialize_transaction(
                email='payer@example.com', amount_minor=105000, reference='bgt_reference',
                callback_url='http://localhost:8000/api/billing/paystack/callback/', metadata={},
            )

    @override_settings(PAYSTACK_ALLOW_LEGACY_WEBHOOK_SIGNATURE=True)
    def test_invalid_current_signature_never_falls_back_to_legacy(self):
        raw = b'{"event":"charge.success"}'
        legacy = hmac.new(b'sk_test_example', raw, hashlib.sha512).hexdigest()
        self.assertFalse(webhook_signature_valid(raw, {'Paystack-Signature': 't=1,v1=invalid', 'x-paystack-signature': legacy}))
