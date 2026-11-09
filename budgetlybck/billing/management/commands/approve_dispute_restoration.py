import getpass

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from billing.services.dispute_restoration import (DisputeRestorationError,
                                                  approve_restoration)
from billing.services.paystack import (PaystackError, verify_transaction)


class Command(BaseCommand):
    help = 'Record an authorized, immutable approval for disputed-access restoration.'

    def add_arguments(self, parser):
        parser.add_argument('--purchase-reference', required=True)
        parser.add_argument('--operator-email', required=True)
        parser.add_argument('--evidence-reference', required=True)
        parser.add_argument('--reason', required=True)
        parser.add_argument('--confirm', action='store_true')

    def handle(self, *args, **options):
        if not options['confirm']:
            raise CommandError('Pass --confirm after reviewing the purchase and evidence.')
        operator = get_user_model().objects.filter(email=options['operator_email']).first()
        if operator is None:
            raise CommandError('The specified operator was not found.')
        if not operator.check_password(getpass.getpass('Budgetly operator password: ')):
            raise CommandError('Operator authentication failed.')
        try:
            decision = approve_restoration(
                purchase_reference=options['purchase_reference'],
                operator=operator,
                evidence_reference=options['evidence_reference'],
                reason=options['reason'],
                provider_data=verify_transaction(options['purchase_reference']),
            )
        except (DisputeRestorationError, PaystackError) as error:
            raise CommandError(str(error)) from error
        self.stdout.write(self.style.SUCCESS(f'Restoration decision approved: {decision.decision_id}'))
