"""Fail-closed TLS configuration for mysqlclient database connections."""

from pathlib import Path

from django.core.exceptions import ImproperlyConfigured


MYSQL_ENGINE = 'django.db.backends.mysql'
VERIFY_IDENTITY = 'VERIFY_IDENTITY'


def database_tls_options(*, enabled, engine, ca_path, host):
    """Return mysqlclient TLS options, rejecting incomplete TLS requests.

    ``VERIFY_IDENTITY`` verifies both the certificate chain and the database
    host name. It is deliberately not possible to enable TLS without a CA
    bundle: a failed TLS setup must never become a plaintext connection.
    """
    if not enabled:
        return {}
    if engine != MYSQL_ENGINE:
        raise ImproperlyConfigured('Database TLS is supported only with the Django MySQL backend.')
    if not host:
        raise ImproperlyConfigured('DJANGO_DB_HOST is required when database TLS is enabled.')
    if not ca_path:
        raise ImproperlyConfigured('DJANGO_DB_TLS_CA_PATH is required when database TLS is enabled.')

    certificate = Path(ca_path).expanduser()
    try:
        with certificate.open('rb') as certificate_file:
            if not certificate_file.read(1):
                raise OSError
    except OSError as error:
        raise ImproperlyConfigured(
            'DJANGO_DB_TLS_CA_PATH must reference a readable, non-empty CA certificate bundle.'
        ) from error

    return {
        'ssl': {'ca': str(certificate)},
        'ssl_mode': VERIFY_IDENTITY,
    }
