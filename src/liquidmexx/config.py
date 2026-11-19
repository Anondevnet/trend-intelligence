"""Typed loaders for the non-secret YAML configuration in ``config/``."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

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

