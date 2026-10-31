from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import mail
from django.test import override_settings
from rest_framework import status
from rest_framework.test import APITestCase


User = get_user_model()


class RegistrationLegalAcceptanceTests(APITestCase):
    url = "/api/auth/register/"

    def payload(self, **overrides):
        data = {
            "username": "new-user", "email": "new-user@example.com",
            "password": "SafePassword123!", "password_confirm": "SafePassword123!",
            "age_eligibility": True, "accept_terms": True, "accept_privacy_policy": True,
        }
        data.update(overrides)
        return data

    def test_registration_records_server_controlled_legal_acceptance(self):
        response = self.client.post(self.url, self.payload(), format="json")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        user = User.objects.get(email="new-user@example.com")
        self.assertTrue(user.age_eligibility_confirmed)
        self.assertIsNotNone(user.terms_accepted_at)
        self.assertIsNotNone(user.privacy_policy_accepted_at)
        self.assertEqual(user.accepted_policy_version, settings.LEGAL_POLICY_VERSION)

    def test_registration_requires_each_legal_declaration(self):
        response = self.client.post(self.url, self.payload(accept_terms=False), format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("accept_terms", response.data)
        self.assertFalse(User.objects.filter(email="new-user@example.com").exists())


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    FRONTEND_PASSWORD_RESET_URL="https://app.example.test/reset-password",
)
class PasswordResetTests(APITestCase):
    url = "/api/auth/forgot-password/"

    def setUp(self):
        self.user = User.objects.create_user(
            username="reset-user", email="reset@example.com", password="OldPassword123!"
        )

    def test_forgot_password_uses_generic_response_and_configured_frontend_url(self):
        existing = self.client.post(self.url, {"email": self.user.email}, format="json")
        unknown = self.client.post(self.url, {"email": "unknown@example.com"}, format="json")

        self.assertEqual(existing.status_code, status.HTTP_200_OK)
        self.assertEqual(existing.data, unknown.data)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("https://app.example.test/reset-password/", mail.outbox[0].body)
