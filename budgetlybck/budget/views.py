from calendar import monthrange
from datetime import date
from decimal import Decimal

from django.db import models
from django.db.models import Sum
from django.utils.dateparse import parse_date
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import Account, Budget, Category, Loan, LoanRepayment, Transaction, Transfer
from .serializers import AccountSerializer, BudgetSerializer, CategorySerializer, LoanRepaymentSerializer, LoanSerializer, TransactionSerializer, TransferSerializer
from billing.permissions import HasLifetimeAccess


class CategoryViewSet(viewsets.ModelViewSet):
    serializer_class = CategorySerializer
    permission_classes = [IsAuthenticated, HasLifetimeAccess]

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
    permission_classes = [IsAuthenticated, HasLifetimeAccess]

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
        with transaction.atomic():
            loan = Loan.objects.select_for_update().get(pk=serializer.instance.pk, user=self.request.user)
            serializer.instance = loan
            serializer.save()
            from .services.loan_interest import recalculate_and_persist
            recalculate_and_persist(loan)


class LoanRepaymentViewSet(viewsets.ModelViewSet):
    serializer_class = LoanRepaymentSerializer
    permission_classes = [IsAuthenticated, HasLifetimeAccess]
    queryset = LoanRepayment.objects.select_related('loan', 'account')

    def get_queryset(self):
        return self.queryset.filter(loan__user=self.request.user)

    def perform_create(self, serializer):
        with transaction.atomic():
            loan = Loan.objects.select_for_update().get(pk=serializer.validated_data['loan'].pk, user=self.request.user)
            repayments = list(LoanRepayment.objects.select_for_update().filter(loan=loan))
            repayment = serializer.save(loan=loan)
            from .services.loan_interest import recalculate_and_persist
            try:
                recalculate_and_persist(loan, repayments + [repayment])
            except ValueError as error:
                raise ValidationError({'amount': str(error)})

    def perform_update(self, serializer):
        with transaction.atomic():
            previous_loan = serializer.instance.loan
            affected_ids = {previous_loan.pk, serializer.validated_data.get('loan', previous_loan).pk}
            loans = {loan.pk: loan for loan in Loan.objects.select_for_update().filter(pk__in=affected_ids, user=self.request.user)}
            for loan in loans.values():
                list(LoanRepayment.objects.select_for_update().filter(loan=loan))
            repayment = serializer.save()
            from .services.loan_interest import recalculate_and_persist
            try:
                for loan in loans.values(): recalculate_and_persist(loan)
            except ValueError as error:
                raise ValidationError({'amount': str(error)})

    def perform_destroy(self, instance):
        with transaction.atomic():
            loan = Loan.objects.select_for_update().get(pk=instance.loan_id, user=self.request.user)
            list(LoanRepayment.objects.select_for_update().filter(loan=loan))
            instance.delete()
            from .services.loan_interest import recalculate_and_persist
            try:
                recalculate_and_persist(loan)
            except ValueError as error:
                raise ValidationError({'amount': str(error)})


class BudgetViewSet(UserOwnedModelViewSet):
    serializer_class = BudgetSerializer
    queryset = Budget.objects.select_related('category')
