from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from billing.models import LifetimeAccessOffer


class Command(BaseCommand):
    help = 'Idempotently create the configured Budgetly lifetime-access offer.'

    def handle(self, *args, **options):
        if settings.BILLING_LIFETIME_CURRENCY != 'KES' or settings.BILLING_LIFETIME_PRICE_MINOR <= 0:
            raise CommandError('Billing offer configuration must be a positive KES minor-unit amount.')

        with transaction.atomic():
            offer = LifetimeAccessOffer.objects.filter(
                name=settings.BILLING_LIFETIME_OFFER_NAME,
                price_minor=settings.BILLING_LIFETIME_PRICE_MINOR,
                currency=settings.BILLING_LIFETIME_CURRENCY,
            ).first()
            if offer:
                self.stdout.write(f'Lifetime offer already exists (id={offer.pk}).')
                return

            if LifetimeAccessOffer.objects.filter(is_active=True).exists():
                raise CommandError(
                    'An active offer already exists. Deactivate it and create a reviewed replacement before changing price.'
                )
            offer = LifetimeAccessOffer.objects.create(
                name=settings.BILLING_LIFETIME_OFFER_NAME,
                description='One-time payment for Budgetly lifetime access.',
                price_minor=settings.BILLING_LIFETIME_PRICE_MINOR,
                currency=settings.BILLING_LIFETIME_CURRENCY,
                is_active=True,
            )
        self.stdout.write(self.style.SUCCESS(f'Created lifetime offer (id={offer.pk}).'))
