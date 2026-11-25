"""Label every piece of evidence: measured, market odds, reported, discussion or rumor.

``claim_status`` uses the same vocabulary as drafts and the publishing gate. Only a measured
value (what a source's API reported at a given time) is ``verified``; a headline or a post is
``speculative`` until something measured backs it, and rumor wording makes it a ``rumor``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

MEASURED = "measured"
MARKET = "market_odds"
REPORTED = "reported"
DISCUSSION = "discussion"
RUMOR = "rumor"
UNCLASSIFIED = "unclassified"

CLAIM_STATUS = {
    MEASURED: "verified",
    MARKET: "speculative",
    REPORTED: "speculative",
    DISCUSSION: "speculative",
    RUMOR: "rumor",
    UNCLASSIFIED: "speculative",
}


@dataclass(frozen=True)
class Label:
    evidence_class: str
    claim_status: str


@lru_cache(maxsize=32)
def _rumor_re(terms: tuple[str, ...]) -> re.Pattern[str] | None:
    if not terms:
        return None
    alts = "|".join(r"\s+".join(map(re.escape, t.split())) for t in terms)
    return re.compile(rf"(?<![\w-])(?:{alts})(?![\w-])", re.IGNORECASE)


def is_rumor(text: str, cfg: Mapping[str, Any]) -> bool:
    pattern = _rumor_re(tuple(str(t) for t in cfg["evidence"].get("rumor_terms", [])))
    return bool(pattern and pattern.search(text))


def classify(source_id: str, text: str | None, cfg: Mapping[str, Any]) -> Label:
    """Evidence class of an item from ``source_id``; ``cfg`` is the whole intel.yaml."""
    ev = cfg["evidence"]
    if source_id in ev["measured_sources"]:
        cls = MEASURED
    elif source_id in ev["market_sources"]:
        cls = MARKET
    elif source_id in ev["reported_sources"]:
        cls = REPORTED
    elif source_id in ev["discussion_sources"]:
        cls = DISCUSSION
    else:
        cls = UNCLASSIFIED
    if cls in (REPORTED, DISCUSSION, UNCLASSIFIED) and text and is_rumor(text, cfg):
        cls = RUMOR
    return Label(cls, CLAIM_STATUS[cls])
