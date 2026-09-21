from decimal import Decimal

from django.conf import settings
from django.db import models
from django.db.models import Sum
from django.core.validators import MinValueValidator


class Category(models.Model):

    CATEGORY_TYPES = [
        ('income', 'Income'),
        ('expense', 'Expense'),
    ]

    name = models.CharField(max_length=100)

    category_type = models.CharField(
        max_length=10,
        choices=CATEGORY_TYPES,
    )

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='categories'
    )

    def __str__(self):
        return self.name


class Account(models.Model):
    ACCOUNT_TYPES = [
        ('mobile_money', 'Mobile money'),
        ('bank', 'Bank'),
        ('cash', 'Cash'),
        ('other', 'Other'),
    ]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='accounts')
    name = models.CharField(max_length=100)
    account_type = models.CharField(max_length=20, choices=ACCOUNT_TYPES)
    currency = models.CharField(max_length=3)
    starting_balance = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [('user', 'name')]
        ordering = ['name']

    @property
    def current_balance(self):
        zero = Decimal('0.00')
        transaction_totals = self.transactions.values('transaction_type').annotate(total=Sum('amount'))
        totals = {row['transaction_type']: row['total'] for row in transaction_totals}
        incoming_transfers = self.incoming_transfers.aggregate(total=Sum('to_amount'))['total'] or zero
        outgoing_transfers = self.outgoing_transfers.aggregate(total=Sum('from_amount'))['total'] or zero
        loan_inflow = self.loans.filter(loan_type='borrowed').aggregate(total=Sum('original_amount'))['total'] or zero
        loan_outflow = self.loans.filter(loan_type='lent').aggregate(total=Sum('original_amount'))['total'] or zero
        repayments = LoanRepayment.objects.filter(account=self).values('loan__loan_type').annotate(total=Sum('amount'))
        repayment_totals = {row['loan__loan_type']: row['total'] for row in repayments}
        return (
            self.starting_balance
            + (totals.get('income') or zero)
            - (totals.get('expense') or zero)
            + incoming_transfers
            - outgoing_transfers
            + loan_inflow
            - loan_outflow
            + (repayment_totals.get('lent') or zero)
            - (repayment_totals.get('borrowed') or zero)
        )

    def __str__(self):
        return f'{self.name} ({self.currency})'


class Transaction(models.Model):
    TRANSACTION_TYPES = Category.CATEGORY_TYPES

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='transactions',
    )
    # Nullable only to preserve pre-account records during the migration. New API
    # requests must supply an account through TransactionSerializer.
    account = models.ForeignKey(
        Account,
        on_delete=models.PROTECT,
        related_name='transactions',
        null=True,
        blank=True,
    )
    category = models.ForeignKey(
        Category,
        on_delete=models.PROTECT,
        related_name='transactions',
    )
    transaction_type = models.CharField(max_length=10, choices=TRANSACTION_TYPES)
    description = models.CharField(max_length=150)
    amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal('0.01'))],
    )
    transaction_date = models.DateField()
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-transaction_date', '-created_at']

    def __str__(self):
        return f'{self.description} ({self.amount})'


class Budget(models.Model):
    PERIOD_TYPES = [
        ('weekly', 'Weekly'),
        ('monthly', 'Monthly'),
        ('custom', 'Custom'),
    ]
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='budgets',
    )
    category = models.ForeignKey(
        Category,
        on_delete=models.PROTECT,
        related_name='budgets',
    )
    amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal('0.01'))],
    )
    period_type = models.CharField(max_length=10, choices=PERIOD_TYPES, default='monthly')
    start_date = models.DateField()
    end_date = models.DateField()
    rollover = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [('user', 'category', 'start_date')]
        ordering = ['-start_date', 'category__name']

    def __str__(self):
        return f'{self.category} budget from {self.start_date}'


class Transfer(models.Model):
    RATE_SOURCES = [('live', 'Live'), ('custom', 'Custom')]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='transfers')
    from_account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name='outgoing_transfers')
    to_account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name='incoming_transfers')
    from_amount = models.DecimalField(max_digits=14, decimal_places=2, validators=[MinValueValidator(Decimal('0.01'))])
    to_amount = models.DecimalField(max_digits=14, decimal_places=2, validators=[MinValueValidator(Decimal('0.01'))])
    exchange_rate = models.DecimalField(max_digits=18, decimal_places=8, validators=[MinValueValidator(Decimal('0.00000001'))])
    rate_source = models.CharField(max_length=10, choices=RATE_SOURCES, default='custom')
    transaction_date = models.DateField()
    description = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-transaction_date', '-created_at']


class Loan(models.Model):
    LOAN_TYPES = [('borrowed', 'Borrowed'), ('lent', 'Lent')]
    STATUSES = [('active', 'Active'), ('partially_paid', 'Partially paid'), ('settled', 'Settled')]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='loans')
    loan_type = models.CharField(max_length=10, choices=LOAN_TYPES)
    person_name = models.CharField(max_length=150)
    original_amount = models.DecimalField(max_digits=14, decimal_places=2, validators=[MinValueValidator(Decimal('0.01'))])
    currency = models.CharField(max_length=3)
    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name='loans')
    loan_date = models.DateField()
    due_date = models.DateField(null=True, blank=True)
    description = models.TextField(blank=True)
    status = models.CharField(max_length=15, choices=STATUSES, default='active')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-loan_date', '-created_at']

    @property
    def remaining_amount(self):
        repaid = self.repayments.aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
        return self.original_amount - repaid

    def refresh_status(self):
        remaining = self.remaining_amount
        self.status = 'settled' if remaining == 0 else ('partially_paid' if remaining < self.original_amount else 'active')
        self.save(update_fields=['status'])


class LoanRepayment(models.Model):
    loan = models.ForeignKey(Loan, on_delete=models.CASCADE, related_name='repayments')
    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name='loan_repayments')
    amount = models.DecimalField(max_digits=14, decimal_places=2, validators=[MinValueValidator(Decimal('0.01'))])
    repayment_date = models.DateField()
    note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-repayment_date', '-created_at']
