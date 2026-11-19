"""Trend identity: seed narratives, entity trends, and item -> trend linking.

Linking is deterministic and versioned (``LINKER_VERSION``). Mentions inherit the
item's ``available_at`` so point-in-time queries stay leak-free.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Any

from liquidmexx.timeutil import now_iso

LINKER_VERSION = "v1"

_GITHUB_URL = re.compile(r"github\.com/([A-Za-z0-9_.\-]+)/([A-Za-z0-9_.\-]+)", re.I)
_HF_URL = re.compile(r"huggingface\.co/(?!spaces/|datasets/|blog/|docs/|papers/)"
                     r"([A-Za-z0-9_.\-]+)/([A-Za-z0-9_.\-]+)", re.I)
_GITHUB_RESERVED = {"orgs", "topics", "trending", "features", "about", "sponsors", "marketplace",
                    "settings", "login", "collections", "apps", "enterprise", "pricing", "site"}

# Sources whose items are discussion/coverage (can confirm an entity from another platform).
DISCUSSION_SOURCES = {"hackernews", "reddit", "rss"}


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:80]


@dataclass(frozen=True)
class AliasRule:
    alias_id: int
    trend_id: int
    pattern: re.Pattern[str]
    context: tuple[str, ...]


def seed_taxonomy(conn: sqlite3.Connection, taxonomy: dict[str, Any]) -> int:
    """Insert narratives/aliases from taxonomy.yaml. Additive and idempotent."""
    added = 0
    ts = now_iso()
    for category, narratives in (taxonomy.get("categories") or {}).items():
        for n in narratives:
            slug = f"{category}/{slugify(n['name'])}"
            conn.execute(
                "INSERT OR IGNORE INTO trends(slug, name, category, kind, origin, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (slug, n["name"], category, "narrative", "taxonomy", ts),
            )
            trend_id = conn.execute("SELECT id FROM trends WHERE slug=?", (slug,)).fetchone()["id"]
            ctx = json.dumps([normalize(c) for c in n.get("requires_context") or []]) \
                if n.get("requires_context") else None
            rules = [(a, "phrase") for a in n.get("aliases") or []]
            rules += [(a, "exact") for a in n.get("exact") or []]
            for alias, mode in rules:
                norm = normalize(alias) if mode == "phrase" else alias
                cur = conn.execute(
                    "INSERT OR IGNORE INTO trend_aliases(trend_id, alias, alias_norm, match_mode,"
                    " requires_context, created_at) VALUES (?,?,?,?,?,?)",
                    (trend_id, alias, norm, mode, ctx, ts),
                )
                added += cur.rowcount
    return added


def _compile(alias_norm: str, mode: str) -> re.Pattern[str]:
    body = re.escape(alias_norm).replace(r"\ ", r"[\s\-]+")
    pattern = rf"(?<![A-Za-z0-9]){body}(?![A-Za-z0-9])"
    return re.compile(pattern, 0 if mode == "exact" else re.IGNORECASE)


def load_rules(conn: sqlite3.Connection) -> list[AliasRule]:
    rules: list[AliasRule] = []
    for row in conn.execute(
        "SELECT id, trend_id, alias_norm, match_mode, requires_context FROM trend_aliases"
        " WHERE match_mode IN ('phrase','exact')"
    ):
        ctx = tuple(json.loads(row["requires_context"])) if row["requires_context"] else ()
        rules.append(AliasRule(row["id"], row["trend_id"],
                               _compile(row["alias_norm"], row["match_mode"]), ctx))
    return rules


def _has_context(text_lower: str, context: tuple[str, ...]) -> bool:
    return any(re.search(rf"(?<![a-z0-9]){re.escape(c)}(?![a-z0-9])", text_lower)
               for c in context)


def match_text(text: str, rules: list[AliasRule]) -> dict[int, int]:
    """Return {trend_id: alias_id} for every rule that matches ``text``."""
    hits: dict[int, int] = {}
    lower = text.lower()
    for rule in rules:
        if rule.trend_id in hits:
            continue
        if rule.pattern.search(text) and (not rule.context or _has_context(lower, rule.context)):
            hits[rule.trend_id] = rule.alias_id
    return hits


def extract_entity_refs(text: str) -> list[tuple[str, str]]:
    """(subject_type, subject_key) for GitHub repos / HF models referenced by URL."""
    refs: list[tuple[str, str]] = []
    for owner, name in _GITHUB_URL.findall(text):
        name = re.sub(r"\.git$", "", name)
        if owner.lower() in _GITHUB_RESERVED or not name or name.startswith("."):
            continue
        refs.append(("repo", f"{owner}/{name}".lower()))
    for owner, name in _HF_URL.findall(text):
        refs.append(("model", f"{owner}/{name}".lower()))
    return list(dict.fromkeys(refs))


def _entity_trend(conn: sqlite3.Connection, subject_type: str, subject_key: str) -> int:
    """Get or create an entity trend for a repo/model referenced by URL.

    Created even if the entity has not been collected yet: a discussion link is
    itself evidence, and the entity's metrics can be measured later.
    """
    src = {"repo": "github", "model": "huggingface"}[subject_type]
    slug = f"entity/{subject_type}/{subject_key}"
    conn.execute(
        "INSERT OR IGNORE INTO trends(slug, name, category, kind, origin, subject_type,"
        " subject_key, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (slug, subject_key, "entity", "entity", src, subject_type, subject_key, now_iso()),
    )
    return int(conn.execute("SELECT id FROM trends WHERE slug=?", (slug,)).fetchone()["id"])


def link_new_items(conn: sqlite3.Connection, batch: int = 5000) -> int:
    """Link raw items not yet processed by this linker version. Returns mentions added."""
    state = conn.execute("SELECT last_raw_item_id FROM linker_state WHERE linker_version=?",
                         (LINKER_VERSION,)).fetchone()
    last_id = int(state["last_raw_item_id"]) if state else 0
    rules = load_rules(conn)
    added = 0
    while True:
        rows = conn.execute(
            "SELECT id, source_id, title, body, url, available_at FROM raw_items"
            " WHERE id > ? ORDER BY id LIMIT ?", (last_id, batch),
        ).fetchall()
        if not rows:
            break
        conn.execute("BEGIN IMMEDIATE")
        try:
            ts = now_iso()
            for r in rows:
                text = " \n".join(filter(None, [r["title"], r["body"]]))
                found = match_text(text, rules)
                if r["source_id"] in DISCUSSION_SOURCES:
                    for stype, skey in extract_entity_refs(" ".join(filter(None, [r["url"],
                                                                                  text]))):
                        found.setdefault(_entity_trend(conn, stype, skey), 0)
                for tid, alias_id in found.items():
                    cur = conn.execute(
                        "INSERT OR IGNORE INTO trend_mentions(trend_id, raw_item_id, source_id,"
                        " alias_id, available_at, linked_at, linker_version)"
                        " VALUES (?,?,?,?,?,?,?)",
                        (tid, r["id"], r["source_id"], alias_id or None, r["available_at"], ts,
                         LINKER_VERSION),
                    )
                    if cur.rowcount:
                        added += 1
                        conn.execute(
                            "UPDATE trends SET first_detected_at=? WHERE id=? AND"
                            " (first_detected_at IS NULL OR first_detected_at > ?)",
                            (r["available_at"], tid, r["available_at"]),
                        )
            last_id = int(rows[-1]["id"])
            conn.execute(
                "INSERT INTO linker_state(linker_version, last_raw_item_id, updated_at)"
                " VALUES (?,?,?) ON CONFLICT(linker_version) DO UPDATE SET"
                " last_raw_item_id=excluded.last_raw_item_id, updated_at=excluded.updated_at",
                (LINKER_VERSION, last_id, ts),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return added
