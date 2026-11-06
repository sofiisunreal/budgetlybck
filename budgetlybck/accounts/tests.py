from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import mail
from django.test import override_settings
from django.test import TransactionTestCase
from django.db import transaction
from unittest.mock import patch
from rest_framework import status
from rest_framework.test import APITestCase, APIRequestFactory
from .views import ForgotPasswordView, RegisterView, send_welcome_email


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
        self.factory = APIRequestFactory()

    def request_reset(self, email):
        return ForgotPasswordView.as_view()(self.factory.post(self.url, {"email": email}, format="json"))

    def test_forgot_password_uses_generic_response_and_configured_frontend_url(self):
        existing = self.request_reset(self.user.email)
        unknown = self.request_reset("unknown@example.com")

        self.assertEqual(existing.status_code, status.HTTP_200_OK)
        self.assertEqual(existing.data, unknown.data)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("https://app.example.test/reset-password/", mail.outbox[0].body)

    def test_password_reset_email_is_multipart_and_uses_branded_html_template(self):
        response = self.request_reset(self.user.email)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        message = mail.outbox[0]
        self.assertEqual(message.content_subtype, "plain")
        self.assertEqual(message.alternatives[0][1], "text/html")
        self.assertIn("A little reset.", message.alternatives[0][0])
        self.assertIn(self.user.first_name or self.user.username, message.alternatives[0][0])
        self.assertIn("https://app.example.test/reset-password/", message.alternatives[0][0])

    def test_email_delivery_failure_returns_safe_error_without_sensitive_logging(self):
        with patch("accounts.views.EmailMultiAlternatives.send", side_effect=OSError("SMTP unavailable")):
            with self.assertLogs("accounts.views", level="ERROR") as logs:
                response = self.request_reset(self.user.email)

        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertEqual(response.data["message"], "Unable to process the password reset request right now.")
        self.assertIn("Password reset email delivery failed", "\n".join(logs.output))
        self.assertNotIn("reset-password/", "\n".join(logs.output))


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    FRONTEND_APP_URL="https://app.example.test",
)
class WelcomeEmailTests(TransactionTestCase):
    def setUp(self):
        self.factory = APIRequestFactory()

    def payload(self, **overrides):
        value = {"username": "welcome-user", "email": "welcome@example.com", "password": "SafePassword123!", "password_confirm": "SafePassword123!", "age_eligibility": True, "accept_terms": True, "accept_privacy_policy": True, "first_name": "Amina"}
        value.update(overrides)
        return value

    def register(self, **overrides):
        return RegisterView.as_view()(self.factory.post("/api/auth/register/", self.payload(**overrides), format="json"))

    def test_successful_registration_sends_one_multipart_welcome_email(self):
        response = self.register()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.subject, "Welcome to Budgetly. Let's make money simpler.")
        self.assertIn("https://app.example.test", message.body)
        self.assertEqual(message.alternatives[0][1], "text/html")
        self.assertIn("Money stuff, a little less complicated.", message.alternatives[0][0])

    def test_duplicate_registration_does_not_send_a_second_welcome_email(self):
        self.assertEqual(self.register().status_code, status.HTTP_201_CREATED)
        self.assertEqual(self.register().status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(len(mail.outbox), 1)

    def test_rollback_does_not_send_welcome_email(self):
        with transaction.atomic():
            response = self.register()
            self.assertEqual(response.status_code, status.HTTP_201_CREATED)
            transaction.set_rollback(True)
        self.assertEqual(len(mail.outbox), 0)

    def test_welcome_delivery_failure_does_not_fail_registration(self):
        with patch("accounts.views.EmailMultiAlternatives.send", side_effect=OSError("SMTP unavailable")):
            with self.assertLogs("accounts.views", level="ERROR") as logs:
                response = self.register()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(User.objects.filter(email="welcome@example.com").exists())
        self.assertIn("Welcome email delivery failed", "\n".join(logs.output))
