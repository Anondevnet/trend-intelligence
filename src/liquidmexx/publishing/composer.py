"""Draft composer: turns measured trend activity into post drafts for the approval queue.

Offline and deterministic (no LLM, no network). It only ever *creates drafts*: it cannot approve
or publish anything, and the quality gate runs on every draft before it is stored.

What goes into a draft
  * Facts come only from our own stored data: mention counts and growth from the trend linker,
    and likes/reposts/replies on public Mastodon posts about the same topic (Mastodon stands in
    while the X read API is off).
  * Style borrows from what is getting engagement: the opening shape (question, number-first or
    plain statement) of the most-engaged recent posts on that topic. Wording is never copied.
  * Every draft cites stored raw items as its sources and is stamped "verified" only because the
    numbers are our own measurements. No advice, predictions, links, @mentions or $cashtags.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from statistics import median
from typing import Any

from liquidmexx.config import PublishingConfig
from liquidmexx.publishing.gate import run_gate
from liquidmexx.publishing.queue import create_draft
from liquidmexx.timeutil import now_iso, parse_iso, to_iso

SOCIAL_SOURCE = "mastodon"
ACTOR = "system:composer"
RISING = ("Accelerating", "Emerging")
MIN_MENTIONS = 5          # volume route: mentions in 24h
MIN_SOCIAL_POSTS = 3      # social route: posts about the topic in 24h
MIN_SOCIAL_ENGAGEMENT = 25


@dataclass
class Exemplar:
    raw_item_id: int
    engagement: int
    style: str
    length: int
    url: str | None


@dataclass
class Composed:
    trend_id: int
    slug: str
    name: str
    text: str
    style: str
    sources: list[dict[str, Any]]
    basis: dict[str, Any]
    score: float = 0.0
    blocked: list[str] = field(default_factory=list)


def hook_style(text: str) -> str:
    """The opening shape of a post: question, number-first or plain statement."""
    t = text.strip()
    head = t[:80]
    if "?" in head:
        return "question"
    if re.match(r"^\W*\d", t):
        return "number"
    return "statement"


def social_posts(conn: sqlite3.Connection, as_of: str) -> list[dict[str, Any]]:
    """Mastodon posts published in the last 24h with their latest known engagement."""
    since = to_iso(parse_iso(as_of) - timedelta(hours=24))
    posts = {f"{SOCIAL_SOURCE}:{r['external_id']}": dict(r) for r in conn.execute(
        "SELECT id, external_id, title, body, url, published_at FROM raw_items"
        " WHERE source_id=? AND published_at>? AND available_at<=?",
        (SOCIAL_SOURCE, since, as_of))}
    latest: dict[tuple[str, str], tuple[str, float]] = {}
    for r in conn.execute(
            "SELECT subject_key, metric, value, available_at FROM observations"
            " WHERE source_id=? AND metric IN ('likes','reposts','replies') AND available_at<=?"
            " AND available_at>=?", (SOCIAL_SOURCE, as_of, since)):
        k = (r["subject_key"], r["metric"])
        if r["subject_key"] in posts and (k not in latest or r["available_at"] > latest[k][0]):
            latest[k] = (r["available_at"], float(r["value"]))
    eng: dict[str, float] = defaultdict(float)
    for (key, _m), (_t, v) in latest.items():
        eng[key] += v
    for key, p in posts.items():
        p["engagement"] = int(eng.get(key, 0))
    return list(posts.values())


def _growth_phrase(g: float | None) -> str:
    if g is None:
        return "new in the last day"
    return f"{g:+.0%} vs the day before"


def _facts(name: str, vol: dict[str, Any] | None, soc: dict[str, Any] | None) -> list[str]:
    parts: list[str] = []
    if soc:
        parts.append(f"{soc['engagement']:,} likes, reposts and replies on {soc['posts']} public"
                     " Mastodon posts in 24h")
    if vol:
        parts.append(f"{vol['n24']} mentions across {vol['sources']} sources"
                     f" ({_growth_phrase(vol['growth'])})")
    return parts


def render_text(style: str, name: str, facts: Sequence[str]) -> str:
    body = "; ".join(facts)
    if style == "question":
        return f"What's driving {name}? {body[0].upper() + body[1:]}."
    if style == "number":
        return f"{body[0].upper() + body[1:]}: {name}."
    return f"{name}: {body}."


def _trend_info(conn: sqlite3.Connection) -> dict[int, sqlite3.Row]:
    return {int(r["id"]): r for r in conn.execute(
        "SELECT id, slug, name, kind, category FROM trends WHERE kind='narrative'")}


def compose(conn: sqlite3.Connection, cfg: PublishingConfig, velocities: Sequence[Any],
            as_of: str, *, limit: int = 3) -> list[Composed]:
    """Candidate drafts, best first. Does not write anything."""
    from liquidmexx.trends.identity import load_rules, match_text

    info = _trend_info(conn)
    by_slug = {v.slug: v for v in velocities}
    rules = load_rules(conn)
    per_trend: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for p in social_posts(conn, as_of):
        text = " \n".join(filter(None, [p["title"], p["body"]]))
        for tid in match_text(text, rules):
            if tid in info:
                per_trend[tid].append(p)
    recent = to_iso(parse_iso(as_of) - timedelta(hours=24))
    out: list[Composed] = []
    for tid, t in info.items():
        v = by_slug.get(t["slug"])
        posts = sorted(per_trend.get(tid, []), key=lambda p: -p["engagement"])
        vol = None
        if v is not None and v.stage in RISING and v.n24 >= MIN_MENTIONS:
            vol = {"n24": v.n24, "sources": v.sources, "growth": v.g24h, "stage": v.stage}
        eng = sum(p["engagement"] for p in posts)
        soc = None
        if len(posts) >= MIN_SOCIAL_POSTS and eng >= MIN_SOCIAL_ENGAGEMENT:
            soc = {"posts": len(posts), "engagement": eng}
        if vol is None and soc is None:
            continue
        if conn.execute("SELECT 1 FROM post_drafts WHERE trend_id=? AND created_at>?",
                        (tid, recent)).fetchone():
            continue  # already drafted today
        top = posts[:5]
        ex = [Exemplar(p["id"], p["engagement"], hook_style(p["title"] or ""),
                       len(p["title"] or ""), p["url"]) for p in top]
        weights: Counter[str] = Counter()
        for e in ex:
            weights[e.style] += max(e.engagement, 1)
        style = weights.most_common(1)[0][0] if weights else "statement"
        facts = _facts(t["name"], vol, soc)
        text = render_text(style, t["name"], facts)
        if len(text) > cfg.max_chars:  # drop the weaker clause rather than truncating mid-word
            text = render_text(style, t["name"], facts[:1])
        sources: list[dict[str, Any]] = [{"raw_item_id": int(p["id"])} for p in top[:2]]
        if not sources:
            row = conn.execute(
                "SELECT m.raw_item_id FROM trend_mentions m WHERE m.trend_id=?"
                " AND m.available_at>? ORDER BY m.available_at DESC LIMIT 2", (tid, recent))
            sources = [{"raw_item_id": int(r["raw_item_id"])} for r in row]
        basis = {
            "style": style, "style_votes": dict(weights),
            "exemplars": [{"engagement": e.engagement, "style": e.style, "chars": e.length,
                           "url": e.url} for e in ex],
            "exemplar_median_chars": int(median(e.length for e in ex)) if ex else None,
            "volume": vol, "social": soc,
            "note": "style borrowed from engaged posts; no wording copied; Mastodon stands in"
                    " for X while the X read API is off"}
        score = (vol["n24"] if vol else 0) + (soc["engagement"] if soc else 0)
        out.append(Composed(tid, t["slug"], t["name"], text, style, sources, basis, float(score)))
    out.sort(key=lambda c: (-c.score, c.slug))
    ready: list[Composed] = []
    for c in out:
        gate = run_gate(conn, cfg, text=c.text, sources=c.sources, claim_status="verified",
                        sponsored=False, now=as_of)
        if gate.passed:
            ready.append(c)
        else:
            c.blocked = gate.reasons
    return ready[:limit] if limit else ready


def save(conn: sqlite3.Connection, cfg: PublishingConfig, drafts: Sequence[Composed],
         now: str | None = None) -> list[int]:
    """Store drafts in the approval queue (status pending_review). Never approves."""
    ids = []
    for c in drafts:
        draft_id, _gate = create_draft(
            conn, cfg, text=c.text, sources=c.sources, claim_status="verified", actor=ACTOR,
            trend_id=c.trend_id, meta={"basis": c.basis}, now=now or now_iso())
        ids.append(draft_id)
    return ids


def basis_of(meta_json: str | None) -> dict[str, Any]:
    try:
        return dict(json.loads(meta_json or "{}").get("basis") or {})
    except (ValueError, AttributeError):
        return {}
