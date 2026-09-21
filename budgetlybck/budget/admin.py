from django.contrib import admin

from .models import Account, Budget, Category, Loan, LoanRepayment, Transaction, Transfer


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ('name', 'category_type', 'user')
    list_filter = ('category_type',)
    search_fields = ('name', 'user__username')


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = ('description', 'user', 'account', 'category', 'transaction_type', 'amount', 'transaction_date')
    list_filter = ('transaction_type', 'transaction_date')
    search_fields = ('title', 'user__username')


@admin.register(Budget)
class BudgetAdmin(admin.ModelAdmin):
    list_display = ('user', 'category', 'amount', 'period_type', 'start_date', 'end_date')
    list_filter = ('period_type', 'start_date')


@admin.register(Account)
class AccountAdmin(admin.ModelAdmin):
    list_display = ('name', 'user', 'account_type', 'currency', 'starting_balance', 'is_active')
    list_filter = ('account_type', 'currency', 'is_active')


@admin.register(Transfer)
class TransferAdmin(admin.ModelAdmin):
    list_display = ('user', 'from_account', 'to_account', 'from_amount', 'to_amount', 'transaction_date')


@admin.register(Loan)
class LoanAdmin(admin.ModelAdmin):
    list_display = ('person_name', 'user', 'loan_type', 'original_amount', 'account', 'status')
    list_filter = ('loan_type', 'status', 'currency')


@admin.register(LoanRepayment)
class LoanRepaymentAdmin(admin.ModelAdmin):
    list_display = ('loan', 'account', 'amount', 'repayment_date')
