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


def get_config():
    if settings.PAYSTACK_MODE not in {'test', 'live'}:
        raise PaystackConfigurationError('Paystack mode is invalid.')
    expected_key_prefix = f'sk_{settings.PAYSTACK_MODE}_'
    if not settings.PAYSTACK_SECRET_KEY or not settings.PAYSTACK_SECRET_KEY.startswith(expected_key_prefix):
        raise PaystackConfigurationError('A matching Paystack secret key is required.')
    if not settings.PAYSTACK_WEBHOOK_SIGNING_SECRET:
        raise PaystackConfigurationError('A Paystack webhook signing secret is required.')
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
    return PaystackConfig(secret_key=settings.PAYSTACK_SECRET_KEY, callback_url=settings.PAYSTACK_CALLBACK_URL)


def _request(method, path, *, payload=None):
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
    try:
        body = response.json()
    except ValueError as error:
        logger.warning('Paystack returned malformed JSON', extra={'operation': path, 'status_code': response.status_code})
        raise PaystackResponseError('Paystack returned an invalid response.') from error
    if not response.ok or body.get('status') is not True or not isinstance(body.get('data'), dict):
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


def payload_digest(raw_body):
    return hashlib.sha256(raw_body).hexdigest()


def _timestamped_signature_valid(raw_body, header):
    secrets = [value for value in (settings.PAYSTACK_WEBHOOK_SIGNING_SECRET, settings.PAYSTACK_WEBHOOK_PREVIOUS_SIGNING_SECRET) if value]
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
    if not legacy or not settings.PAYSTACK_SECRET_KEY:
        return False
    expected = hmac.new(settings.PAYSTACK_SECRET_KEY.encode('utf-8'), raw_body, hashlib.sha512).hexdigest()
    return hmac.compare_digest(expected, legacy)
