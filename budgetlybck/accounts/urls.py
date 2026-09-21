from django.urls import path

from .views import RegisterView, ProfileView, LogoutView, ChangePasswordView, ForgotPasswordView, ResetPasswordView, DeleteAccountView

from rest_framework_simplejwt.views import (
    TokenObtainPairView,
    TokenRefreshView,
)


urlpatterns = [
    path('register/', RegisterView.as_view(), name='register'),
    path('login/', TokenObtainPairView.as_view(), name='login'),
    path('token/refresh/', TokenRefreshView.as_view(), name='token_refresh'),
    path('profile/', ProfileView.as_view(), name='profile'),
    path('logout/', LogoutView.as_view(), name='logout'),
    path('change-password/', ChangePasswordView.as_view(),name='change-password'),
    path('forgot-password/',ForgotPasswordView.as_view(),name='forgot-password'),
    path('reset-password/<uid>/<token>/',ResetPasswordView.as_view(),name='reset-password'),
    path('delete-account/',DeleteAccountView.as_view(),name='delete-account'),
]
