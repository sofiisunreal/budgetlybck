from collections import defaultdict
from decimal import Decimal

from django.db.models import Sum
from django.utils.dateparse import parse_date

from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.exceptions import ValidationError

from budget.models import (Account,Transaction,Budget,Loan,)

from savings.models import SavingsGoal

class DashboardSummaryView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        month_value = request.query_params.get("month")

        if month_value:
            month = parse_date(f"{month_value}-01")

            if not month:
                raise ValidationError({
                    "month": "Use YYYY-MM."
                })
        else:
            month = __import__("datetime").date.today().replace(day=1)

        next_month = (
            month.replace(day=28)
            + __import__("datetime").timedelta(days=4)
        ).replace(day=1)

        transactions = Transaction.objects.filter(
            user=request.user,
            transaction_date__gte=month,
            transaction_date__lt=next_month,
        )

        # -------------------------
        # Account balances
        # -------------------------

        accounts = Account.objects.filter(
            user=request.user,
            is_active=True
        )

        balances = defaultdict(lambda: Decimal("0.00"))

        for account in accounts:
            balances[account.currency] += account.current_balance

        # -------------------------
        # Monthly income/expenses
        # -------------------------

        income = defaultdict(lambda: Decimal("0.00"))
        expenses = defaultdict(lambda: Decimal("0.00"))

        transaction_data = (
            transactions
            .values("account__currency", "transaction_type")
            .annotate(total=Sum("amount"))
        )

        for item in transaction_data:
            currency = item["account__currency"]
            total = item["total"] or Decimal("0.00")

            if item["transaction_type"] == "income":
                income[currency] += total
            else:
                expenses[currency] += total

        # -------------------------
        # Cash flow
        # -------------------------

        cash_flow = defaultdict(lambda: Decimal("0.00"))

        for currency in set(income) | set(expenses):
            cash_flow[currency] = (
                income[currency] - expenses[currency]
            )

        # -------------------------
        # Budgets
        # -------------------------

        active_budgets = Budget.objects.filter(
            user=request.user,
            start_date__lte=next_month - __import__("datetime").timedelta(days=1),
            end_date__gte=month,
        ).count()

        # -------------------------
        # Loans
        # -------------------------

        active_loans = Loan.objects.filter(
            user=request.user,
            status__in=["active", "partially_paid"]
        )

        loan_counts = active_loans.count()

        loan_outstanding = defaultdict(lambda: Decimal("0.00"))

        for loan in active_loans:
            loan_outstanding[loan.currency] += loan.remaining_amount

        # -------------------------
        # Savings
        # -------------------------

        savings_goals = SavingsGoal.objects.filter(
            user=request.user,
            status__in=["active", "target_reached"]
        )

        savings_count = savings_goals.count()

        savings_totals = defaultdict(lambda: Decimal("0.00"))

        for goal in savings_goals:
            savings_totals[goal.currency] += goal.current_saved

        # -------------------------
        # Recent transactions
        # -------------------------

        recent_transactions = Transaction.objects.filter(
            user=request.user
        ).select_related(
            "account",
            "category"
        ).order_by(
            "-transaction_date",
            "-created_at"
        )[:5]

        recent_data = [
            {
                "id": transaction.id,
                "description": transaction.description,
                "amount": transaction.amount,
                "type": transaction.transaction_type,
                "currency": transaction.account.currency
                    if transaction.account
                    else None,
                "category": transaction.category.name,
                "date": transaction.transaction_date,
            }
            for transaction in recent_transactions
        ]

        return Response({
            "month": month.strftime("%Y-%m"),

            "balances": dict(balances),

            "income": dict(income),

            "expenses": dict(expenses),

            "cash_flow": dict(cash_flow),

            "accounts": {
                "total": accounts.count(),
            },

            "budgets": {
                "active": active_budgets,
            },

            "loans": {
                "active": loan_counts,
                "outstanding": dict(loan_outstanding),
            },

            "savings": {
                "active_goals": savings_count,
                "saved": dict(savings_totals),
            },

            "recent_transactions": recent_data,
        })
