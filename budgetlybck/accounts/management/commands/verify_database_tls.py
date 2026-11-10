"""Read-only confirmation that the active MariaDB/MySQL connection uses TLS."""

from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from budgetlybck.database_tls import VERIFY_IDENTITY


class Command(BaseCommand):
    help = 'Confirm that the configured MariaDB/MySQL connection negotiated verified TLS.'

    def handle(self, *args, **options):
        if connection.vendor != 'mysql':
            raise CommandError('Database TLS verification is available only for MariaDB/MySQL connections.')

        connection_options = connection.settings_dict.get('OPTIONS', {})
        if connection_options.get('ssl_mode') != VERIFY_IDENTITY:
            raise CommandError(
                'Database TLS is not configured for strict certificate and host identity verification.'
            )

        with connection.cursor() as cursor:
            cursor.execute("SHOW SESSION STATUS LIKE 'Ssl_cipher'")
            cipher_row = cursor.fetchone()
            cursor.execute("SHOW SESSION STATUS LIKE 'Ssl_version'")
            version_row = cursor.fetchone()

        cipher = cipher_row[1] if cipher_row and len(cipher_row) > 1 else ''
        version = version_row[1] if version_row and len(version_row) > 1 else ''
        if not cipher:
            raise CommandError('TLS was required but the active database connection did not negotiate a cipher.')

        self.stdout.write(self.style.SUCCESS('Database TLS verification passed.'))
        self.stdout.write(f'SSL cipher: {cipher}')
        self.stdout.write(f'SSL version: {version or "not reported"}')
