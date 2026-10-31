"""Deterministic simple-interest schedules for Budgetly loans."""
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from django.utils import timezone


MONEY = Decimal('0.01')
ZERO = Decimal('0')
DAYS_IN_YEAR = Decimal('365')


def post(value):
    """Round only monetary postings; accrual itself retains Decimal precision."""
    return value.quantize(MONEY, rounding=ROUND_HALF_UP)


def _interest(principal, annual_rate, days):
    return principal * annual_rate / Decimal('100') * Decimal(days) / DAYS_IN_YEAR


def _days_to_first_event(loan_date, event_date):
    # Loan-date interest is excluded; repayment date is excluded by the caller.
    return max((event_date - loan_date).days - 1, 0)


def build_schedule(loan, repayments, as_of_date=None):
    """Return allocations and a snapshot; callers persist allocations atomically."""
    as_of_date = as_of_date or timezone.localdate()
    ordered = sorted(repayments, key=lambda item: (item.repayment_date, item.created_at or timezone.now(), item.pk or 0))
    principal = Decimal(loan.original_amount)
    accrued = ZERO
    last_date = loan.loan_date
    first_event = True
    allocations = {}
    for repayment in ordered:
        if repayment.repayment_date < loan.loan_date:
            raise ValueError('Repayment date cannot be before the loan date.')
        days = _days_to_first_event(last_date, repayment.repayment_date) if first_event else max((repayment.repayment_date - last_date).days, 0)
        accrued += _interest(principal, Decimal(loan.annual_interest_rate), days)
        due_interest = post(accrued)
        total_due = principal + due_interest
        if Decimal(repayment.amount) > total_due:
            raise ValueError('Repayment cannot exceed the outstanding loan obligation.')
        interest_amount = min(Decimal(repayment.amount), due_interest)
        principal_amount = Decimal(repayment.amount) - interest_amount
        principal -= principal_amount
        accrued -= interest_amount
        # A sub-cent residual is resolved at the posting boundary on settlement.
        if principal == ZERO and post(accrued) == ZERO:
            accrued = ZERO
        allocations[repayment.pk] = {'principal_amount': post(principal_amount), 'interest_amount': post(interest_amount)}
        last_date = repayment.repayment_date
        first_event = False
    if principal > ZERO and as_of_date > last_date:
        days = _days_to_first_event(last_date, as_of_date) if first_event else (as_of_date - last_date).days
        accrued += _interest(principal, Decimal(loan.annual_interest_rate), days)
    return allocations, {
        'outstanding_principal': post(principal),
        'accrued_interest': post(max(accrued, ZERO)),
        'total_outstanding': post(principal + max(accrued, ZERO)),
        'interest_as_of_date': as_of_date,
    }


def loan_snapshot(loan, as_of_date=None):
    return build_schedule(loan, list(loan.repayments.all()), as_of_date)[1]


def recalculate_and_persist(loan, repayments=None):
    repayments = list(repayments if repayments is not None else loan.repayments.all())
    allocations, snapshot = build_schedule(loan, repayments)
    for repayment in repayments:
        allocation = allocations[repayment.pk]
        if repayment.principal_amount != allocation['principal_amount'] or repayment.interest_amount != allocation['interest_amount']:
            repayment.principal_amount = allocation['principal_amount']
            repayment.interest_amount = allocation['interest_amount']
            repayment.save(update_fields=['principal_amount', 'interest_amount'])
    loan.status = 'settled' if snapshot['total_outstanding'] == ZERO else ('partially_paid' if snapshot['outstanding_principal'] < loan.original_amount else 'active')
    loan.save(update_fields=['status'])
    return snapshot
