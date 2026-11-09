import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils import timezone


def generate_purchase_reference():
    return f'bgt_{uuid.uuid4().hex}'


class LifetimeAccessOffer(models.Model):
    """A server-managed one-time lifetime-access offer."""

    name = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    price_minor = models.PositiveBigIntegerField()
    currency = models.CharField(max_length=3, default='KES')
    is_active = models.BooleanField(default=False)
    # MySQL permits several NULLs in a unique field. This provides a portable
    # database-level guarantee that at most one offer can be active.
    active_marker = models.PositiveSmallIntegerField(null=True, unique=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            models.CheckConstraint(check=models.Q(price_minor__gt=0), name='billing_offer_price_positive'),
            models.CheckConstraint(
                check=(
                    models.Q(is_active=True, active_marker=1)
                    | models.Q(is_active=False, active_marker__isnull=True)
                ),
                name='billing_offer_active_marker_match',
            ),
        ]

    def clean(self):
        if self.currency != 'KES':
            raise ValidationError({'currency': 'Only KES lifetime offers are supported.'})
        if self.price_minor <= 0:
            raise ValidationError({'price_minor': 'Price must be positive.'})

    def save(self, *args, **kwargs):
        if self.pk:
            previous = type(self).objects.get(pk=self.pk)
            if self.purchase_attempts.exists() and (
                previous.price_minor != self.price_minor or previous.currency != self.currency
            ):
                raise ValidationError('An offer price or currency cannot change after a purchase attempt exists.')
        self.active_marker = 1 if self.is_active else None
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f'{self.name} ({self.currency} {self.price_minor})'


class PaystackPurchaseAttempt(models.Model):
    INITIALIZED = 'initialized'
    PENDING = 'pending'
    SUCCESSFUL = 'successful'
    FAILED = 'failed'
    ABANDONED = 'abandoned'
    REVERSED = 'reversed'
    STATUSES = [
        (INITIALIZED, 'Initialized'), (PENDING, 'Pending'), (SUCCESSFUL, 'Successful'),
        (FAILED, 'Failed'), (ABANDONED, 'Abandoned'), (REVERSED, 'Reversed'),
    ]

    # Retain payment evidence after account deletion without retaining the
    # account or its personal data. A null user means the account was deleted;
    # the attempt may still be reconciled by its provider reference.
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='paystack_purchase_attempts',
    )
    reference = models.CharField(max_length=80, unique=True, default=generate_purchase_reference, editable=False)
    offer = models.ForeignKey(LifetimeAccessOffer, on_delete=models.PROTECT, related_name='purchase_attempts')
    expected_amount_minor = models.PositiveBigIntegerField()
    expected_currency = models.CharField(max_length=3)
    provider_transaction_id = models.CharField(max_length=80, null=True, blank=True, unique=True)
    status = models.CharField(max_length=16, choices=STATUSES, default=INITIALIZED)
    initialized_at = models.DateTimeField(null=True, blank=True)
    verified_at = models.DateTimeField(null=True, blank=True)
    # Deliberately limited to non-sensitive integration fields such as channel
    # and gateway response; never store card, authorization, or bank details.
    provider_metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            models.CheckConstraint(check=models.Q(expected_amount_minor__gt=0), name='billing_attempt_amount_positive'),
        ]

    def clean(self):
        if self.expected_currency != 'KES':
            raise ValidationError({'expected_currency': 'Only KES purchases are supported.'})
        if self.expected_amount_minor <= 0:
            raise ValidationError({'expected_amount_minor': 'Expected amount must be positive.'})
        if self.offer_id and (
            self.expected_amount_minor != self.offer.price_minor or self.expected_currency != self.offer.currency
        ):
            raise ValidationError('Purchase snapshots must match their offer at creation.')

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return self.reference


class LifetimeAccessEntitlement(models.Model):
    ACTIVE = 'active'
    REVOKED = 'revoked'
    REFUNDED = 'refunded'
    CHARGEBACK_SUSPENDED = 'chargeback_suspended'
    STATUSES = [
        (ACTIVE, 'Active'), (REVOKED, 'Revoked'), (REFUNDED, 'Refunded'),
        (CHARGEBACK_SUSPENDED, 'Chargeback suspended'),
    ]
    PAYSTACK_PURCHASE = 'paystack_purchase'
    LEGACY_GRANT = 'legacy_grant'
    SOURCES = [(PAYSTACK_PURCHASE, 'Paystack purchase'), (LEGACY_GRANT, 'Legacy grant')]
    ALLOWED_TRANSITIONS = {
        ACTIVE: {REVOKED, REFUNDED, CHARGEBACK_SUSPENDED},
        CHARGEBACK_SUSPENDED: {ACTIVE, REVOKED, REFUNDED},
        REVOKED: set(),
        REFUNDED: set(),
    }

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='lifetime_access_entitlement')
    status = models.CharField(max_length=24, choices=STATUSES, default=ACTIVE)
    source = models.CharField(max_length=24, choices=SOURCES)
    activating_purchase = models.OneToOneField(
        PaystackPurchaseAttempt, null=True, blank=True, on_delete=models.PROTECT, related_name='activated_entitlement'
    )
    granted_at = models.DateTimeField(default=timezone.now)
    status_changed_at = models.DateTimeField(default=timezone.now)
    access_email_sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                check=(
                    models.Q(source='legacy_grant', activating_purchase__isnull=True)
                    | models.Q(source='paystack_purchase', activating_purchase__isnull=False)
                ),
                name='billing_entitlement_source_purchase_match',
            ),
        ]

    def clean(self):
        if self.source == self.LEGACY_GRANT and self.activating_purchase_id is not None:
            raise ValidationError('Legacy grants cannot have an activating purchase.')
        if self.source == self.PAYSTACK_PURCHASE:
            if self.activating_purchase_id is None:
                raise ValidationError('Paystack entitlements require an activating purchase.')
            if self.activating_purchase.status not in {
                PaystackPurchaseAttempt.SUCCESSFUL,
                PaystackPurchaseAttempt.REVERSED,
            }:
                raise ValidationError('Only successful purchases can activate entitlement.')
            if self.user_id and self.activating_purchase.user_id != self.user_id:
                raise ValidationError('An activating purchase must belong to the entitlement user.')

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def transition_to(self, new_status):
        if new_status == self.status:
            return
        if new_status not in self.ALLOWED_TRANSITIONS.get(self.status, set()):
            raise ValidationError(f'Cannot transition entitlement from {self.status} to {new_status}.')
        self.status = new_status
        self.status_changed_at = timezone.now()
        self.save(update_fields=['status', 'status_changed_at'])

    @classmethod
    def grant_legacy_access(cls, user):
        """Idempotently grant access without changing an existing entitlement."""
        with transaction.atomic():
            locked_user = type(user).objects.select_for_update().get(pk=user.pk)
            entitlement, created = cls.objects.select_for_update().get_or_create(
                user=locked_user,
                defaults={'source': cls.LEGACY_GRANT, 'status': cls.ACTIVE},
            )
        return entitlement, created

    def __str__(self):
        return f'{self.user} - {self.status}'


