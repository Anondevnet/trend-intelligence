"""Stage 7 crypto and AI intelligence reports.

A report is rebuilt from stored history as of a time T. It uses no LLM and no network. It
lists narrative activity with labelled evidence, measured movers and new listings. It does not
predict, rank content for posting, or make investment recommendations.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from liquidmexx.intel.evidence import classify
from liquidmexx.intel.movers import (
    Mover,
    clone_farm_names,
    data_watermark,
    keyword_re,
    matches,
    metric_movers,
    new_items,
    shift,
)
from liquidmexx.scoring.engine import Scorer
from liquidmexx.timeutil import normalize_iso, parse_iso

GROUPS = ("crypto", "ai")
DISCLAIMER = ("Descriptive only: activity and measured changes from stored data. Not a"
              " prediction and not investment advice.")


@dataclass(frozen=True)
class Evidence:
    source_id: str
    title: str
    url: str | None
    available_at: str
    evidence_class: str
    claim_status: str


@dataclass
class Narrative:
    slug: str
    name: str
    category: str
    weighted_mentions: float
    sources: dict[str, float]
    latest_score: float | None
    top_source_share: float
    evidence: list[Evidence] = field(default_factory=list)


@dataclass
class Section:
    title: str
    note: str
    columns: list[str]
    rows: list[dict[str, Any]] = field(default_factory=list)
    skipped: dict[str, int] = field(default_factory=dict)


@dataclass
class Report:
    group: str
    as_of: str
    config_hash: str
    data_watermark: str | None
    score_run: int | None
    narratives: list[Narrative]
    quiet_narratives: list[str]
    sections: list[Section]
    concentration_warning: float
    disclaimer: str = DISCLAIMER

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True)


def config_hash(cfg: Mapping[str, Any]) -> str:
    blob = json.dumps(cfg, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _r(x: float | None, nd: int = 4) -> float | None:
    return None if x is None else round(x, nd)


def _text(row: sqlite3.Row) -> str:
    return " ".join(str(row[k]) for k in ("title", "body") if row[k])


# ---- narratives ------------------------------------------------------------------------------
def latest_score_run(conn: sqlite3.Connection, as_of: str) -> int | None:
    row = conn.execute("SELECT id FROM scoring_runs WHERE as_of<=? ORDER BY as_of DESC, id DESC"
                       " LIMIT 1", (as_of,)).fetchone()
    return int(row["id"]) if row else None


def narratives(conn: sqlite3.Connection, as_of: str, categories: Sequence[str],
               cfg: Mapping[str, Any], scorer: Scorer) -> tuple[list[Narrative], list[str],
                                                               int | None]:
    """Narratives with eligible mentions in the window, ordered by weighted mentions."""
    start = shift(as_of, float(cfg["window_hours"]))
    trends = {int(r["id"]): r for r in conn.execute(
        "SELECT id, slug, name, category FROM trends WHERE kind='narrative' AND category IN"
        f" ({','.join('?' * len(categories))}) ORDER BY slug", list(categories))}
    mentions, _ = scorer.eligible_mentions(as_of)
    weight: dict[int, float] = defaultdict(float)
    by_source: dict[int, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    items: dict[int, list[tuple[str, int]]] = defaultdict(list)
    for m in mentions:
        if m.trend_id not in trends or m.available_at <= start or m.weight <= 0:
            continue
        weight[m.trend_id] += m.weight
        by_source[m.trend_id][m.source_id] += m.weight
        items[m.trend_id].append((m.available_at, m.raw_item_id))
    run = latest_score_run(conn, as_of)
    scores: dict[int, float] = {}
    if run is not None:
        scores = {int(r["trend_id"]): float(r["score"]) for r in conn.execute(
            "SELECT trend_id, score FROM trend_scores WHERE run_id=?", (run,))}
    k = int(cfg["evidence_per_narrative"])
    out: list[Narrative] = []
    for tid, w in weight.items():
        t = trends[tid]
        recent = [i for _, i in sorted(set(items[tid]), reverse=True)][:k]
        ev = []
        for rid in recent:
            r = conn.execute("SELECT source_id, title, body, url, available_at FROM raw_items"
                             " WHERE id=?", (rid,)).fetchone()
            label = classify(r["source_id"], _text(r), cfg)
            ev.append(Evidence(r["source_id"], r["title"] or "", r["url"], r["available_at"],
                               label.evidence_class, label.claim_status))
        share = max(by_source[tid].values()) / w
        out.append(Narrative(t["slug"], t["name"], t["category"], round(w, 4),
                             {s: round(v, 4) for s, v in sorted(by_source[tid].items())},
                             scores.get(tid), round(share, 4), ev))
    out.sort(key=lambda n: (-n.weighted_mentions, n.slug))
    quiet = sorted(t["slug"] for tid, t in trends.items() if tid not in weight)
    return out, quiet, run


# ---- sections --------------------------------------------------------------------------------
def mover_row(m: Mover, cfg: Mapping[str, Any], **extra: Any) -> dict[str, Any]:
    label = classify(m.source_id, m.title, cfg)
    return {"subject": m.subject_key, "title": m.title, "url": m.url, "now": _r(m.now, 2),
            "change": _r(m.change, 2), "pct": _r(m.pct), "now_at": m.now_at,
            "then_at": m.then_at, "evidence_class": label.evidence_class,
            "claim_status": label.claim_status, **extra}


def movers_section(conn: sqlite3.Connection, as_of: str, cfg: Mapping[str, Any], *,
                   title: str, note: str, source: str, subject_type: str, metric: str,
                   min_now: float = 0.0) -> Section:
    sec = Section(title, note, ["title", "now", "change", "pct"])
    rows = metric_movers(conn, source, subject_type, metric, as_of, cfg)
    big = [m for m in rows if m.now >= min_now]
    comparable = [m for m in big if m.pct is not None]
    sec.skipped = {"below_minimum": len(rows) - len(big),
                   "no_reading_at_lookback": len(big) - len(comparable)}
    comparable.sort(key=lambda m: (-abs(m.pct or 0.0), m.subject_key))
    sec.rows = [mover_row(m, cfg) for m in comparable[: int(cfg["top_n"])]]
    return sec


def listings_section(conn: sqlite3.Connection, as_of: str, cfg: Mapping[str, Any], *,
                     title: str, note: str, source: str, item_type: str | None) -> Section:
    sec = Section(title, note, ["title", "published_at"])
    found = new_items(conn, source, item_type, as_of, float(cfg["window_hours"]))
    sec.skipped = {"total_in_window": len(found)}
    for r in found[: int(cfg["top_n"])]:
        label = classify(source, _text(r), cfg)
        sec.rows.append({"subject": r["external_id"], "title": r["title"] or r["external_id"],
                         "url": r["url"], "published_at": r["published_at"],
                         "available_at": r["available_at"],
                         "evidence_class": label.evidence_class,
                         "claim_status": label.claim_status})
    return sec


def headlines_section(conn: sqlite3.Connection, as_of: str, cfg: Mapping[str, Any],
                      group: str, feed_categories: Mapping[str, str]) -> Section:
    wanted = set(cfg["headline_feeds"][group])
    sec = Section("Headlines (RSS)", "Publications exist; their claims are not verified here."
                  " Rumor wording is labelled.", ["title", "evidence_class"])
    found = [r for r in new_items(conn, "rss", None, as_of, float(cfg["window_hours"]))
             if feed_categories.get(str(r["external_id"]).split(":", 1)[0]) in wanted]
    sec.skipped = {"total_in_window": len(found)}
    for r in found[: int(cfg["top_n"])]:
        label = classify("rss", _text(r), cfg)
        sec.rows.append({"subject": r["external_id"], "title": r["title"] or "",
                         "url": r["url"], "available_at": r["available_at"],
                         "evidence_class": label.evidence_class,
                         "claim_status": label.claim_status})
    return sec


def repos_section(conn: sqlite3.Connection, as_of: str, cfg: Mapping[str, Any], group: str,
                  min_owners: int) -> Section:
    """GitHub repos: age, stars, star and fork growth. Stars are attention, not quality."""
    not_collected = ", ".join(cfg["ai"]["repo_not_collected"])
    sec = Section("GitHub repositories (now = stars)", "Stars and forks measure attention,"
                  " not quality. Brand-new repos with fast stars are often promotion or spam;"
                  f" check them before writing about them. Not collected: {not_collected}.",
                  ["title", "age_days", "now", "change", "stars_per_day", "forks_change",
                   "clone_farm"])
    pattern = keyword_re(cfg["repo_keywords"][group])
    stars = metric_movers(conn, "github", "repo", "stars", as_of, cfg)
    forks = {m.subject_key: m for m in metric_movers(conn, "github", "repo", "forks", as_of, cfg)}
    farms = clone_farm_names(conn, as_of, min_owners)
    t = parse_iso(as_of)
    rows = []
    skipped = 0
    for m in stars:
        item = conn.execute("SELECT title, body, published_at FROM raw_items WHERE"
                            " source_id='github' AND item_type='repo' AND external_id=? AND"
                            " available_at<=?", (m.subject_key, as_of)).fetchone()
        if item is None or not matches(pattern, m.subject_key, item["body"]):
            skipped += 1
            continue
        age = None
        if item["published_at"]:
            age = max((t - parse_iso(item["published_at"])).total_seconds() / 86400, 0.0)
        f = forks.get(m.subject_key)
        name = m.subject_key.split("/", 1)[-1].lower()
        rows.append(mover_row(
            m, cfg, age_days=_r(age, 1),
            stars_per_day=_r(m.now / max(age, 1.0), 2) if age is not None else None,
            forks_change=_r(f.change, 2) if f else None, clone_farm=name in farms))
    rows.sort(key=lambda r: (r["clone_farm"], r["change"] is None, -(r["change"] or 0.0),
                             -(r["stars_per_day"] or 0.0), r["subject"]))
    sec.skipped = {"other_topics": skipped,
                   "no_reading_at_lookback": sum(1 for r in rows if r["change"] is None)}
    sec.rows = rows[: int(cfg["top_n"])]
    return sec


def markets_section(conn: sqlite3.Connection, as_of: str, cfg: Mapping[str, Any]) -> Section:
    cc = cfg["crypto"]
    sec = Section("Polymarket crypto markets (24h volume)", "Bets show what traders expect,"
                  " not what is true.", ["title", "now", "change"])
    pattern = keyword_re(cc["market_keywords"])
    rows = []
    vols = metric_movers(conn, "polymarket", "market", "volume_24h_usd", as_of, cfg)
    for m in vols:
        if m.now < float(cc["min_market_volume_24h_usd"]):
            continue
        body = None
        if m.raw_item_id is not None:
            r = conn.execute("SELECT body FROM raw_items WHERE id=?", (m.raw_item_id,)).fetchone()
            body = r["body"] if r else None
        if matches(pattern, m.title, body):
            rows.append(m)
    rows.sort(key=lambda m: (-m.now, m.subject_key))
    sec.skipped = {"not_crypto_or_below_minimum": len(vols) - len(rows)}
    sec.rows = [mover_row(m, cfg) for m in rows[: int(cfg["top_n"])]]
    return sec


# ---- reports ---------------------------------------------------------------------------------
def build_report(conn: sqlite3.Connection, group: str, as_of: str, cfg: Mapping[str, Any],
                 scoring: Mapping[str, Any], credibility: Mapping[str, float],
                 feed_categories: Mapping[str, str]) -> Report:
    if group not in GROUPS:
        raise ValueError(f"unknown report group {group!r}; expected one of {GROUPS}")
    as_of = normalize_iso(as_of)
    scorer = Scorer(conn, dict(scoring), dict(credibility))
    min_owners = int(scoring["clone_farm"]["min_owners"])
    narr, quiet, run = narratives(conn, as_of, cfg["groups"][group], cfg, scorer)
    sections: list[Section] = []
    if group == "crypto":
        cc = cfg["crypto"]
        sections += [
            movers_section(conn, as_of, cfg, title="Protocol TVL movers (DeFiLlama)",
                           note="Measured TVL in USD; moves can come from token prices.",
                           source="defillama", subject_type="protocol", metric="tvl_usd",
                           min_now=float(cc["min_protocol_tvl_usd"])),
            movers_section(conn, as_of, cfg, title="Chain TVL movers (DeFiLlama)",
                           note="Measured TVL in USD.", source="defillama",
                           subject_type="chain", metric="tvl_usd",
                           min_now=float(cc["min_chain_tvl_usd"])),
            listings_section(conn, as_of, cfg, title="New on DeFiLlama",
                             note="Newly listed on DeFiLlama in the window. A listing is not"
                             " necessarily a launch.", source="defillama",
                             item_type="protocol"),
            movers_section(conn, as_of, cfg, title="Solana network (RPC)",
                           note="Measured from public RPC performance samples.",
                           source="solana_rpc", subject_type="network", metric="non_vote_tps"),
            markets_section(conn, as_of, cfg),
            repos_section(conn, as_of, cfg, group, min_owners),
        ]
    else:
        sections += [
            repos_section(conn, as_of, cfg, group, min_owners),
            _models_section(conn, as_of, cfg),
            listings_section(conn, as_of, cfg, title="New MCP servers (registry)",
                             note="Newly seen in the official MCP registry.",
                             source="mcp_registry", item_type=None),
            listings_section(conn, as_of, cfg, title="New arXiv papers",
                             note="Preprints, not peer reviewed.", source="arxiv",
                             item_type=None),
        ]
    sections.append(headlines_section(conn, as_of, cfg, group, feed_categories))
    return Report(group, as_of, config_hash(cfg), data_watermark(conn, as_of), run, narr, quiet,
                  sections, float(cfg["concentration_warning"]))


def _models_section(conn: sqlite3.Connection, as_of: str, cfg: Mapping[str, Any]) -> Section:
    sec = movers_section(conn, as_of, cfg, title="Hugging Face models (likes)",
                         note="Likes measure attention, not quality.", source="huggingface",
                         subject_type="model", metric="likes",
                         min_now=float(cfg["ai"]["min_model_likes"]))
    sec.rows.sort(key=lambda r: (-(r["change"] or 0.0), r["subject"]))
    return sec


# ---- rendering -------------------------------------------------------------------------------
def _fmt(v: Any) -> str:
    if v is None:
        return "-"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, float):
        return f"{v:,.2f}"
    return str(v)


def render(rep: Report) -> str:
    lines = [f"== {rep.group} intelligence as of {rep.as_of}",
             f"   config {rep.config_hash}  data watermark {rep.data_watermark}"
             f"  score run {rep.score_run}", f"   {rep.disclaimer}", "",
             "-- Narrative activity (weighted mentions in the window; ordered by volume,"
             " which is not a prediction)"]
    if not rep.narratives:
        lines.append("   (no eligible mentions in the window)")
    for n in rep.narratives:
        score = "-" if n.latest_score is None else f"{n.latest_score:.1f}"
        srcs = ", ".join(f"{s} {v:g}" for s, v in n.sources.items())
        lines.append(f"   {n.weighted_mentions:>7.1f}  score {score:>5}  {n.slug}  [{srcs}]")
        if n.top_source_share >= float(rep.concentration_warning):
            top = max(n.sources, key=lambda s: (n.sources[s], s))
            lines.append(f"            ! {n.top_source_share:.0%} of this activity is from {top}"
                         " alone: one source, not broad attention")
        for e in n.evidence:
            lines.append(f"            {e.claim_status:<11} {e.evidence_class:<10}"
                         f" {e.source_id}: {e.title[:90]}")
    if rep.quiet_narratives:
        lines.append(f"   quiet: {', '.join(rep.quiet_narratives)}")
    for sec in rep.sections:
        lines += ["", f"-- {sec.title}", f"   {sec.note}"]
        if not sec.rows:
            lines.append("   (nothing to show)")
        for row in sec.rows:
            cells = []
            for col in sec.columns:
                val = row.get(col)
                if col == "pct":
                    cells.append("-" if val is None else f"{val:+.1%}")
                elif col == "title":
                    cells.append(str(val)[:60])
                else:
                    cells.append(f"{col}={_fmt(val)}")
            lines.append(f"   {cells[0]:<60} " + "  ".join(cells[1:]))
        if sec.skipped:
            lines.append("   " + ", ".join(f"{k}: {v}" for k, v in sec.skipped.items()))
    return "\n".join(lines)
