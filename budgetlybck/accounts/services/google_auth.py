"""Google Identity Services verification and safe Budgetly account linking."""

import uuid

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.utils import timezone

from accounts.models import GoogleIdentity, PolicyAcceptance
from accounts.serializers import validate_active_document_identities


class GoogleCredentialError(Exception):
    def __init__(self, code='invalid_google_credential'):
        self.code = code
        super().__init__(code)


class GoogleConfigurationError(Exception):
    pass


def _verify_with_google_auth(credential):
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token
    return id_token.verify_oauth2_token(credential, google_requests.Request(), audience=None)


def verify_google_credential(credential):
    """Return verified claims; never log or persist the short-lived credential."""
    if not settings.GOOGLE_OAUTH_CLIENT_IDS:
        raise GoogleConfigurationError()
    try:
        claims = _verify_with_google_auth(credential)
    except ImportError as error:
        # This is a deployment configuration error, not a bad user credential.
        raise GoogleConfigurationError() from error
    except Exception as error:
        # google-auth reports invalid signature, expiration and certificate
        # validation failures as ValueError. Do not leak specifics to clients.
        raise GoogleCredentialError() from error

    if claims.get('iss') not in {'accounts.google.com', 'https://accounts.google.com'}:
        raise GoogleCredentialError()
    if claims.get('aud') not in settings.GOOGLE_OAUTH_CLIENT_IDS:
        raise GoogleCredentialError()
    if claims.get('azp') and claims['azp'] not in settings.GOOGLE_OAUTH_CLIENT_IDS:
        raise GoogleCredentialError()
    subject, email = claims.get('sub'), claims.get('email')
    if not isinstance(subject, str) or not subject or len(subject) > 255:
        raise GoogleCredentialError()
    if not isinstance(email, str) or not email or claims.get('email_verified') is not True:
        raise GoogleCredentialError()
    try:
        validate_email(email)
    except ValidationError as error:
        raise GoogleCredentialError() from error
    return {'subject': subject, 'email': email.lower(), 'given_name': claims.get('given_name', ''), 'family_name': claims.get('family_name', '')}


def issue_tokens(user):
    from rest_framework_simplejwt.tokens import RefreshToken
    refresh = RefreshToken.for_user(user)
    return {'refresh': str(refresh), 'access': str(refresh.access_token)}


def _google_username():
    User = get_user_model()
    while True:
        username = f'google-{uuid.uuid4().hex[:24]}'
        if not User.objects.filter(username=username).exists():
            return username


def authenticate_or_register_google(claims, legal_identities=None):
    """Return (user, created); collision handling is deliberately explicit."""
    User = get_user_model()
    try:
        identity = GoogleIdentity.objects.select_related('user').get(subject=claims['subject'])
        if not identity.user.is_active:
            raise GoogleCredentialError('inactive_user')
        return identity.user, False
    except GoogleIdentity.DoesNotExist:
        pass

    if User.objects.filter(email__iexact=claims['email']).exists():
        raise GoogleCredentialError('link_required')
    if legal_identities is None:
        raise GoogleCredentialError('legal_acceptance_required')

    documents = validate_active_document_identities(
        legal_identities['terms_document'], legal_identities['privacy_document']
    )
    accepted_at = timezone.now()
    try:
        with transaction.atomic():
            if User.objects.filter(email__iexact=claims['email']).exists():
                raise GoogleCredentialError('link_required')
            user = User(
                username=_google_username(),
                email=claims['email'],
                first_name=claims['given_name'] if isinstance(claims['given_name'], str) else '',
                last_name=claims['family_name'] if isinstance(claims['family_name'], str) else '',
                age_eligibility_confirmed=True,
                terms_accepted_at=accepted_at,
                privacy_policy_accepted_at=accepted_at,
                accepted_policy_version=settings.LEGAL_POLICY_VERSION,
            )
            user.set_unusable_password()
            user.save()
            GoogleIdentity.objects.create(user=user, subject=claims['subject'])
            for document in documents.values():
                PolicyAcceptance.objects.create(user=user, document=document)
            return user, True
    except IntegrityError as error:
        try:
            return GoogleIdentity.objects.select_related('user').get(subject=claims['subject']).user, False
        except GoogleIdentity.DoesNotExist:
            raise GoogleCredentialError('link_required') from error


def link_google_identity(user, claims):
    with transaction.atomic():
        locked_user = get_user_model().objects.select_for_update().get(pk=user.pk)
        if not locked_user.is_active:
            raise GoogleCredentialError('inactive_user')
        try:
            identity = GoogleIdentity.objects.select_for_update().get(subject=claims['subject'])
        except GoogleIdentity.DoesNotExist:
            if GoogleIdentity.objects.select_for_update().filter(user=locked_user).exists():
                raise GoogleCredentialError('user_already_linked')
            try:
                identity = GoogleIdentity.objects.create(user=locked_user, subject=claims['subject'])
                return identity, True
            except IntegrityError as error:
                identity = GoogleIdentity.objects.select_for_update().get(subject=claims['subject'])
                if identity.user_id != locked_user.pk:
                    raise GoogleCredentialError('identity_in_use') from error
                return identity, False
        if identity.user_id != locked_user.pk:
            raise GoogleCredentialError('identity_in_use')
        return identity, False
