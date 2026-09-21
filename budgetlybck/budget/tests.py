from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase

from .models import Account, Category, Loan, Transaction


class FinancialApiTests(APITestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username='alice', email='alice@example.com', password='password123')
        self.other_user = User.objects.create_user(username='bob', email='bob@example.com', password='password123')
        self.income = Category.objects.create(name='Salary', category_type='income')
        self.expense = Category.objects.create(name='Food', category_type='expense')
        self.other_category = Category.objects.create(name='Bob food', category_type='expense', user=self.other_user)
        self.account = Account.objects.create(user=self.user, name='Cash', account_type='cash', currency='KES', starting_balance=Decimal('100.00'))
        self.other_account = Account.objects.create(user=self.other_user, name='Bob cash', account_type='cash', currency='KES')
        self.client.force_authenticate(self.user)

    def transaction_payload(self, **overrides):
        payload = {'account': self.account.id, 'category': self.income.id, 'transaction_type': 'income', 'description': 'Payment', 'amount': '25.00', 'transaction_date': '2026-08-23'}
        payload.update(overrides)
        return payload

    def test_users_cannot_access_or_use_other_users_accounts_or_categories(self):
        response = self.client.get('/api/accounts/')
        self.assertEqual([account['id'] for account in response.data], [self.account.id])
        self.assertEqual(self.client.post('/api/transactions/', self.transaction_payload(account=self.other_account.id), format='json').status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.client.post('/api/transactions/', self.transaction_payload(category=self.other_category.id, transaction_type='expense'), format='json').status_code, status.HTTP_400_BAD_REQUEST)

    def test_transaction_balance_recalculates_after_update_and_delete(self):
        income = self.client.post('/api/transactions/', self.transaction_payload(), format='json')
        expense = self.client.post('/api/transactions/', self.transaction_payload(category=self.expense.id, transaction_type='expense', description='Lunch', amount='10.00'), format='json')
        self.assertEqual(income.status_code, status.HTTP_201_CREATED)
        self.assertEqual(expense.status_code, status.HTTP_201_CREATED)
        self.assertEqual(self.account.current_balance, Decimal('115.00'))
        self.assertEqual(self.client.patch(f'/api/transactions/{expense.data["id"]}/', {'amount': '15.00'}, format='json').status_code, status.HTTP_200_OK)
        self.assertEqual(self.account.current_balance, Decimal('110.00'))
        self.assertEqual(self.client.delete(f'/api/transactions/{expense.data["id"]}/').status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(self.account.current_balance, Decimal('125.00'))

    def test_transfers_are_separate_from_income_and_expenses(self):
        destination = Account.objects.create(user=self.user, name='Bank', account_type='bank', currency='KES')
        response = self.client.post('/api/transfers/', {'from_account': self.account.id, 'to_account': destination.id, 'from_amount': '20.00', 'exchange_rate': '1', 'rate_source': 'custom', 'transaction_date': '2026-08-23'}, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(self.account.current_balance, Decimal('80.00'))
        self.assertEqual(destination.current_balance, Decimal('20.00'))
        self.assertEqual(Transaction.objects.count(), 0)
        usd = Account.objects.create(user=self.user, name='USD wallet', account_type='other', currency='USD')
        with patch('budget.serializers.get_exchange_rate', return_value=Decimal('0.0075')):
            response = self.client.post('/api/transfers/', {'from_account': destination.id, 'to_account': usd.id, 'from_amount': '10.00', 'exchange_rate': '0.0075', 'rate_source': 'live', 'transaction_date': '2026-08-23'}, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['to_amount'], '0.08')

    def test_historical_live_transfer_patch_preserves_recorded_rate_and_amount(self):
        usd = Account.objects.create(user=self.user, name='USD wallet', account_type='other', currency='USD')
        with patch('budget.serializers.get_exchange_rate', return_value=Decimal('0.0075')):
            created = self.client.post('/api/transfers/', {
                'from_account': self.account.id, 'to_account': usd.id,
                'from_amount': '20.00', 'rate_source': 'live',
                'transaction_date': '2026-08-23', 'description': 'Original transfer',
            }, format='json')
        self.assertEqual(created.status_code, status.HTTP_201_CREATED)
        self.assertEqual((created.data['exchange_rate'], created.data['to_amount']), ('0.00750000', '0.15'))

        with patch('budget.serializers.get_exchange_rate', side_effect=AssertionError('must not fetch a new rate')) as rate_lookup:
            updated = self.client.patch(f'/api/transfers/{created.data["id"]}/', {
                'description': 'Corrected description',
            }, format='json')
        self.assertEqual(updated.status_code, status.HTTP_200_OK)
        rate_lookup.assert_not_called()
        self.assertEqual((updated.data['exchange_rate'], updated.data['to_amount']), ('0.00750000', '0.15'))
        self.assertEqual(self.account.current_balance, Decimal('80.00'))
        self.assertEqual(usd.current_balance, Decimal('0.15'))

        with patch('budget.serializers.get_exchange_rate', side_effect=AssertionError('must not fetch a new rate')) as rate_lookup:
            full_update = self.client.put(f'/api/transfers/{created.data["id"]}/', {
                'from_account': self.account.id, 'to_account': usd.id,
                'from_amount': '20.00', 'exchange_rate': '0.00750000',
                'rate_source': 'live', 'transaction_date': '2026-08-24',
                'description': 'Corrected date',
            }, format='json')
        self.assertEqual(full_update.status_code, status.HTTP_200_OK)
        rate_lookup.assert_not_called()
        self.assertEqual((full_update.data['exchange_rate'], full_update.data['to_amount']), ('0.00750000', '0.15'))

        rejected = self.client.patch(f'/api/transfers/{created.data["id"]}/', {
            'from_amount': '30.00',
        }, format='json')
        self.assertEqual(rejected.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.account.current_balance, Decimal('80.00'))
        self.assertEqual(usd.current_balance, Decimal('0.15'))

    def test_loan_repayments_update_status_and_balance(self):
        created = self.client.post('/api/loans/', {'loan_type': 'borrowed', 'person_name': 'Amina', 'original_amount': '100.00', 'currency': 'KES', 'account': self.account.id, 'loan_date': '2026-08-01'}, format='json')
        self.assertEqual(created.status_code, status.HTTP_201_CREATED)
        self.assertEqual(self.account.current_balance, Decimal('200.00'))
        repayment = self.client.post('/api/loan-repayments/', {'loan': created.data['id'], 'account': self.account.id, 'amount': '40.00', 'repayment_date': '2026-08-10'}, format='json')
        self.assertEqual(repayment.status_code, status.HTTP_201_CREATED)
        loan = Loan.objects.get(pk=created.data['id'])
        self.assertEqual((loan.status, loan.remaining_amount), ('partially_paid', Decimal('60.00')))
        overpayment = self.client.post('/api/loan-repayments/', {'loan': created.data['id'], 'account': self.account.id, 'amount': '70.00', 'repayment_date': '2026-08-11'}, format='json')
        self.assertEqual(overpayment.status_code, status.HTTP_400_BAD_REQUEST)

    def test_loan_principal_cannot_be_reduced_below_repayments_on_patch_or_put(self):
        loan = self.client.post('/api/loans/', {'loan_type': 'borrowed', 'person_name': 'Amina', 'original_amount': '100.00', 'currency': 'KES', 'account': self.account.id, 'loan_date': '2026-08-01'}, format='json')
        self.client.post('/api/loan-repayments/', {'loan': loan.data['id'], 'account': self.account.id, 'amount': '40.00', 'repayment_date': '2026-08-10'}, format='json')
        self.assertEqual(self.client.patch(f'/api/loans/{loan.data["id"]}/', {'original_amount': '30.00'}, format='json').status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.client.put(f'/api/loans/{loan.data["id"]}/', {
            'loan_type': 'borrowed', 'person_name': 'Amina', 'original_amount': '30.00',
            'currency': 'KES', 'account': self.account.id, 'loan_date': '2026-08-01',
        }, format='json').status_code, status.HTTP_400_BAD_REQUEST)
        updated = self.client.patch(f'/api/loans/{loan.data["id"]}/', {'original_amount': '120.00'}, format='json')
        self.assertEqual(updated.status_code, status.HTTP_200_OK)
        record = Loan.objects.get(pk=loan.data['id'])
        self.assertEqual((record.remaining_amount, record.status), (Decimal('80.00'), 'partially_paid'))

    def test_repayment_account_currency_must_match_loan_currency(self):
        usd = Account.objects.create(user=self.user, name='USD wallet', account_type='other', currency='USD')
        loan = self.client.post('/api/loans/', {'loan_type': 'borrowed', 'person_name': 'Amina', 'original_amount': '100.00', 'currency': 'KES', 'account': self.account.id, 'loan_date': '2026-08-01'}, format='json')
        response = self.client.post('/api/loan-repayments/', {'loan': loan.data['id'], 'account': usd.id, 'amount': '10.00', 'repayment_date': '2026-08-10'}, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_budget_spending_uses_only_expense_transactions(self):
        self.client.post('/api/transactions/', self.transaction_payload(category=self.expense.id, transaction_type='expense', amount='120.00'), format='json')
        self.client.post('/api/transactions/', self.transaction_payload(category=self.income.id, transaction_type='income', amount='500.00'), format='json')
        response = self.client.post('/api/budgets/', {'category': self.expense.id, 'amount': '100.00', 'period_type': 'monthly', 'start_date': '2026-08-01', 'end_date': '2026-08-31', 'rollover': False}, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual((response.data['spent_amount'], response.data['remaining_amount'], response.data['percentage_used']), (Decimal('120.00'), Decimal('-20.00'), Decimal('120.00')))
