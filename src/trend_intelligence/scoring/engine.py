"""Stage 4 trend scoring: deterministic, point-in-time, no LLM and no network.

Every input is read from stored history filtered on ``available_at <= as_of``, so the score
for a given ``as_of`` does not change when later data arrives. The config (plus source
credibility and versions) is hashed and snapshotted with each run, so stored scores can be
recomputed and verified exactly (see :func:`verify_run`).

Sub-scores, each normalised to [0, 1]:
- acceleration: per-source z-score of the current 24h window against that source's own
  14-day rolling baseline (only sources with enough baseline coverage), combined by
  source credibility. Sources are never compared against each other's history.
- volume:       credibility- and quality-weighted mentions in the window.
- breadth:      distinct sources with mentions in the window (cross-platform confirmation).
- novelty:      age of the first eligible mention (only when that source had coverage before
                it), scaled by window support so a single mention is not a trend.

Exclusions/discounts: first-run catalog items (baseline), HN live stories not yet at the
backfill's points threshold, and GitHub clone-farm repos (as known at ``as_of``).
"""

from __future__ import annotations

import bisect
import hashlib
import json
import math
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from trend_intelligence.timeutil import normalize_iso, now_iso, parse_iso, to_iso

SCORER_VERSION = "s2"  # bump on any change to scoring logic (stored runs keep theirs)
COMPONENTS = ("acceleration", "volume", "breadth", "novelty")


@dataclass(frozen=True)
class Mention:
    trend_id: int
    raw_item_id: int
    source_id: str
    available_at: str
    weight: float          # quality weight (1.0, or clone-farm discount)
    is_backfill: bool = False


@dataclass
class TrendScore:
    trend_id: int
    slug: str
    score: float
    confidence: float
    components: dict[str, float | None]
    evidence: dict[str, Any] = field(default_factory=dict)


def _r(x: float) -> float:
    return round(x, 6)


def config_snapshot(scoring: dict[str, Any], credibility: dict[str, float]) -> dict[str, Any]:
    return {
        "scorer_version": SCORER_VERSION,
        "scoring": scoring,
        "credibility": {k: credibility[k] for k in sorted(credibility)},
    }


def config_hash(snapshot: dict[str, Any]) -> str:
    blob = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


