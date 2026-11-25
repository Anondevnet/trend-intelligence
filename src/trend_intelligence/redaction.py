"""Secret redaction for logs, errors and anything that leaves the process."""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable

REDACTED = "[REDACTED]"

# Well-known credential shapes. Kept conservative to avoid mangling normal text.
_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{10,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=\-]{16,}"),
    re.compile(r"(?i)(authorization|x-api-key|api[_-]?key|access[_-]?token|client[_-]?secret)"
               r"(\"?\s*[:=]\s*\"?)([^\s\"',;]{8,})"),
    re.compile(r"\bAAAAAAAAAAAAAAAAAAAA[A-Za-z0-9%]{20,}\b"),  # X app bearer tokens
)


def redact(text: str, extra_secrets: Iterable[str] = ()) -> str:
    out = text
    for secret in sorted({s for s in extra_secrets if s and len(s) >= 6}, key=len, reverse=True):
        out = out.replace(secret, REDACTED)
    for pattern in _PATTERNS:
        if pattern.groups >= 3:
            out = pattern.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", out)
        else:
            out = pattern.sub(REDACTED, out)
    return out


class RedactingFilter(logging.Filter):
    """Logging filter that scrubs configured secrets and known token shapes."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        super().__init__()
        self._secrets = [s for s in secrets if s]

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        record.msg = redact(message, self._secrets)
        record.args = None
        if record.exc_text:
            record.exc_text = redact(record.exc_text, self._secrets)
        return True


def setup_logging(level: int = logging.INFO, log_file: str | None = None) -> None:
    from trend_intelligence.settings import get_settings

    secrets = get_settings().secret_values()
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file:
        handlers.append(logging.FileHandler(log_file))
    for handler in handlers:
        handler.setFormatter(fmt)
        handler.addFilter(RedactingFilter(secrets))
        root.addHandler(handler)
    # httpx logs full URLs at INFO; keep it quiet.
    logging.getLogger("httpx").setLevel(logging.WARNING)
