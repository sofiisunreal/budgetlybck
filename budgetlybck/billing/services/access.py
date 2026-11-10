from django.core.exceptions import ValidationError
from django.contrib.auth import get_user_model
from django.conf import settings
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


def _minor_amount(value):
    if isinstance(value, bool):
        raise PurchaseVerificationError('Provider amount was invalid.')
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    raise PurchaseVerificationError('Provider amount was invalid.')


def _record_refund(attempt, provider_data):
    """Retain non-sensitive refund facts and return the cumulative amount."""
    amount = _minor_amount(provider_data.get('amount'))
    if amount <= 0 or provider_data.get('currency') != attempt.expected_currency:
        raise PurchaseVerificationError('Refund amount or currency did not match the purchase.')

    metadata = dict(attempt.provider_metadata or {})
    refunds = list(metadata.get('refunds', []))
    refund_id = str(provider_data.get('id') or provider_data.get('refund_reference') or '')
    if not refund_id:
        raise PurchaseVerificationError('Refund event did not include an identifier.')
    if not any(item.get('id') == refund_id for item in refunds):
        refunds.append({
            'id': refund_id,
            'amount': amount,
            'currency': attempt.expected_currency,
            'status': 'processed',
        })
    try:
        total = sum(_minor_amount(item.get('amount')) for item in refunds)
    except (AttributeError, TypeError) as error:
        raise PurchaseVerificationError('Stored refund metadata was invalid.') from error
    if total > attempt.expected_amount_minor:
        raise PurchaseVerificationError('Refund total exceeded the purchase amount.')
    metadata['refunds'] = refunds
    attempt.provider_metadata = metadata
    return total


def _mark_dispute_for_review(attempt, event_type, provider_data):
    """Keep the payment record intact while holding access for human review."""
    metadata = dict(attempt.provider_metadata or {})
    metadata['dispute_review_required'] = True
    metadata['latest_dispute_event'] = event_type
    if provider_data.get('id') is not None:
        metadata['latest_dispute_id'] = str(provider_data['id'])
    attempt.provider_metadata = metadata


def apply_verification(reference, provider_data):
    if provider_data.get('reference') != reference:
        raise PurchaseVerificationError('Paystack reference did not match the purchase.')
    provider_status = provider_data.get('status')
    if not isinstance(provider_status, str):
        raise PurchaseVerificationError('Paystack transaction status was invalid.')
    with transaction.atomic():
        attempt = PaystackPurchaseAttempt.objects.select_for_update().select_related('offer', 'user').get(reference=reference)
        if attempt.provider_mode not in {'test', 'live'} or attempt.provider_mode != settings.PAYSTACK_MODE:
            raise PurchaseVerificationError('Purchase environment did not match the configured Paystack mode.')
        provider_id = str(provider_data['id']) if provider_data.get('id') is not None else None
        if not provider_id:
            raise PurchaseVerificationError('Paystack transaction identifier was missing.')
        if attempt.provider_transaction_id and attempt.provider_transaction_id != provider_id:
            raise PurchaseVerificationError('Paystack transaction identifier changed unexpectedly.')
        if provider_id and PaystackPurchaseAttempt.objects.exclude(pk=attempt.pk).filter(
            provider_mode=attempt.provider_mode,
            provider_transaction_id=provider_id,
        ).exists():
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
        metadata = dict(attempt.provider_metadata or {})
        metadata.update(_safe_metadata(provider_data))
        attempt.provider_metadata, attempt.verified_at = metadata, timezone.now()
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

        entitlement = LifetimeAccessEntitlement.objects.select_for_update().filter(
            user=user,
            source=LifetimeAccessEntitlement.PAYSTACK_PURCHASE,
            provider_mode=attempt.provider_mode,
        ).first()
        if entitlement:
            if metadata.get('dispute_review_required') and entitlement.status == LifetimeAccessEntitlement.ACTIVE:
                entitlement.transition_to(LifetimeAccessEntitlement.CHARGEBACK_SUSPENDED)
            return attempt, entitlement, False
        try:
            entitlement = LifetimeAccessEntitlement.objects.create(
                user=user,
                source=LifetimeAccessEntitlement.PAYSTACK_PURCHASE,
                activating_purchase=attempt,
                provider_mode=attempt.provider_mode,
                status=(
                    LifetimeAccessEntitlement.CHARGEBACK_SUSPENDED
                    if metadata.get('dispute_review_required')
                    else LifetimeAccessEntitlement.ACTIVE
                ),
            )
        except IntegrityError:
            entitlement = LifetimeAccessEntitlement.objects.select_for_update().get(
                user=attempt.user,
                source=LifetimeAccessEntitlement.PAYSTACK_PURCHASE,
                provider_mode=attempt.provider_mode,
            )
            return attempt, entitlement, False
        return attempt, entitlement, True


def apply_reversal(reference, event_type, provider_data):
    """Apply only verified, unambiguous refund/dispute access effects."""
    with transaction.atomic():
        attempt = PaystackPurchaseAttempt.objects.select_for_update().get(reference=reference)
        if attempt.provider_mode not in {'test', 'live'} or attempt.provider_mode != settings.PAYSTACK_MODE:
            raise PurchaseVerificationError('Purchase environment did not match the configured Paystack mode.')
        entitlement = LifetimeAccessEntitlement.objects.select_for_update().filter(activating_purchase=attempt).first()

        if event_type == 'refund.processed':
            total_refunded = _record_refund(attempt, provider_data)
            if total_refunded == attempt.expected_amount_minor:
                metadata = dict(attempt.provider_metadata or {})
                metadata['refund_review_required'] = False
                attempt.provider_metadata = metadata
                attempt.status, attempt.verified_at = PaystackPurchaseAttempt.REVERSED, timezone.now()
                attempt.save(update_fields=['status', 'verified_at', 'provider_metadata', 'updated_at'])
                if entitlement and entitlement.status in {
                    LifetimeAccessEntitlement.ACTIVE,
                    LifetimeAccessEntitlement.CHARGEBACK_SUSPENDED,
                }:
                    entitlement.transition_to(LifetimeAccessEntitlement.REFUNDED)
            else:
                # A partial refund is not a reliable instruction to revoke a
                # lifetime entitlement. Its business treatment needs review.
                metadata = dict(attempt.provider_metadata or {})
                metadata['refund_review_required'] = True
                metadata['latest_partial_refund_id'] = str(
                    provider_data.get('id') or provider_data.get('refund_reference')
                )
                attempt.provider_metadata = metadata
                attempt.save(update_fields=['provider_metadata', 'updated_at'])
            return attempt, entitlement

        if event_type in {'charge.dispute.create', 'charge.dispute.remind'}:
            _mark_dispute_for_review(attempt, event_type, provider_data)
            attempt.save(update_fields=['provider_metadata', 'updated_at'])
            if entitlement and entitlement.status == LifetimeAccessEntitlement.ACTIVE:
                entitlement.transition_to(LifetimeAccessEntitlement.CHARGEBACK_SUSPENDED)
            return attempt, entitlement

        if event_type == 'charge.dispute.resolve':
            # Resolution lacks a sufficiently explicit access instruction.
            # Preserve any suspension until an operator verifies the outcome.
            _mark_dispute_for_review(attempt, event_type, provider_data)
            attempt.save(update_fields=['provider_metadata', 'updated_at'])
        return attempt, entitlement
