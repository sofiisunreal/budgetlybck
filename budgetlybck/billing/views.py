import json
import logging
from datetime import timedelta
from urllib.parse import urlencode, urlparse

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, models, transaction
from django.http import HttpResponseRedirect
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import NotFound
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle
from rest_framework.views import APIView

from .models import (LifetimeAccessEntitlement, LifetimeAccessOffer,
                     PaystackPurchaseAttempt, PaystackWebhookEvent)
from .permissions import has_lifetime_access
from .services.access import PurchaseVerificationError, apply_reversal
from .services.payments import verify_and_activate_purchase
from .services.paystack import (PaystackConfigurationError, PaystackError,
                                PaystackResponseError, PaystackUnavailableError,
                                get_config, initialize_transaction,
                                payload_digest, webhook_signature_valid)


logger = logging.getLogger(__name__)


class BillingAccessThrottle(UserRateThrottle):
    scope = 'billing_access'


class BillingCheckoutThrottle(UserRateThrottle):
    scope = 'billing_checkout'


class BillingVerificationThrottle(UserRateThrottle):
    scope = 'billing_verify'


def active_offer():
    offer = LifetimeAccessOffer.objects.filter(is_active=True).first()
    if not offer or offer.currency != settings.BILLING_LIFETIME_CURRENCY or offer.price_minor != settings.BILLING_LIFETIME_PRICE_MINOR:
        logger.error('billing_event=active_offer_invalid category=configuration')
        raise PaystackConfigurationError('Active billing offer is unavailable or mismatched.')
    return offer


def purchase_data(attempt):
    return {
        'reference': attempt.reference, 'status': attempt.status, 'provider_mode': attempt.provider_mode,
        'expected_amount_minor': attempt.expected_amount_minor, 'expected_currency': attempt.expected_currency,
        'initialized_at': attempt.initialized_at, 'verified_at': attempt.verified_at, 'created_at': attempt.created_at,
    }


def access_data(user):
    entitlement = LifetimeAccessEntitlement.objects.filter(user=user).filter(
        models.Q(source=LifetimeAccessEntitlement.LEGACY_GRANT)
        | models.Q(source=LifetimeAccessEntitlement.PAYSTACK_PURCHASE, provider_mode=settings.PAYSTACK_MODE)
    ).first()
    active = bool(entitlement and entitlement.status == LifetimeAccessEntitlement.ACTIVE)
    return {'has_lifetime_access': active, 'entitlement_status': entitlement.status if entitlement else None, 'purchase_required': not active}


def live_checkout_allowed(user):
    """Fail closed unless this account is explicitly approved for Live checkout."""
    if settings.PAYSTACK_MODE != 'live':
        return True
    allowed_ids = settings.BILLING_LIVE_CHECKOUT_ALLOWED_USER_IDS
    return bool(
        settings.BILLING_LIVE_CHECKOUT_ENABLED
        and isinstance(allowed_ids, (set, frozenset))
        and user.pk in allowed_ids
    )


class BillingAccessView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [BillingAccessThrottle]

    def get(self, request):
        return Response(access_data(request.user))


