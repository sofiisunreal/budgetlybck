import logging
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import override_settings
from rest_framework import status
from rest_framework.test import APITestCase

from budgetlybck.logging_filters import SensitiveDataFilter

from .models import LifetimeAccessOffer, PaystackPurchaseAttempt
from .views import (BillingAccessThrottle, BillingCheckoutThrottle,
                    BillingVerificationThrottle, PaystackWebhookView)


User = get_user_model()


@override_settings(
    PAYSTACK_MODE='test',
    PAYSTACK_TEST_SECRET_KEY='sk_test_throttling',
    PAYSTACK_TEST_WEBHOOK_SIGNING_SECRET='test-webhook-secret',
)
class BillingThrottleTests(APITestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.user = User.objects.create_user('billing-rate-user', 'billing-rate@example.com', 'SafePassword123!')
        self.offer = LifetimeAccessOffer.objects.create(
            name='Budgetly Lifetime Access', description='Lifetime access',
            price_minor=105000, currency='KES', is_active=True,
        )
        self.client.force_authenticate(self.user)
        self._original_rates = {
            throttle: throttle.THROTTLE_RATES
            for throttle in (BillingAccessThrottle, BillingCheckoutThrottle, BillingVerificationThrottle)
        }

    def tearDown(self):
        for throttle, rates in self._original_rates.items():
            throttle.THROTTLE_RATES = rates
        cache.clear()
        super().tearDown()

    def set_rate(self, throttle, scope, value):
        throttle.THROTTLE_RATES = {**throttle.THROTTLE_RATES, scope: value}

    @patch('billing.views.initialize_transaction')
    def test_checkout_initialization_is_limited(self, initialize):
        self.set_rate(BillingCheckoutThrottle, 'billing_checkout', '1/minute')
        initialize.side_effect = lambda **kwargs: {
            'reference': kwargs['reference'], 'authorization_url': 'https://checkout.example.test',
        }

        first = self.client.post('/api/billing/purchases/initialize/', {}, format='json')
        second = self.client.post('/api/billing/purchases/initialize/', {}, format='json')

        self.assertEqual(first.status_code, status.HTTP_201_CREATED)
        self.assertEqual(second.status_code, status.HTTP_429_TOO_MANY_REQUESTS)

    @patch('billing.views.verify_and_activate_purchase')
    def test_verification_allows_normal_recovery_retries(self, verify):
        self.set_rate(BillingVerificationThrottle, 'billing_verify', '2/minute')
        attempt = PaystackPurchaseAttempt.objects.create(
            user=self.user, offer=self.offer, expected_amount_minor=105000,
            expected_currency='KES', provider_mode='test',
        )
        verify.return_value = (attempt, None, False)
        url = f'/api/billing/purchases/{attempt.reference}/verify/'

        first = self.client.post(url, {}, format='json')
        second = self.client.post(url, {}, format='json')

        self.assertEqual(first.status_code, status.HTTP_200_OK)
        self.assertEqual(second.status_code, status.HTTP_200_OK)
        self.assertEqual(verify.call_count, 2)

    def test_billing_status_is_limited_and_webhooks_remain_exempt(self):
        self.set_rate(BillingAccessThrottle, 'billing_access', '1/minute')

        first = self.client.get('/api/billing/access/')
        second = self.client.get('/api/billing/access/')

        self.assertEqual(first.status_code, status.HTTP_200_OK)
        self.assertEqual(second.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        self.assertEqual(PaystackWebhookView.throttle_classes, [])


class SensitiveLoggingTests(APITestCase):
    def test_sensitive_values_are_redacted_before_console_output(self):
        record = logging.LogRecord(
            'billing', logging.WARNING, __file__, 1,
            (
                'authorization: Bearer jwt-value password=not-safe '
                'access_token=refreshable-jwt secret=provider-secret'
            ),
            (),
            None,
        )

        SensitiveDataFilter().filter(record)

        self.assertNotIn('jwt-value', record.msg)
        self.assertNotIn('not-safe', record.msg)
        self.assertNotIn('refreshable-jwt', record.msg)
        self.assertNotIn('provider-secret', record.msg)
        self.assertIn('[REDACTED]', record.msg)
