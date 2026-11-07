from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone
import hashlib

class User(AbstractUser):
    email = models.EmailField(unique=True)
    age_eligibility_confirmed = models.BooleanField(default=False)
    terms_accepted_at = models.DateTimeField(null=True, blank=True)
    privacy_policy_accepted_at = models.DateTimeField(null=True, blank=True)
    accepted_policy_version = models.CharField(max_length=50, blank=True)

    def __str__(self):
        return self.username


class PolicyDocument(models.Model):
    """An immutable, versioned Terms or Privacy document once accepted."""

    TERMS = 'terms'
    PRIVACY = 'privacy'
    DOCUMENT_TYPES = [(TERMS, 'Terms of Service'), (PRIVACY, 'Privacy Policy')]

    document_type = models.CharField(max_length=16, choices=DOCUMENT_TYPES)
    version = models.CharField(max_length=50)
    content = models.TextField()
    content_sha256 = models.CharField(max_length=64, editable=False)
    is_active = models.BooleanField(default=False)
    published_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['document_type', 'version'], name='unique_policy_document_version'),
            models.UniqueConstraint(
                fields=['document_type'],
                condition=Q(is_active=True),
                name='one_active_policy_document_per_type',
            ),
        ]
        ordering = ['document_type', '-created_at']

    def save(self, *args, **kwargs):
        if self.pk:
            previous = type(self).objects.get(pk=self.pk)
            if PolicyAcceptance.objects.filter(document_id=self.pk).exists() and any(
                getattr(previous, field) != getattr(self, field)
                for field in ('document_type', 'version', 'content')
            ):
                raise ValidationError('Accepted policy documents cannot be changed.')

        self.content_sha256 = hashlib.sha256(self.content.encode('utf-8')).hexdigest()
        if self.is_active and self.published_at is None:
            self.published_at = timezone.now()
        super().save(*args, **kwargs)

    @classmethod
    def active_documents(cls):
        documents = {document.document_type: document for document in cls.objects.filter(is_active=True)}
        if not documents:
            return None
        required = {cls.TERMS, cls.PRIVACY}
        if set(documents) != required:
            raise ValidationError('Both an active Terms document and an active Privacy document are required.')
        return documents

    def __str__(self):
        return f'{self.get_document_type_display()} v{self.version}'


class PolicyAcceptance(models.Model):
    """Evidence of the exact immutable document accepted by an active user."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='policy_acceptances')
    document = models.ForeignKey(PolicyDocument, on_delete=models.PROTECT, related_name='acceptances')
    accepted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['user', 'document'], name='unique_user_policy_acceptance'),
        ]
        ordering = ['-accepted_at']
