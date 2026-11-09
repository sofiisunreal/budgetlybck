from django.core.exceptions import ValidationError
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.utils import timezone

from billing.models import LifetimeAccessEntitlement, PaystackPurchaseAttempt


class PurchaseVerificationError(Exception):
    category = 'verification_failed'


def provider_status_to_attempt_status(provider_status):
    return {
        'success': PaystackPurchaseAttempt.SUCCESSFUL, 'failed': PaystackPurchaseAttempt.FAILED,
        'abandoned': PaystackPurchaseAttempt.ABANDONED, 'reversed': PaystackPurchaseAttempt.REVERSED,
        'pending': PaystackPurchaseAttempt.PENDING, 'ongoing': PaystackPurchaseAttempt.PENDING,
        'processing': PaystackPurchaseAttempt.PENDING, 'queued': PaystackPurchaseAttempt.PENDING,
    }.get(provider_status, PaystackPurchaseAttempt.PENDING)


def _safe_metadata(provider_data):
    return {key: provider_data[key] for key in ('channel', 'gateway_response', 'paid_at') if provider_data.get(key) is not None}


def apply_verification(reference, provider_data):
    if provider_data.get('reference') != reference:
        raise PurchaseVerificationError('Paystack reference did not match the purchase.')
    provider_status = provider_data.get('status')
    if not isinstance(provider_status, str):
        raise PurchaseVerificationError('Paystack transaction status was invalid.')
    with transaction.atomic():
        attempt = PaystackPurchaseAttempt.objects.select_for_update().select_related('offer', 'user').get(reference=reference)
        provider_id = str(provider_data['id']) if provider_data.get('id') is not None else None
        if provider_id and PaystackPurchaseAttempt.objects.exclude(pk=attempt.pk).filter(provider_transaction_id=provider_id).exists():
            raise PurchaseVerificationError('Paystack transaction is already linked to another purchase.')
        if provider_data.get('amount') != attempt.expected_amount_minor:
            raise PurchaseVerificationError('Paystack amount did not match the purchase.')
        if provider_data.get('currency') != attempt.expected_currency:
            raise PurchaseVerificationError('Paystack currency did not match the purchase.')
        mapped_status = provider_status_to_attempt_status(provider_status)
        if attempt.status == PaystackPurchaseAttempt.REVERSED and mapped_status == PaystackPurchaseAttempt.SUCCESSFUL:
            raise PurchaseVerificationError('A reversed purchase cannot be reactivated automatically.')
        if attempt.status == PaystackPurchaseAttempt.SUCCESSFUL and mapped_status != PaystackPurchaseAttempt.SUCCESSFUL:
            raise PurchaseVerificationError('A successful purchase cannot be downgraded by verification.')
        attempt.status, attempt.provider_transaction_id = mapped_status, provider_id
        attempt.provider_metadata, attempt.verified_at = _safe_metadata(provider_data), timezone.now()
        attempt.save(update_fields=['status', 'provider_transaction_id', 'provider_metadata', 'verified_at', 'updated_at'])
        if mapped_status != PaystackPurchaseAttempt.SUCCESSFUL:
            return attempt, None, False
        # A payment can complete after its user has deleted their account. Keep
        # the verified payment evidence, but never recreate an entitlement for
        # a deleted account.
        if attempt.user_id is None:
            return attempt, None, False
        user = get_user_model().objects.select_for_update().filter(pk=attempt.user_id).first()
        if user is None:
            return attempt, None, False

        entitlement = LifetimeAccessEntitlement.objects.select_for_update().filter(user=user).first()
        if entitlement:
            return attempt, entitlement, False
        try:
            entitlement = LifetimeAccessEntitlement.objects.create(
                user=user, source=LifetimeAccessEntitlement.PAYSTACK_PURCHASE, activating_purchase=attempt
            )
        except IntegrityError:
            entitlement = LifetimeAccessEntitlement.objects.select_for_update().get(user=attempt.user)
            return attempt, entitlement, False
        return attempt, entitlement, True


def apply_reversal(reference, event_type):
    with transaction.atomic():
        attempt = PaystackPurchaseAttempt.objects.select_for_update().get(reference=reference)
        attempt.status, attempt.verified_at = PaystackPurchaseAttempt.REVERSED, timezone.now()
        attempt.save(update_fields=['status', 'verified_at', 'updated_at'])
        entitlement = LifetimeAccessEntitlement.objects.select_for_update().filter(activating_purchase=attempt).first()
        if not entitlement:
            return attempt, None
        if event_type == 'refund.processed' and entitlement.status == LifetimeAccessEntitlement.ACTIVE:
            entitlement.transition_to(LifetimeAccessEntitlement.REFUNDED)
        elif event_type.startswith('charge.dispute') and entitlement.status == LifetimeAccessEntitlement.ACTIVE:
            entitlement.transition_to(LifetimeAccessEntitlement.CHARGEBACK_SUSPENDED)
        return attempt, entitlement
