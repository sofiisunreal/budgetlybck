from django.contrib.auth import get_user_model
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from rest_framework import serializers

from .models import PolicyAcceptance, PolicyDocument


User = get_user_model()


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

        # The document registry is opt-in during rollout: existing
        # registration remains compatible until an operator stores and
        # activates both actual documents. A partial activation is rejected.
        try:
            self.active_policy_documents = PolicyDocument.active_documents()
        except ValidationError as error:
            raise serializers.ValidationError({'non_field_errors': error.messages})

        return data

    def create(self, validated_data):
        validated_data.pop('password_confirm')
        validated_data.pop('age_eligibility')
        validated_data.pop('accept_terms')
        validated_data.pop('accept_privacy_policy')
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
            for document in (getattr(self, 'active_policy_documents', None) or {}).values():
                PolicyAcceptance.objects.create(user=user, document=document)

        return user


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
