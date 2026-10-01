from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping

from app.config.models import AppConfig
from app.config.secrets import collect_secret_values

REDACTED = "[REDACTED]"
SENSITIVE_HEADER_NAMES = (
    "authorization",
    "proxy-authorization",
    "x-api-key",
    "x-goog-api-key",
    "api-key",
    "api_key",
    "apikey",
)

_HEADER_RE = re.compile(
    r"""(?P<prefix>['"]?\b(?:"""
    + "|".join(re.escape(n) for n in SENSITIVE_HEADER_NAMES)
    + r""")['"]?\s*[:=]\s*['"]?\s*(?:(?:Bearer|Basic)\s+)?)"""
    + r"""(?!\[REDACTED\]|(?:Bearer|Basic)\s)(?P<value>[^\s'",;&}\]]+)""",
    re.IGNORECASE,
)
_QUERY_RE = re.compile(r"""([?&](?:key|api_key|apikey)=)[^&\s'"#]+""", re.IGNORECASE)


class SecretRedactor:
    def __init__(self, secrets: Iterable[str] = (), *, min_length: int = 6) -> None:
        self._min_length = min_length
        self._secrets: tuple[str, ...] = ()
        self.add(*secrets)

    def add(self, *secrets: str) -> None:
        new = {s for s in secrets if len(s.strip()) >= self._min_length}
        merged = set(self._secrets) | new
        self._secrets = tuple(sorted(merged, key=lambda s: (-len(s), s)))

    def redact(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, REDACTED)
        text = _HEADER_RE.sub(lambda m: m.group("prefix") + REDACTED, text)
        return _QUERY_RE.sub(lambda m: m.group(1) + REDACTED, text)

    def __repr__(self) -> str:
        return f"SecretRedactor(<{len(self._secrets)} secrets>)"

    @classmethod
    def from_config(
        cls, config: AppConfig, environ: Mapping[str, str] | None = None
    ) -> SecretRedactor:
        return cls(collect_secret_values(config, environ))


class RedactingFilter(logging.Filter):
    def __init__(self, redactor: SecretRedactor) -> None:
        super().__init__()
        self._redactor = redactor

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            msg = str(record.msg)
        record.msg = self._redactor.redact(msg)
        record.args = ()
        if record.exc_info:
            record.exc_text = self._redactor.redact(
                logging.Formatter().formatException(record.exc_info)
            )
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = self._redactor.redact(record.exc_text)
        if record.stack_info:
            record.stack_info = self._redactor.redact(record.stack_info)
        return True


def install_redaction(
    redactor: SecretRedactor, *, logger: logging.Logger | None = None
) -> RedactingFilter:
    target = logger if logger is not None else logging.getLogger()
    flt = RedactingFilter(redactor)
    for handler in target.handlers:
        if not any(isinstance(f, RedactingFilter) for f in handler.filters):
            handler.addFilter(flt)
    return flt
