from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.core.exceptions import ValidationError
from django.core import mail
from django.core.management import call_command, CommandError
from django.test import override_settings
from django.test import TransactionTestCase
from django.db import transaction
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from unittest.mock import patch
import hashlib
import tempfile
from io import StringIO
from pathlib import Path
from rest_framework import status
from rest_framework.test import APITestCase, APIRequestFactory, force_authenticate
from .views import ActivePolicyDocumentsView, DeleteAccountView, ForgotPasswordView, RegisterView, send_welcome_email
from .models import PolicyAcceptance, PolicyDocument
from .models import GoogleIdentity
from .services.google_auth import GoogleCredentialError, verify_google_credential
from budget.models import Account, Budget, Category, Loan, LoanRepayment, Transaction, Transfer
from feedback.models import Feedback
from savings.models import SavingsContribution, SavingsGoal, SavingsPlan, SavingsWithdrawal
from billing.models import LifetimeAccessEntitlement, LifetimeAccessOffer, PaystackPurchaseAttempt
from billing.services.access import apply_verification
from rest_framework_simplejwt.tokens import AccessToken, RefreshToken


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

    def activate_documents(self):
        terms = PolicyDocument.objects.create(
            document_type=PolicyDocument.TERMS,
            version="1.0",
            content="Terms of Service v1.0",
            is_active=True,
        )
        privacy = PolicyDocument.objects.create(
            document_type=PolicyDocument.PRIVACY,
            version="1.0",
            content="Privacy Policy v1.0",
            is_active=True,
        )
        return terms, privacy

    def identity(self, document):
        return {"id": document.id, "version": document.version, "content_sha256": document.content_sha256}

    def test_registration_records_server_controlled_legal_acceptance(self):
        terms, privacy = self.activate_documents()
        response = self.client.post(
            self.url,
            self.payload(terms_document=self.identity(terms), privacy_document=self.identity(privacy)),
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        user = User.objects.get(email="new-user@example.com")
        self.assertTrue(user.age_eligibility_confirmed)
        self.assertIsNotNone(user.terms_accepted_at)
        self.assertIsNotNone(user.privacy_policy_accepted_at)
        self.assertEqual(user.accepted_policy_version, settings.LEGAL_POLICY_VERSION)

    def test_registration_requires_each_legal_declaration(self):
        terms, privacy = self.activate_documents()
        response = self.client.post(
            self.url,
            self.payload(
                accept_terms=False,
                terms_document=self.identity(terms),
                privacy_document=self.identity(privacy),
            ),
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("accept_terms", response.data)
        self.assertFalse(User.objects.filter(email="new-user@example.com").exists())

    def test_active_documents_create_acceptances_for_the_exact_documents(self):
        terms, privacy = self.activate_documents()

        response = self.client.post(
            self.url,
            self.payload(terms_document=self.identity(terms), privacy_document=self.identity(privacy)),
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        user = User.objects.get(email="new-user@example.com")
        acceptances = PolicyAcceptance.objects.filter(user=user).select_related("document")
        self.assertEqual(set(acceptance.document_id for acceptance in acceptances), {terms.id, privacy.id})
        self.assertTrue(all(acceptance.accepted_at for acceptance in acceptances))
        self.assertEqual(terms.content_sha256, hashlib.sha256(terms.content.encode("utf-8")).hexdigest())

    def test_legacy_acceptance_data_is_not_rewritten_for_existing_users(self):
        user = User.objects.create_user(
            username="legacy-user",
            email="legacy@example.com",
            password="SafePassword123!",
            accepted_policy_version="1.0",
        )
        original_version = user.accepted_policy_version
        self.activate_documents()

        user.refresh_from_db()

        self.assertEqual(user.accepted_policy_version, original_version)
        self.assertFalse(PolicyAcceptance.objects.filter(user=user).exists())

    def test_partial_document_activation_blocks_registration(self):
        PolicyDocument.objects.create(
            document_type=PolicyDocument.TERMS,
            version="1.0",
            content="Terms of Service v1.0",
            is_active=True,
        )

        response = RegisterView.as_view()(
            APIRequestFactory().post(self.url, self.payload(), format="json")
        )

        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertFalse(User.objects.filter(email="new-user@example.com").exists())

    def test_registration_rejects_missing_or_stale_document_identity(self):
        terms, privacy = self.activate_documents()
        missing = self.client.post(self.url, self.payload(), format="json")
        stale = self.client.post(
            self.url,
            self.payload(
                terms_document={**self.identity(terms), "content_sha256": "0" * 64},
                privacy_document=self.identity(privacy),
            ),
            format="json",
        )

        self.assertEqual(missing.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("terms_document", missing.data)
        self.assertEqual(stale.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("terms_document", stale.data)
        self.assertFalse(User.objects.filter(email="new-user@example.com").exists())

    def test_accepted_document_content_cannot_be_changed(self):
        document = PolicyDocument.objects.create(
            document_type=PolicyDocument.TERMS,
            version="1.0",
            content="Terms of Service v1.0",
        )
        user = User.objects.create_user("accepted-user", "accepted@example.com", "SafePassword123!")
        PolicyAcceptance.objects.create(user=user, document=document)
        document.content = "Changed text"

        with self.assertRaises(ValidationError):
            document.save()

    def test_unaccepted_document_content_cannot_be_changed(self):
        document = PolicyDocument.objects.create(
            document_type=PolicyDocument.TERMS, version="draft-1.0", content="Original terms"
        )
        document.content = "Replacement terms"

        with self.assertRaises(ValidationError):
            document.save()


@override_settings(GOOGLE_OAUTH_CLIENT_IDS=['google-client-id.apps.googleusercontent.com'])
class GoogleAuthenticationTests(APITestCase):
    url = '/api/auth/google/'
    link_url = '/api/auth/google/link/'

    def setUp(self):
        self.terms = PolicyDocument.objects.create(
            document_type=PolicyDocument.TERMS, version='1.0', content='Terms', is_active=True
        )
        self.privacy = PolicyDocument.objects.create(
            document_type=PolicyDocument.PRIVACY, version='1.0', content='Privacy', is_active=True
        )

    def claims(self, **overrides):
        result = {
            'subject': 'google-subject-123',
            'email': 'google.user@example.com',
            'given_name': 'Google',
            'family_name': 'User',
        }
        result.update(overrides)
        return result

    def legal_payload(self):
        return {
            'age_eligibility': True,
            'accept_terms': True,
            'accept_privacy_policy': True,
            'terms_document': {'id': self.terms.id, 'version': self.terms.version, 'content_sha256': self.terms.content_sha256},
            'privacy_document': {'id': self.privacy.id, 'version': self.privacy.version, 'content_sha256': self.privacy.content_sha256},
        }

    @patch('accounts.views.verify_google_credential')
    def test_valid_credential_creates_user_with_legal_acceptance_and_jwt(self, verify):
        verify.return_value = self.claims()

        response = self.client.post(self.url, {'credential': 'not-logged', **self.legal_payload()}, format='json')

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertIn('access', response.data)
        self.assertIn('refresh', response.data)
        user = User.objects.get(email='google.user@example.com')
        self.assertFalse(user.has_usable_password())
        self.assertEqual(GoogleIdentity.objects.get(user=user).subject, 'google-subject-123')
        self.assertEqual(PolicyAcceptance.objects.filter(user=user).count(), 2)
        self.assertFalse(LifetimeAccessEntitlement.objects.filter(user=user).exists())

    @patch('accounts.views.verify_google_credential')
    def test_existing_linked_identity_signs_in_without_legal_resubmission(self, verify):
        user = User.objects.create_user('linked-google', 'linked@example.com', 'SafePassword123!')
        GoogleIdentity.objects.create(user=user, subject='google-subject-123')
        verify.return_value = self.claims(email=user.email)

        response = self.client.post(self.url, {'credential': 'not-logged'}, format='json')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('access', response.data)
        self.assertEqual(User.objects.filter(email=user.email).count(), 1)

    @patch('accounts.views.verify_google_credential')
    def test_inactive_linked_user_cannot_obtain_tokens(self, verify):
        user = User.objects.create_user('inactive-google', 'inactive@example.com', 'SafePassword123!')
        user.is_active = False
        user.save(update_fields=['is_active'])
        GoogleIdentity.objects.create(user=user, subject='google-subject-123')
        verify.return_value = self.claims(email=user.email)

        response = self.client.post(self.url, {'credential': 'not-logged'}, format='json')

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(response.data['code'], 'inactive_user')

    def test_google_user_can_use_existing_password_reset_recovery(self):
        user = User.objects.create_user('recover-google', 'recover@example.com', 'UnusedPassword123!')
        user.set_unusable_password()
        user.save(update_fields=['password'])
        GoogleIdentity.objects.create(user=user, subject='recovery-google-subject')
        uid = urlsafe_base64_encode(force_bytes(user.pk))
        token = default_token_generator.make_token(user)

        response = self.client.post(
            f'/api/auth/reset-password/{uid}/{token}/',
            {'new_password': 'RecoveredPassword123!', 'confirm_password': 'RecoveredPassword123!'},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        user.refresh_from_db()
        self.assertTrue(user.check_password('RecoveredPassword123!'))

    @patch('accounts.views.verify_google_credential')
    def test_existing_password_email_requires_explicit_authenticated_link(self, verify):
        existing = User.objects.create_user('password-user', 'google.user@example.com', 'SafePassword123!')
        verify.return_value = self.claims()

        response = self.client.post(self.url, {'credential': 'not-logged', **self.legal_payload()}, format='json')

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.data['code'], 'link_required')
        self.assertFalse(GoogleIdentity.objects.filter(user=existing).exists())

    @patch('accounts.views.verify_google_credential')
    def test_authenticated_user_can_link_once_and_other_user_cannot_take_identity(self, verify):
        first = User.objects.create_user('first-user', 'first@example.com', 'SafePassword123!')
        second = User.objects.create_user('second-user', 'second@example.com', 'SafePassword123!')
        verify.return_value = self.claims()
        self.client.force_authenticate(first)

        created = self.client.post(self.link_url, {'credential': 'not-logged'}, format='json')
        repeated = self.client.post(self.link_url, {'credential': 'not-logged'}, format='json')
        self.client.force_authenticate(second)
        conflict = self.client.post(self.link_url, {'credential': 'not-logged'}, format='json')

        self.assertEqual((created.status_code, repeated.status_code, conflict.status_code), (201, 200, 409))
        self.assertEqual(conflict.data['code'], 'identity_in_use')

    @patch('accounts.views.verify_google_credential')
    def test_user_cannot_link_a_second_google_identity(self, verify):
        user = User.objects.create_user('one-google-user', 'one-google@example.com', 'SafePassword123!')
        GoogleIdentity.objects.create(user=user, subject='first-google-subject')
        verify.return_value = self.claims(subject='second-google-subject')
        self.client.force_authenticate(user)

        response = self.client.post(self.link_url, {'credential': 'not-logged'}, format='json')

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.data['code'], 'user_already_linked')

    @patch('accounts.views.verify_google_credential')
    def test_missing_or_stale_legal_acceptance_cannot_create_google_user(self, verify):
        verify.return_value = self.claims()
        missing = self.client.post(self.url, {'credential': 'not-logged'}, format='json')
        stale = self.client.post(self.url, {
            'credential': 'not-logged',
            **self.legal_payload(),
            'terms_document': {'id': self.terms.id, 'version': self.terms.version, 'content_sha256': '0' * 64},
        }, format='json')

        self.assertEqual(missing.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(missing.data['code'], 'legal_acceptance_required')
        self.assertEqual(stale.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('terms_document', stale.data)
        self.assertFalse(User.objects.filter(email='google.user@example.com').exists())

    @patch('accounts.services.google_auth._verify_with_google_auth')
    def test_wrong_audience_issuer_or_unverified_email_is_rejected(self, verify):
        base = {'sub': 'google-subject-123', 'email': 'google.user@example.com', 'email_verified': True, 'aud': 'google-client-id.apps.googleusercontent.com', 'iss': 'https://accounts.google.com'}
        for overrides in (
            {'aud': 'wrong-client'}, {'iss': 'https://issuer.example'}, {'email_verified': False}, {'sub': ''},
        ):
            verify.return_value = {**base, **overrides}
            with self.assertRaises(GoogleCredentialError):
                verify_google_credential('not-logged')

    @patch('accounts.services.google_auth._verify_with_google_auth', side_effect=ValueError('expired'))
    def test_expired_or_invalid_credential_is_rejected(self, verify):
        with self.assertRaises(GoogleCredentialError):
            verify_google_credential('not-logged')

    @patch('accounts.views.verify_google_credential')
    def test_account_deletion_cascades_google_identity(self, verify):
        user = User.objects.create_user('delete-google', 'delete-google@example.com', 'SafePassword123!')
        identity = GoogleIdentity.objects.create(user=user, subject='deletion-subject')
        self.client.force_authenticate(user)

        response = self.client.delete('/api/auth/delete-account/', {'password': 'SafePassword123!'}, format='json')

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(GoogleIdentity.objects.filter(pk=identity.pk).exists())


class PolicyPublicationCommandTests(APITestCase):
    def setUp(self):
        self.files = tempfile.TemporaryDirectory()
        self.terms_path = Path(self.files.name) / "terms.md"
        self.privacy_path = Path(self.files.name) / "privacy.md"
        self.terms_path.write_text("# Terms\n\nExact v1 text.\n", encoding="utf-8")
        self.privacy_path.write_text("# Privacy\n\nExact v1 text.\n", encoding="utf-8")
        self.terms_content = self.read_exact(self.terms_path)
        self.privacy_content = self.read_exact(self.privacy_path)
        self.base_arguments = [
            "--terms-file", str(self.terms_path),
            "--privacy-file", str(self.privacy_path),
            "--policy-version", "1.0",
            "--effective-at", "2025-12-01T00:00:00Z",
        ]

    def tearDown(self):
        self.files.cleanup()

    @staticmethod
    def read_exact(path):
        with path.open("r", encoding="utf-8", newline="") as source:
            return source.read()

    def run_command(self, *arguments):
        output = StringIO()
        call_command("publish_policy_documents", *self.base_arguments, *arguments, stdout=output)
        return output.getvalue()

    def test_imports_exact_inactive_pair_and_is_idempotent(self):
        self.run_command()
        self.run_command()

        terms = PolicyDocument.objects.get(document_type=PolicyDocument.TERMS, version="1.0")
        privacy = PolicyDocument.objects.get(document_type=PolicyDocument.PRIVACY, version="1.0")
        self.assertEqual(PolicyDocument.objects.count(), 2)
        self.assertEqual(terms.content, self.terms_content)
        self.assertEqual(terms.content_sha256, hashlib.sha256(terms.content.encode("utf-8")).hexdigest())
        self.assertEqual(privacy.content_sha256, hashlib.sha256(privacy.content.encode("utf-8")).hexdigest())
        self.assertFalse(terms.is_active)
        self.assertFalse(privacy.is_active)

    def test_dry_run_and_activation_confirmation_do_not_change_records(self):
        self.run_command("--dry-run")
        self.assertFalse(PolicyDocument.objects.exists())

    def test_rejects_early_activation_of_a_future_effective_pair(self):
        future_arguments = [
            "--terms-file", str(self.terms_path),
            "--privacy-file", str(self.privacy_path),
            "--policy-version", "future-1.0",
            "--effective-at", "2099-12-01T00:00:00Z",
            "--activate", "--confirm-activate", "ACTIVATE",
        ]
        with self.assertRaises(CommandError):
            call_command("publish_policy_documents", *future_arguments)
        self.assertFalse(PolicyDocument.objects.exists())
        with self.assertRaises(CommandError):
            self.run_command("--activate")
        self.assertFalse(PolicyDocument.objects.exists())

    def test_activates_both_documents_only_with_explicit_confirmation(self):
        self.run_command("--activate", "--confirm-activate", "ACTIVATE")

        self.assertEqual(PolicyDocument.objects.filter(is_active=True).count(), 2)
        self.assertIsNotNone(PolicyDocument.active_documents())

    def test_rejects_content_mismatch_for_existing_version(self):
        self.run_command()
        self.terms_path.write_text("# Terms\n\nChanged v1 text.\n", encoding="utf-8")

        with self.assertRaises(CommandError):
            self.run_command()
        self.assertEqual(PolicyDocument.objects.get(document_type=PolicyDocument.TERMS).content, self.terms_content)


class LegalPolicyApiTests(APITestCase):
    documents_url = "/api/auth/legal-documents/"
    status_url = "/api/auth/legal-status/"
    acceptances_url = "/api/auth/legal-acceptances/"

    def activate_documents(self):
        terms = PolicyDocument.objects.create(
            document_type=PolicyDocument.TERMS,
            version="1.0",
            content="Terms of Service v1.0",
            is_active=True,
        )
        privacy = PolicyDocument.objects.create(
            document_type=PolicyDocument.PRIVACY,
            version="1.0",
            content="Privacy Policy v1.0",
            is_active=True,
        )
        return terms, privacy

    def identities(self, terms, privacy):
        return {
            "terms_document": {"id": terms.id, "version": terms.version, "content_sha256": terms.content_sha256},
            "privacy_document": {"id": privacy.id, "version": privacy.version, "content_sha256": privacy.content_sha256},
        }

    def test_public_documents_are_unavailable_until_a_complete_pair_is_active(self):
        response = ActivePolicyDocumentsView.as_view()(APIRequestFactory().get(self.documents_url))
        unauthenticated_acceptance = self.client.post(self.acceptances_url, {}, format="json")
        user = User.objects.create_user("status-user", "status@example.com", "SafePassword123!")
        self.client.force_authenticate(user=user)
        pending_status = self.client.get(self.status_url)

        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertEqual(response.data["code"], "legal_documents_unavailable")
        self.assertEqual(unauthenticated_acceptance.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertFalse(pending_status.data["documents_available"])
        self.assertFalse(pending_status.data["acceptance_required"])

    def test_public_documents_return_exact_text_hash_and_dates(self):
        terms, privacy = self.activate_documents()

        response = self.client.get(self.documents_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["terms"]["id"], terms.id)
        self.assertEqual(response.data["terms"]["content"], terms.content)
        self.assertEqual(response.data["terms"]["content_sha256"], terms.content_sha256)
        self.assertEqual(response.data["terms"]["effective_at"], response.data["terms"]["published_at"])
        self.assertEqual(response.data["privacy"]["id"], privacy.id)

    def test_existing_user_can_explicitly_accept_and_duplicate_is_idempotent(self):
        terms, privacy = self.activate_documents()
        user = User.objects.create_user("pending-user", "pending@example.com", "SafePassword123!")
        other_user = User.objects.create_user("other-user", "other@example.com", "SafePassword123!")
        self.client.force_authenticate(user=user)

        pending = self.client.get(self.status_url)
        first = self.client.post(self.acceptances_url, self.identities(terms, privacy), format="json")
        second = self.client.post(self.acceptances_url, self.identities(terms, privacy), format="json")
        current = self.client.get(self.status_url)

        self.assertTrue(pending.data["acceptance_required"])
        self.assertEqual(first.status_code, status.HTTP_201_CREATED)
        self.assertEqual(second.status_code, status.HTTP_200_OK)
        self.assertFalse(current.data["acceptance_required"])
        self.assertIn("accepted_at", first.data["acceptances"]["terms"])
        self.assertEqual(PolicyAcceptance.objects.filter(user=user).count(), 2)
        self.assertFalse(PolicyAcceptance.objects.filter(user=other_user).exists())


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
        self.terms = PolicyDocument.objects.create(
            document_type=PolicyDocument.TERMS, version="1.0", content="Terms", is_active=True
        )
        self.privacy = PolicyDocument.objects.create(
            document_type=PolicyDocument.PRIVACY, version="1.0", content="Privacy", is_active=True
        )

    def payload(self, **overrides):
        value = {"username": "welcome-user", "email": "welcome@example.com", "password": "SafePassword123!", "password_confirm": "SafePassword123!", "age_eligibility": True, "accept_terms": True, "accept_privacy_policy": True, "first_name": "Amina", "terms_document": {"id": self.terms.id, "version": self.terms.version, "content_sha256": self.terms.content_sha256}, "privacy_document": {"id": self.privacy.id, "version": self.privacy.version, "content_sha256": self.privacy.content_sha256}}
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


class AccountDeletionTests(APITestCase):
    """Exercise the explicit, FK-safe self-service deletion workflow."""

    def setUp(self):
        self.factory = APIRequestFactory()
        self.user = User.objects.create_user("delete-user", "delete@example.com", "SafePassword123!")
        self.account = Account.objects.create(user=self.user, name="Cash", account_type="cash", currency="KES")
        self.second_account = Account.objects.create(user=self.user, name="Bank", account_type="bank", currency="KES")
        self.category = Category.objects.create(user=self.user, name="Food", category_type="expense")
        self.global_category = Category.objects.create(name="Global income", category_type="income")
        self.transaction = Transaction.objects.create(user=self.user, account=self.account, category=self.category, transaction_type="expense", description="Lunch", amount="10.00", transaction_date="2026-01-01")
        self.budget = Budget.objects.create(user=self.user, category=self.category, amount="100.00", period_type="monthly", start_date="2026-01-01", end_date="2026-01-31")
        self.transfer = Transfer.objects.create(user=self.user, from_account=self.account, to_account=self.second_account, from_amount="10.00", to_amount="10.00", exchange_rate="1.00000000", transaction_date="2026-01-01")
        self.loan = Loan.objects.create(user=self.user, loan_type="borrowed", person_name="Amina", original_amount="100.00", currency="KES", account=self.account, loan_date="2026-01-01")
        self.repayment = LoanRepayment.objects.create(loan=self.loan, account=self.account, amount="10.00", principal_amount="10.00", repayment_date="2026-01-02")
        self.goal = SavingsGoal.objects.create(user=self.user, name="Emergency", target_amount="100.00", currency="KES", account=self.account)
        self.contribution = SavingsContribution.objects.create(goal=self.goal, amount="10.00", contribution_date="2026-01-02")
        self.withdrawal = SavingsWithdrawal.objects.create(goal=self.goal, amount="1.00", withdrawal_date="2026-01-03")
        self.plan = SavingsPlan.objects.create(goal=self.goal, amount="10.00", frequency="monthly", start_date="2026-01-01")
        self.feedback = Feedback.objects.create(user=self.user, category="general", title="Idea", description="Test feedback")
        self.terms_document = PolicyDocument.objects.create(
            document_type=PolicyDocument.TERMS, version="test-1.0", content="Test terms"
        )
        self.privacy_document = PolicyDocument.objects.create(
            document_type=PolicyDocument.PRIVACY, version="test-1.0", content="Test privacy"
        )
        PolicyAcceptance.objects.create(user=self.user, document=self.terms_document)
        PolicyAcceptance.objects.create(user=self.user, document=self.privacy_document)

        self.other_user = User.objects.create_user("other-user", "other@example.com", "SafePassword123!")
        self.other_account = Account.objects.create(user=self.other_user, name="Other cash", account_type="cash", currency="KES")
        self.other_category = Category.objects.create(user=self.other_user, name="Other food", category_type="expense")
        self.other_transaction = Transaction.objects.create(user=self.other_user, account=self.other_account, category=self.other_category, transaction_type="expense", description="Other lunch", amount="10.00", transaction_date="2026-01-01")
        self.other_feedback = Feedback.objects.create(user=self.other_user, category="general", title="Other idea", description="Other feedback")

    def delete_request(self, password="SafePassword123!"):
        request = self.factory.delete("/api/auth/delete-account/", {"password": password}, format="json")
        force_authenticate(request, user=self.user)
        return DeleteAccountView.as_view()(request)

    def billing_offer(self):
        return LifetimeAccessOffer.objects.create(
            name="Account deletion test offer", price_minor=105000, currency="KES"
        )

    def purchase_attempt(self, status=PaystackPurchaseAttempt.INITIALIZED):
        return PaystackPurchaseAttempt.objects.create(
            user=self.user,
            offer=self.billing_offer(),
            expected_amount_minor=105000,
            expected_currency="KES",
            status=status,
        )

    def test_empty_account_deletion_succeeds(self):
        empty_user = User.objects.create_user("empty-user", "empty@example.com", "SafePassword123!")
        request = self.factory.delete("/api/auth/delete-account/", {"password": "SafePassword123!"}, format="json")
        force_authenticate(request, user=empty_user)

        response = DeleteAccountView.as_view()(request)

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(User.objects.filter(pk=empty_user.pk).exists())

    def test_populated_account_deletion_removes_only_owned_records(self):
        response = self.delete_request()

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(User.objects.filter(pk=self.user.pk).exists())
        self.assertFalse(Account.objects.filter(pk__in=[self.account.pk, self.second_account.pk]).exists())
        self.assertFalse(Transaction.objects.filter(pk=self.transaction.pk).exists())
        self.assertFalse(Budget.objects.filter(pk=self.budget.pk).exists())
        self.assertFalse(Transfer.objects.filter(pk=self.transfer.pk).exists())
        self.assertFalse(Loan.objects.filter(pk=self.loan.pk).exists())
        self.assertFalse(LoanRepayment.objects.filter(pk=self.repayment.pk).exists())
        self.assertFalse(SavingsGoal.objects.filter(pk=self.goal.pk).exists())
        self.assertFalse(SavingsContribution.objects.filter(pk=self.contribution.pk).exists())
        self.assertFalse(SavingsWithdrawal.objects.filter(pk=self.withdrawal.pk).exists())
        self.assertFalse(SavingsPlan.objects.filter(pk=self.plan.pk).exists())
        self.assertFalse(Feedback.objects.filter(pk=self.feedback.pk).exists())
        self.assertFalse(PolicyAcceptance.objects.filter(user_id=self.user.pk).exists())
        self.assertTrue(PolicyDocument.objects.filter(pk=self.terms_document.pk).exists())
        self.assertTrue(PolicyDocument.objects.filter(pk=self.privacy_document.pk).exists())
        self.assertFalse(Category.objects.filter(pk=self.category.pk).exists())
        self.assertTrue(Category.objects.filter(pk=self.global_category.pk).exists())
        self.assertTrue(User.objects.filter(pk=self.other_user.pk).exists())
        self.assertTrue(Account.objects.filter(pk=self.other_account.pk).exists())
        self.assertTrue(Transaction.objects.filter(pk=self.other_transaction.pk).exists())
        self.assertTrue(Feedback.objects.filter(pk=self.other_feedback.pk).exists())
        self.assertTrue(Category.objects.filter(pk=self.other_category.pk).exists())

    def test_incorrect_password_keeps_records_and_returns_bad_request(self):
        response = self.delete_request("wrong-password")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("password", response.data)
        self.assertTrue(User.objects.filter(pk=self.user.pk).exists())
        self.assertTrue(Transaction.objects.filter(pk=self.transaction.pk).exists())
        self.assertTrue(LoanRepayment.objects.filter(pk=self.repayment.pk).exists())
        self.assertTrue(SavingsGoal.objects.filter(pk=self.goal.pk).exists())

    def test_failure_rolls_back_all_deletions(self):
        with patch("accounts.services.account_deletion.delete_user_financial_records", side_effect=RuntimeError("forced failure")):
            with self.assertLogs("accounts.views", level="ERROR"):
                response = self.delete_request()

        self.assertEqual(response.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)
        self.assertEqual(response.data["detail"], "Account deletion could not be completed. No records were deleted.")
        self.assertTrue(User.objects.filter(pk=self.user.pk).exists())
        self.assertTrue(Account.objects.filter(pk=self.account.pk).exists())
        self.assertTrue(Transaction.objects.filter(pk=self.transaction.pk).exists())
        self.assertTrue(Budget.objects.filter(pk=self.budget.pk).exists())
        self.assertTrue(Transfer.objects.filter(pk=self.transfer.pk).exists())
        self.assertTrue(Loan.objects.filter(pk=self.loan.pk).exists())
        self.assertTrue(LoanRepayment.objects.filter(pk=self.repayment.pk).exists())
        self.assertTrue(SavingsGoal.objects.filter(pk=self.goal.pk).exists())
        self.assertTrue(SavingsContribution.objects.filter(pk=self.contribution.pk).exists())
        self.assertTrue(SavingsWithdrawal.objects.filter(pk=self.withdrawal.pk).exists())
        self.assertTrue(SavingsPlan.objects.filter(pk=self.plan.pk).exists())
        self.assertEqual(PolicyAcceptance.objects.filter(user=self.user).count(), 2)

    def test_cross_user_savings_link_returns_conflict_without_changes(self):
        foreign_goal = SavingsGoal.objects.create(
            user=self.other_user,
            name="Foreign goal",
            target_amount="50.00",
            currency="KES",
            account=self.account,
        )

        response = self.delete_request()

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertTrue(User.objects.filter(pk=self.user.pk).exists())
        self.assertTrue(Account.objects.filter(pk=self.account.pk).exists())
        foreign_goal.refresh_from_db()
        self.assertEqual(foreign_goal.account_id, self.account.pk)

    def test_cross_user_financial_reference_returns_conflict_without_changes(self):
        foreign_transaction = Transaction.objects.create(
            user=self.other_user,
            account=self.account,
            category=self.other_category,
            transaction_type="expense",
            description="Invalid foreign reference",
            amount="5.00",
            transaction_date="2026-01-02",
        )

        response = self.delete_request()

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertTrue(User.objects.filter(pk=self.user.pk).exists())
        self.assertTrue(Transaction.objects.filter(pk=foreign_transaction.pk).exists())

    def test_unauthenticated_deletion_is_rejected(self):
        response = self.client.delete("/api/auth/delete-account/", {"password": "SafePassword123!"}, format="json")

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertTrue(User.objects.filter(pk=self.user.pk).exists())

    def test_deletion_remains_available_when_current_policy_acceptance_is_pending(self):
        self.terms_document.is_active = True
        self.terms_document.save()
        self.privacy_document.is_active = True
        self.privacy_document.save()
        pending_user = User.objects.create_user("pending-delete", "pending-delete@example.com", "SafePassword123!")
        request = self.factory.delete("/api/auth/delete-account/", {"password": "SafePassword123!"}, format="json")
        force_authenticate(request, user=pending_user)

        response = DeleteAccountView.as_view()(request)

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(User.objects.filter(pk=pending_user.pk).exists())

    def test_failed_or_pending_purchase_attempt_is_retained_without_user(self):
        failed = self.purchase_attempt(PaystackPurchaseAttempt.FAILED)
        pending = PaystackPurchaseAttempt.objects.create(
            user=self.user, offer=failed.offer, expected_amount_minor=105000,
            expected_currency="KES", status=PaystackPurchaseAttempt.PENDING,
        )

        response = self.delete_request()

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        failed.refresh_from_db()
        pending.refresh_from_db()
        self.assertEqual((failed.user_id, failed.status), (None, PaystackPurchaseAttempt.FAILED))
        self.assertEqual((pending.user_id, pending.status), (None, PaystackPurchaseAttempt.PENDING))

    def test_successful_purchase_is_retained_but_its_entitlement_is_removed(self):
        attempt = self.purchase_attempt(PaystackPurchaseAttempt.SUCCESSFUL)
        entitlement = LifetimeAccessEntitlement.objects.create(
            user=self.user,
            source=LifetimeAccessEntitlement.PAYSTACK_PURCHASE,
            activating_purchase=attempt,
        )

        response = self.delete_request()

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        attempt.refresh_from_db()
        self.assertIsNone(attempt.user_id)
        self.assertEqual(attempt.status, PaystackPurchaseAttempt.SUCCESSFUL)
        self.assertFalse(LifetimeAccessEntitlement.objects.filter(pk=entitlement.pk).exists())

    def test_founding_user_entitlement_is_deleted_with_the_user(self):
        entitlement, _ = LifetimeAccessEntitlement.grant_legacy_access(self.user)

        response = self.delete_request()

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(LifetimeAccessEntitlement.objects.filter(pk=entitlement.pk).exists())

    def test_late_success_for_deleted_pending_account_is_reconciled_without_access(self):
        attempt = self.purchase_attempt(PaystackPurchaseAttempt.PENDING)
        self.assertEqual(self.delete_request().status_code, status.HTTP_204_NO_CONTENT)

        verified, entitlement, activated = apply_verification(attempt.reference, {
            "id": 222,
            "reference": attempt.reference,
            "status": "success",
            "amount": 105000,
            "currency": "KES",
        })

        self.assertEqual(verified.status, PaystackPurchaseAttempt.SUCCESSFUL)
        self.assertIsNone(verified.user_id)
        self.assertIsNone(entitlement)
        self.assertFalse(activated)
        self.assertFalse(LifetimeAccessEntitlement.objects.exists())

    def test_second_request_with_stale_token_is_unauthorized(self):
        token = AccessToken.for_user(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

        first = self.client.delete("/api/auth/delete-account/", {"password": "SafePassword123!"}, format="json")
        second = self.client.delete("/api/auth/delete-account/", {"password": "SafePassword123!"}, format="json")

        self.assertEqual(first.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(second.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_deletion_blacklists_refresh_token(self):
        refresh = RefreshToken.for_user(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {AccessToken.for_user(self.user)}")

        response = self.client.delete("/api/auth/delete-account/", {"password": "SafePassword123!"}, format="json")

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        refresh_response = self.client.post("/api/auth/token/refresh/", {"refresh": str(refresh)}, format="json")
        self.assertEqual(refresh_response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_authenticated_user_cannot_target_another_users_account(self):
        request = self.factory.delete(
            "/api/auth/delete-account/",
            {"password": "not-the-authenticated-users-password"},
            format="json",
        )
        force_authenticate(request, user=self.other_user)

        response = DeleteAccountView.as_view()(request)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue(User.objects.filter(pk=self.user.pk).exists())
        self.assertTrue(User.objects.filter(pk=self.other_user.pk).exists())