class Scorer:
    def __init__(
        self, conn: sqlite3.Connection, scoring: dict[str, Any], credibility: dict[str, float]
    ) -> None:
        self.conn = conn
        self.cfg = scoring
        self.credibility = credibility
        self.snapshot = config_snapshot(scoring, credibility)
        self.hash = config_hash(self.snapshot)

    # ---- point-in-time inputs ------------------------------------------------------------
    def _clone_farm_names(self, as_of: str) -> dict[str, int]:
        """Repo names published by >= min_owners owners, using only GitHub items known by as_of."""
        min_owners = int(self.cfg["clone_farm"]["min_owners"])
        rows = self.conn.execute(
            "SELECT lower(substr(external_id, instr(external_id,'/')+1)) AS name,"
            " COUNT(DISTINCT lower(substr(external_id, 1, instr(external_id,'/')-1))) AS owners"
            " FROM raw_items WHERE source_id='github' AND item_type='repo' AND available_at<=?"
            " GROUP BY name HAVING owners >= ?",
            (as_of, min_owners),
        )
        return {r["name"]: int(r["owners"]) for r in rows}

    def _latest_metric(self, source_id: str, metric: str, as_of: str) -> dict[int, float]:
        """Latest value of ``metric`` per raw item, as knowable at as_of."""
        out: dict[int, float] = {}
        for r in self.conn.execute(
            "SELECT raw_item_id, value FROM observations WHERE source_id=? AND metric=?"
            " AND raw_item_id IS NOT NULL AND available_at<=? ORDER BY observed_at, id",
            (source_id, metric, as_of),
        ):
            out[int(r["raw_item_id"])] = float(r["value"])
        return out

    def coverage(self, as_of: str) -> dict[str, list[str]]:
        """Sorted available_at of each source's non-baseline items (collection activity)."""
        first_run_ok = set(self.cfg.get("first_run_counts_for", []))
        cov: dict[str, list[str]] = defaultdict(list)
        for r in self.conn.execute(
            "SELECT source_id, available_at, is_initial_snapshot FROM v_raw_items"
            " WHERE available_at<=? ORDER BY available_at",
            (as_of,),
        ):
            if r["is_initial_snapshot"] and r["source_id"] not in first_run_ok:
                continue
            cov[r["source_id"]].append(r["available_at"])
        return cov

    def eligible_mentions(self, as_of: str) -> tuple[list[Mention], dict[str, int]]:
        cfg = self.cfg
        first_run_ok = set(cfg.get("first_run_counts_for", []))
        gates: dict[str, dict[str, Any]] = cfg.get("engagement_gates", {})
        gate_values = {
            src: self._latest_metric(src, str(g["metric"]), as_of) for src, g in gates.items()
        }
        farms = self._clone_farm_names(as_of)
        farm_weight = float(cfg["clone_farm"]["weight"])
        entity_keys = {
            int(r["id"]): r["subject_key"]
            for r in self.conn.execute(
                "SELECT id, subject_key FROM trends WHERE kind='entity' AND subject_type='repo'")
        }
        excluded = {"initial_snapshot": 0, "below_engagement_gate": 0, "clone_farm_discounted": 0}
        out: list[Mention] = []
        for r in self.conn.execute(
            "SELECT m.trend_id, m.raw_item_id, m.source_id, m.available_at, v.is_backfill,"
            " v.is_initial_snapshot, v.item_type, v.external_id"
            " FROM trend_mentions m JOIN v_raw_items v ON v.id = m.raw_item_id"
            " WHERE m.linker_version=? AND m.available_at<=? ORDER BY m.available_at, m.id",
            (cfg["linker_version"], as_of),
        ):
            src = r["source_id"]
            if r["is_initial_snapshot"] and src not in first_run_ok:
                excluded["initial_snapshot"] += 1
                continue
            gate = gates.get(src)
            gated = gate is not None and (
                not r["is_backfill"] or bool(gate.get("applies_to_backfill", False)))
            if gate is not None and gated and (
                    gate_values[src].get(int(r["raw_item_id"]), 0.0) < float(gate["min"])):
                excluded["below_engagement_gate"] += 1
                continue
            weight = 1.0
            repo_name: str | None = None
            if src == "github" and r["item_type"] == "repo":
                repo_name = str(r["external_id"]).split("/", 1)[-1].lower()
            elif int(r["trend_id"]) in entity_keys:
                repo_name = str(entity_keys[int(r["trend_id"])]).split("/", 1)[-1].lower()
            if repo_name is not None and repo_name in farms:
                weight = farm_weight
                excluded["clone_farm_discounted"] += 1
            out.append(Mention(int(r["trend_id"]), int(r["raw_item_id"]), src,
                               r["available_at"], weight, bool(r["is_backfill"])))
        return out, excluded

    # ---- scoring ---------------------------------------------------------------------------
    def score(self, as_of: str) -> tuple[list[TrendScore], dict[str, int]]:
        as_of = normalize_iso(as_of)
        cfg = self.cfg
        t = parse_iso(as_of)
        hours = int(cfg["window_hours"])
        days = int(cfg["baseline_days"])
        bucket = timedelta(hours=hours)
        # edges[k] = end of bucket k; bucket k = (edges[k+1], edges[k]]; k=0 is the window.
        edges = [to_iso(t - k * bucket) for k in range(days + 2)]
        acc = cfg["acceleration"]
        weights: dict[str, float] = cfg["weights"]

        mentions, excluded = self.eligible_mentions(as_of)
        coverage = self.coverage(as_of)
        slugs = {int(r["id"]): r["slug"] for r in self.conn.execute("SELECT id, slug FROM trends")}

        def bucket_of(ts: str) -> int | None:
            # Index k with edges[k+1] < ts <= edges[k]; None if older than the baseline.
            for k in range(days + 1):
                if edges[k + 1] < ts <= edges[k]:
                    return k
            return None

        def covered_buckets(source: str, end: str) -> list[int]:
            """0-based indices of the ``days`` buckets ending at ``end`` with source activity."""
            times = coverage.get(source, [])
            end_t = parse_iso(end)
            out: list[int] = []
            for j in range(days):
                hi = to_iso(end_t - j * bucket)
                lo = to_iso(end_t - (j + 1) * bucket)
                i = bisect.bisect_right(times, lo)
                if i < len(times) and times[i] <= hi:
                    out.append(j)
            return out

        by_trend: dict[int, list[Mention]] = defaultdict(list)
        for m in mentions:
            by_trend[m.trend_id].append(m)

        results: list[TrendScore] = []
        for trend_id in sorted(by_trend):
            ms = by_trend[trend_id]
            counts: dict[str, list[float]] = defaultdict(lambda: [0.0] * (days + 1))
            items_in_window: list[Mention] = []
            for m in ms:
                k = bucket_of(m.available_at)
                if k is None:
                    continue
                counts[m.source_id][k] += m.weight
                if k == 0:
                    items_in_window.append(m)
            window = {s: c[0] for s, c in counts.items() if c[0] > 0}
            if not window:
                continue

            # acceleration: per source, against that source's own covered baseline buckets
            per_source: dict[str, dict[str, Any]] = {}
            z_num = z_den = 0.0
            for src in sorted(counts):
                c = counts[src]
                covered = covered_buckets(src, edges[1])   # baseline j -> bucket k = j + 1
                info: dict[str, Any] = {"window": _r(c[0]),
                                        "baseline_covered_buckets": len(covered)}
                if len(covered) >= int(acc["min_baseline_days"]):
                    base = [c[j + 1] for j in covered]
                    mean = sum(base) / len(base)
                    var = sum((x - mean) ** 2 for x in base) / len(base)
                    denom = max(math.sqrt(var), math.sqrt(mean), 1.0)
                    z = (c[0] - mean) / denom
                    if c[0] < float(acc["min_window_count"]):
                        z = min(z, 0.0)
                    info.update(baseline_mean=_r(mean), z=_r(z))
                    cred = self.credibility.get(src, 0.5)
                    z_num += cred * z
                    z_den += cred
                per_source[src] = info
            accel: float | None = None
            if z_den > 0:
                accel = min(max((z_num / z_den) / float(acc["z_cap"]), 0.0), 1.0)

            volume_raw = sum(self.credibility.get(m.source_id, 0.5) * m.weight
                             for m in items_in_window)
            volume = 1.0 - math.exp(-volume_raw / float(cfg["volume_scale"]))
            n_sources = len(window)
            cap = int(cfg["breadth_cap"])
            breadth = min((n_sources - 1) / (cap - 1), 1.0) if cap > 1 else 1.0

            first = min(ms, key=lambda m: (m.available_at, m.raw_item_id))
            novelty: float | None = None
            # Novelty needs that source to have been collecting before the first mention.
            before = to_iso(parse_iso(first.available_at) - timedelta(microseconds=1))
            if len(covered_buckets(first.source_id, before)) >= int(acc["min_baseline_days"]):
                age_days = (t - parse_iso(first.available_at)).total_seconds() / 86400
                # Scaled by support: one or two mentions of a brand-new name are not a trend.
                support = min(sum(window.values()) / float(acc["min_window_count"]), 1.0)
                novelty = math.exp(-age_days / float(cfg["novelty_tau_days"])) * support

            comps: dict[str, float | None] = {
                "acceleration": None if accel is None else _r(accel),
                "volume": _r(volume), "breadth": _r(breadth),
                "novelty": None if novelty is None else _r(novelty),
            }
            total = sum(weights[c] * (comps[c] or 0.0) for c in COMPONENTS)
            confidence = sum(weights[c] for c in COMPONENTS if comps[c] is not None)
            top = sorted(items_in_window,
                         key=lambda m: (-self.credibility.get(m.source_id, 0.5) * m.weight,
                                        m.available_at, m.raw_item_id))[:5]
            results.append(TrendScore(
                trend_id=trend_id, slug=slugs.get(trend_id, str(trend_id)),
                score=_r(100.0 * total), confidence=_r(confidence), components=comps,
                evidence={
                    "window_weighted_mentions": _r(volume_raw),
                    "window_sources": {s: _r(v) for s, v in sorted(window.items())},
                    "per_source": per_source,
                    "first_mention_at": first.available_at,
                    "top_raw_item_ids": [m.raw_item_id for m in top],
                    "discounted_in_window": sum(1 for m in items_in_window if m.weight < 1.0),
                },
            ))
        results.sort(key=lambda s: (-s.score, s.slug))
        return results, excluded

    # ---- persistence -----------------------------------------------------------------------
    def save(self, as_of: str, results: list[TrendScore]) -> int | None:
        """Store a run atomically. Returns run id, or None if this exact run already exists."""
        as_of = normalize_iso(as_of)
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            exists = self.conn.execute(
                "SELECT id FROM scoring_runs WHERE as_of=? AND scorer_version=? AND config_hash=?",
                (as_of, SCORER_VERSION, self.hash)).fetchone()
            if exists:
                self.conn.execute("ROLLBACK")
                return None
            cur = self.conn.execute(
                "INSERT INTO scoring_runs(as_of, scorer_version, linker_version, config_hash,"
                " config_json, trends_scored, computed_at) VALUES (?,?,?,?,?,?,?)",
                (as_of, SCORER_VERSION, self.cfg["linker_version"], self.hash,
                 json.dumps(self.snapshot, sort_keys=True), len(results), now_iso()))
            run_id = int(cur.lastrowid or 0)
            self.conn.executemany(
                "INSERT INTO trend_scores(run_id, trend_id, as_of, score, confidence, components,"
                " evidence) VALUES (?,?,?,?,?,?,?)",
                [(run_id, s.trend_id, as_of, s.score, s.confidence,
                  json.dumps(s.components, sort_keys=True), json.dumps(s.evidence, sort_keys=True))
                 for s in results])
            self.conn.execute("COMMIT")
            return run_id
        except Exception:
            self.conn.execute("ROLLBACK")
            raise


