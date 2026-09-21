from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import AccountViewSet, BudgetViewSet, CategoryViewSet, LoanRepaymentViewSet, LoanViewSet, TransactionViewSet, TransferViewSet


router = DefaultRouter()
router.register('categories', CategoryViewSet, basename='category')
router.register('accounts', AccountViewSet, basename='account')
router.register('transactions', TransactionViewSet, basename='transaction')
router.register('transfers', TransferViewSet, basename='transfer')
router.register('loans', LoanViewSet, basename='loan')
router.register('loan-repayments', LoanRepaymentViewSet, basename='loan-repayment')
router.register('budgets', BudgetViewSet, basename='budget')


urlpatterns = [
    path('', include(router.urls)),
]
