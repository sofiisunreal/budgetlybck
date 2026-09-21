from decimal import Decimal

from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase

from budget.models import Account


class SavingsApiTests(APITestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username='alice', email='alice@example.com', password='password123')
        self.other_user = User.objects.create_user(username='bob', email='bob@example.com', password='password123')
        self.account = Account.objects.create(user=self.user, name='Cash', account_type='cash', currency='KES')
        self.other_account = Account.objects.create(user=self.other_user, name='Bob cash', account_type='cash', currency='KES')
        self.client.force_authenticate(self.user)

    def create_goal(self, **overrides):
        payload = {'name': 'Emergency fund', 'target_amount': '100.00', 'currency': 'KES'}
        payload.update(overrides)
        return self.client.post('/api/savings/goals/', payload, format='json')

    def test_savings_goal_account_must_belong_to_authenticated_user(self):
        rejected = self.create_goal(account=self.other_account.id)
        self.assertEqual(rejected.status_code, status.HTTP_400_BAD_REQUEST)

        created = self.create_goal(account=self.account.id)
        self.assertEqual(created.status_code, status.HTTP_201_CREATED)
        rejected_update = self.client.patch(
            f'/api/savings/goals/{created.data["id"]}/',
            {'account': self.other_account.id}, format='json'
        )
        self.assertEqual(rejected_update.status_code, status.HTTP_400_BAD_REQUEST)
        cleared = self.client.patch(
            f'/api/savings/goals/{created.data["id"]}/',
            {'account': None}, format='json'
        )
        self.assertEqual(cleared.status_code, status.HTTP_200_OK)
        self.assertIsNone(cleared.data['account'])

    def test_savings_partial_updates_keep_existing_goal(self):
        goal = self.create_goal().data
        contribution = self.client.post('/api/savings/contributions/', {
            'goal': goal['id'], 'amount': '50.00', 'contribution_date': '2026-09-21',
        }, format='json')
        self.assertEqual(contribution.status_code, status.HTTP_201_CREATED)
        self.assertEqual(self.client.patch(
            f'/api/savings/contributions/{contribution.data["id"]}/',
            {'note': 'Updated note'}, format='json'
        ).status_code, status.HTTP_200_OK)

        withdrawal = self.client.post('/api/savings/withdrawals/', {
            'goal': goal['id'], 'amount': '10.00', 'withdrawal_date': '2026-09-21',
        }, format='json')
        self.assertEqual(withdrawal.status_code, status.HTTP_201_CREATED)
        self.assertEqual(self.client.patch(
            f'/api/savings/withdrawals/{withdrawal.data["id"]}/',
            {'note': 'Updated note'}, format='json'
        ).status_code, status.HTTP_200_OK)

        plan = self.client.post('/api/savings/plans/', {
            'goal': goal['id'], 'amount': '10.00', 'frequency': 'weekly',
            'start_date': '2026-09-21',
        }, format='json')
        self.assertEqual(plan.status_code, status.HTTP_201_CREATED)
        self.assertEqual(self.client.patch(
            f'/api/savings/plans/{plan.data["id"]}/',
            {'amount': '15.00'}, format='json'
        ).status_code, status.HTTP_200_OK)

    def test_target_reached_goal_rejects_withdrawals(self):
        goal = self.create_goal().data
        contribution = self.client.post('/api/savings/contributions/', {
            'goal': goal['id'], 'amount': '100.00', 'contribution_date': '2026-09-21',
        }, format='json')
        self.assertEqual(contribution.status_code, status.HTTP_201_CREATED)
        rejected = self.client.post('/api/savings/withdrawals/', {
            'goal': goal['id'], 'amount': '10.00', 'withdrawal_date': '2026-09-21',
        }, format='json')
        self.assertEqual(rejected.status_code, status.HTTP_400_BAD_REQUEST)
