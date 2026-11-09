from rest_framework import generics
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework import status
from rest_framework.views import APIView
from rest_framework.throttling import AnonRateThrottle, UserRateThrottle

from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken

from .serializers import (ChangePasswordSerializer, DeleteAccountSerializer,
                          ForgotPasswordSerializer, PolicyAcceptanceSerializer,
                          PolicyDocumentSerializer, RegisterSerializer,
                          ResetPasswordSerializer, UserSerializer,
                          GoogleAuthenticationSerializer,
                          validate_active_document_identities)
from .models import PolicyAcceptance, PolicyDocument
from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils.http import urlsafe_base64_decode
from django.conf import settings
from django.db import transaction
from django.db.models.deletion import ProtectedError
from django.core.exceptions import ValidationError
import logging

from .services.account_deletion import delete_user_account
from .services.google_auth import (GoogleConfigurationError, GoogleCredentialError,
                                   authenticate_or_register_google, issue_tokens,
                                   link_google_identity, verify_google_credential)

logger = logging.getLogger(__name__)

User = get_user_model()


class GoogleAuthenticationThrottle(AnonRateThrottle):
    scope = 'google_auth'


class GoogleLinkThrottle(UserRateThrottle):
    scope = 'google_link'


def active_policy_documents_response():
    """Return active documents or a safe, explicit unavailable response."""
    try:
        documents = PolicyDocument.active_documents()
    except ValidationError:
        documents = None
    if documents is None:
        return None
    return documents


def current_policy_acceptances(user, documents):
    acceptances = PolicyAcceptance.objects.filter(
        user=user, document__in=documents.values()
    ).select_related('document')
    return {
        acceptance.document.document_type: {
            'document': PolicyDocumentSerializer(acceptance.document).data,
            'accepted_at': acceptance.accepted_at,
        }
        for acceptance in acceptances
    }


def send_welcome_email(user):
    """Best-effort post-commit email; registration must not depend on SMTP."""
    try:
        context = {'first_name': user.first_name or user.username, 'app_url': settings.FRONTEND_APP_URL.rstrip('/')}
        message = EmailMultiAlternatives(
            subject="Welcome to Budgetly. Let's make money simpler.",
            body=render_to_string('accounts/emails/welcome.txt', context),
            from_email=None,
            to=[user.email],
        )
        message.attach_alternative(render_to_string('accounts/emails/welcome.html', context), 'text/html')
        message.send(fail_silently=False)
    except Exception:
        logger.exception('Welcome email delivery failed')

