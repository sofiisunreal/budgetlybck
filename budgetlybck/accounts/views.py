from rest_framework import generics
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken

from .serializers import RegisterSerializer, UserSerializer,ChangePasswordSerializer, ForgotPasswordSerializer, ResetPasswordSerializer, DeleteAccountSerializer
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
import logging

logger = logging.getLogger(__name__)

User = get_user_model()


def delete_user_financial_records(user):
    """Delete only this user's dependent records, in FK-safe order."""
    from budget.models import Account, Budget, Category, Loan, LoanRepayment, Transaction, Transfer
    from feedback.models import Feedback
    from savings.models import SavingsContribution, SavingsGoal, SavingsPlan, SavingsWithdrawal

    # Lock the accounts before deleting records that affect their calculated
    # balances. This also makes concurrent writes that reference an account
    # wait for the deletion transaction to finish on databases that support
    # row-level locking.
    list(Account.objects.select_for_update().filter(user=user).only('pk'))

    # A savings goal owned by someone else would otherwise be silently
    # retained with its account set to NULL. Such a cross-user link should be
    # repaired explicitly, not changed as a side effect of self-service
    # deletion.
    foreign_goals = SavingsGoal.objects.exclude(user=user).filter(account__user=user)
    if foreign_goals.exists():
        raise ProtectedError('Accounts are referenced by another user\'s savings goal.', [])

    SavingsPlan.objects.filter(goal__user=user).delete()
    SavingsWithdrawal.objects.filter(goal__user=user).delete()
    SavingsContribution.objects.filter(goal__user=user).delete()
    SavingsGoal.objects.filter(user=user).delete()

    LoanRepayment.objects.filter(loan__user=user).delete()
    Loan.objects.filter(user=user).delete()
    Transfer.objects.filter(user=user).delete()
    Transaction.objects.filter(user=user).delete()
    Budget.objects.filter(user=user).delete()
    Feedback.objects.filter(user=user).delete()
    Category.objects.filter(user=user).delete()
    Account.objects.filter(user=user).delete()


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

    def perform_create(self, serializer):
        user = serializer.save()
        transaction.on_commit(lambda: send_welcome_email(user))


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
            with transaction.atomic():
                user = User.objects.select_for_update().get(pk=request.user.pk)
                delete_user_financial_records(user)
                user.delete()
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
