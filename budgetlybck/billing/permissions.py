from rest_framework.permissions import BasePermission

from .models import LifetimeAccessEntitlement


def has_lifetime_access(user):
    return bool(
        user
        and user.is_authenticated
        and LifetimeAccessEntitlement.objects.filter(user=user, status=LifetimeAccessEntitlement.ACTIVE).exists()
    )


class HasLifetimeAccess(BasePermission):
    message = 'Lifetime access is required for this feature.'

    def has_permission(self, request, view):
        return has_lifetime_access(request.user)