class RegisterView(generics.CreateAPIView):
    serializer_class = RegisterSerializer
    permission_classes = [AllowAny]

    def create(self, request, *args, **kwargs):
        if active_policy_documents_response() is None:
            return Response(
                {'detail': 'Legal documents are not currently available for registration.', 'code': 'legal_documents_unavailable'},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return super().create(request, *args, **kwargs)

    def perform_create(self, serializer):
        user = serializer.save()
        transaction.on_commit(lambda: send_welcome_email(user))


class GoogleAuthenticationView(APIView):
    """Authenticate a linked Google identity or create a legally-consented user."""

    permission_classes = [AllowAny]
    throttle_classes = [GoogleAuthenticationThrottle]

    def post(self, request):
        serializer = GoogleAuthenticationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            claims = verify_google_credential(serializer.validated_data['credential'])
            user, created = authenticate_or_register_google(claims, serializer.legal_identities())
        except GoogleConfigurationError:
            return Response({'detail': 'Google sign-in is not configured.', 'code': 'google_auth_unavailable'}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        except GoogleCredentialError as error:
            messages = {
                'link_required': 'Sign in to your existing Budgetly account before linking Google.',
                'legal_acceptance_required': 'Review and accept the current legal documents before creating an account.',
                'identity_in_use': 'This Google identity is already linked to another account.',
                'inactive_user': 'This account is inactive.',
            }
            return Response(
                {'detail': messages.get(error.code, 'Google credential could not be verified.'), 'code': error.code},
                status=(status.HTTP_409_CONFLICT if error.code in {'link_required', 'identity_in_use'}
                        else status.HTTP_403_FORBIDDEN if error.code == 'inactive_user'
                        else status.HTTP_400_BAD_REQUEST),
            )
        if created:
            transaction.on_commit(lambda: send_welcome_email(user))
        return Response(issue_tokens(user), status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


class GoogleIdentityLinkView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [GoogleLinkThrottle]

    def post(self, request):
        serializer = GoogleAuthenticationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            identity, created = link_google_identity(
                request.user,
                verify_google_credential(serializer.validated_data['credential']),
            )
        except GoogleConfigurationError:
            return Response({'detail': 'Google sign-in is not configured.', 'code': 'google_auth_unavailable'}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        except GoogleCredentialError as error:
            messages = {
                'identity_in_use': 'This Google identity is already linked to another account.',
                'user_already_linked': 'Your Budgetly account already has a Google identity linked.',
                'inactive_user': 'This account is inactive.',
            }
            return Response(
                {'detail': messages.get(error.code, 'Google credential could not be verified.'), 'code': error.code},
                status=(status.HTTP_409_CONFLICT if error.code in {'identity_in_use', 'user_already_linked'}
                        else status.HTTP_403_FORBIDDEN if error.code == 'inactive_user'
                        else status.HTTP_400_BAD_REQUEST),
            )
        return Response({'linked': created}, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


class ActivePolicyDocumentsView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        documents = active_policy_documents_response()
        if documents is None:
            return Response(
                {'detail': 'Legal documents are not currently available.', 'code': 'legal_documents_unavailable'},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return Response({
            'terms': PolicyDocumentSerializer(documents[PolicyDocument.TERMS]).data,
            'privacy': PolicyDocumentSerializer(documents[PolicyDocument.PRIVACY]).data,
        })


class LegalAcceptanceStatusView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        documents = active_policy_documents_response()
        if documents is None:
            return Response({
                'documents_available': False,
                'acceptance_required': False,
                'reason': 'legal_documents_unavailable',
                'documents': None,
                'acceptances': {},
            })

        acceptances = current_policy_acceptances(request.user, documents)
        active_ids = {document.id for document in documents.values()}
        return Response({
            'documents_available': True,
            'acceptance_required': set(
                acceptance['document']['id'] for acceptance in acceptances.values()
            ) != active_ids,
            'documents': {
                'terms': PolicyDocumentSerializer(documents[PolicyDocument.TERMS]).data,
                'privacy': PolicyDocumentSerializer(documents[PolicyDocument.PRIVACY]).data,
            },
            'acceptances': acceptances,
        })


class PolicyAcceptanceView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = PolicyAcceptanceSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        with transaction.atomic():
            user = User.objects.select_for_update().get(pk=request.user.pk)
            documents = validate_active_document_identities(
                serializer.validated_data['terms_document'],
                serializer.validated_data['privacy_document'],
            )
            created = False
            for document in documents.values():
                _, was_created = PolicyAcceptance.objects.get_or_create(user=user, document=document)
                created = created or was_created

        return Response(
            {
                'acceptance_required': False,
                'documents': {
                    'terms': PolicyDocumentSerializer(documents[PolicyDocument.TERMS]).data,
                    'privacy': PolicyDocumentSerializer(documents[PolicyDocument.PRIVACY]).data,
                },
                'acceptances': current_policy_acceptances(user, documents),
            },
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class ProfileView(generics.RetrieveUpdateAPIView):
    serializer_class = UserSerializer
    permission_classes = [IsAuthenticated]

    def get_object(self):
        return self.request.user


class LogoutView(generics.GenericAPIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        refresh_token = request.data.get('refresh')

        if not refresh_token:
            return Response(
                {'error': 'Refresh token is required.'},
                status=400
            )

        try:
            token = RefreshToken(refresh_token)
            token.blacklist()

            return Response(
                {'message': 'Successfully logged out.'},
                status=200
            )

        except TokenError:
            return Response(
                {'error': 'Invalid or expired refresh token.'},
                status=400
            )

class ChangePasswordView(generics.GenericAPIView):
    serializer_class = ChangePasswordSerializer
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = request.user
        if not user.check_password(
            serializer.validated_data['old_password']
        ):
            return Response(
                {'old_password': 'Current password is incorrect.'},
                status=400
            )
        user.set_password(
            serializer.validated_data['new_password']
        )
        user.save()
        return Response(
            {'message': 'Password changed successfully.'},
            status=200
        )


class ForgotPasswordView(generics.GenericAPIView):
    serializer_class = ForgotPasswordSerializer
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        email = serializer.validated_data['email']
        generic_response = {
            'message': 'If an account exists for this email, password reset instructions have been sent.'
        }

        try:
            user = User.objects.get(email=email)
        except User.DoesNotExist:
            return Response(generic_response, status=200)

        uid = urlsafe_base64_encode(force_bytes(user.pk))
        token = default_token_generator.make_token(user)

        reset_link = (
            f'{settings.FRONTEND_PASSWORD_RESET_URL.rstrip("/")}/'
            f'{uid}/{token}/'
        )

        try:
            context = {
                'first_name': user.first_name or user.username,
                'reset_link': reset_link,
            }
            message = EmailMultiAlternatives(
                subject='Reset your Budgetly password',
                body=render_to_string('accounts/emails/password_reset.txt', context),
                from_email=None,
                to=[user.email],
            )
            message.attach_alternative(
                render_to_string('accounts/emails/password_reset.html', context),
                'text/html',
            )
            message.send(fail_silently=False)

        except Exception:
            logger.exception("Password reset email delivery failed")
            return Response(
                {
                    'message': 'Unable to process the password reset request right now.'
                },
                status=503,
            )

        return Response(generic_response, status=200)



class ResetPasswordView(generics.GenericAPIView):
    serializer_class = ResetPasswordSerializer
    permission_classes = [AllowAny]

    def post(self, request, uid, token):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            uid = urlsafe_base64_decode(uid).decode()
            user = User.objects.get(pk=uid)
        except (TypeError, ValueError, OverflowError, User.DoesNotExist):
            return Response(
                {'error': 'Invalid password reset link.'},
                status=400
            )

        if not default_token_generator.check_token(user, token):
            return Response(
                {'error': 'Invalid or expired password reset link.'},
                status=400
            )

        user.set_password(
            serializer.validated_data['new_password']
        )
        user.save()

        return Response(
            {'message': 'Password has been reset successfully.'},
            status=200
        )

class DeleteAccountView(generics.GenericAPIView):
    serializer_class = DeleteAccountSerializer
    permission_classes = [IsAuthenticated]

    def delete(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        user = request.user

        if not user.check_password(
            serializer.validated_data['password']
        ):
            return Response(
                {'password': 'Current password is incorrect.'},
                status=400
            )

        try:
            delete_user_account(request.user.pk)
        except ProtectedError:
            return Response(
                {'detail': 'Account deletion could not be completed because protected records remain.'},
                status=409,
            )
        except Exception:
            logger.exception('Account deletion failed')
            return Response(
                {'detail': 'Account deletion could not be completed. No records were deleted.'},
                status=500,
            )

        return Response(
            {'message': 'Account deleted successfully.'},
            status=204
        )
