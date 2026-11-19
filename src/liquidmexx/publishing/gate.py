"""Deterministic quality gate for post drafts (no LLM, no network).

Returns every reason a draft is not postable. An empty list means it passed.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from liquidmexx.config import PublishingConfig
from liquidmexx.timeutil import parse_iso, to_iso

CLAIM_STATUSES = ("verified", "speculative", "rumor")

_TLDS = ("com|io|xyz|org|net|ai|co|app|dev|fi|finance|gg|so|sh|me|ly|fun|trade|exchange|"
         "money|cash|link|to|tv|info|biz|news|site|online|club|pro|vip|one|lol|wtf|eth|sol")
URL_RE = re.compile(rf"(?i)\bhttps?://|\bwww\.|\b[a-z0-9-]+\.(?:{_TLDS})\b|\bt\.co/")
MENTION_RE = re.compile(r"(?<![\w@])@[A-Za-z0-9_]{1,15}\b")
CASHTAG_RE = re.compile(r"(?<![\w$])\$[A-Za-z][A-Za-z0-9_]{0,9}\b")
SOURCE_URL_RE = re.compile(r"^https?://[^\s]+$")
_WORD_RE = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True)
class GateResult:
    reasons: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.reasons


def words(text: str) -> set[str]:
    return set(_WORD_RE.findall(text.lower()))


def normalized(text: str) -> str:
    return " ".join(_WORD_RE.findall(text.lower()))


def _contains_label(text: str, labels: Sequence[str]) -> bool:
    low = text.lower()
    return any(re.search(rf"(?<!\w){re.escape(lbl.lower())}(?!\w)", low) for lbl in labels)


def check_text(text: str, cfg: PublishingConfig, *, claim_status: str,
               sponsored: bool) -> list[str]:
    reasons: list[str] = []
    if not text.strip():
        return ["empty: the post has no text"]
    if len(text) > cfg.max_chars:
        reasons.append(f"length: {len(text)} chars > {cfg.max_chars}")
    if claim_status not in CLAIM_STATUSES:
        reasons.append(f"claim_status: {claim_status!r} is not one of {CLAIM_STATUSES}")
    if not cfg.allow_urls and URL_RE.search(text):
        reasons.append("url: the text contains a link or domain (allow_urls is false)")
    if not cfg.allow_mentions and MENTION_RE.search(text):
        reasons.append("mention: the text tags an @account (allow_mentions is false)")
    if not cfg.allow_cashtags and CASHTAG_RE.search(text):
        reasons.append("cashtag: the text contains a $TICKER (allow_cashtags is false)")
    for pattern in cfg.financial_advice_patterns:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            reasons.append(f"financial_advice: matched {m.group(0)!r}")
    for pattern in cfg.price_prediction_patterns:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            reasons.append(f"price_prediction: matched {m.group(0)!r}")
    if sponsored and not any(d.lower() in text.lower() for d in cfg.sponsored_disclosures):
        reasons.append("sponsored: no disclosure tag in the text")
    if claim_status in ("speculative", "rumor") and not _contains_label(
            text, cfg.uncertainty_labels):
        reasons.append(f"uncertainty: a {claim_status} post must say so in the text")
    return reasons


def check_sources(conn: sqlite3.Connection, sources: Sequence[dict[str, Any]],
                  cfg: PublishingConfig) -> list[str]:
    reasons: list[str] = []
    distinct: set[str] = set()
    for src in sources:
        if set(src) == {"raw_item_id"} and isinstance(src["raw_item_id"], int):
            row = conn.execute("SELECT 1 FROM raw_items WHERE id=?",
                               (src["raw_item_id"],)).fetchone()
            if row is None:
                reasons.append(f"source: raw item {src['raw_item_id']} does not exist")
            else:
                distinct.add(f"raw:{src['raw_item_id']}")
        elif (set(src) == {"url"} and isinstance(src["url"], str)
              and SOURCE_URL_RE.match(src["url"])):
            distinct.add(f"url:{src['url']}")
        else:
            reasons.append(f"source: malformed entry {src!r}")
    if len(distinct) < cfg.min_sources:
        reasons.append(f"unsourced: {len(distinct)} valid source(s), need {cfg.min_sources}")
    return reasons


def check_duplicates(conn: sqlite3.Connection, text: str, cfg: PublishingConfig, *,
                     now: str, exclude_draft: int | None = None) -> list[str]:
    """Compare with posts published (dry-run or live) within the duplicate window."""
    since = to_iso(parse_iso(now) - timedelta(days=cfg.duplicate_window_days))
    rows = conn.execute(
        "SELECT d.id, d.text FROM post_events e JOIN post_drafts d ON d.id = e.draft_id"
        " WHERE e.event IN ('published_dry_run','published_live') AND e.at >= ?"
        " AND d.id IS NOT ?", (since, exclude_draft)).fetchall()
    mine_norm, mine_words = normalized(text), words(text)
    for row in rows:
        if normalized(row["text"]) == mine_norm:
            return [f"duplicate: same text as published draft {row['id']}"]
        other = words(row["text"])
        union = mine_words | other
        if union and len(mine_words & other) / len(union) >= cfg.near_duplicate_jaccard:
            return [f"near_duplicate: too similar to published draft {row['id']}"]
    return []


def run_gate(conn: sqlite3.Connection, cfg: PublishingConfig, *, text: str,
             sources: Sequence[dict[str, Any]], claim_status: str, sponsored: bool,
             now: str, draft_id: int | None = None) -> GateResult:
    reasons = check_text(text, cfg, claim_status=claim_status, sponsored=sponsored)
    reasons += check_sources(conn, sources, cfg)
    reasons += check_duplicates(conn, text, cfg, now=now, exclude_draft=draft_id)
    return GateResult(reasons)
