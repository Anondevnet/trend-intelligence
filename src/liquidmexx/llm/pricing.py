"""Token cost from config/pricing.yaml (no hard-coded prices)."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

MTOK = 1_000_000


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0            # uncached input
    output_tokens: int = 0
    cache_write_tokens: int = 0      # cache_creation_input_tokens
    cache_read_tokens: int = 0       # cache_read_input_tokens

    @property
    def prompt_tokens(self) -> int:
        return self.input_tokens + self.cache_write_tokens + self.cache_read_tokens

    @classmethod
    def from_api(cls, usage: Mapping[str, Any] | None) -> Usage:
        u = usage or {}
        return cls(int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0),
                   int(u.get("cache_creation_input_tokens") or 0),
                   int(u.get("cache_read_input_tokens") or 0))


def _rates(pricing: Mapping[str, Any], model: str, prompt_tokens: int) -> Mapping[str, Any]:
    anth = pricing["anthropic"]
    long = anth.get("long_prompt", {}).get(model)
    if long and prompt_tokens > int(long["threshold_tokens"]):
        rates: Mapping[str, Any] = long
        return rates
    if model not in anth["usd_per_million_tokens"]:
        raise KeyError(f"no price for model {model!r} in pricing.yaml")
    rates = anth["usd_per_million_tokens"][model]
    return rates


def cost_usd(pricing: Mapping[str, Any], model: str, usage: Usage, *, batch: bool = False,
             cache_ttl: str = "5m") -> float:
    r = _rates(pricing, model, usage.prompt_tokens)
    write_rate = float(r["cache_write_1h" if cache_ttl == "1h" else "cache_write_5m"])
    total = (usage.input_tokens * float(r["input"]) + usage.output_tokens * float(r["output"])
             + usage.cache_write_tokens * write_rate
             + usage.cache_read_tokens * float(r["cache_read"])) / MTOK
    if batch:
        total *= float(pricing["anthropic"]["batch_discount"])
    return round(total, 8)


def estimate_tokens(text: str) -> int:
    """Deliberately high token estimate (~3 chars/token) for pre-call budget checks."""
    return math.ceil(len(text) / 3) + 16


def worst_case_cost(pricing: Mapping[str, Any], model: str, prompt_chars: int, max_tokens: int,
                    *, batch: bool = False, cache_ttl: str = "5m") -> float:
    """Upper bound before a call: whole prompt billed at the dearer of input or cache write,
    and every allowed output token used."""
    prompt = math.ceil(prompt_chars / 3) + 16
    r = _rates(pricing, model, prompt)
    write_rate = float(r["cache_write_1h" if cache_ttl == "1h" else "cache_write_5m"])
    per_in = max(float(r["input"]), write_rate)
    total = (prompt * per_in + max_tokens * float(r["output"])) / MTOK
    if batch:
        total *= float(pricing["anthropic"]["batch_discount"])
    return round(total, 8)
