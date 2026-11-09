from uuid import UUID
import getpass

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from billing.services.dispute_restoration import (DisputeRestorationError,
                                                  execute_restoration)
from billing.services.paystack import (PaystackError, fetch_transaction_disputes,
                                       verify_transaction)
from billing.models import EntitlementRestorationDecision


class Command(BaseCommand):
    help = 'Execute one approved, provider-verified disputed-access restoration.'

    def add_arguments(self, parser):
        parser.add_argument('--decision-id', required=True)
        parser.add_argument('--operator-email', required=True)
        parser.add_argument('--confirm', action='store_true')

    def handle(self, *args, **options):
        if not options['confirm']:
            raise CommandError('Pass --confirm after reviewing the approved decision and provider outcome.')
        try:
            decision_id = UUID(options['decision_id'])
        except ValueError as error:
            raise CommandError('--decision-id must be a UUID.') from error
        operator = get_user_model().objects.filter(email=options['operator_email']).first()
        decision = EntitlementRestorationDecision.objects.filter(decision_id=decision_id).select_related('purchase').first()
        if operator is None or decision is None:
            raise CommandError('The specified operator or restoration decision was not found.')
        if not operator.check_password(getpass.getpass('Budgetly operator password: ')):
            raise CommandError('Operator authentication failed.')
        try:
            executed = execute_restoration(
                decision_id=decision_id,
                operator=operator,
                provider_data=verify_transaction(decision.purchase_reference),
                disputes=fetch_transaction_disputes(decision.provider_transaction_id),
            )
        except (DisputeRestorationError, PaystackError) as error:
            raise CommandError(str(error)) from error
        self.stdout.write(self.style.SUCCESS(f'Restoration decision executed: {executed.decision_id}'))
