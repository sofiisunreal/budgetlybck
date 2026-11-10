from io import StringIO
from unittest.mock import MagicMock, patch

from django.core.exceptions import ImproperlyConfigured
from django.core.management import CommandError, call_command
from django.test import SimpleTestCase

from budgetlybck.database_tls import VERIFY_IDENTITY, database_tls_options


class DatabaseTLSConfigurationTests(SimpleTestCase):
    def test_tls_is_optional_for_local_development(self):
        self.assertEqual(
            database_tls_options(
                enabled=False, engine='django.db.backends.sqlite3', ca_path='', host='',
            ),
            {},
        )

    def test_enabled_tls_requires_a_mysql_ca_bundle_and_host(self):
        with self.assertRaisesMessage(ImproperlyConfigured, 'CA_PATH'):
            database_tls_options(
                enabled=True, engine='django.db.backends.mysql', ca_path='', host='mysql.example.test',
            )
        with self.assertRaisesMessage(ImproperlyConfigured, 'MySQL backend'):
            database_tls_options(
                enabled=True, engine='django.db.backends.sqlite3', ca_path=__file__, host='localhost',
            )

    def test_enabled_tls_uses_ca_and_server_identity_verification(self):
        options = database_tls_options(
            enabled=True, engine='django.db.backends.mysql', ca_path=__file__, host='mysql.example.test',
        )

        self.assertEqual(options['ssl_mode'], VERIFY_IDENTITY)
        self.assertEqual(options['ssl']['ca'], __file__)


class DatabaseTLSVerificationCommandTests(SimpleTestCase):
    def mysql_connection(self, *, cipher='TLS_AES_256_GCM_SHA384', mode=VERIFY_IDENTITY):
        database = MagicMock()
        database.vendor = 'mysql'
        database.settings_dict = {'OPTIONS': {'ssl_mode': mode, 'ssl': {'ca': '/safe/ca.pem'}}}
        cursor = database.cursor.return_value.__enter__.return_value
        cursor.fetchone.side_effect = [('Ssl_cipher', cipher), ('Ssl_version', 'TLSv1.3')]
        return database

    @patch('accounts.management.commands.verify_database_tls.connection')
    def test_command_reports_negotiated_tls_without_database_details(self, connection):
        connection.configure_mock(**self.mysql_connection().__dict__)
        output = StringIO()

        call_command('verify_database_tls', stdout=output)

        self.assertIn('Database TLS verification passed.', output.getvalue())
        self.assertIn('TLS_AES_256_GCM_SHA384', output.getvalue())

    @patch('accounts.management.commands.verify_database_tls.connection')
    def test_command_fails_when_strict_tls_is_not_configured(self, connection):
        connection.configure_mock(**self.mysql_connection(mode='REQUIRED').__dict__)

        with self.assertRaisesMessage(CommandError, 'strict certificate'):
            call_command('verify_database_tls')

    @patch('accounts.management.commands.verify_database_tls.connection')
    def test_command_fails_when_no_tls_cipher_is_negotiated(self, connection):
        connection.configure_mock(**self.mysql_connection(cipher='').__dict__)

        with self.assertRaisesMessage(CommandError, 'did not negotiate a cipher'):
            call_command('verify_database_tls')
