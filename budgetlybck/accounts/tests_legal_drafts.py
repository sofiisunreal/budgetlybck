from io import StringIO
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.test import TestCase

from .models import PolicyDocument


class LegalDocumentDraftTests(TestCase):
    def test_v11_drafts_validate_as_an_inactive_pair(self):
        drafts = Path(settings.BASE_DIR) / 'legal_documents' / 'drafts'
        output = StringIO()

        call_command(
            'publish_policy_documents',
            '--terms-file', str(drafts / 'terms-of-service-v1.1.md'),
            '--privacy-file', str(drafts / 'privacy-policy-v1.1.md'),
            '--policy-version', '1.1',
            '--effective-at', '2026-11-11T00:00:00Z',
            '--dry-run',
            stdout=output,
        )

        self.assertIn('Would Import Terms and Privacy version 1.1', output.getvalue())
        self.assertIn('Dry run: no policy records were changed.', output.getvalue())
        self.assertFalse(PolicyDocument.objects.filter(version='1.1').exists())