def verify_run(conn: sqlite3.Connection, run_id: int) -> list[str]:
    """Recompute a stored run from its own config snapshot; return mismatches (empty = OK)."""
    run = conn.execute("SELECT * FROM scoring_runs WHERE id=?", (run_id,)).fetchone()
    if run is None:
        raise KeyError(f"no scoring run {run_id}")
    snap = json.loads(run["config_json"])
    if snap.get("scorer_version") != SCORER_VERSION:
        return [f"run {run_id} used scorer {snap.get('scorer_version')}, current is "
                f"{SCORER_VERSION}"]
    scorer = Scorer(conn, snap["scoring"], snap["credibility"])
    fresh, _ = scorer.score(run["as_of"])
    stored = {
        int(r["trend_id"]): (float(r["score"]), float(r["confidence"]), r["components"],
                             r["evidence"])
        for r in conn.execute("SELECT * FROM trend_scores WHERE run_id=?", (run_id,))
    }
    problems: list[str] = []
    fresh_map = {s.trend_id: s for s in fresh}
    for tid in sorted(set(stored) | set(fresh_map)):
        if tid not in fresh_map or tid not in stored:
            problems.append(f"trend {tid}: present in only one of stored/recomputed")
            continue
        s = fresh_map[tid]
        if stored[tid] != (s.score, s.confidence, json.dumps(s.components, sort_keys=True),
                           json.dumps(s.evidence, sort_keys=True)):
            problems.append(f"trend {tid} ({s.slug}): score/evidence differ")
    return problems