class PurchaseInitializeView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [BillingCheckoutThrottle]

    def post(self, request):
        # This is intentionally before all provider/configuration work.  It
        # restricts only new Live checkout initialization, never verification.
        if not live_checkout_allowed(request.user):
            return Response(
                {
                    'detail': 'Live checkout is not available for this account.',
                    'code': 'live_checkout_restricted',
                },
                status=status.HTTP_403_FORBIDDEN,
            )
        if has_lifetime_access(request.user):
            return Response({'detail': 'This account already has lifetime access.'}, status=status.HTTP_409_CONFLICT)
        try:
            config, offer = get_config(), active_offer()
        except PaystackConfigurationError:
            return Response({'detail': 'Payments are not configured.'}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        with transaction.atomic():
            # Serializing on the user row prevents two browser tabs or retrying
            # clients from receiving separate checkout references concurrently.
            user = get_user_model().objects.select_for_update().get(pk=request.user.pk)
            cooldown = timezone.now() - timedelta(minutes=settings.PAYSTACK_PENDING_ATTEMPT_MINUTES)
            if PaystackPurchaseAttempt.objects.filter(
                user=user,
                provider_mode=settings.PAYSTACK_MODE,
                status__in=[PaystackPurchaseAttempt.INITIALIZED, PaystackPurchaseAttempt.PENDING],
                created_at__gte=cooldown,
            ).exists():
                return Response({'detail': 'A payment attempt is already in progress. Verify its status before starting another checkout.'}, status=status.HTTP_409_CONFLICT)
            attempt = PaystackPurchaseAttempt.objects.create(
                user=user, offer=offer, expected_amount_minor=offer.price_minor,
                expected_currency=offer.currency, provider_mode=settings.PAYSTACK_MODE,
            )
        try:
            checkout = initialize_transaction(
                email=request.user.email, amount_minor=attempt.expected_amount_minor, reference=attempt.reference,
                callback_url=config.callback_url, metadata={'purchase_reference': attempt.reference},
            )
        except PaystackError as error:
            # A timeout can occur after Paystack accepted the initialization.
            # Keep that reference pending rather than silently creating another
            # checkout that could expose the user to a second charge.
            attempt.status = (
                PaystackPurchaseAttempt.PENDING
                if isinstance(error, PaystackUnavailableError)
                else PaystackPurchaseAttempt.FAILED
            )
            attempt.save(update_fields=['status', 'updated_at'])
            logger.warning(
                'billing_event=initialization_failed category=%s reference=%s',
                error.category,
                attempt.reference,
            )
            return Response({'detail': 'Unable to start checkout. Please try again.'}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        attempt.initialized_at = timezone.now()
        attempt.save(update_fields=['initialized_at', 'updated_at'])
        return Response({'reference': checkout['reference'], 'authorization_url': checkout['authorization_url']}, status=status.HTTP_201_CREATED)


class PurchaseDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get_object(self, request, reference):
        try:
            return PaystackPurchaseAttempt.objects.get(reference=reference, user=request.user)
        except PaystackPurchaseAttempt.DoesNotExist as error:
            raise NotFound('Purchase not found.') from error

    def get(self, request, reference):
        return Response(purchase_data(self.get_object(request, reference)))


class PurchaseVerifyView(PurchaseDetailView):
    throttle_classes = [BillingVerificationThrottle]

    def post(self, request, reference):
        self.get_object(request, reference)
        try:
            attempt, entitlement, activated = verify_and_activate_purchase(reference)
        except PaystackConfigurationError:
            return Response({'detail': 'Payments are not configured.'}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        except PaystackUnavailableError:
            return Response({'detail': 'Payment verification is temporarily unavailable.'}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        except (PaystackResponseError, PurchaseVerificationError):
            return Response({'detail': 'Payment could not be verified.'}, status=status.HTTP_400_BAD_REQUEST)
        result = purchase_data(attempt)
        result.update({'has_lifetime_access': bool(entitlement and entitlement.status == LifetimeAccessEntitlement.ACTIVE), 'activated': activated})
        return Response(result)


def payment_return_url(reference, outcome):
    parsed = urlparse(settings.FRONTEND_PAYMENT_RETURN_URL)
    if parsed.scheme not in {'http', 'https'} or not parsed.netloc:
        raise PaystackConfigurationError('A valid frontend payment return URL is required.')
    separator = '&' if parsed.query else '?'
    return f'{settings.FRONTEND_PAYMENT_RETURN_URL}{separator}{urlencode({"reference": reference, "status": outcome})}'


def event_transaction_reference(data):
    """Paystack refund and dispute payloads use transaction_reference."""
    transaction = data.get('transaction')
    candidates = (
        data.get('reference'),
        data.get('transaction_reference'),
        transaction.get('reference') if isinstance(transaction, dict) else None,
    )
    for value in candidates:
        if isinstance(value, str) and value:
            return value
    return ''


class PaystackCallbackView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        reference, outcome = request.query_params.get('reference', ''), 'pending'
        if reference:
            try:
                attempt, entitlement, _ = verify_and_activate_purchase(reference)
                outcome = 'success' if entitlement and entitlement.status == LifetimeAccessEntitlement.ACTIVE else attempt.status
            except (PaystackError, PurchaseVerificationError, PaystackPurchaseAttempt.DoesNotExist):
                logger.info('billing_event=callback_verification_incomplete reference=%s', reference)
        try:
            return HttpResponseRedirect(payment_return_url(reference, outcome))
        except PaystackConfigurationError:
            return Response({'detail': 'Payment return is not configured.'}, status=status.HTTP_503_SERVICE_UNAVAILABLE)


class PaystackWebhookView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        raw_body = request.body
        if not webhook_signature_valid(raw_body, request.headers):
            logger.warning('billing_event=webhook_signature_rejected')
            return Response({'detail': 'Invalid webhook signature.'}, status=status.HTTP_401_UNAUTHORIZED)
        try:
            event = json.loads(raw_body.decode('utf-8'))
            event_type, data = event['event'], event.get('data') or {}
            if not isinstance(data, dict):
                raise ValueError
        except (UnicodeDecodeError, ValueError, TypeError, KeyError):
            logger.warning('billing_event=webhook_payload_rejected')
            return Response({'detail': 'Invalid webhook payload.'}, status=status.HTTP_400_BAD_REQUEST)

        reference, provider_id = event_transaction_reference(data), str(data.get('id') or '')
        provider_mode = settings.PAYSTACK_MODE
        key_component = provider_id or reference or payload_digest(raw_body)
        deduplication_key = f'{provider_mode}:{event_type}:{key_component}'
        try:
            webhook_event, _ = PaystackWebhookEvent.objects.get_or_create(
                deduplication_key=deduplication_key,
                defaults={
                    'event_type': event_type, 'provider_mode': provider_mode,
                    'transaction_reference': reference, 'payload_digest': payload_digest(raw_body),
                },
            )
        except IntegrityError:
            webhook_event = PaystackWebhookEvent.objects.get(deduplication_key=deduplication_key)
        if webhook_event.processing_status in {PaystackWebhookEvent.PROCESSED, PaystackWebhookEvent.IGNORED}:
            return Response(status=status.HTTP_200_OK)
        try:
            if event_type == 'charge.success':
                if not reference:
                    raise PurchaseVerificationError('Webhook did not include a reference.')
                verify_and_activate_purchase(reference)
            elif event_type in {'refund.processed', 'charge.dispute.create', 'charge.dispute.remind', 'charge.dispute.resolve'}:
                if not reference:
                    raise PurchaseVerificationError('Webhook did not include a transaction reference.')
                apply_reversal(reference, event_type, data)
            else:
                webhook_event.processing_status, webhook_event.processed_at = PaystackWebhookEvent.IGNORED, timezone.now()
                webhook_event.save(update_fields=['processing_status', 'processed_at'])
                return Response(status=status.HTTP_200_OK)
        except PaystackUnavailableError:
            webhook_event.processing_status, webhook_event.error_category = PaystackWebhookEvent.FAILED, 'provider_unavailable'
            webhook_event.save(update_fields=['processing_status', 'error_category'])
            logger.warning(
                'billing_event=webhook_processing_failed category=provider_unavailable reference=%s',
                reference or '-',
            )
            return Response({'detail': 'Temporary processing failure.'}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        except (PaystackError, PurchaseVerificationError, PaystackPurchaseAttempt.DoesNotExist, DjangoValidationError):
            webhook_event.processing_status, webhook_event.error_category = PaystackWebhookEvent.FAILED, 'verification_failed'
            webhook_event.save(update_fields=['processing_status', 'error_category'])
            logger.warning(
                'billing_event=webhook_processing_failed category=verification_failed reference=%s',
                reference or '-',
            )
            return Response(status=status.HTTP_200_OK)
        webhook_event.processing_status, webhook_event.processed_at, webhook_event.error_category = PaystackWebhookEvent.PROCESSED, timezone.now(), ''
        webhook_event.save(update_fields=['processing_status', 'processed_at', 'error_category'])
        return Response(status=status.HTTP_200_OK)
