"""Point-in-time readers for Stage 7: metric movers and new listings.

Every query filters on ``available_at <= as_of``. A mover compares the freshest reading known
at ``as_of`` with the reading known at ``as_of - lookback``. Stale readings are not used.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from trend_intelligence.timeutil import parse_iso, to_iso


@dataclass(frozen=True)
class Mover:
    source_id: str
    subject_type: str
    subject_key: str
    metric: str
    title: str
    url: str | None
    now: float
    now_at: str
    then: float | None = None
    then_at: str | None = None
    raw_item_id: int | None = None

    @property
    def change(self) -> float | None:
        return None if self.then is None else self.now - self.then

    @property
    def pct(self) -> float | None:
        if self.then is None or self.then <= 0:
            return None
        return (self.now - self.then) / self.then


def shift(as_of: str, hours: float) -> str:
    return to_iso(parse_iso(as_of) - timedelta(hours=hours))


def metric_movers(conn: sqlite3.Connection, source_id: str, subject_type: str, metric: str,
                  as_of: str, cfg: Mapping[str, Any]) -> list[Mover]:
    """Every subject with a fresh reading at ``as_of``; ``then`` is None without history."""
    lookback, gap = float(cfg["lookback_hours"]), float(cfg["max_gap_hours"])
    t_then = shift(as_of, lookback)
    fresh_from = shift(as_of, gap)
    rows = conn.execute(
        "SELECT o.subject_key, o.value, o.observed_at, o.available_at, o.raw_item_id,"
        " r.title, r.url FROM observations o LEFT JOIN raw_items r ON r.id = o.raw_item_id"
        " WHERE o.source_id=? AND o.subject_type=? AND o.metric=? AND o.available_at<=?"
        " AND o.observed_at>=? ORDER BY o.observed_at, o.id",
        (source_id, subject_type, metric, as_of, shift(as_of, lookback + gap)))
    now: dict[str, sqlite3.Row] = {}
    then: dict[str, sqlite3.Row] = {}
    for r in rows:
        key = r["subject_key"]
        if r["observed_at"] >= fresh_from:
            now[key] = r
        if r["available_at"] <= t_then:
            then[key] = r
    out = []
    for key in sorted(now):
        n, t = now[key], then.get(key)
        out.append(Mover(
            source_id, subject_type, key, metric, n["title"] or key, n["url"],
            float(n["value"]), n["observed_at"],
            float(t["value"]) if t is not None else None, t["observed_at"] if t else None,
            int(n["raw_item_id"]) if n["raw_item_id"] is not None else None))
    return out


def new_items(conn: sqlite3.Connection, source_id: str, item_type: str | None, as_of: str,
              window_hours: float) -> list[sqlite3.Row]:
    """Live items first seen in (as_of - window, as_of]; first-run catalogs never count."""
    sql = ("SELECT * FROM v_raw_items WHERE source_id=? AND is_initial_snapshot=0"
           " AND is_backfill=0 AND available_at<=? AND available_at>?")
    params: list[Any] = [source_id, as_of, shift(as_of, window_hours)]
    if item_type is not None:
        sql += " AND item_type=?"
        params.append(item_type)
    return list(conn.execute(sql + " ORDER BY available_at DESC, id DESC", params))


def keyword_re(words: Iterable[str]) -> re.Pattern[str] | None:
    alts = "|".join(re.escape(str(w)) for w in words)
    return re.compile(rf"(?<![\w-])(?:{alts})(?![\w-])", re.IGNORECASE) if alts else None


def matches(pattern: re.Pattern[str] | None, *texts: str | None) -> bool:
    if pattern is None:
        return True
    return any(t and pattern.search(t) for t in texts)


def clone_farm_names(conn: sqlite3.Connection, as_of: str, min_owners: int) -> set[str]:
    """Repo names used by ``min_owners``+ owners among repos known by ``as_of``."""
    return {r[0] for r in conn.execute(
        "SELECT lower(substr(external_id, instr(external_id,'/')+1)) AS name"
        " FROM raw_items WHERE source_id='github' AND item_type='repo' AND available_at<=?"
        " GROUP BY name HAVING COUNT(DISTINCT lower(substr(external_id,1,"
        " instr(external_id,'/')-1))) >= ?", (as_of, min_owners))}


def data_watermark(conn: sqlite3.Connection, as_of: str) -> str | None:
    row = conn.execute(
        "SELECT MAX(a) FROM (SELECT MAX(available_at) a FROM observations WHERE available_at<=?"
        " UNION ALL SELECT MAX(available_at) FROM raw_items WHERE available_at<=?)",
        (as_of, as_of)).fetchone()
    return row[0] if row else None