class EntitlementRestorationDecision(models.Model):
    """Immutable approval and one-time execution record for dispute restoration."""

    APPROVED = 'approved'
    EXECUTED = 'executed'
    STATES = [(APPROVED, 'Approved'), (EXECUTED, 'Executed')]

    decision_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    purchase = models.OneToOneField(
        PaystackPurchaseAttempt,
        on_delete=models.PROTECT,
        related_name='restoration_decision',
    )
    purchase_reference = models.CharField(max_length=80, editable=False)
    entitlement = models.ForeignKey(
        LifetimeAccessEntitlement,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='restoration_decisions',
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='entitlement_restoration_decisions',
    )
    provider_transaction_id = models.CharField(max_length=80, editable=False)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='approved_entitlement_restorations',
    )
    approved_by_identifier = models.CharField(max_length=180, editable=False)
    evidence_reference = models.CharField(max_length=255)
    reason = models.TextField()
    previous_status = models.CharField(max_length=24, editable=False)
    new_status = models.CharField(max_length=24, null=True, blank=True, editable=False)
    state = models.CharField(max_length=16, choices=STATES, default=APPROVED, editable=False)
    approved_at = models.DateTimeField(auto_now_add=True)
    executed_at = models.DateTimeField(null=True, blank=True, editable=False)
    executed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='executed_entitlement_restorations',
    )
    executed_by_identifier = models.CharField(max_length=180, blank=True, editable=False)

    class Meta:
        ordering = ['-approved_at']
        permissions = [
            ('approve_dispute_restoration', 'Can approve disputed lifetime-access restoration'),
            ('restore_disputed_access', 'Can execute disputed lifetime-access restoration'),
        ]

    def clean(self):
        if self.purchase_id and self.purchase_reference != self.purchase.reference:
            raise ValidationError('Decision purchase reference must match the purchase.')
        if self.purchase_id and self.provider_transaction_id != (self.purchase.provider_transaction_id or ''):
            raise ValidationError('Decision provider transaction ID must match the purchase.')
        if self.entitlement_id and self.user_id and self.entitlement.user_id != self.user_id:
            raise ValidationError('Decision user must match the entitlement user.')
        if self.state == self.EXECUTED and (
            self.executed_at is None or self.executed_by_id is None or self.new_status != LifetimeAccessEntitlement.ACTIVE
        ):
            raise ValidationError('Executed restoration decisions require executor, timestamp and active status.')

    def save(self, *args, **kwargs):
        if self.pk:
            previous = type(self).objects.get(pk=self.pk)
            immutable_fields = (
                'decision_id', 'purchase_id', 'purchase_reference', 'entitlement_id', 'user_id',
                'provider_transaction_id', 'approved_by_id', 'approved_by_identifier',
                'evidence_reference', 'reason', 'previous_status', 'approved_at',
            )
            if any(getattr(previous, field) != getattr(self, field) for field in immutable_fields):
                raise ValidationError('Restoration decision audit facts cannot be changed.')
            if previous.state == self.EXECUTED:
                raise ValidationError('Executed restoration decisions cannot be changed.')
            if self.state != self.EXECUTED:
                raise ValidationError('Restoration decisions can only be executed once.')
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f'Restoration decision {self.decision_id}'


class PaystackWebhookEvent(models.Model):
    RECEIVED = 'received'
    PROCESSED = 'processed'
    IGNORED = 'ignored'
    FAILED = 'failed'
    PROCESSING_STATUSES = [
        (RECEIVED, 'Received'), (PROCESSED, 'Processed'), (IGNORED, 'Ignored'), (FAILED, 'Failed'),
    ]

    deduplication_key = models.CharField(max_length=180, unique=True)
    event_type = models.CharField(max_length=80)
    transaction_reference = models.CharField(max_length=80, blank=True)
    payload_digest = models.CharField(max_length=64)
    processing_status = models.CharField(max_length=16, choices=PROCESSING_STATUSES, default=RECEIVED)
    received_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    error_category = models.CharField(max_length=80, blank=True)

    class Meta:
        ordering = ['-received_at']

    def __str__(self):
        return f'{self.event_type} ({self.transaction_reference or self.deduplication_key})'
