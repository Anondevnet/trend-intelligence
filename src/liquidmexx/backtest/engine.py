"""Stage 5 backtester: point-in-time, leakage-guarded evaluation of trend detectors.

For each evaluation time T (daily) and horizon H:
- the detector ranks trends using only data with ``available_at <= T`` (the scorer enforces it);
- the outcome label uses only mentions in ``(T, T+H]`` read as of ``T+H``, counted with the
  same eligibility rules as scoring, and only over sources collecting both before T and
  during the outcome window (so a source switching on cannot look like emergence);
- walk-forward threshold selection trains only on dates whose labels were knowable by the
  test date (``t + H <= T_test``, an embargo), so no future outcome leaks into a decision.

Detectors are plain functions ``as_of -> {trend_id: score}`` so Stage 6 can add more.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import sqlite3
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from liquidmexx.detectors import core as det
from liquidmexx.scoring.engine import SCORER_VERSION, Mention, Scorer
from liquidmexx.timeutil import now_iso, parse_iso, to_iso

DAY = timedelta(days=1)
Ranker = Callable[[str], dict[int, float]]


@dataclass(frozen=True)
class Point:
    as_of: str
    trend_id: int
    score: float
    label: int
    future_mentions: float
    baseline_rate: float
    backfill_share: float


@dataclass
class DateLabels:
    as_of: str
    labels: dict[int, tuple[int, float, float]]      # trend -> (label, future, baseline_rate)
    backfill_share: dict[int, float]
    label_sources: list[str]
    unseen_emergers: int


@dataclass
class BacktestResult:
    detector: str
    horizon_days: int
    dates: list[str]
    points: list[Point]
    metrics: dict[str, Any] = field(default_factory=dict)


def _r(x: float) -> float:
    return round(x, 6)


def _ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def spearman(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 3:
        return None
    rx, ry = _ranks(xs), _ranks(ys)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry, strict=True))
    vx = sum((a - mx) ** 2 for a in rx)
    vy = sum((b - my) ** 2 for b in ry)
    if vx == 0 or vy == 0:
        return None
    return cov / math.sqrt(vx * vy)


class Backtester:
    def __init__(
        self, conn: sqlite3.Connection, scoring: dict[str, Any], credibility: dict[str, float],
        cfg: dict[str, Any], detectors: dict[str, Any] | None = None,
    ) -> None:
        """``detectors`` (config/detectors.yaml) adds the Stage 6 detectors and the ensemble;
        without it only the scorer and the two baselines run."""
        self.conn = conn
        self.cfg = cfg
        self.det_cfg = detectors
        self.scorer = Scorer(conn, scoring, credibility)
        self._score_cache: dict[str, dict[int, tuple[float, float]]] = {}
        self._mention_cache: dict[str, list[Mention]] = {}
        self._coverage_cache: dict[str, dict[str, list[str]]] = {}
        self._engagement_cache: dict[str, dict[str, dict[str, dict[int, float]]]] = {}
        self._labels_cache: dict[tuple[str, int], DateLabels] = {}
        self._rank_cache: dict[tuple[str, str], dict[int, float]] = {}
        self.ensemble_log: dict[int, dict[str, dict[str, Any]]] = {}
        self._categories: dict[int, str] | None = None
        snapshot: dict[str, Any] = {"backtest": cfg, "scoring": self.scorer.snapshot}
        if detectors is not None:
            snapshot["detectors"] = detectors
        self.snapshot = snapshot
        self.hash = hashlib.sha256(
            json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]

    # ---- cached point-in-time inputs -------------------------------------------------------
    def _mentions(self, as_of: str) -> list[Mention]:
        if as_of not in self._mention_cache:
            self._mention_cache[as_of] = self.scorer.eligible_mentions(as_of)[0]
        return self._mention_cache[as_of]

    def _coverage(self, as_of: str) -> dict[str, list[str]]:
        if as_of not in self._coverage_cache:
            self._coverage_cache[as_of] = self.scorer.coverage(as_of)
        return self._coverage_cache[as_of]

    def scored(self, as_of: str) -> dict[int, tuple[float, float]]:
        """trend -> (composite score, window weighted mentions), as known at as_of."""
        if as_of not in self._score_cache:
            results, _ = self.scorer.score(as_of)
            self._score_cache[as_of] = {
                s.trend_id: (s.score, float(s.evidence["window_weighted_mentions"]))
                for s in results}
        return self._score_cache[as_of]

    def window_mentions(self, as_of: str) -> list[Mention]:
        assert self.det_cfg is not None
        start = to_iso(parse_iso(as_of) - timedelta(hours=float(self.det_cfg["window_hours"])))
        return [m for m in self._mentions(as_of) if m.available_at > start]

    def engagement(self, as_of: str) -> dict[str, dict[str, dict[int, float]]]:
        """Engagement values the detectors need, as knowable at as_of."""
        assert self.det_cfg is not None
        if as_of not in self._engagement_cache:
            self._engagement_cache[as_of] = {
                src: {metric: self.scorer._latest_metric(src, metric, as_of)
                      for metric in sorted(metrics)}
                for src, metrics in sorted(det.engagement_metrics(self.det_cfg).items())}
        return self._engagement_cache[as_of]

    def categories(self) -> dict[int, str]:
        if self._categories is None:
            self._categories = {int(r["id"]): str(r["category"])
                                for r in self.conn.execute("SELECT id, category FROM trends")}
        return self._categories

    def data_watermark(self) -> str:
        row = self.conn.execute("SELECT MAX(available_at) AS m FROM raw_items").fetchone()
        return str(row["m"] or now_iso())

    # ---- evaluation grid -------------------------------------------------------------------
    def eval_dates(self, horizon_days: int) -> list[str]:
        """Daily times T with enough history before T and a complete outcome window after."""
        row = self.conn.execute("SELECT MIN(available_at) AS m FROM raw_items").fetchone()
        if row["m"] is None:
            return []
        min_days = int(self.scorer.cfg["acceleration"]["min_baseline_days"])
        first = parse_iso(row["m"]) + timedelta(days=min_days + 1)
        hour = int(self.cfg["eval_hour_utc"])
        t = first.replace(hour=hour, minute=0, second=0, microsecond=0)
        if t < first:
            t += DAY
        last = parse_iso(self.data_watermark()) - timedelta(days=horizon_days)
        step = timedelta(days=int(self.cfg["step_days"]))
        out: list[str] = []
        while t <= last:
            out.append(to_iso(t))
            t += step
        return out

    # ---- outcome labels --------------------------------------------------------------------
    def _active_days(self, times: list[str], start: str, days: int) -> int:
        s = parse_iso(start)
        n = 0
        for d in range(days):
            lo, hi = to_iso(s + d * DAY), to_iso(s + (d + 1) * DAY)
            if any(lo < x <= hi for x in times):
                n += 1
        return n

    def labels(self, as_of: str, horizon_days: int) -> DateLabels:
        key = (as_of, horizon_days)
        if key not in self._labels_cache:
            self._labels_cache[key] = self._labels(as_of, horizon_days)
        return self._labels_cache[key]

    def _labels(self, as_of: str, horizon_days: int) -> DateLabels:
        lab = self.cfg["label"]
        base_days = int(self.scorer.cfg["baseline_days"])
        min_base = int(self.scorer.cfg["acceleration"]["min_baseline_days"])
        t = parse_iso(as_of)
        end = to_iso(t + timedelta(days=horizon_days))
        base_start = to_iso(t - timedelta(days=base_days))

        cov_now = self._coverage(as_of)
        cov_end = self._coverage(end)
        sources: list[str] = []
        for src in sorted(cov_end):
            before = self._active_days(cov_now.get(src, []), base_start, base_days)
            after = self._active_days(cov_end.get(src, []), as_of, horizon_days)
            if before >= min_base and after >= math.ceil(
                    float(lab["min_future_coverage"]) * horizon_days):
                sources.append(src)
        src_set = set(sources)

        past = [m for m in self._mentions(as_of) if m.source_id in src_set]
        # Universe: trends active (>= 1 eligible mention) in the baseline span before T.
        seen = {m.trend_id for m in past if m.available_at > base_start}
        base: dict[int, float] = defaultdict(float)
        total_w: dict[int, float] = defaultdict(float)
        backfill_w: dict[int, float] = defaultdict(float)
        for m in past:
            if m.available_at > base_start:
                base[m.trend_id] += m.weight
            total_w[m.trend_id] += m.weight
            if m.is_backfill:
                backfill_w[m.trend_id] += m.weight
        # base: covered-source mentions over the baseline span (incl. the current window,
        # which is ranking-time knowledge) expressed as a daily rate.
        future: dict[int, float] = defaultdict(float)
        for m in self._mentions(end):
            if m.source_id in src_set and as_of < m.available_at <= end:
                future[m.trend_id] += m.weight

        out: dict[int, tuple[int, float, float]] = {}
        for tid in sorted(seen):
            rate = base[tid] / base_days
            fut = future.get(tid, 0.0)
            positive = fut >= float(lab["min_future_mentions"]) and (
                fut / horizon_days >= float(lab["lift_min"]) * rate)
            out[tid] = (int(positive), _r(fut), _r(rate))
        unseen = sum(
            1 for tid, fut in future.items()
            if tid not in seen and fut >= float(lab["min_future_mentions"]))
        share = {tid: _r(backfill_w[tid] / total_w[tid]) if total_w[tid] else 0.0
                 for tid in seen}
        return DateLabels(as_of, out, share, sources, unseen)

    # ---- detectors -------------------------------------------------------------------------
    def rankers(self, horizon_days: int | None = None) -> dict[str, Ranker]:
        """Detectors by name. The ensemble needs a horizon (its weights come from past
        outcomes over that horizon), so it is only included when one is given."""
        seed = int(self.cfg["random_seed"])

        def scorer(as_of: str) -> dict[int, float]:
            return {t: v[0] for t, v in self.scored(as_of).items()}

        def volume_only(as_of: str) -> dict[int, float]:
            return {t: v[1] for t, v in self.scored(as_of).items()}

        def rand(as_of: str) -> dict[int, float]:
            seen = {m.trend_id for m in self._mentions(as_of)}  # superset of the universe
            return {t: random.Random(f"{seed}:{as_of}:{t}").random()  # noqa: S311 (baseline)
                    for t in sorted(seen)}

        out: dict[str, Ranker] = {
            f"scorer:{SCORER_VERSION}": scorer, "volume_only": volume_only, "random": rand}
        if self.det_cfg is None:
            return out
        dcfg = self.det_cfg

        def hotlist(as_of: str) -> dict[int, float]:
            return det.hotlist(self.window_mentions(as_of), self.engagement(as_of), dcfg)

        def signal_sum(as_of: str) -> dict[int, float]:
            return det.signal_sum(self.window_mentions(as_of), self.engagement(as_of),
                                  self.categories(), dcfg)

        out["hotlist"] = self._cached("hotlist", hotlist)
        out["signal_sum"] = self._cached("signal_sum", signal_sum)
        if horizon_days is not None:
            out["ensemble"] = self._ensemble(horizon_days, out)
        return out

    def _cached(self, name: str, fn: Ranker) -> Ranker:
        def wrapped(as_of: str) -> dict[int, float]:
            key = (name, as_of)
            if key not in self._rank_cache:
                self._rank_cache[key] = fn(as_of)
            return self._rank_cache[key]
        return wrapped

    def _ensemble(self, horizon_days: int, rankers: dict[str, Ranker]) -> Ranker:
        assert self.det_cfg is not None
        ecfg = self.det_cfg["ensemble"]
        alias = {"scorer": f"scorer:{SCORER_VERSION}"}
        members = [alias.get(str(m), str(m)) for m in ecfg["members"]]
        unknown = [m for m in members if m not in rankers]
        if unknown:
            raise ValueError(f"ensemble members not available: {unknown}")
        h = timedelta(days=horizon_days)
        log = self.ensemble_log.setdefault(horizon_days, {})

        def ensemble(as_of: str) -> dict[int, float]:
            t = parse_iso(as_of)
            # Only dates whose outcome window closed by as_of (embargo): no future labels.
            train = [d for d in self.eval_dates(horizon_days)
                     if d < as_of and parse_iso(d) + h <= t]
            history = [({m: rankers[m](d) for m in members},
                        {tid: v[0] for tid, v in self.labels(d, horizon_days).labels.items()})
                       for d in train]
            weights, status = det.member_weights(history, members, int(ecfg["k"]),
                                                 int(ecfg["min_train_dates"]))
            log[as_of] = {"weights": weights, "status": status, "train_dates": len(train)}
            return det.combine({m: rankers[m](as_of) for m in members}, weights)

        return ensemble

    # ---- run -------------------------------------------------------------------------------
    def run(self, horizon_days: int, detectors: list[str] | None = None) -> list[BacktestResult]:
        dates = self.eval_dates(horizon_days)
        all_rankers = self.rankers(horizon_days)
        names = detectors or list(all_rankers)
        labels = {d: self.labels(d, horizon_days) for d in dates}
        results: list[BacktestResult] = []
        for name in names:
            rank = all_rankers[name]
            points: list[Point] = []
            for d in dates:
                dl = labels[d]
                scores = rank(d)
                for tid, (label, fut, rate) in sorted(dl.labels.items()):
                    points.append(Point(d, tid, _r(scores.get(tid, 0.0)), label, fut, rate,
                                        dl.backfill_share.get(tid, 0.0)))
            res = BacktestResult(name, horizon_days, dates, points)
            res.metrics = self.metrics(res, labels)
            if name == "ensemble" and res.metrics.get("status") == "ok":
                log = self.ensemble_log.get(horizon_days, {})
                res.metrics["ensemble_weights"] = {d: log[d] for d in dates if d in log}
            results.append(res)
        return results

    def metrics(self, res: BacktestResult, labels: dict[str, DateLabels]) -> dict[str, Any]:
        h = res.horizon_days
        if not res.dates:
            return {"status": "insufficient_history",
                    "reason": f"no evaluation date has {h}d of outcome data after it yet"}
        by_date: dict[str, list[Point]] = defaultdict(list)
        for p in res.points:
            by_date[p.as_of].append(p)
        ks = [int(k) for k in self.cfg["top_k"]]
        prec: dict[int, list[float]] = {k: [] for k in ks}
        hits: dict[int, list[int]] = {k: [0, 0] for k in ks}   # [positives in top-k, picks]
        base_rates: list[float] = []
        rhos: list[float] = []
        for d in res.dates:
            pts = sorted(by_date.get(d, []), key=lambda p: (-p.score, p.trend_id))
            if not pts:
                continue
            base_rates.append(sum(p.label for p in pts) / len(pts))
            for k in ks:
                if len(pts) >= k:
                    top = sum(p.label for p in pts[:k])
                    prec[k].append(top / k)
                    hits[k][0] += top
                    hits[k][1] += k
            lift = [math.log((p.future_mentions / h + 0.1) / (p.baseline_rate + 0.1))
                    for p in pts]
            rho = spearman([p.score for p in pts], lift)
            if rho is not None:
                rhos.append(rho)

        def mean(xs: list[float]) -> float | None:
            return _r(sum(xs) / len(xs)) if xs else None

        base = mean(base_rates)
        out: dict[str, Any] = {
            "status": "ok",
            "n_dates": len(res.dates),
            "n_points": len(res.points),
            "positives": sum(p.label for p in res.points),
            "base_rate": base,
            "spearman_mean": mean(rhos),
            "backfill_share_mean": mean([p.backfill_share for p in res.points]),
            "label_sources": sorted({s for d in res.dates for s in labels[d].label_sources}),
            "unseen_emergers": sum(labels[d].unseen_emergers for d in res.dates),
        }
        for k in ks:
            pk = mean(prec[k])
            out[f"precision_at_{k}"] = pk
            out[f"lift_at_{k}"] = _r(pk / base) if pk is not None and base else None
            out[f"hits_at_{k}"] = f"{hits[k][0]}/{hits[k][1]}"
        out["walk_forward"] = self.walk_forward(res, by_date)
        return out

    def walk_forward(self, res: BacktestResult, by_date: dict[str, list[Point]]) -> dict[str, Any]:
        wf = self.cfg["walk_forward"]
        quantiles = [float(q) for q in wf["threshold_quantiles"]]
        h = timedelta(days=res.horizon_days)
        tp = fp = fn = 0
        chosen: list[dict[str, Any]] = []
        for test in res.dates:
            tt = parse_iso(test)
            train = [d for d in res.dates if d < test and parse_iso(d) + h <= tt]  # embargo
            if len(train) < int(wf["min_train_dates"]):
                continue
            pts = [p for d in train for p in by_date.get(d, [])]
            if not pts or sum(p.label for p in pts) < int(wf["min_train_positives"]):
                continue
            # Detector-agnostic thresholds: quantiles of the training scores.
            ordered = sorted(p.score for p in pts)
            grid = sorted({ordered[min(int(q * len(ordered)), len(ordered) - 1)]
                           for q in quantiles})
            best: tuple[float, float] | None = None
            for th in grid:
                a_tp = sum(1 for p in pts if p.score >= th and p.label)
                a_fp = sum(1 for p in pts if p.score >= th and not p.label)
                a_fn = sum(1 for p in pts if p.score < th and p.label)
                f1 = 2 * a_tp / (2 * a_tp + a_fp + a_fn) if a_tp else 0.0
                if best is None or f1 > best[1] or (f1 == best[1] and th > best[0]):
                    best = (th, f1)
            assert best is not None
            th = best[0]
            for p in by_date.get(test, []):
                if p.score >= th and p.label:
                    tp += 1
                elif p.score >= th:
                    fp += 1
                elif p.label:
                    fn += 1
            chosen.append({"test": test, "threshold": _r(th), "train_dates": len(train),
                           "train_f1": _r(best[1])})
        if not chosen:
            return {"status": "insufficient_history",
                    "reason": "not enough embargoed training dates with positives"}
        precision = tp / (tp + fp) if tp + fp else None
        recall = tp / (tp + fn) if tp + fn else None
        f1 = 2 * tp / (2 * tp + fp + fn) if tp else 0.0
        return {"status": "ok", "test_dates": len(chosen), "alerts": tp + fp, "tp": tp,
                "fp": fp, "fn": fn,
                "precision": None if precision is None else _r(precision),
                "recall": None if recall is None else _r(recall), "f1": _r(f1),
                "thresholds": chosen}

    # ---- persistence -----------------------------------------------------------------------
    def save(self, res: BacktestResult) -> int:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            cur = self.conn.execute(
                "INSERT INTO backtest_runs(detector, horizon_days, start_as_of, end_as_of,"
                " n_dates, config_hash, config_json, data_watermark, metrics_json, computed_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (res.detector, res.horizon_days, res.dates[0] if res.dates else None,
                 res.dates[-1] if res.dates else None, len(res.dates), self.hash,
                 json.dumps(self.snapshot, sort_keys=True), self.data_watermark(),
                 json.dumps(res.metrics, sort_keys=True), now_iso()))
            run_id = int(cur.lastrowid or 0)
            self.conn.executemany(
                "INSERT INTO backtest_points(run_id, as_of, trend_id, score, label,"
                " future_mentions, baseline_rate, backfill_share) VALUES (?,?,?,?,?,?,?,?)",
                [(run_id, p.as_of, p.trend_id, p.score, p.label, p.future_mentions,
                  p.baseline_rate, p.backfill_share) for p in res.points])
            self.conn.execute("COMMIT")
            return run_id
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
