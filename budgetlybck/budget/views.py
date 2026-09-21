from calendar import monthrange
from datetime import date
from decimal import Decimal

from django.db import models
from django.db.models import Sum
from django.utils.dateparse import parse_date
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import Account, Budget, Category, Loan, LoanRepayment, Transaction, Transfer
from .serializers import AccountSerializer, BudgetSerializer, CategorySerializer, LoanRepaymentSerializer, LoanSerializer, TransactionSerializer, TransferSerializer


class CategoryViewSet(viewsets.ModelViewSet):
    serializer_class = CategorySerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return Category.objects.filter(models.Q(user__isnull=True) | models.Q(user=self.request.user))

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)

    def perform_update(self, serializer):
        if self.get_object().user is None:
            raise PermissionDenied('Default categories cannot be modified.')
        serializer.save()

    def perform_destroy(self, instance):
        if instance.user is None:
            raise PermissionDenied('Default categories cannot be deleted.')
        instance.delete()


class UserOwnedModelViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return self.queryset.filter(user=self.request.user)

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)


class AccountViewSet(UserOwnedModelViewSet):
    serializer_class = AccountSerializer
    queryset = Account.objects.all()

    def perform_destroy(self, instance):
        instance.is_active = False
        instance.save(update_fields=['is_active'])

    @action(detail=True, methods=['post'])
    def archive(self, request, pk=None):
        account = self.get_object()
        account.is_active = False
        account.save(update_fields=['is_active'])
        return Response(self.get_serializer(account).data)


class TransactionViewSet(UserOwnedModelViewSet):
    serializer_class = TransactionSerializer
    queryset = Transaction.objects.select_related('account', 'category')

    def get_queryset(self):
        queryset = super().get_queryset()
        transaction_type = self.request.query_params.get('type')
        month = self.request.query_params.get('month')
        account_id = self.request.query_params.get('account')
        if transaction_type:
            if transaction_type not in dict(Transaction.TRANSACTION_TYPES):
                raise ValidationError({'type': 'Use "income" or "expense".'})
            queryset = queryset.filter(transaction_type=transaction_type)
        if month:
            parsed_month = parse_date(f'{month}-01')
            if not parsed_month:
                raise ValidationError({'month': 'Use YYYY-MM.'})
            queryset = queryset.filter(transaction_date__year=parsed_month.year, transaction_date__month=parsed_month.month)
        if account_id:
            queryset = queryset.filter(account_id=account_id)
        return queryset

    @action(detail=False, methods=['get'])
    def summary(self, request):
        month_value = request.query_params.get('month')
        month = parse_date(f'{month_value}-01') if month_value else date.today().replace(day=1)
        if not month:
            raise ValidationError({'month': 'Use YYYY-MM.'})
        last_day = monthrange(month.year, month.month)[1]
        transactions = self.get_queryset().filter(transaction_date__range=(month, month.replace(day=last_day)))
        totals = {item['transaction_type']: item['total'] for item in transactions.values('transaction_type').annotate(total=Sum('amount'))}
        income = totals.get('income', Decimal('0.00'))
        expenses = totals.get('expense', Decimal('0.00'))
        return Response({'month': month.strftime('%Y-%m'), 'income': income, 'expenses': expenses, 'balance': income - expenses})


class TransferViewSet(UserOwnedModelViewSet):
    serializer_class = TransferSerializer
    queryset = Transfer.objects.select_related('from_account', 'to_account')


class LoanViewSet(UserOwnedModelViewSet):
    serializer_class = LoanSerializer
    queryset = Loan.objects.select_related('account').prefetch_related('repayments__account')

    def perform_update(self, serializer):
        loan = serializer.save()
        loan.refresh_status()


class LoanRepaymentViewSet(viewsets.ModelViewSet):
    serializer_class = LoanRepaymentSerializer
    permission_classes = [IsAuthenticated]
    queryset = LoanRepayment.objects.select_related('loan', 'account')

    def get_queryset(self):
        return self.queryset.filter(loan__user=self.request.user)

    def perform_create(self, serializer):
        repayment = serializer.save()
        repayment.loan.refresh_status()

    def perform_update(self, serializer):
        previous_loan = serializer.instance.loan
        repayment = serializer.save()
        if previous_loan != repayment.loan:
            previous_loan.refresh_status()
        repayment.loan.refresh_status()

    def perform_destroy(self, instance):
        loan = instance.loan
        instance.delete()
        loan.refresh_status()


class BudgetViewSet(UserOwnedModelViewSet):
    serializer_class = BudgetSerializer
    queryset = Budget.objects.select_related('category')
