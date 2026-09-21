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
from django.core.mail import send_mail
from django.utils.http import urlsafe_base64_decode

User = get_user_model()

class RegisterView(generics.CreateAPIView):
    serializer_class = RegisterSerializer
    permission_classes = [AllowAny]


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

        try:
            user = User.objects.get(email=email)

        except User.DoesNotExist:
            return Response(
                {'error': 'No account found with this email address.'},
                status=404
            )

        uid = urlsafe_base64_encode(force_bytes(user.pk))
        token = default_token_generator.make_token(user)
        reset_link = (
            f'http://localhost:5173/reset-password/{uid}/{token}/'
        )
        send_mail(
            subject='Reset your Budgetly password',
            message=(
                f'Hello {user.first_name or user.username},\n\n'
                f'Click the link below to reset your password:\n\n'
                f'{reset_link}\n\n'
                f'If you did not request this, you can ignore this email.'
            ),
            from_email=None,
            recipient_list=[user.email],
        )

        return Response(
            {
                'message': 'Password reset instructions have been sent.'
            },
            status=200
        )

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

        user.delete()

        return Response(
            {'message': 'Account deleted successfully.'},
            status=204
        )
