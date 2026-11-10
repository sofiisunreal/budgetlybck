from django.conf import settings
from django.db import models
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import BasePermission

from .models import LifetimeAccessEntitlement


def has_lifetime_access(user):
    return bool(
        user
        and user.is_authenticated
        and LifetimeAccessEntitlement.objects.filter(
            user=user,
            status=LifetimeAccessEntitlement.ACTIVE,
        ).filter(
            # Founding-user grants are deliberately independent of Paystack;
            # a null mode on a Paystack entitlement is never accepted.
            models.Q(source=LifetimeAccessEntitlement.LEGACY_GRANT)
            | models.Q(
                source=LifetimeAccessEntitlement.PAYSTACK_PURCHASE,
                provider_mode=settings.PAYSTACK_MODE,
            )
        ).exists()
    )


class HasLifetimeAccess(BasePermission):
    """Gate financial APIs only when the server-side rollout flag is enabled."""

    message = 'Lifetime access is required for financial features.'

    def has_permission(self, request, view):
        if not settings.BILLING_ENFORCE_LIFETIME_ACCESS:
            return True
        if has_lifetime_access(request.user):
            return True
        raise PaymentRequired()


class PaymentRequired(PermissionDenied):
    """Stable, non-sensitive response used by the frontend payment flow."""

    default_detail = {
        'detail': 'Lifetime access is required for financial features.',
        'code': 'payment_required',
    }
    default_code = 'payment_required'
