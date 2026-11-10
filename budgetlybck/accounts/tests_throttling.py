from django.contrib.auth import get_user_model
from django.core.cache import cache
from rest_framework import status
from rest_framework.test import APITestCase

from .views import (ForgotPasswordThrottle, GoogleAuthenticationThrottle,
                    LoginThrottle, RegistrationThrottle, ResetPasswordThrottle)


User = get_user_model()


class AuthenticationThrottleTests(APITestCase):
    """Keep public authentication limits intentional and regression-tested."""

    def setUp(self):
        super().setUp()
        cache.clear()
        self._original_rates = {
            throttle: throttle.THROTTLE_RATES
            for throttle in (
                LoginThrottle, RegistrationThrottle, ForgotPasswordThrottle,
                ResetPasswordThrottle, GoogleAuthenticationThrottle,
            )
        }

    def tearDown(self):
        for throttle, rates in self._original_rates.items():
            throttle.THROTTLE_RATES = rates
        cache.clear()
        super().tearDown()

    def set_rate(self, throttle, scope, value='1/minute'):
        throttle.THROTTLE_RATES = {**throttle.THROTTLE_RATES, scope: value}

    def test_login_is_limited_by_request_identity_not_submitted_credentials(self):
        self.set_rate(LoginThrottle, 'login')
        User.objects.create_user('rate-user', 'rate@example.com', 'SafePassword123!')

        first = self.client.post('/api/auth/login/', {
            'username': 'rate-user', 'password': 'SafePassword123!',
        }, format='json')
        second = self.client.post('/api/auth/login/', {
            'username': 'another-name', 'password': 'incorrect',
        }, format='json')

        self.assertEqual(first.status_code, status.HTTP_200_OK)
        self.assertEqual(second.status_code, status.HTTP_429_TOO_MANY_REQUESTS)

    def test_public_registration_and_password_endpoints_are_limited(self):
        cases = (
            (RegistrationThrottle, 'registration', '/api/auth/register/', {}),
            (ForgotPasswordThrottle, 'forgot_password', '/api/auth/forgot-password/', {'email': 'any@example.com'}),
            (ResetPasswordThrottle, 'reset_password', '/api/auth/reset-password/invalid/invalid/', {'new_password': 'SafePassword123!'}),
        )
        for throttle, scope, url, payload in cases:
            with self.subTest(scope=scope):
                cache.clear()
                self.set_rate(throttle, scope)
                self.client.post(url, payload, format='json')
                response = self.client.post(url, payload, format='json')
                self.assertEqual(response.status_code, status.HTTP_429_TOO_MANY_REQUESTS)

    def test_existing_google_authentication_limit_is_preserved(self):
        self.set_rate(GoogleAuthenticationThrottle, 'google_auth')

        self.client.post('/api/auth/google/', {'credential': 'not-a-token'}, format='json')
        response = self.client.post('/api/auth/google/', {'credential': 'another-not-a-token'}, format='json')

        self.assertEqual(response.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
