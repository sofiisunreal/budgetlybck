from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils.dateparse import parse_datetime

from billing.models import LifetimeAccessEntitlement


class Command(BaseCommand):
    help = 'Grant complimentary lifetime access to users created before an explicitly approved cutoff.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--cutoff', required=True,
            help='Approved ISO-8601 UTC cutoff. Users strictly before it are eligible.',
        )
        parser.add_argument('--dry-run', action='store_true', help='Report counts without creating entitlements.')

    def handle(self, *args, **options):
        cutoff = parse_datetime(options['cutoff'])
        if cutoff is None or cutoff.tzinfo is None:
            raise CommandError('--cutoff must be an ISO-8601 timestamp with a timezone, for example 2026-11-07T00:00:00+00:00.')

        User = get_user_model()
        eligible = User.objects.filter(date_joined__lt=cutoff)
        eligible_count = eligible.count()
        already_entitled_count = LifetimeAccessEntitlement.objects.filter(user__in=eligible).count()
        grant_count = eligible_count - already_entitled_count
        self.stdout.write(
            f'Eligible users: {eligible_count}; already entitled: {already_entitled_count}; proposed grants: {grant_count}.'
        )
        if options['dry_run']:
            return

        granted = 0
        with transaction.atomic():
            for user in eligible.select_for_update().iterator():
                _, created = LifetimeAccessEntitlement.grant_legacy_access(user)
                granted += int(created)
        self.stdout.write(self.style.SUCCESS(f'Granted complimentary lifetime access to {granted} users.'))
