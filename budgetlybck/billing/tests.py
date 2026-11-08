from datetime import datetime, timezone as datetime_timezone
import hashlib
import hmac
from io import StringIO
import json
import time
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import IntegrityError
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APITestCase, APIRequestFactory, force_authenticate

from budget.models import Account, Category, Transaction
from .models import (LifetimeAccessEntitlement, LifetimeAccessOffer,
                     PaystackPurchaseAttempt, PaystackWebhookEvent)
from .permissions import has_lifetime_access
from .services.paystack import (PaystackUnavailableError, initialize_transaction,
                                webhook_signature_valid)
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

    def test_refund_and_chargeback_events_move_paid_access_to_review_states(self):
        attempt = self.attempt(status=PaystackPurchaseAttempt.SUCCESSFUL)
        entitlement = LifetimeAccessEntitlement.objects.create(
            user=self.user, source=LifetimeAccessEntitlement.PAYSTACK_PURCHASE, activating_purchase=attempt
        )
        refund = {'event': 'refund.processed', 'data': {'id': 90, 'reference': attempt.reference}}
        raw, signature = self.signed_webhook(refund)
        response = self.client.post('/api/billing/paystack/webhook/', raw, content_type='application/json', HTTP_PAYSTACK_SIGNATURE=signature)
        entitlement.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(entitlement.status, LifetimeAccessEntitlement.REFUNDED)

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


@override_settings(DEBUG=True, PAYSTACK_MODE='test', PAYSTACK_SECRET_KEY='sk_test_example', PAYSTACK_CALLBACK_URL='http://localhost:8000/api/billing/paystack/callback/')
class PaystackServiceTests(TestCase):
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

    @override_settings(PAYSTACK_ALLOW_LEGACY_WEBHOOK_SIGNATURE=True)
    def test_invalid_current_signature_never_falls_back_to_legacy(self):
        raw = b'{"event":"charge.success"}'
        legacy = hmac.new(b'sk_test_example', raw, hashlib.sha512).hexdigest()
        self.assertFalse(webhook_signature_valid(raw, {'Paystack-Signature': 't=1,v1=invalid', 'x-paystack-signature': legacy}))
