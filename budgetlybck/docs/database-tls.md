# Django to MariaDB TLS

Budgetly uses mysqlclient's `VERIFY_IDENTITY` mode when database TLS is enabled.
That mode validates the certificate chain against a trusted CA bundle and checks
that the certificate is valid for `DJANGO_DB_HOST`.

## Required production environment

Set these website-environment values only after placing a trusted, readable CA
bundle outside the repository:

```text
DJANGO_DB_TLS_ENABLED=true
DJANGO_DB_TLS_CA_PATH=/home/<alwaysdata-account>/certs/alwaysdata-mariadb-ca.pem
```

The CA file must be a non-empty PEM CA bundle trusted for the MariaDB endpoint.
Obtain it from AlwaysData's documented database TLS material or support; do not
disable verification and do not substitute an unverified server certificate.
The configured `DJANGO_DB_HOST` must remain the hostname covered by that
certificate (for example, the `mysql-<account>.alwaysdata.net` endpoint shown
in the AlwaysData database panel).

After restarting the website, run from SSH:

```text
python manage.py verify_database_tls
```

It performs only `SHOW SESSION STATUS` reads and succeeds only when strict TLS
is configured and the active connection reports a cipher.

## Local development and disposable test databases

Leave `DJANGO_DB_TLS_ENABLED` unset or `false` for local MariaDB and SQLite
test databases that do not have a trusted TLS setup. Enabling it without a
readable CA file, host, or MySQL backend stops Django at startup; it never
falls back to plaintext.

## Rollback

If the staged TLS configuration fails before it is verified, set
`DJANGO_DB_TLS_ENABLED=false` and restart the website to return to the prior
connection behavior. Investigate and correct the CA/hostname mismatch before
re-enabling it. This rollback affects only database transport security; it does
not change schema or data.
