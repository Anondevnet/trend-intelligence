"""Typed loaders for the non-secret YAML configuration in ``config/``."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, model_validator

from liquidmexx.settings import get_settings


class SourceConfig(BaseModel):
    enabled: bool = False
    interval_minutes: int = Field(default=60, ge=1)
    credibility: float = Field(default=0.5, ge=0.0, le=1.0)
    params: dict[str, Any] = Field(default_factory=dict)


class ProviderBudget(BaseModel):
    daily_cap_usd: float = Field(ge=0.0)
    warn_fraction: float = Field(default=0.8, gt=0.0, le=1.0)
    initial_credit_usd: float | None = None
    tiers: dict[str, float] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _tiers_within_cap(self) -> ProviderBudget:
        if any(v < 0 for v in self.tiers.values()):
            raise ValueError("tier caps must be >= 0")
        if sum(self.tiers.values()) > self.daily_cap_usd + 1e-9:
            raise ValueError("tier caps sum to more than the provider daily_cap_usd")
        return self


class BudgetsConfig(BaseModel):
    providers: dict[str, ProviderBudget]


def _load_yaml(name: str, config_dir: Path | None = None) -> dict[str, Any]:
    base = config_dir or get_settings().config_dir
    path = base / name
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a mapping")
    return data


def load_sources(config_dir: Path | None = None) -> dict[str, SourceConfig]:
    raw = _load_yaml("sources.yaml", config_dir)
    return {name: SourceConfig.model_validate(block or {}) for name, block in raw.items()}


def load_budgets(config_dir: Path | None = None) -> BudgetsConfig:
    return BudgetsConfig.model_validate(_load_yaml("budgets.yaml", config_dir))


def load_pricing(config_dir: Path | None = None) -> dict[str, Any]:
    return _load_yaml("pricing.yaml", config_dir)


def load_taxonomy(config_dir: Path | None = None) -> dict[str, Any]:
    return _load_yaml("taxonomy.yaml", config_dir)


class TaskRoute(BaseModel):
    tier: str
    schema_: bool = Field(default=True, alias="schema")
    batch: bool = False
    cache_system_prompt: bool = True
    max_per_day: int | None = Field(default=None, ge=1)


class Escalation(BaseModel):
    on_schema_failure: dict[str, str] = Field(default_factory=dict)
    max_retries: int = Field(default=1, ge=0, le=3)
    after_retries: str = "flag_for_review"
    never_autopost_after_failure: bool = True

    @model_validator(mode="after")
    def _never_autopost(self) -> Escalation:
        if not self.never_autopost_after_failure:
            raise ValueError("never_autopost_after_failure must stay true")
        return self


class ModelsConfig(BaseModel):
    provider: str
    live_calls: bool = False
    max_tokens: dict[str, int] = Field(default_factory=dict)
    tiers: dict[str, str]
    no_llm: list[str] = Field(default_factory=list)
    tasks: dict[str, TaskRoute]
    escalation: Escalation = Field(default_factory=Escalation)
    caching: dict[str, Any] = Field(default_factory=dict)
    batch: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _consistent(self) -> ModelsConfig:
        for name, route in self.tasks.items():
            if route.tier not in self.tiers:
                raise ValueError(f"task {name!r} routes to unknown tier {route.tier!r}")
        overlap = set(self.no_llm) & set(self.tasks)
        if overlap:
            raise ValueError(f"tasks marked no_llm cannot be routed to a model: {sorted(overlap)}")
        missing = sorted(set(self.tiers) - set(self.max_tokens))
        if self.max_tokens and missing:
            raise ValueError(f"max_tokens missing for tiers {missing}")
        if any(v < 1 for v in self.max_tokens.values()):
            raise ValueError("max_tokens must be >= 1")
        for src, dst in self.escalation.on_schema_failure.items():
            if src not in self.tiers or dst not in self.tiers:
                raise ValueError(f"escalation {src}->{dst} names an unknown tier")
        return self

    def model_for(self, task: str) -> str:
        """Model ID for a task; raises for no-LLM or unknown tasks."""
        if task in self.no_llm:
            raise ValueError(f"task {task!r} is deterministic and must not call an LLM")
        if task not in self.tasks:
            raise KeyError(f"no model route for task {task!r}")
        return self.tiers[self.tasks[task].tier]


def load_models(config_dir: Path | None = None) -> ModelsConfig:
    """Load models.yaml and cross-check it against pricing.yaml and budgets.yaml."""
    models = ModelsConfig.model_validate(_load_yaml("models.yaml", config_dir))
    prices = load_pricing(config_dir).get(models.provider, {}).get("usd_per_million_tokens", {})
    for tier, model_id in models.tiers.items():
        if model_id not in prices:
            raise ValueError(f"tier {tier!r} model {model_id!r} has no price in pricing.yaml")
    budget = load_budgets(config_dir).providers.get(models.provider)
    if budget is None:
        raise ValueError(f"no budget for provider {models.provider!r}")
    missing = sorted(set(models.tiers) - set(budget.tiers))
    if missing:
        raise ValueError(f"no daily cap in budgets.yaml for tiers {missing}")
    return models


def load_scoring(config_dir: Path | None = None) -> dict[str, Any]:
    """Load scoring.yaml; weights must cover the four sub-scores and sum to 1."""
    cfg = _load_yaml("scoring.yaml", config_dir)
    weights = cfg.get("weights", {})
    expected = {"acceleration", "volume", "breadth", "novelty"}
    if set(weights) != expected:
        raise ValueError(f"scoring weights must be exactly {sorted(expected)}")
    if abs(sum(float(v) for v in weights.values()) - 1.0) > 1e-9:
        raise ValueError("scoring weights must sum to 1")
    return cfg


def source_credibility(config_dir: Path | None = None) -> dict[str, float]:
    return {name: s.credibility for name, s in load_sources(config_dir).items()}


def load_backtest(config_dir: Path | None = None) -> dict[str, Any]:
    cfg = _load_yaml("backtest.yaml", config_dir)
    for key in ("horizons_days", "label", "top_k", "walk_forward", "eval_hour_utc", "step_days",
                "random_seed"):
        if key not in cfg:
            raise ValueError(f"backtest.yaml is missing {key!r}")
    if not 0 <= int(cfg["eval_hour_utc"]) <= 23:
        raise ValueError("eval_hour_utc must be 0-23")
    return cfg


class PublishingConfig(BaseModel):
    """Stage 10 posting safeguards (config/publishing.yaml). Defaults are the safe ones."""

    mode: Literal["off", "dry_run", "live"] = "off"
    kill_switch: bool = False
    require_human_approval: bool = True
    approval_ttl_hours: float = Field(default=24, gt=0, le=168)
    max_posts_per_day: int = Field(default=3, ge=0, le=20)
    max_chars: int = Field(default=280, ge=1, le=280)
    min_sources: int = Field(default=1, ge=1)
    allow_urls: bool = False
    allow_mentions: bool = False
    allow_cashtags: bool = False
    duplicate_window_days: int = Field(default=30, ge=0)
    near_duplicate_jaccard: float = Field(default=0.8, gt=0, le=1)
    x_cost_usd: dict[str, float] = Field(
        default_factory=lambda: {"post": 0.015, "post_with_url": 0.20})
    sponsored_disclosures: list[str] = Field(min_length=1)
    uncertainty_labels: list[str] = Field(min_length=1)
    financial_advice_patterns: list[str] = Field(min_length=1)
    price_prediction_patterns: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def _safe(self) -> PublishingConfig:
        if not self.require_human_approval:
            raise ValueError("require_human_approval must stay true")
        if set(self.x_cost_usd) != {"post", "post_with_url"}:
            raise ValueError("x_cost_usd needs exactly 'post' and 'post_with_url'")
        if any(v <= 0 for v in self.x_cost_usd.values()):
            raise ValueError("x_cost_usd values must be > 0")
        for pattern in self.financial_advice_patterns + self.price_prediction_patterns:
            re.compile(pattern, re.IGNORECASE)
        return self


def load_publishing(config_dir: Path | None = None) -> PublishingConfig:
    return PublishingConfig.model_validate(_load_yaml("publishing.yaml", config_dir))


def load_detectors(config_dir: Path | None = None) -> dict[str, Any]:
    """Load detectors.yaml (Stage 6) and check the parts the detectors rely on."""
    cfg = _load_yaml("detectors.yaml", config_dir)
    for key in ("window_hours", "hotlist", "signal_sum", "ensemble", "comparison"):
        if key not in cfg:
            raise ValueError(f"detectors.yaml is missing {key!r}")
    w = cfg["hotlist"]["weights"]
    if set(w) != {"prominence", "frequency", "hotness"}:
        raise ValueError("hotlist weights must be prominence, frequency and hotness")
    if abs(sum(float(v) for v in w.values()) - 1.0) > 1e-9:
        raise ValueError("hotlist weights must sum to 1")
    if "default" not in cfg["signal_sum"]["points"]:
        raise ValueError("signal_sum.points needs a 'default' rule")
    for src, rule in cfg["signal_sum"]["points"].items():
        if ("flat" in rule) == ("tiers" in rule):
            raise ValueError(f"signal_sum rule {src!r} needs exactly one of flat/tiers")
        tiers = rule.get("tiers", [])
        if [float(t[0]) for t in tiers] != sorted((float(t[0]) for t in tiers), reverse=True):
            raise ValueError(f"signal_sum rule {src!r}: tiers must be in descending order")
        if tiers and ("metric" not in rule or float(tiers[-1][0]) != 0.0):
            raise ValueError(f"signal_sum rule {src!r}: tiers need a metric and a 0 floor")
    if not cfg["ensemble"]["members"]:
        raise ValueError("ensemble needs at least one member")
    return cfg
