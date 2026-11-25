"""models.yaml routing is config-driven and cross-checked against pricing and budgets."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from trend_intelligence.config import load_budgets, load_models, load_pricing

CONFIG = Path(__file__).resolve().parents[1] / "config"


def _copy(tmp_path: Path) -> Path:
    for name in ("models.yaml", "pricing.yaml", "budgets.yaml"):
        shutil.copy(CONFIG / name, tmp_path / name)
    return tmp_path


def _edit(path: Path, fn) -> None:  # type: ignore[no-untyped-def]
    data = yaml.safe_load(path.read_text())
    fn(data)
    path.write_text(yaml.safe_dump(data))


def test_shipped_config_routes_as_approved() -> None:
    m = load_models(CONFIG)
    assert m.model_for("entity_extraction") == "claude-haiku-5-5"
    assert m.model_for("post_draft") == "claude-sonnet-5-5"
    assert m.model_for("safety_gate") == "claude-opus-5-5"
    assert m.tasks["deep_research"].max_per_day == 3
    assert m.escalation.on_schema_failure == {"haiku": "sonnet"}
    for task in ("scoring", "z_scores", "backtesting", "alias_dedup"):
        with pytest.raises(ValueError, match="deterministic"):
            m.model_for(task)


def test_tier_caps_fit_provider_cap() -> None:
    a = load_budgets(CONFIG).providers["anthropic"]
    assert set(a.tiers) == {"haiku", "sonnet", "opus"}
    assert sum(a.tiers.values()) <= a.daily_cap_usd


def test_every_tier_model_is_priced() -> None:
    prices = load_pricing(CONFIG)["anthropic"]["usd_per_million_tokens"]
    for model_id in load_models(CONFIG).tiers.values():
        assert {"input", "output", "cache_read"} <= set(prices[model_id])


def test_unpriced_model_rejected(tmp_path: Path) -> None:
    d = _copy(tmp_path)
    _edit(d / "models.yaml", lambda c: c["tiers"].update(opus="claude-unknown"))
    with pytest.raises(ValueError, match="no price"):
        load_models(d)


def test_missing_tier_cap_rejected(tmp_path: Path) -> None:
    d = _copy(tmp_path)
    _edit(d / "budgets.yaml", lambda c: c["providers"]["anthropic"]["tiers"].pop("opus"))
    with pytest.raises(ValueError, match="no daily cap"):
        load_models(d)


def test_tier_caps_over_provider_cap_rejected(tmp_path: Path) -> None:
    d = _copy(tmp_path)
    _edit(d / "budgets.yaml", lambda c: c["providers"]["anthropic"]["tiers"].update(opus=5.0))
    with pytest.raises(ValueError, match="sum to more"):
        load_budgets(d)


def test_cannot_disable_never_autopost(tmp_path: Path) -> None:
    d = _copy(tmp_path)
    _edit(d / "models.yaml",
          lambda c: c["escalation"].update(never_autopost_after_failure=False))
    with pytest.raises(ValueError, match="must stay true"):
        load_models(d)


def test_unknown_tier_route_rejected(tmp_path: Path) -> None:
    d = _copy(tmp_path)
    _edit(d / "models.yaml", lambda c: c["tasks"]["post_draft"].update(tier="gpt"))
    with pytest.raises(ValueError, match="unknown tier"):
        load_models(d)
