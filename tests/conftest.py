from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from trend_intelligence import settings as settings_mod
from trend_intelligence.db import connect, migrate
from trend_intelligence.settings import Settings

SECRET_ENV = [
    "GITHUB_TOKEN", "REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET", "REDDIT_USER_AGENT",
    "ANTHROPIC_API_KEY", "X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN",
    "X_ACCESS_TOKEN_SECRET", "X_BEARER_TOKEN", "SOLANA_RPC_URL", "TREND_INTEL_LIVE_POSTING",
    "TREND_INTEL_LIVE_LLM",
]


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
    """Never read the real .env or real credentials; keep all data under tmp_path."""
    for name in SECRET_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TREND_INTEL_DATA_DIR", str(tmp_path / "data"))
    test_settings = Settings(_env_file=None)  # type: ignore[call-arg]
    settings_mod.get_settings.cache_clear()
    monkeypatch.setattr(settings_mod, "get_settings", lambda: test_settings)
    for mod in ("trend_intelligence.config", "trend_intelligence.collector",
                "trend_intelligence.db", "trend_intelligence.backfill",
                "trend_intelligence.redaction", "trend_intelligence.cli"):
        try:
            module = __import__(mod, fromlist=["get_settings"])
        except ImportError:
            continue
        if hasattr(module, "get_settings"):
            monkeypatch.setattr(module, "get_settings", lambda: test_settings)
    yield test_settings


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = connect(":memory:")
    migrate(c)
    yield c
    c.close()


def no_sleep(_: float) -> None:
    return None
