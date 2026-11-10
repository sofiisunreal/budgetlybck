import hashlib
import hmac
import logging
import time
from dataclasses import dataclass
from urllib.parse import urlparse

import requests
from django.conf import settings


logger = logging.getLogger(__name__)
PAYSTACK_API_URL = 'https://api.paystack.co'


class PaystackError(Exception):
    category = 'provider_error'


class PaystackConfigurationError(PaystackError):
    category = 'configuration'


class PaystackUnavailableError(PaystackError):
    category = 'unavailable'


class PaystackResponseError(PaystackError):
    category = 'invalid_response'


@dataclass(frozen=True)
class PaystackConfig:
    secret_key: str
    callback_url: str


def _active_credentials():
    if settings.PAYSTACK_MODE not in {'test', 'live'}:
        raise PaystackConfigurationError('Paystack mode is invalid.')

    if settings.PAYSTACK_MODE == 'test':
        # Preserve development compatibility while deployments adopt the
        # explicit variables.  Live mode must never use these generic values.
        secret_key = settings.PAYSTACK_TEST_SECRET_KEY or settings.PAYSTACK_SECRET_KEY
        signing_secret = (
            settings.PAYSTACK_TEST_WEBHOOK_SIGNING_SECRET
            or settings.PAYSTACK_WEBHOOK_SIGNING_SECRET
        )
        previous_signing_secret = (
            settings.PAYSTACK_TEST_WEBHOOK_PREVIOUS_SIGNING_SECRET
            or settings.PAYSTACK_WEBHOOK_PREVIOUS_SIGNING_SECRET
        )
    else:
        secret_key = settings.PAYSTACK_LIVE_SECRET_KEY
        signing_secret = settings.PAYSTACK_LIVE_WEBHOOK_SIGNING_SECRET
        previous_signing_secret = settings.PAYSTACK_LIVE_WEBHOOK_PREVIOUS_SIGNING_SECRET

    expected_key_prefix = f'sk_{settings.PAYSTACK_MODE}_'
    if not secret_key or not secret_key.startswith(expected_key_prefix):
        raise PaystackConfigurationError('A matching Paystack secret key is required.')
    if not signing_secret:
        raise PaystackConfigurationError('A Paystack webhook signing secret is required.')
    return secret_key, signing_secret, previous_signing_secret


def get_config():
    secret_key, _, _ = _active_credentials()
    parsed = urlparse(settings.PAYSTACK_CALLBACK_URL)
    if parsed.scheme not in {'http', 'https'} or not parsed.netloc:
        raise PaystackConfigurationError('A valid Paystack callback URL is required.')
    if settings.PAYSTACK_MODE == 'live' and parsed.scheme != 'https':
        raise PaystackConfigurationError('The Paystack callback URL must use HTTPS in live mode.')
    return_url = urlparse(settings.FRONTEND_PAYMENT_RETURN_URL)
    if return_url.scheme not in {'http', 'https'} or not return_url.netloc:
        raise PaystackConfigurationError('A valid frontend payment return URL is required.')
    if settings.PAYSTACK_MODE == 'live' and return_url.scheme != 'https':
        raise PaystackConfigurationError('The frontend payment return URL must use HTTPS in live mode.')
    return PaystackConfig(secret_key=secret_key, callback_url=settings.PAYSTACK_CALLBACK_URL)


