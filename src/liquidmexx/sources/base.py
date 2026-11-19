"""Source adapter interface."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from html import unescape
from typing import Any, ClassVar

from liquidmexx.settings import Settings
from liquidmexx.sources.http import PoliteClient
from liquidmexx.store import Observation, RawItem, Store


@dataclass
class CollectResult:
    items: list[RawItem] = field(default_factory=list)
    observations: list[Observation] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass
class CollectContext:
    client: PoliteClient
    store: Store
    settings: Settings
    params: dict[str, Any]
    fetched_at: str
    last_success_at: str | None


class SourceAdapter(ABC):
    """One external provider. Must be read-only, HTTPS-only and rate-limit aware."""

    id: ClassVar[str]
    kind: ClassVar[str]
    min_interval_s: ClassVar[float] = 1.0

    def availability(self, settings: Settings) -> tuple[bool, str]:
        """(usable, reason). Override when credentials are required."""
        return True, "no credentials required"

    def client_headers(self, settings: Settings) -> dict[str, str]:
        return {}

    def pacing(self, settings: Settings) -> float:
        return self.min_interval_s

    @abstractmethod
    def collect(self, ctx: CollectContext) -> CollectResult: ...


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def strip_html(text: str | None, limit: int = 4000) -> str:
    if not text:
        return ""
    return _WS_RE.sub(" ", unescape(_TAG_RE.sub(" ", text))).strip()[:limit]
