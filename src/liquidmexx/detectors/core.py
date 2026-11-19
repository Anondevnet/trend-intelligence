"""Stage 6 detectors as pure functions over point-in-time inputs.

The backtester supplies the eligible mentions in the window ending at T and the engagement
values known at T; nothing here reads the database or the clock, so every score can be
recomputed exactly.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from liquidmexx.scoring.engine import Mention

Scores = dict[int, float]
Engagement = Mapping[str, Mapping[str, Mapping[int, float]]]  # source -> metric -> item -> value


def _r(x: float) -> float:
    return round(x, 6)


def engagement_metrics(cfg: Mapping[str, Any]) -> dict[str, set[str]]:
    """Every (source -> metrics) pair the detectors read, so callers fetch them once."""
    need: dict[str, set[str]] = defaultdict(set)
    for src, metric in cfg["hotlist"]["engagement_metrics"].items():
        need[src].add(str(metric))
    for src, rule in cfg["signal_sum"]["points"].items():
        if "metric" in rule:
            need[src].add(str(rule["metric"]))
    return dict(need)


def source_ranks(mentions: Sequence[Mention], values: Mapping[int, float] | None,
                 rank_cap: int) -> dict[int, int]:
    """1-based engagement rank of each distinct item of one source (ties: lower id first).

    Items with no value known at T, or a source with no metric, rank ``rank_cap``.
    """
    items = sorted({m.raw_item_id for m in mentions})
    if not values:
        return {i: rank_cap for i in items}
    known = sorted((i for i in items if i in values), key=lambda i: (-values[i], i))
    ranks = {i: n + 1 for n, i in enumerate(known)}
    return {i: ranks.get(i, rank_cap) for i in items}


def hotlist(mentions: Sequence[Mention], engagement: Engagement, cfg: Mapping[str, Any]
            ) -> Scores:
    """Prominence + frequency + hotness, each scaled to 0-100, then weighted."""
    hc = cfg["hotlist"]
    cap, top, fcap = int(hc["rank_cap"]), int(hc["top_rank"]), float(hc["frequency_cap"])
    w = hc["weights"]
    metric_of: Mapping[str, str] = hc["engagement_metrics"]
    by_source: dict[str, list[Mention]] = defaultdict(list)
    for m in mentions:
        by_source[m.source_id].append(m)
    rank: dict[tuple[str, int], int] = {}
    for src, ms in by_source.items():
        metric = metric_of.get(src)
        values = engagement.get(src, {}).get(metric, {}) if metric else None
        for item, r in source_ranks(ms, values, cap).items():
            rank[(src, item)] = r
    acc: dict[int, list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0])  # weight, prom, hot
    for m in mentions:
        r = min(rank[(m.source_id, m.raw_item_id)], cap)
        a = acc[m.trend_id]
        a[0] += m.weight
        a[1] += m.weight * (cap + 1 - r)
        a[2] += m.weight * (1.0 if r <= top else 0.0)
    out: Scores = {}
    for tid, (wt, prom, hot) in acc.items():
        if wt <= 0:
            continue
        prominence = prom / wt / cap * 100
        frequency = min(wt, fcap) / fcap * 100
        hotness = hot / wt * 100
        out[tid] = _r(float(w["prominence"]) * prominence + float(w["frequency"]) * frequency
                      + float(w["hotness"]) * hotness)
    return out


def signal_points(source: str, value: float | None, cfg: Mapping[str, Any]) -> float:
    rules = cfg["signal_sum"]["points"]
    rule = rules.get(source, rules["default"])
    if "flat" in rule:
        return float(rule["flat"])
    v = 0.0 if value is None else value
    for threshold, pts in rule["tiers"]:
        if v >= float(threshold):
            return float(pts)
    return 0.0


def signal_sum(mentions: Sequence[Mention], engagement: Engagement,
               categories: Mapping[int, str], cfg: Mapping[str, Any]) -> Scores:
    sc = cfg["signal_sum"]
    cat_w: Mapping[str, float] = sc.get("category_weights", {})
    totals: dict[int, float] = defaultdict(float)
    for m in mentions:
        rule = sc["points"].get(m.source_id, sc["points"]["default"])
        value = None
        if "metric" in rule:
            value = engagement.get(m.source_id, {}).get(rule["metric"], {}).get(m.raw_item_id)
        totals[m.trend_id] += m.weight * signal_points(m.source_id, value, cfg)
    return {tid: _r(v * float(cat_w.get(categories.get(tid, ""), 1.0)))
            for tid, v in totals.items()}


def percentile_ranks(scores: Mapping[int, float], universe: Iterable[int]) -> Scores:
    """Average-rank percentile in (0, 1] over ``universe``; unscored trends count as 0."""
    ids = sorted(set(universe))
    if not ids:
        return {}
    vals = [scores.get(i, 0.0) for i in ids]
    order = sorted(range(len(ids)), key=lambda k: vals[k])
    pct = [0.0] * len(ids)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and vals[order[j + 1]] == vals[order[i]]:
            j += 1
        for k in range(i, j + 1):
            pct[order[k]] = ((i + j) / 2 + 1) / len(ids)
        i = j + 1
    return {tid: pct[n] for n, tid in enumerate(ids)}


def combine(member_scores: Mapping[str, Scores], weights: Mapping[str, float]) -> Scores:
    """Weighted mean of member percentile ranks over the union of scored trends."""
    total = sum(max(w, 0.0) for w in weights.values())
    universe: set[int] = set()
    for s in member_scores.values():
        universe |= set(s)
    if total <= 0 or not universe:
        return {}
    out: dict[int, float] = defaultdict(float)
    for name, s in member_scores.items():
        w = max(weights.get(name, 0.0), 0.0) / total
        if w == 0:
            continue
        for tid, p in percentile_ranks(s, universe).items():
            out[tid] += w * p
    return {tid: _r(v) for tid, v in out.items()}


def top_k_edge(scores: Mapping[int, float], labels: Mapping[int, int], k: int) -> float | None:
    """Precision of the top-k (over the labelled universe) minus the base rate."""
    ids = sorted(labels, key=lambda t: (-scores.get(t, 0.0), t))
    if not ids:
        return None
    kk = min(k, len(ids))
    precision = sum(labels[t] for t in ids[:kk]) / kk
    base = sum(labels.values()) / len(labels)
    return precision - base


def member_weights(history: Sequence[tuple[Mapping[str, Scores], Mapping[int, int]]],
                   members: Sequence[str], k: int, min_train_dates: int
                   ) -> tuple[dict[str, float], str]:
    """Weights from past (member scores, labels) pairs that were known by the test date.

    Weight = mean top-k edge over the base rate, floored at 0. Returns (weights, status):
    ``earned`` when history supports it, ``equal_no_history`` or ``equal_no_edge`` otherwise.
    """
    usable = [(s, lab) for s, lab in history if lab]
    if len(usable) < min_train_dates:
        return {m: 1.0 for m in members}, "equal_no_history"
    weights: dict[str, float] = {}
    for m in members:
        edges = [e for s, lab in usable if (e := top_k_edge(s.get(m, {}), lab, k)) is not None]
        weights[m] = _r(max(sum(edges) / len(edges), 0.0)) if edges else 0.0
    if sum(weights.values()) <= 0:
        return {m: 1.0 for m in members}, "equal_no_edge"
    return weights, "earned"
