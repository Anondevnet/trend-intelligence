"""Data-access layer: runs, raw items, append-only observations, point-in-time reads."""

from __future__ import annotations

import gzip
import hashlib
import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from liquidmexx.timeutil import normalize_iso, now_iso


@dataclass(frozen=True)
class RawItem:
    item_type: str
    external_id: str
    title: str | None = None
    url: str | None = None
    body: str | None = None
    author: str | None = None
    published_at: str | None = None
    payload: dict[str, Any] | None = None


@dataclass(frozen=True)
class Observation:
    subject_type: str
    subject_key: str
    metric: str
    value: float
    observed_at: str | None = None   # defaults to the fetch time
    available_at: str | None = None  # defaults to observed_at (live)
    item_ref: tuple[str, str] | None = None  # (item_type, external_id) of a RawItem


@dataclass
class RunStats:
    requests_made: int = 0
    items_seen: int = 0
    items_new: int = 0
    observations_written: int = 0
    errors: list[str] = field(default_factory=list)


def content_hash(item: RawItem) -> str:
    basis = "\n".join([(item.title or "").strip().lower(), (item.url or "").strip().lower()])
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


class Store:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # ---- sources & runs -------------------------------------------------
    def ensure_source(self, source_id: str, kind: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO sources(id, kind, created_at) VALUES (?,?,?)",
            (source_id, kind, now_iso()),
        )

    def start_run(self, source_id: str, mode: str = "live") -> int:
        cur = self.conn.execute(
            "INSERT INTO collection_runs(source_id, mode, started_at) VALUES (?,?,?)",
            (source_id, mode, now_iso()),
        )
        assert cur.lastrowid is not None
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, status: str, stats: RunStats) -> None:
        error = "; ".join(stats.errors)[:2000] if stats.errors else None
        self.conn.execute(
            "UPDATE collection_runs SET finished_at=?, status=?, requests_made=?, items_seen=?,"
            " items_new=?, observations_written=?, error=? WHERE id=?",
            (now_iso(), status, stats.requests_made, stats.items_seen, stats.items_new,
             stats.observations_written, error, run_id),
        )

    def last_successful_run(self, source_id: str, mode: str = "live") -> str | None:
        row = self.conn.execute(
            "SELECT MAX(started_at) AS t FROM collection_runs"
            " WHERE source_id=? AND mode=? AND status IN ('ok','partial')",
            (source_id, mode),
        ).fetchone()
        return row["t"] if row and row["t"] else None

    # ---- raw items ---------------------------------------------------------
    def insert_raw_item(
        self,
        source_id: str,
        item: RawItem,
        run_id: int | None,
        seen_at: str,
        available_at: str | None = None,
        is_backfill: bool = False,
    ) -> tuple[int, bool]:
        """Insert-if-new. Returns (raw_item_id, is_new). Existing rows are never modified."""
        row = self.conn.execute(
            "SELECT id FROM raw_items WHERE source_id=? AND item_type=? AND external_id=?",
            (source_id, item.item_type, item.external_id),
        ).fetchone()
        if row:
            return int(row["id"]), False
        published = normalize_iso(item.published_at) if item.published_at else None
        seen_at = normalize_iso(seen_at)
        available_at = normalize_iso(available_at) if available_at else seen_at
        cur = self.conn.execute(
            "INSERT INTO raw_items(source_id, item_type, external_id, url, title, body, author,"
            " published_at, first_seen_at, available_at, is_backfill, content_hash, run_id)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (source_id, item.item_type, item.external_id, item.url, item.title,
             (item.body or "")[:20000] or None, item.author, published, seen_at,
             available_at, int(is_backfill), content_hash(item), run_id),
        )
        assert cur.lastrowid is not None
        raw_id = int(cur.lastrowid)
        if item.payload is not None:
            blob = gzip.compress(json.dumps(item.payload, default=str).encode("utf-8"))
            self.conn.execute(
                "INSERT INTO raw_payloads(raw_item_id, payload_gz, stored_at) VALUES (?,?,?)",
                (raw_id, blob, seen_at),
            )
        return raw_id, True

    def raw_item_id(self, source_id: str, item_type: str, external_id: str) -> int | None:
        row = self.conn.execute(
            "SELECT id FROM raw_items WHERE source_id=? AND item_type=? AND external_id=?",
            (source_id, item_type, external_id),
        ).fetchone()
        return int(row["id"]) if row else None

    # ---- observations --------------------------------------------------------
    def add_observations(
        self,
        source_id: str,
        observations: Iterable[Observation],
        run_id: int | None,
        fetched_at: str,
        is_backfill: bool = False,
    ) -> int:
        written = 0
        ingested = now_iso()
        for obs in observations:
            observed = normalize_iso(obs.observed_at) if obs.observed_at else fetched_at
            available = normalize_iso(obs.available_at) if obs.available_at else observed
            if available < observed:
                raise ValueError("available_at cannot precede observed_at")
            if not is_backfill and available > ingested:
                raise ValueError("live observation claims availability in the future")
            raw_id = None
            if obs.item_ref is not None:
                raw_id = self.raw_item_id(source_id, obs.item_ref[0], obs.item_ref[1])
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO observations(source_id, subject_type, subject_key, metric,"
                " value, observed_at, available_at, ingested_at, is_backfill, raw_item_id, run_id)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (source_id, obs.subject_type, obs.subject_key, obs.metric, float(obs.value),
                 observed, available, ingested, int(is_backfill), raw_id, run_id),
            )
            written += cur.rowcount
        return written

    # ---- point-in-time reads ---------------------------------------------------
    def value_as_of(
        self, subject_type: str, subject_key: str, metric: str, as_of: str,
        include_backfill: bool = True,
    ) -> float | None:
        """Latest value knowable at ``as_of`` (filters on available_at, never later data)."""
        sql = (
            "SELECT value FROM observations WHERE subject_type=? AND subject_key=? AND metric=?"
            " AND available_at <= ?"
        )
        if not include_backfill:
            sql += " AND is_backfill = 0"
        sql += " ORDER BY observed_at DESC, id DESC LIMIT 1"
        row = self.conn.execute(sql, (subject_type, subject_key, metric, as_of)).fetchone()
        return float(row["value"]) if row else None

    def series_as_of(
        self, subject_type: str, subject_key: str, metric: str, as_of: str,
        since: str | None = None,
    ) -> list[tuple[str, float]]:
        sql = (
            "SELECT observed_at, value FROM observations WHERE subject_type=? AND subject_key=?"
            " AND metric=? AND available_at <= ?"
        )
        params: list[Any] = [subject_type, subject_key, metric, as_of]
        if since:
            sql += " AND observed_at >= ?"
            params.append(since)
        sql += " ORDER BY observed_at, id"
        return [(r["observed_at"], float(r["value"])) for r in self.conn.execute(sql, params)]

    def items_available_as_of(self, as_of: str, since: str | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM raw_items WHERE available_at <= ?"
        params: list[Any] = [as_of]
        if since:
            sql += " AND available_at >= ?"
            params.append(since)
        return list(self.conn.execute(sql + " ORDER BY available_at, id", params))
