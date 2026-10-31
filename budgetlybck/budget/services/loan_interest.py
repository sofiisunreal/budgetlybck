"""Deterministic simple-interest schedules for Budgetly loans."""
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from calendar import monthrange

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
    if getattr(loan, 'interest_method', 'annual_percentage') not in ('none', 'annual_percentage'):
        return _build_flexible_schedule(loan, repayments, as_of_date)
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


def _add_months(value, count):
    index = value.month - 1 + count
    year, month = value.year + index // 12, index % 12 + 1
    return date(year, month, min(value.day, monthrange(year, month)[1]))


def _charge_dates(loan, end_date):
    method = loan.interest_method
    if method in ('none', 'fixed_total'):
        return []
    if method in ('daily_percentage', 'fixed_daily'):
        current = loan.loan_date + timedelta(days=2)
        step = lambda value: value + timedelta(days=1)
    elif method in ('weekly_percentage', 'fixed_weekly'):
        current = loan.loan_date + timedelta(days=8)
        step = lambda value: value + timedelta(days=7)
    else:
        anniversaries = 1
        current = _add_months(loan.loan_date, anniversaries) + timedelta(days=1)
        def step(value):
            nonlocal anniversaries
            anniversaries += 1
            return _add_months(loan.loan_date, anniversaries) + timedelta(days=1)
    dates = []
    while current <= end_date:
        dates.append(current)
        current = step(current)
    return dates


def _build_flexible_schedule(loan, repayments, as_of_date=None):
    as_of_date = as_of_date or timezone.localdate()
    ordered = sorted(repayments, key=lambda item: (item.repayment_date, item.created_at or timezone.now(), item.pk or 0))
    if any(item.repayment_date < loan.loan_date for item in ordered):
        raise ValueError('Repayment date cannot be before the loan date.')
    end_date = max([as_of_date, *(item.repayment_date for item in ordered)])
    events = [(value, 0, None) for value in _charge_dates(loan, end_date)]
    events += [(item.repayment_date, 1, item) for item in ordered]
    events.sort(key=lambda event: (event[0], event[1], event[2].created_at if event[2] else timezone.now(), event[2].pk if event[2] else 0))
    principal, accrued, allocations = Decimal(loan.original_amount), ZERO, {}
    if loan.interest_method == 'fixed_total':
        accrued = Decimal(loan.fixed_interest_amount or ZERO)
    for _, kind, repayment in events:
        if kind == 0:
            if principal <= ZERO:
                continue
            if loan.interest_method.endswith('percentage'):
                accrued += principal * Decimal(loan.periodic_interest_rate) / Decimal('100')
            else:
                charge = Decimal(loan.fixed_interest_amount)
                if loan.fixed_charge_behavior == 'proportional_to_principal':
                    charge *= principal / Decimal(loan.original_amount)
                accrued += charge
            continue
        due_interest = post(accrued)
        if Decimal(repayment.amount) > principal + due_interest:
            raise ValueError('Repayment cannot exceed the outstanding loan obligation.')
        interest_amount = min(Decimal(repayment.amount), due_interest)
        principal_amount = Decimal(repayment.amount) - interest_amount
        principal -= principal_amount
        accrued -= interest_amount
        if principal == ZERO and post(accrued) == ZERO:
            accrued = ZERO
        allocations[repayment.pk] = {'principal_amount': post(principal_amount), 'interest_amount': post(interest_amount)}
    return allocations, {'outstanding_principal': post(max(principal, ZERO)), 'accrued_interest': post(max(accrued, ZERO)), 'total_outstanding': post(max(principal, ZERO) + max(accrued, ZERO)), 'interest_as_of_date': as_of_date}


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
