"""Safely import an exact Terms/Privacy pair and optionally activate it together."""

from __future__ import annotations

import hashlib
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from accounts.models import PolicyDocument


class Command(BaseCommand):
    help = (
        "Import exact, immutable Terms and Privacy markdown files. "
        "Documents stay inactive unless --activate --confirm-activate ACTIVATE is supplied."
    )

    def add_arguments(self, parser):
        parser.add_argument("--terms-file", required=True, help="UTF-8 Terms of Service markdown file.")
        parser.add_argument("--privacy-file", required=True, help="UTF-8 Privacy Policy markdown file.")
        parser.add_argument(
            "--policy-version",
            dest="policy_version",
            required=True,
            help="The shared release version, for example 1.0.",
        )
        parser.add_argument(
            "--effective-at",
            required=True,
            help="ISO-8601 publication/effective timestamp, for example 2026-12-01T00:00:00Z.",
        )
        parser.add_argument("--dry-run", action="store_true", help="Validate and report without writing records.")
        parser.add_argument("--activate", action="store_true", help="Activate this complete pair in the same transaction.")
        parser.add_argument(
            "--confirm-activate",
            default="",
            help="Must be the literal value ACTIVATE when --activate is used.",
        )

    @staticmethod
    def read_document(path_option: str, label: str) -> str:
        path = Path(path_option)
        if not path.is_file():
            raise CommandError(f"{label} file does not exist or is not a file: {path}")
        try:
            # Preserve the authored line endings. The frontend verifies the hash
            # of the exact string returned by the API, so normalizing CRLF to LF
            # here would create a different legal document.
            with path.open("r", encoding="utf-8", newline="") as source:
                content = source.read()
        except UnicodeDecodeError as error:
            raise CommandError(f"{label} file must be UTF-8 encoded.") from error
        if not content.strip():
            raise CommandError(f"{label} file is empty.")
        return content

    @staticmethod
    def parse_effective_at(value: str):
        parsed = parse_datetime(value)
        if parsed is None:
            raise CommandError("--effective-at must be a valid ISO-8601 timestamp with a timezone.")
        if timezone.is_naive(parsed):
            raise CommandError("--effective-at must include a timezone, for example Z.")
        return parsed

    def handle(self, *args, **options):
        if options["activate"] and options["confirm_activate"] != "ACTIVATE":
            raise CommandError("Activation requires --activate --confirm-activate ACTIVATE.")

        effective_at = self.parse_effective_at(options["effective_at"])
        if options["activate"] and effective_at > timezone.now():
            raise CommandError(
                "A future-effective policy pair may be imported inactive, but cannot be activated early. "
                "Run activation at or after --effective-at."
            )
        documents = {
            PolicyDocument.TERMS: self.read_document(options["terms_file"], "Terms"),
            PolicyDocument.PRIVACY: self.read_document(options["privacy_file"], "Privacy"),
        }
        hashes = {document_type: hashlib.sha256(content.encode("utf-8")).hexdigest()
                  for document_type, content in documents.items()}
        existing = {
            document_type: PolicyDocument.objects.filter(
                document_type=document_type, version=options["policy_version"]
            ).first()
            for document_type in documents
        }
        for document_type, record in existing.items():
            if record and record.content_sha256 != hashes[document_type]:
                raise CommandError(
                    f"Refusing to overwrite existing {document_type} version {options['policy_version']}: "
                    "its content hash differs. Publish a new version instead."
                )

        action = "would import"
        if options["activate"]:
            action += " and activate"
        self.stdout.write(f"{action.title()} Terms and Privacy version {options['policy_version']} at {effective_at.isoformat()}.")
        for document_type in (PolicyDocument.TERMS, PolicyDocument.PRIVACY):
            state = "existing" if existing[document_type] else "new"
            self.stdout.write(f"  {document_type}: {state}, sha256={hashes[document_type]}")
        if options["dry_run"]:
            self.stdout.write(self.style.WARNING("Dry run: no policy records were changed."))
            return

        with transaction.atomic():
            records = {}
            for document_type, content in documents.items():
                record = existing[document_type]
                if record is None:
                    record = PolicyDocument.objects.create(
                        document_type=document_type,
                        version=options["policy_version"],
                        content=content,
                        published_at=effective_at,
                    )
                records[document_type] = record

            if options["activate"]:
                # Deactivation and activation are one database transaction, so callers
                # never observe only one half of an intended document release.
                PolicyDocument.objects.filter(is_active=True).exclude(
                    pk__in=[record.pk for record in records.values()]
                ).update(is_active=False)
                PolicyDocument.objects.filter(pk__in=[record.pk for record in records.values()]).update(
                    is_active=True,
                    published_at=effective_at,
                )

        self.stdout.write(self.style.SUCCESS(
            "Policy pair activated." if options["activate"] else "Policy pair imported and remains inactive."
        ))
