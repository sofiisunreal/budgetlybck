"""Audited, provider-verified restoration of a suspended entitlement."""

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from billing.models import (EntitlementRestorationDecision,
                            LifetimeAccessEntitlement,
                            PaystackPurchaseAttempt)
from billing.services.access import apply_verification


class DisputeRestorationError(Exception):
    pass


def operator_identifier(user):
    return f'{user.pk}:{user.get_username()}'


def require_operator(operator, permission):
    if not operator or not operator.is_active or not operator.is_staff or not operator.has_perm(permission):
        raise DisputeRestorationError('The specified operator is not authorized for this action.')


def refund_total(attempt):
    try:
        return sum(int(item['amount']) for item in attempt.provider_metadata.get('refunds', []))
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        raise DisputeRestorationError('Stored refund facts are invalid and require manual review.') from error


def dispute_outcome_allows_restoration(disputes, attempt):
    """Return false until Paystack exposes an authoritative final outcome.

    The available dispute payload records the merchant's ``resolution`` action.
    In particular, ``resolution="declined"`` means that the merchant declined
    the dispute; it is not evidence that Paystack, the issuer, or the card
    network finally ruled for the merchant.  ``status="resolved"`` has the
    same ambiguity.  Transaction verification can prove that the payment
    exists, but cannot establish that a chargeback can no longer reverse it.

    Consequently the automated path deliberately fails closed.  Keep the
    parameter signature because execution still obtains and records the
    provider facts before refusing the status transition.  A future provider
    API field with a documented, final merchant-favourable outcome can be
    handled here with a targeted review and regression tests.
    """
    del disputes, attempt
    return False


def approve_restoration(*, purchase_reference, operator, evidence_reference, reason, provider_data):
    require_operator(operator, 'billing.approve_dispute_restoration')
    if not evidence_reference or not reason:
        raise DisputeRestorationError('Evidence reference and reason are required.')
    # Fresh transaction verification is mandatory before an approval can be
    # recorded, even though it cannot itself establish the dispute outcome.
    apply_verification(purchase_reference, provider_data)
    with transaction.atomic():
        attempt = PaystackPurchaseAttempt.objects.select_for_update().select_related('user').get(reference=purchase_reference)
        entitlement = LifetimeAccessEntitlement.objects.select_for_update().filter(activating_purchase=attempt).first()
        if attempt.user_id is None or entitlement is None:
            raise DisputeRestorationError('The account or entitlement no longer exists.')
        if entitlement.user_id != attempt.user_id:
            raise DisputeRestorationError('Purchase ownership no longer matches the entitlement.')
        if entitlement.status != LifetimeAccessEntitlement.CHARGEBACK_SUSPENDED:
            raise DisputeRestorationError('Only suspended entitlements can be approved for restoration.')
        if attempt.status != PaystackPurchaseAttempt.SUCCESSFUL:
            raise DisputeRestorationError('Only a currently verified successful purchase can be restored.')
        if not attempt.provider_transaction_id or attempt.status == PaystackPurchaseAttempt.REVERSED:
            raise DisputeRestorationError('The purchase is not eligible for restoration.')
        if refund_total(attempt) >= attempt.expected_amount_minor:
            raise DisputeRestorationError('A full refund prevents restoration.')
        if not attempt.provider_metadata.get('dispute_review_required'):
            raise DisputeRestorationError('No recorded dispute review is pending for this purchase.')
        if EntitlementRestorationDecision.objects.filter(purchase=attempt).exists():
            raise DisputeRestorationError('A restoration decision already exists for this purchase.')
        return EntitlementRestorationDecision.objects.create(
            purchase=attempt,
            purchase_reference=attempt.reference,
            entitlement=entitlement,
            user=attempt.user,
            provider_transaction_id=attempt.provider_transaction_id,
            approved_by=operator,
            approved_by_identifier=operator_identifier(operator),
            evidence_reference=evidence_reference,
            reason=reason,
            previous_status=entitlement.status,
        )


def execute_restoration(*, decision_id, operator, provider_data, disputes):
    require_operator(operator, 'billing.restore_disputed_access')
    with transaction.atomic():
        decision = EntitlementRestorationDecision.objects.select_for_update().select_related(
            'purchase', 'entitlement', 'user', 'approved_by'
        ).get(decision_id=decision_id)
        if decision.state != EntitlementRestorationDecision.APPROVED or decision.executed_at is not None:
            raise DisputeRestorationError('This restoration decision has already been executed or is invalid.')
        if decision.approved_by_id == operator.pk:
            raise DisputeRestorationError('A different authorized operator must execute the approved decision.')

        # Re-run verified payment facts inside the surrounding transaction.
        apply_verification(decision.purchase_reference, provider_data)
        attempt = PaystackPurchaseAttempt.objects.select_for_update().get(pk=decision.purchase_id)
        entitlement = LifetimeAccessEntitlement.objects.select_for_update().filter(pk=decision.entitlement_id).first()
        user = get_user_model().objects.select_for_update().filter(pk=decision.user_id).first()
        if user is None or attempt.user_id != user.pk or entitlement is None or entitlement.user_id != user.pk:
            raise DisputeRestorationError('The user, purchase, or entitlement no longer match the decision.')
        if attempt.reference != decision.purchase_reference or attempt.provider_transaction_id != decision.provider_transaction_id:
            raise DisputeRestorationError('Provider transaction facts no longer match the approved decision.')
        if entitlement.status != LifetimeAccessEntitlement.CHARGEBACK_SUSPENDED:
            raise DisputeRestorationError('The entitlement is no longer suspended.')
        if attempt.status != PaystackPurchaseAttempt.SUCCESSFUL:
            raise DisputeRestorationError('Only a currently verified successful purchase can be restored.')
        if attempt.status == PaystackPurchaseAttempt.REVERSED or refund_total(attempt) >= attempt.expected_amount_minor:
            raise DisputeRestorationError('A full refund prevents restoration.')
        if not dispute_outcome_allows_restoration(disputes, attempt):
            raise DisputeRestorationError('Paystack cannot establish a favorable final dispute outcome; manual review is required.')

        entitlement.transition_to(LifetimeAccessEntitlement.ACTIVE)
        decision.state = EntitlementRestorationDecision.EXECUTED
        decision.new_status = LifetimeAccessEntitlement.ACTIVE
        decision.executed_at = timezone.now()
        decision.executed_by = operator
        decision.executed_by_identifier = operator_identifier(operator)
        decision.save(update_fields=[
            'state', 'new_status', 'executed_at', 'executed_by', 'executed_by_identifier'
        ])
        return decision
