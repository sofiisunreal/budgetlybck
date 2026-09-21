from django.core.management.base import BaseCommand
from budget.models import Category


class Command(BaseCommand):
    help = 'Create Budgetly default categories'

    def handle(self, *args, **kwargs):

        expense_categories = [
            'Food',
            'Transport',
            'Housing',
            'Utilities',
            'Healthcare',
            'Education',
            'Shopping',
            'Entertainment',
            'Personal Care',
            'Subscriptions',
            'Savings',
            'Other',
        ]

        income_categories = [
            'Salary',
            'Freelance',
            'Business',
            'Investment',
            'Gifts',
            'Other Income',
        ]

        for name in expense_categories:
            Category.objects.get_or_create(
                name=name,
                category_type='expense',
                user=None
            )

        for name in income_categories:
            Category.objects.get_or_create(
                name=name,
                category_type='income',
                user=None
            )

        self.stdout.write(
            self.style.SUCCESS(
                'Default categories created successfully.'
            )
        )
