"""Transactional, user-scoped account deletion."""

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Q
from django.db.models.deletion import ProtectedError
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken, OutstandingToken


def _raise_for_cross_user_dependencies(user):
    """Never delete another user's record to satisfy this user's request."""
    from budget.models import Budget, Loan, LoanRepayment, Transaction, Transfer
    from savings.models import SavingsGoal

    protected_queries = (
        SavingsGoal.objects.exclude(user=user).filter(account__user=user),
        Transaction.objects.exclude(user=user).filter(Q(account__user=user) | Q(category__user=user)),
        Budget.objects.exclude(user=user).filter(category__user=user),
        Transfer.objects.exclude(user=user).filter(Q(from_account__user=user) | Q(to_account__user=user)),
        Loan.objects.exclude(user=user).filter(account__user=user),
        LoanRepayment.objects.exclude(loan__user=user).filter(account__user=user),
    )
    if any(query.exists() for query in protected_queries):
        raise ProtectedError('User-owned records are referenced by another user.', [])


def delete_user_financial_records(user):
    """Remove only the requesting user's financial graph in FK-safe order."""
    from budget.models import Account, Budget, Category, Loan, LoanRepayment, Transaction, Transfer
    from feedback.models import Feedback
    from savings.models import SavingsContribution, SavingsGoal, SavingsPlan, SavingsWithdrawal

    # Serialize concurrent account writes before removing their protected
    # dependants. MySQL will hold these row locks until the outer transaction.
    list(Account.objects.select_for_update().filter(user=user).only('pk'))
    _raise_for_cross_user_dependencies(user)

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
    # Global/default categories have user=None and are deliberately excluded.
    Category.objects.filter(user=user).delete()
    Account.objects.filter(user=user).delete()


def _blacklist_user_refresh_tokens(user):
    """Invalidate every known refresh token before the user row disappears."""
    for token in OutstandingToken.objects.select_for_update().filter(user=user):
        BlacklistedToken.objects.get_or_create(token=token)


def delete_user_account(user_id):
    """Delete a user and owned records atomically after dependency checks."""
    User = get_user_model()
    with transaction.atomic():
        user = User.objects.select_for_update().get(pk=user_id)
        delete_user_financial_records(user)
        _blacklist_user_refresh_tokens(user)
        # Policy acceptances and entitlements cascade. Billing attempts use
        # SET_NULL and remain as non-personal payment reconciliation evidence.
        user.delete()