def _request(method, path, *, payload=None, expected_data_type=dict):
    config = get_config()
    try:
        response = requests.request(
            method, f'{PAYSTACK_API_URL}{path}',
            headers={'Authorization': f'Bearer {config.secret_key}', 'Content-Type': 'application/json'},
            json=payload,
            timeout=(settings.PAYSTACK_CONNECT_TIMEOUT_SECONDS, settings.PAYSTACK_READ_TIMEOUT_SECONDS),
        )
    except (requests.ConnectionError, requests.Timeout) as error:
        logger.warning('Paystack request unavailable', extra={'operation': path, 'category': type(error).__name__})
        raise PaystackUnavailableError('Paystack is temporarily unavailable.') from error
    except requests.RequestException as error:
        logger.warning('Paystack request failed', extra={'operation': path, 'category': type(error).__name__})
        raise PaystackUnavailableError('Paystack is temporarily unavailable.') from error
    if response.status_code >= 500 or response.status_code == 429:
        logger.warning('Paystack service unavailable', extra={'operation': path, 'status_code': response.status_code})
        raise PaystackUnavailableError('Paystack is temporarily unavailable.')
    try:
        body = response.json()
    except ValueError as error:
        logger.warning('Paystack returned malformed JSON', extra={'operation': path, 'status_code': response.status_code})
        raise PaystackResponseError('Paystack returned an invalid response.') from error
    if not response.ok or body.get('status') is not True or not isinstance(body.get('data'), expected_data_type):
        logger.warning('Paystack rejected request', extra={'operation': path, 'status_code': response.status_code})
        raise PaystackResponseError('Paystack could not process the request.')
    return body['data']


def initialize_transaction(*, email, amount_minor, reference, callback_url, metadata):
    data = _request('POST', '/transaction/initialize', payload={
        'email': email, 'amount': str(amount_minor), 'currency': 'KES', 'reference': reference,
        'callback_url': callback_url, 'metadata': metadata,
    })
    if data.get('reference') != reference or not isinstance(data.get('authorization_url'), str):
        raise PaystackResponseError('Paystack returned an invalid checkout response.')
    return {'reference': data['reference'], 'authorization_url': data['authorization_url']}


def verify_transaction(reference):
    data = _request('GET', f'/transaction/verify/{reference}')
    if not isinstance(data.get('reference'), str) or not isinstance(data.get('status'), str):
        raise PaystackResponseError('Paystack returned an invalid verification response.')
    return data


def fetch_transaction_disputes(provider_transaction_id):
    """Return Paystack disputes for one verified provider transaction ID."""
    if not provider_transaction_id:
        raise PaystackResponseError('A provider transaction identifier is required.')
    data = _request(
        'GET',
        f'/dispute/transaction/{provider_transaction_id}',
        expected_data_type=list,
    )
    return data


def payload_digest(raw_body):
    return hashlib.sha256(raw_body).hexdigest()


def _timestamped_signature_valid(raw_body, header):
    try:
        _, signing_secret, previous_signing_secret = _active_credentials()
    except PaystackConfigurationError:
        return False
    secrets = [value for value in (signing_secret, previous_signing_secret) if value]
    if not secrets:
        return False
    timestamp_value, signatures = None, []
    for part in header.split(','):
        key, separator, value = part.strip().partition('=')
        if separator and key == 't':
            timestamp_value = value
        elif separator and key == 'v1':
            signatures.append(value)
    try:
        timestamp = int(timestamp_value)
    except (TypeError, ValueError):
        return False
    if abs(time.time() - timestamp) > settings.PAYSTACK_WEBHOOK_TOLERANCE_SECONDS:
        return False
    signed = str(timestamp).encode('ascii') + b'.' + raw_body
    for secret in secrets:
        expected = hmac.new(secret.encode('utf-8'), signed, hashlib.sha512).hexdigest()
        for supplied in signatures:
            signature = supplied.partition(':')[2] or supplied
            if hmac.compare_digest(expected, signature):
                return True
    return False


def webhook_signature_valid(raw_body, headers):
    """Validate the current signature first; legacy is used only if absent."""
    current = headers.get('Paystack-Signature') or headers.get('paystack-signature')
    if current:
        return _timestamped_signature_valid(raw_body, current)
    if not settings.PAYSTACK_ALLOW_LEGACY_WEBHOOK_SIGNATURE:
        return False
    legacy = headers.get('x-paystack-signature', '')
    try:
        secret_key, _, _ = _active_credentials()
    except PaystackConfigurationError:
        return False
    if not legacy:
        return False
    expected = hmac.new(secret_key.encode('utf-8'), raw_body, hashlib.sha512).hexdigest()
    return hmac.compare_digest(expected, legacy)
