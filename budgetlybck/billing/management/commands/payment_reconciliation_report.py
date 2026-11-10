"""Read-only operational report for Paystack purchase reconciliation."""

from collections import defaultdict
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from billing.models import (EntitlementRestorationDecision,
                            LifetimeAccessEntitlement,
                            PaystackPurchaseAttempt, PaystackWebhookEvent)


class Command(BaseCommand):
    help = 'Report payment records requiring reconciliation without changing any data.'

    def add_arguments(self, parser):
        parser.add_argument('--mode', choices=('test', 'live'), required=True)
        parser.add_argument(
            '--stale-after-minutes',
            type=int,
            default=60,
            help='Age at which initialized or pending purchases require review (default: 60).',
        )

    def handle(self, *args, **options):
        mode = options['mode']
        stale_after_minutes = options['stale_after_minutes']
        if stale_after_minutes <= 0:
            raise CommandError('stale-after-minutes must be greater than zero.')

        cutoff = timezone.now() - timedelta(minutes=stale_after_minutes)
        attempts = PaystackPurchaseAttempt.objects.filter(provider_mode=mode).select_related(
            'user', 'activated_entitlement'
        )
        successful = attempts.filter(status=PaystackPurchaseAttempt.SUCCESSFUL)

        missing, suspended, revoked, refunded, deleted = [], [], [], [], []
        for attempt in successful:
            if attempt.user_id is None:
                deleted.append(attempt.reference)
                continue
            entitlement = getattr(attempt, 'activated_entitlement', None)
            if entitlement is None:
                missing.append(attempt.reference)
            elif entitlement.status == LifetimeAccessEntitlement.CHARGEBACK_SUSPENDED:
                suspended.append(attempt.reference)
            elif entitlement.status == LifetimeAccessEntitlement.REVOKED:
                revoked.append(attempt.reference)
            elif entitlement.status == LifetimeAccessEntitlement.REFUNDED:
                refunded.append(attempt.reference)

        stale = list(
            attempts.filter(
                status__in=[PaystackPurchaseAttempt.INITIALIZED, PaystackPurchaseAttempt.PENDING],
                created_at__lte=cutoff,
            ).values_list('reference', flat=True)
        )

        webhook_failures = defaultdict(list)
        for event in PaystackWebhookEvent.objects.filter(
            provider_mode=mode,
            processing_status=PaystackWebhookEvent.FAILED,
        ).only('error_category', 'transaction_reference'):
            webhook_failures[event.error_category or 'uncategorized'].append(
                event.transaction_reference or '(no purchase reference)'
            )

        review_required = []
        for attempt in attempts.values('reference', 'provider_metadata'):
            metadata = attempt['provider_metadata'] or {}
            reasons = []
            if metadata.get('refund_review_required'):
                reasons.append('partial-refund review')
            if metadata.get('dispute_review_required'):
                reasons.append('dispute review')
            if reasons:
                review_required.append((attempt['reference'], ', '.join(reasons)))

        unclassified_attempts = list(
            PaystackPurchaseAttempt.objects.filter(provider_mode__isnull=True).values_list('reference', flat=True)
        )
        unclassified_webhooks = PaystackWebhookEvent.objects.filter(provider_mode__isnull=True).count()
        unclassified_entitlements = LifetimeAccessEntitlement.objects.filter(
            source=LifetimeAccessEntitlement.PAYSTACK_PURCHASE,
            provider_mode__isnull=True,
        ).count()
        unclassified_decisions = EntitlementRestorationDecision.objects.filter(provider_mode__isnull=True).count()

        self.stdout.write('Payment reconciliation report (read-only)')
        self.stdout.write(f'Requested provider mode: {mode}')
        self.stdout.write(f'Configured Paystack mode: {settings.PAYSTACK_MODE}')
        self.stdout.write(f'Stale threshold: {stale_after_minutes} minutes')
        if mode != settings.PAYSTACK_MODE:
            self.stdout.write('NOTE: Report mode differs from configured mode; no provider calls or record changes were made.')
        self.stdout.write('')
        self.stdout.write(f'Purchase attempts in {mode}: {attempts.count()}')
        self.stdout.write(f'Successful purchases in {mode}: {successful.count()}')

        self._section('Successful purchases without active access — manual investigation', {
            'missing entitlement': missing,
            'chargeback suspended': suspended,
            'revoked': revoked,
            'refunded': refunded,
            'deleted account (payment retained, no access expected)': deleted,
        })
        self._section('Stale initialized or pending purchases — verify with provider before acting', {
            'stale purchases': stale,
        })
        self._section('Failed webhook processing — manual investigation', webhook_failures)
        self._section('Partial-refund or dispute review required — manual investigation', {
            reason: [reference for reference, detail in review_required if detail == reason]
            for reason in sorted({detail for _, detail in review_required})
        })
        self._section('Historical unclassified records — excluded from both modes', {
            'purchase attempts': unclassified_attempts,
            'webhook events': [f'{unclassified_webhooks} record(s)'] if unclassified_webhooks else [],
            'Paystack entitlements': [f'{unclassified_entitlements} record(s)'] if unclassified_entitlements else [],
            'restoration decisions': [f'{unclassified_decisions} record(s)'] if unclassified_decisions else [],
        })

        finding_count = sum(map(len, (missing, suspended, revoked, refunded, stale, review_required, unclassified_attempts)))
        finding_count += sum(len(values) for values in webhook_failures.values())
        self.stdout.write('')
        self.stdout.write(
            self.style.WARNING(f'Manual investigation findings: {finding_count}')
            if finding_count else self.style.SUCCESS('Manual investigation findings: 0')
        )

    def _section(self, title, groups):
        self.stdout.write('')
        self.stdout.write(title)
        found = False
        for label, references in groups.items():
            if not references:
                continue
            found = True
            self.stdout.write(f'  {label}: {len(references)}')
            self.stdout.write(f"    references: {', '.join(sorted(references))}")
        if not found:
            self.stdout.write('  none')
