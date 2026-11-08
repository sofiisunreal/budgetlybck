from django.contrib.auth import get_user_model
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from rest_framework import serializers

from .models import PolicyAcceptance, PolicyDocument


User = get_user_model()


class PolicyDocumentSerializer(serializers.ModelSerializer):
    effective_at = serializers.DateTimeField(source='published_at', read_only=True)

    class Meta:
        model = PolicyDocument
        fields = ['id', 'document_type', 'version', 'content', 'content_sha256', 'published_at', 'effective_at']
        read_only_fields = fields


class PolicyDocumentIdentitySerializer(serializers.Serializer):
    id = serializers.IntegerField(min_value=1)
    version = serializers.CharField(max_length=50)
    content_sha256 = serializers.RegexField(regex=r'^[0-9a-f]{64}$')


def validate_active_document_identities(terms_identity, privacy_identity):
    """Return the active exact documents, rejecting stale client identities."""
    try:
        documents = PolicyDocument.active_documents()
    except ValidationError as error:
        raise serializers.ValidationError({'legal_documents': error.messages})

    if documents is None:
        raise serializers.ValidationError({
            'legal_documents': 'Legal documents are not currently available for acceptance.'
        })

    submitted = {
        PolicyDocument.TERMS: terms_identity,
        PolicyDocument.PRIVACY: privacy_identity,
    }
    errors = {}
    for document_type, document in documents.items():
        identity = submitted[document_type]
        if any(identity[key] != getattr(document, key) for key in ('id', 'version', 'content_sha256')):
            errors[f'{document_type}_document'] = (
                'Document identity must match the currently active document.'
            )
    if errors:
        raise serializers.ValidationError(errors)
    return documents


class RegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(
        write_only=True,
        min_length=8
    )

    password_confirm = serializers.CharField(
        write_only=True
    )
    age_eligibility = serializers.BooleanField(write_only=True, required=True)
    accept_terms = serializers.BooleanField(write_only=True, required=True)
    accept_privacy_policy = serializers.BooleanField(write_only=True, required=True)
    terms_document = PolicyDocumentIdentitySerializer(write_only=True, required=False)
    privacy_document = PolicyDocumentIdentitySerializer(write_only=True, required=False)

    class Meta:
        model = User
        fields = [
            'username',
            'email',
            'password',
            'password_confirm',
            'age_eligibility',
            'accept_terms',
            'accept_privacy_policy',
            'terms_document',
            'privacy_document',
            'first_name',
            'last_name',
        ]

    def validate(self, data):
        if data['password'] != data['password_confirm']:
            raise serializers.ValidationError({
                'password': 'Passwords do not match.'
            })

        declarations = {
            'age_eligibility': 'You must confirm that you are 18 years of age or older.',
            'accept_terms': 'You must accept the Terms of Service.',
            'accept_privacy_policy': 'You must accept the Privacy Policy.',
        }
        errors = {
            field: message for field, message in declarations.items()
            if data.get(field) is not True
        }
        if errors:
            raise serializers.ValidationError(errors)

        required_documents = {
            'terms_document': data.get('terms_document'),
            'privacy_document': data.get('privacy_document'),
        }
        missing = {
            field: 'This field is required.' for field, identity in required_documents.items() if identity is None
        }
        if missing:
            raise serializers.ValidationError(missing)

        self.active_policy_documents = validate_active_document_identities(
            required_documents['terms_document'], required_documents['privacy_document']
        )

        return data

    def create(self, validated_data):
        validated_data.pop('password_confirm')
        validated_data.pop('age_eligibility')
        validated_data.pop('accept_terms')
        validated_data.pop('accept_privacy_policy')
        validated_data.pop('terms_document')
        validated_data.pop('privacy_document')
        accepted_at = timezone.now()

        with transaction.atomic():
            user = User.objects.create_user(
                username=validated_data['username'],
                email=validated_data['email'],
                password=validated_data['password'],
                first_name=validated_data.get('first_name', ''),
                last_name=validated_data.get('last_name', ''),
                age_eligibility_confirmed=True,
                terms_accepted_at=accepted_at,
                privacy_policy_accepted_at=accepted_at,
                accepted_policy_version=settings.LEGAL_POLICY_VERSION,
            )
            for document in self.active_policy_documents.values():
                PolicyAcceptance.objects.create(user=user, document=document)

        return user


class PolicyAcceptanceSerializer(serializers.Serializer):
    terms_document = PolicyDocumentIdentitySerializer(write_only=True)
    privacy_document = PolicyDocumentIdentitySerializer(write_only=True)

    def validate(self, attrs):
        self.documents = validate_active_document_identities(
            attrs['terms_document'], attrs['privacy_document']
        )
        return attrs


class UserSerializer(serializers.ModelSerializer):

    class Meta:
        model = User
        fields = [
            'id',
            'username',
            'email',
            'first_name',
            'last_name',
        ]

class ChangePasswordSerializer(serializers.Serializer):
    old_password = serializers.CharField(
        write_only=True,
        required=True
    )
    new_password = serializers.CharField(
        write_only=True,
        min_length=8,
        required=True
    )
    confirm_password = serializers.CharField(
        write_only=True,
        required=True
    )
    def validate(self, data):
        if data['new_password'] != data['confirm_password']:
            raise serializers.ValidationError({
                'confirm_password': 'New passwords do not match.'
            })
        return data

class ForgotPasswordSerializer(serializers.Serializer):
    email = serializers.EmailField()

class ResetPasswordSerializer(serializers.Serializer):
    new_password = serializers.CharField(
        write_only=True,
        min_length=8,
        required=True
    )

    confirm_password = serializers.CharField(
        write_only=True,
        required=True
    )

    def validate(self, data):
        if data['new_password'] != data['confirm_password']:
            raise serializers.ValidationError({
                'confirm_password': 'Passwords do not match.'
            })

        return data

class DeleteAccountSerializer(serializers.Serializer):
    password = serializers.CharField(
        write_only=True,
        required=True
    )
