from django.contrib import admin

from .models import (LifetimeAccessEntitlement, LifetimeAccessOffer,
                     EntitlementRestorationDecision, PaystackPurchaseAttempt,
                     PaystackWebhookEvent)


@admin.register(LifetimeAccessOffer)
class LifetimeAccessOfferAdmin(admin.ModelAdmin):
    list_display = ('name', 'price_minor', 'currency', 'is_active', 'updated_at')
    list_filter = ('is_active', 'currency')
    readonly_fields = ('active_marker', 'created_at', 'updated_at')

    def get_readonly_fields(self, request, obj=None):
        fields = list(super().get_readonly_fields(request, obj))
        if obj and obj.purchase_attempts.exists():
            fields.extend(['price_minor', 'currency'])
        return fields


@admin.register(PaystackPurchaseAttempt)
class PaystackPurchaseAttemptAdmin(admin.ModelAdmin):
    list_display = ('reference', 'provider_mode', 'user', 'expected_amount_minor', 'expected_currency', 'status', 'created_at')
    list_filter = ('provider_mode', 'status', 'expected_currency')
    search_fields = ('reference', 'provider_transaction_id', 'user__username', 'user__email')
    readonly_fields = [field.name for field in PaystackPurchaseAttempt._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(LifetimeAccessEntitlement)
class LifetimeAccessEntitlementAdmin(admin.ModelAdmin):
    list_display = ('user', 'status', 'source', 'granted_at', 'status_changed_at')
    list_filter = ('status', 'source')
    search_fields = ('user__username', 'user__email')
    readonly_fields = [field.name for field in LifetimeAccessEntitlement._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(PaystackWebhookEvent)
class PaystackWebhookEventAdmin(admin.ModelAdmin):
    list_display = ('event_type', 'provider_mode', 'transaction_reference', 'processing_status', 'received_at', 'processed_at')
    list_filter = ('provider_mode', 'event_type', 'processing_status')
    search_fields = ('transaction_reference', 'deduplication_key', 'payload_digest')
    readonly_fields = [field.name for field in PaystackWebhookEvent._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(EntitlementRestorationDecision)
class EntitlementRestorationDecisionAdmin(admin.ModelAdmin):
    list_display = ('decision_id', 'provider_mode', 'purchase_reference', 'state', 'previous_status', 'new_status', 'approved_at', 'executed_at')
    list_filter = ('provider_mode', 'state', 'previous_status', 'new_status')
    search_fields = ('decision_id', 'purchase_reference', 'provider_transaction_id', 'approved_by_identifier')
    readonly_fields = [field.name for field in EntitlementRestorationDecision._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
