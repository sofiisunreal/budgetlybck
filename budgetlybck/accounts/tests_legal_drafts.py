from io import StringIO
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.test import TestCase

from .models import PolicyDocument


class LegalDocumentDraftTests(TestCase):
    def test_v11_drafts_contain_the_approved_launch_disclosures(self):
        drafts = Path(settings.BASE_DIR) / 'legal_documents' / 'drafts'
        terms = (drafts / 'terms-of-service-v1.1.md').read_text(encoding='utf-8')
        privacy = (drafts / 'privacy-policy-v1.1.md').read_text(encoding='utf-8')

        self.assertIn('**Effective date:** November 12, 2026', terms)
        self.assertIn('KES 1,050, paid once through Paystack', terms)
        self.assertIn('does not impose recurring charges or renewals', terms)
        self.assertIn('total customer payment charged by Budgetly is KES 1,050', terms)
        self.assertIn('does not add any additional checkout charges', terms)
        self.assertIn('Voluntary refund requests are considered individually', terms)
        self.assertIn('as long as Budgetly continues to make the service available', terms)
        self.assertIn('Google Sign-In', privacy)
        self.assertIn('Paystack', privacy)
        self.assertIn('payment-reconciliation evidence with the deleted account link removed', privacy)

    def test_v11_drafts_validate_as_an_inactive_pair(self):
        drafts = Path(settings.BASE_DIR) / 'legal_documents' / 'drafts'
        output = StringIO()

        call_command(
            'publish_policy_documents',
            '--terms-file', str(drafts / 'terms-of-service-v1.1.md'),
            '--privacy-file', str(drafts / 'privacy-policy-v1.1.md'),
            '--policy-version', '1.1',
            '--effective-at', '2026-11-11T21:00:00Z',
            '--dry-run',
            stdout=output,
        )

        self.assertIn('Would Import Terms and Privacy version 1.1', output.getvalue())
        self.assertIn('Dry run: no policy records were changed.', output.getvalue())
        self.assertFalse(PolicyDocument.objects.filter(version='1.1').exists())
