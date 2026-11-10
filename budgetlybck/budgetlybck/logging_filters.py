"""Small defensive redaction layer for application console logs."""

import logging
import re


class SensitiveDataFilter(logging.Filter):
    """Redact common credential assignments without retaining raw values."""

    _assignment = re.compile(
        r'(?i)\b(password|access[_-]?token|refresh[_-]?token|token|authorization|api[_-]?key|client[_-]?secret|secret|pin)\s*([=:])\s*(?:Bearer\s+)?[^\s,;]+'
    )

    @classmethod
    def _redact(cls, value):
        if isinstance(value, str):
            return cls._assignment.sub(r'\1\2[REDACTED]', value)
        if isinstance(value, dict):
            return {
                key: '[REDACTED]' if str(key).lower() in {
                    'password', 'access_token', 'refresh_token', 'token',
                    'authorization', 'api_key', 'client_secret', 'secret', 'pin',
                } else cls._redact(item)
                for key, item in value.items()
            }
        if isinstance(value, tuple):
            return tuple(cls._redact(item) for item in value)
        return value

    def filter(self, record):
        record.msg = self._redact(record.msg)
        record.args = self._redact(record.args)
        return True
