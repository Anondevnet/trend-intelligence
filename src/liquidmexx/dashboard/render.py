"""Server-side HTML for the dashboard. No JavaScript, no forms, no buttons.

Every dynamic value goes through ``esc``. Links from collected data are kept only when they are
plain http(s) URLs.
"""

from __future__ import annotations

import html
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from liquidmexx.dashboard.data import Command, DraftsView, Overview, TrendRow
from liquidmexx.intel.reports import Report
from liquidmexx.timeutil import parse_iso

LOCAL_TZ = "America/Detroit"
DASH = "\u2013"  # en dash for missing values

CSS = """
:root{--bg:#fbfbfa;--fg:#1d1d1b;--muted:#6b6b66;--line:#e2e1dc;--card:#fff;--warn:#9a3412;
--warnbg:#fff4ec;--ok:#166534;--okbg:#effaf2;--accent:#1d4ed8}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--fg:#ecebe6;--muted:#a3a29b;
--line:#2f2f2c;--card:#1c1c1a;--warn:#fdba74;--warnbg:#2b1a0e;--ok:#86efac;--okbg:#0f2617;
--accent:#93c5fd}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 -apple-system,
BlinkMacSystemFont,"Segoe UI",sans-serif}
main{max-width:1100px;margin:0 auto;padding:16px}
header{display:flex;flex-wrap:wrap;gap:8px 20px;align-items:baseline;margin-bottom:8px}
h1{font-size:20px;margin:0}h2{font-size:16px;margin:0 0 6px}
nav a{margin-right:14px;color:var(--accent)}
.meta,.note{color:var(--muted);font-size:12.5px}
section{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px 14px;
margin:12px 0;overflow-x:auto}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}
th,td{text-align:left;padding:4px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-weight:600;color:var(--muted);font-size:12.5px}
td.n,th.n{text-align:right}
.pill{display:inline-block;padding:0 7px;border-radius:9px;font-size:12px;border:1px solid
var(--line)}
.ok{color:var(--ok);background:var(--okbg)}.warn{color:var(--warn);background:var(--warnbg)}
.flag{color:var(--warn);font-size:12.5px}
.ev{color:var(--muted);font-size:12.5px;margin-left:12px}
a{color:var(--accent)}
/* command center: dark, like the reference mockups */
body.cc{--bg:#0b1220;--fg:#e6edf7;--muted:#8b9bb4;--line:#22304a;--card:#111a2e;--accent:#60a5fa;
--warn:#fdba74;--warnbg:#2b1a0e;--ok:#86efac;--okbg:#0f2617}
.brand{display:flex;gap:12px;align-items:center}.live{color:#86efac;font-size:12px;letter-spacing:.08em}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:10px;margin:12px 0}
.kpi{background:var(--card);border:1px solid var(--line);border-top:3px solid var(--accent);
border-radius:10px;padding:12px 14px}
.kpi.warn{border-top-color:#f59e0b}.kpi.na{border-top-color:#475569}
.kpi .l{color:var(--muted);font-size:12.5px}.kpi .v{font-size:30px;font-weight:700;margin:2px 0}
.kpi.na .v{color:var(--muted)}.kpi .s{color:var(--muted);font-size:12px}
.grid2{display:grid;grid-template-columns:2fr 1fr;gap:10px}
.grid2>section{min-width:0;overflow-wrap:anywhere}
.grid2 ul{padding-left:18px;margin:4px 0}
.grid2 li{margin:6px 0}
@media (max-width:800px){.grid2{grid-template-columns:1fr}}
svg.chart{width:100%;height:auto}
.stage{display:inline-block;padding:0 8px;border-radius:9px;font-size:12px;border:1px solid
var(--line)}.stage.Accelerating{color:#86efac}.stage.Emerging{color:#60a5fa}
body .card{border:1px solid var(--line);border-radius:8px;padding:10px;margin:8px 0;
background:var(--card)}
textarea{width:100%;font:inherit;color:var(--fg);background:var(--card);
border:1px solid var(--line);border-radius:6px;padding:8px;resize:vertical}
.stage.Cooling{color:#fdba74}.stage.Detection,.stage.Steady{color:var(--muted)}
.glance{border-left:4px solid var(--accent)}.glance p{margin:4px 0;font-size:15px}
.btn{display:inline-block;margin:6px 8px 0 0;padding:7px 14px;border-radius:8px;
border:1px solid var(--accent);color:var(--accent);text-decoration:none;font-weight:600}
.btn.primary{background:var(--accent);color:var(--bg)}
details.fold{background:var(--card);border:1px solid var(--line);border-radius:8px;margin:8px 0}
details.fold>summary{cursor:pointer;padding:10px 14px;font-weight:600}
details.fold>section{border:0;margin:0;border-top:1px solid var(--line);border-radius:0}
.steps{display:flex;flex-wrap:wrap;gap:10px;margin:8px 0}
.steps div{flex:1 1 200px;border:1px solid var(--line);border-radius:8px;padding:8px 12px}
dt{font-weight:600;margin-top:10px}dd{margin:2px 0 0}
"""


def esc(v: Any) -> str:
    return html.escape("" if v is None else str(v), quote=True)


def num(v: float | None, nd: int = 1) -> str:
    return DASH if v is None else f"{v:,.{nd}f}"


def usd(v: float) -> str:
    return f"${v:,.4f}"


def local(ts: str | None) -> str:
    """UTC ISO string shown in America/Detroit, with the UTC value kept in a tooltip."""
    if not ts:
        return DASH
    try:
        dt = parse_iso(ts).astimezone(ZoneInfo(LOCAL_TZ))
    except Exception:
        return esc(ts)
    return f'<span title="{esc(ts)}">{esc(dt.strftime("%Y-%m-%d %H:%M %Z"))}</span>'


def link(url: str | None, text: str) -> str:
    if url and url.lower().startswith(("https://", "http://")):
        return f'<a href="{esc(url)}" rel="noopener noreferrer nofollow">{esc(text)}</a>'
    return esc(text)


def pill(text: str, good: bool) -> str:
    return f'<span class="pill {"ok" if good else "warn"}">{esc(text)}</span>'


def table(head: Sequence[str], rows: Iterable[Sequence[str]], numeric: set[int] | None = None
          ) -> str:
    numeric = numeric or set()
    th = "".join(f'<th{" class=n" if i in numeric else ""}>{esc(h)}</th>'
                 for i, h in enumerate(head))
    body = "".join("<tr>" + "".join(f'<td{" class=n" if i in numeric else ""}>{c}</td>'
                                    for i, c in enumerate(r)) + "</tr>" for r in rows)
    return f"<table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table>"


def flag_line(share: float | None, source: str | None, threshold: float) -> str:
    if share is None or source is None or share < threshold:
        return ""
    return (f'<div class="flag">⚠ {share:.0%} of this activity is from {esc(source)} alone:'
            " one source, not broad attention</div>")


def page(title: str, body: str, as_of: str, *, refresh: int | None = None,
         dark: bool = False) -> str:
    meta = f"<meta http-equiv=refresh content={refresh}>" if refresh else ""
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>" + meta +
        '<meta name=viewport content="width=device-width,initial-scale=1">'
        '<meta name=referrer content="no-referrer">'
        f"<title>{esc(title)}</title><link rel=stylesheet href=/style.css></head>"
        f"<body{' class=cc' if dark else ''}><main>"
        f"<header><h1>{esc(title)}</h1><nav><a href=/>Overview</a>"
        "<a href=/#emerging>Topics</a><a href=/drafts>Post Creator</a>"
        "<a href=/intel/crypto>Crypto report</a><a href=/intel/ai>AI report</a>"
        "<a href=/help>Help</a></nav>"
        f'<span class="meta">as of {local(as_of)} · read-only · local only</span></header>'
        f"{body}"
        '<p class="note">Every number on this page is read from data/liquidmexx.db when the page'
        " loads. The dashboard opens the database read-only and cannot post, approve or change"
        " anything. Descriptive only: not a prediction and not investment advice.</p>"
        "</main></body></html>")


# ---- overview --------------------------------------------------------------------------------
def _readiness(ov: Overview) -> str:
    r = ov.readiness
    lock = (pill("detectors.yaml matches the lock", True) if r.lock_problem is None
            else pill(f"lock problem: {r.lock_problem}", False))
    state = pill("ready to run" if r.ready else "not ready", r.ready)
    rows = [(esc(s.source_id), str(s.live_days),
             pill("ready", True) if s.days_needed == 0 else esc(f"{s.days_needed} more"),
             esc(s.first_day or DASH), local(s.last_item)) for s in r.sources]
    return (
        "<section><h2>Stage 6 comparison: live days per source</h2>"
        f"<p>{state} {lock} <span class=meta>needs {r.min_live_days}+ live days in at least"
        f" {r.min_ready_sources} sources ({r.ready_sources} ready now). Earliest run"
        f" {esc(r.first_comparison)}; if it reports INSUFFICIENT DATA, it is re-run once a week"
        " with the same locked settings and every result is reported. Sources that only record"
        " measurements (e.g. solana_rpc) have no items, so they never count as ready.</span></p>"
        + table(["source", "live days", "status", "first live day", "last live item"], rows,
                {1}) + "</section>")


def _trend_cells(t: TrendRow, threshold: float) -> list[str]:
    srcs = ", ".join(f"{esc(s)} {v:g}" for s, v in t.sources.items()) or DASH
    return [esc(t.slug) + flag_line(t.top_source_share, t.top_source, threshold),
            num(t.score), num(t.confidence, 2), num(t.window_mentions), srcs]


def _trends(ov: Overview) -> str:
    run = ov.score_run
    head = (f"scoring run {esc(run['id'])} ({esc(run['scorer_version'])}), as of"
            f" {local(run['as_of'])}" if run else "no scoring run stored yet")
    rows = [_trend_cells(t, ov.concentration_warning) for t in ov.trends]
    return (
        "<section><h2>Trends</h2>"
        f'<p class="note">{head}. Mentions are weighted eligible mentions in the last 24h.'
        " The scorer has not beaten plain mention volume so far, so scores are descriptive."
        f" Topics where one source supplies {ov.concentration_warning:.0%}+ of the activity"
        " are flagged.</p>"
        + table(["trend", "score", "confidence", "mentions 24h", "by source"], rows, {1, 2, 3})
        + "</section>")


def _posting(ov: Overview) -> str:
    p = ov.posting
    counts = ", ".join(f"{esc(k)} {v}" for k, v in sorted(p.queue_counts.items())) or "empty"
    rows = [(esc(d["id"]), esc(d["status"]), esc(d["claim_status"]), str(d["sources"]),
             local(d["created_at"]), esc(d["text"])) for d in p.open_drafts]
    drafts = table(["id", "status", "claim", "sources", "created", "text"], rows, {0, 3}) \
        if rows else '<p class="note">No open drafts.</p>'
    return (
        "<section><h2>Posting and draft queue</h2><p>"
        f"mode {pill(p.mode, p.mode == 'off')} "
        f"kill switch {pill('engaged' if p.kill_switch else 'off', True)} "
        f"live opt-in {pill('set' if p.live_opt_in else 'not set', not p.live_opt_in)} "
        f"live X transport {pill(p.live_transport, True)}</p>"
        f'<p class="note">Queue: {counts}. Approving and publishing happen only in the'
        " terminal (`liquidmexx queue`), never here.</p>" + drafts + "</section>")


def _spend(ov: Overview) -> str:
    rows = []
    for s in ov.spend:
        share = f"{s.spent_today / s.cap:.0%}" if s.cap else DASH
        rows.append((esc(s.provider), usd(s.spent_today), usd(s.cap), share, usd(s.lifetime)))
        for tier, spent, cap in s.tiers:
            rows.append((f"&nbsp;&nbsp;{esc(s.provider)} · {esc(tier)}", usd(spent), usd(cap),
                         f"{spent / cap:.0%}" if cap else DASH, ""))
    llm = (f"Claude calls: live_calls {pill(str(ov.llm_live_calls).lower(), not ov.llm_live_calls)}"
           f" opt-in {pill('set' if ov.llm_live_opt_in else 'not set', not ov.llm_live_opt_in)}"
           " · X posting: off unless mode is live")
    return ("<section><h2>Spend today (UTC day) vs daily caps</h2>"
            f"<p>{llm}</p>"
            + table(["provider", "today", "cap", "used", "lifetime"], rows, {1, 2, 3, 4})
            + "</section>")


def _runs(ov: Overview) -> str:
    rows = [(esc(r["source_id"]), esc(r["mode"]), pill(str(r["status"]), r["status"] == "ok"),
             local(r["started_at"]), str(r["items_new"]), str(r["observations_written"]),
             esc((r["error"] or "")[:160])) for r in ov.runs]
    counts = " · ".join(f"{esc(k)} {v:,}" for k, v in ov.counts.items())
    return ("<section><h2>Collection</h2>"
            f'<p class="note">Last run per source. Stored rows: {counts}. Data watermark'
            f" {local(ov.data_watermark)}.</p>"
            + table(["source", "mode", "status", "started", "new items", "observations",
                     "error"], rows, {4, 5}) + "</section>")


def _fmt_growth(g: float | None) -> str:
    return "new" if g is None else f"{g:+.0%}"


STAGE_LABEL = {"Detection": "Just spotted", "Emerging": "Rising", "Accelerating": "Surging",
               "Steady": "Holding steady", "Cooling": "Fading"}
STAGE_HELP = {
    "Detection": "Very few mentions so far (fewer than 3 in 24h).",
    "Emerging": "Mentions are growing over both the last 24h and 6h.",
    "Accelerating": "Mentions have at least doubled over both the last 24h and 6h.",
    "Steady": "Mentions are about flat.",
    "Cooling": "Mentions dropped by 30%+ over the last 6h.",
}
STAGE_ORDER = {"Accelerating": 0, "Emerging": 1, "Detection": 2, "Steady": 3, "Cooling": 4}


def _chart(hourly: list[int]) -> str:
    top = max(hourly) or 1
    w, h = 24 * 20, 140
    bars = "".join(
        f'<rect x="{i * 20 + 2}" y="{h - int(v / top * (h - 10))}" width="16"'
        f' height="{int(v / top * (h - 10))}" rx="2" fill="#60a5fa"><title>{v} mentions,'
        f" {23 - i}h ago</title></rect>" for i, v in enumerate(hourly))
    return (f'<svg class="chart" viewBox="0 0 {w} {h + 14}" role="img"'
            ' aria-label="Mentions per hour, last 24 hours">' + bars
            + f'<text x="2" y="{h + 12}" fill="#8b9bb4" font-size="10">24h ago</text>'
            f'<text x="{w - 2}" y="{h + 12}" fill="#8b9bb4" font-size="10"'
            ' text-anchor="end">now</text></svg>')


def _hour_chart(values: list[int], posts: list[int]) -> str:
    top = max(values) or 1
    w, h = 24 * 20, 120
    bars = "".join(
        f'<rect x="{i * 20 + 2}" y="{h - int(v / top * (h - 10))}" width="16"'
        f' height="{int(v / top * (h - 10))}" rx="2" fill="#a78bfa"><title>{v} reactions on'
        f" {posts[i]} posts made {23 - i}h ago</title></rect>" for i, v in enumerate(values))
    return (f'<svg class="chart" viewBox="0 0 {w} {h + 14}" role="img"'
            ' aria-label="Reactions on Mastodon posts by the hour they were posted">' + bars
            + f'<text x="2" y="{h + 12}" fill="#8b9bb4" font-size="10">24h ago</text>'
            f'<text x="{w - 2}" y="{h + 12}" fill="#8b9bb4" font-size="10"'
            ' text-anchor="end">now</text></svg>')


def _pulse(c: Command) -> str:
    s = c.social
    if s is None:
        return ('<section><h2>Social pulse</h2><p class="note">No social posts stored yet. The'
                " free Mastodon collector fills this in; X is off.</p></section>")
    posts = "".join(
        f"<li>{link(p['url'], (p['text'][:90] or 'post'))} <span class=note>"
        f"{esc(p['author'] or 'unknown')} \u00b7 {p['engagement']:,} reactions</span></li>"
        for p in s.top_posts) or "<li class=note>No reactions yet.</li>"
    accts = "".join(f"<li>{esc(a)} <span class=note>{v:,} reactions</span></li>"
                    for a, v in s.top_accounts) or "<li class=note>None yet.</li>"
    return ('<div class="grid2"><section><h2>Reactions by hour posted</h2>'
            f"{_hour_chart(s.hourly_engagement, s.hourly_posts)}"
            f"<p class=note>{s.engagement_24h:,} likes, reposts and replies on {s.posts_24h:,}"
            " public Mastodon posts from the last 24h. Each bar is the posts made in one hour,"
            " oldest left; older posts have had longer to collect reactions. Mastodon stands in"
            " while X is off.</p></section>"
            f"<section><h2>Social pulse</h2><h3>Most-reacted posts</h3><ul>{posts}</ul>"
            f"<h3>Accounts getting reactions</h3><ul>{accts}</ul></section></div>")


def _stage(stage: str) -> str:
    return (f'<span class="stage {esc(stage)}" title="{esc(STAGE_HELP.get(stage, ""))}">'
            f"{esc(STAGE_LABEL.get(stage, stage))}</span>")


def _glance(ov: Overview) -> str:
    c = ov.command
    vel = sorted(c.velocities, key=lambda v: (STAGE_ORDER.get(v.stage, 9), -v.n24))
    risers = [v for v in vel if v.stage in ("Accelerating", "Emerging")]
    if risers:
        v = risers[0]
        hot = (f"<b>{esc(v.name or v.slug)}</b> is {esc(STAGE_LABEL[v.stage].lower())}:"
               f" {v.n24} mentions in the last 24h ({esc(_fmt_growth(v.g24h))} vs the day"
               " before).")
    else:
        hot = "Nothing is rising right now."
    fading = sum(1 for v in vel if v.stage == "Cooling")
    steady = sum(1 for v in vel if v.stage in ("Steady", "Detection"))
    lines = [f"<p>{hot} {steady} topics are holding steady or just starting; {fading} are"
             " fading.</p>"]
    if c.social is not None:
        lines.append(f"<p>Social check (Mastodon): {c.social.engagement_24h:,} likes, reposts and"
                     f" replies on {c.social.posts_24h:,} posts in the last 24h.</p>")
    live = [r for r in ov.runs if r["mode"] == "live"]
    bad = [r for r in live if r["status"] != "ok"]
    spent = sum(sp.spent_today for sp in ov.spend)
    issues = ("; needs a look: " + ", ".join(
        f"{esc(r['source_id'])} ({esc((r['error'] or r['status'])[:48])})" for r in bad)
        if bad else "")
    lines.append(f'<p class="note">Health: {len(live) - len(bad)} of {len(live)} collectors OK'
                 f"{issues} \u00b7 spent today ${spent:,.2f} \u00b7 posting is"
                 f" {esc(ov.posting.mode.upper())} (nothing can post by itself).</p>")
    return ('<section class="glance"><h2>Right now</h2>' + "".join(lines)
            + '<a class="btn primary" href=/drafts>Write a post about it \u2192</a>'
            '<a class="btn" href=/help>How to read this page</a></section>')


def _command(c: Command) -> str:
    kpis = "".join(f'<div class="kpi {esc(k.tone)}" title="{esc(k.help)}">'
                   f'<div class="l">{esc(k.label)}</div>'
                   f'<div class="v">{esc(k.value)}</div><div class="s">{esc(k.sub)}</div></div>'
                   for k in c.kpis)
    vel = sorted(c.velocities, key=lambda v: (STAGE_ORDER.get(v.stage, 9), -v.n24, v.slug))
    rows = [(esc(v.name or v.slug), _stage(v.stage), str(v.n24), _fmt_growth(v.g24h),
             _fmt_growth(v.g6h), str(v.sources)) for v in vel]
    tbl = table(["topic", "status", "mentions (24h)", "vs yesterday", "last 6h", "sources"],
                rows, {2, 3, 4, 5}) if rows else \
        '<p class="note">No linked mentions in the last 48h yet.</p>'
    return (f'<div class="kpis">{kpis}</div><div class="grid2"><section><h2>Mentions per hour'
            f"</h2>{_chart(c.hourly)}<p class=note>Each bar is one hour, oldest on the left."
            " Updates every minute; new data arrives every 15 minutes.</p>"
            '</section><section><h2>Quick guide</h2><p class="note">Hover a tile for a plain'
            " explanation. <b>n/a</b> means the data is not collected, so we show nothing rather"
            " than invent a number. Status words are simple rules on mention growth, not"
            ' predictions. <a href=/help>Full guide</a></p></section></div>'
            + _pulse(c)
            + f'<section id=emerging><h2>Topics, rising first</h2>{tbl}</section>')


def _fold(summary: str, inner: str) -> str:
    return f'<details class="fold"><summary>{esc(summary)}</summary>{inner}</details>'


def _ready_posts(ov: Overview) -> str:
    if not ov.suggested:
        return ('<section><h2>Post ready to copy</h2><p class="note">Nothing is rising or'
                " engaging enough right now to suggest a post.</p></section>")
    cards = "".join(_draft_card(c.name, c.text, "") for c in ov.suggested)
    return ('<section><h2>Posts ready to copy</h2><p class="note">Click inside a box, press'
            " Cmd+A then Cmd+C, and paste it into X yourself. Written from real numbers."
            f' <a href=/drafts>More suggestions and why</a></p>{cards}</section>')


def overview_page(ov: Overview) -> str:
    body = (_glance(ov) + _ready_posts(ov) + _command(ov.command)
            + "<h2>Under the hood</h2>"
            '<p class="note">Technical detail. You do not need these to use the dashboard.</p>'
            + _fold("Is the data healthy? (collection)", _runs(ov))
            + _fold("Detailed trend scores", _trends(ov))
            + _fold("Posting safety switches and queue", _posting(ov))
            + _fold("Spend vs daily limits", _spend(ov))
            + _fold("When can we test the detectors? (Stage 6)", _readiness(ov)))
    return page("Trend Intelligence \u00b7 Command Center", body, ov.as_of, refresh=60,
                dark=True)


# ---- intel reports ---------------------------------------------------------------------------
def report_page(rep: Report) -> str:
    parts = [
        f'<p class="note">config {esc(rep.config_hash)} · data watermark'
        f" {local(rep.data_watermark)} · score run {esc(rep.score_run or DASH)}."
        f" {esc(rep.disclaimer)}</p>",
        "<section><h2>Narrative activity</h2>"
        '<p class="note">Weighted mentions in the last 24h, ordered by volume (not a'
        " prediction). Evidence is labelled: verified only means a source's API returned this"
        " value or item.</p>"]
    rows = []
    for n in rep.narratives:
        ev = "".join(f'<div class="ev">{esc(e.claim_status)} · {esc(e.evidence_class)} ·'
                     f" {esc(e.source_id)}: {link(e.url, e.title[:120])}</div>"
                     for e in n.evidence)
        top = max(n.sources, key=lambda s: (n.sources[s], s)) if n.sources else None
        rows.append((esc(n.slug) + flag_line(n.top_source_share, top, rep.concentration_warning)
                     + ev, num(n.weighted_mentions), num(n.latest_score),
                     ", ".join(f"{esc(s)} {v:g}" for s, v in n.sources.items())))
    parts.append(table(["narrative", "mentions 24h", "score", "by source"], rows, {1, 2})
                 if rows else '<p class="note">No eligible mentions in the window.</p>')
    if rep.quiet_narratives:
        parts.append(f'<p class="note">Quiet: {esc(", ".join(rep.quiet_narratives))}</p>')
    parts.append("</section>")
    for sec in rep.sections:
        cols = [c for c in sec.columns if c != "title"]
        srows = []
        for r in sec.rows:
            cells = [link(r.get("url"), str(r.get("title") or r.get("subject") or ""))]
            for c in cols:
                v = r.get(c)
                if c == "pct":
                    cells.append(DASH if v is None else f"{v:+.1%}")
                elif isinstance(v, bool):
                    cells.append(pill("yes", False) if v else "no")
                elif isinstance(v, float):
                    cells.append(num(v, 2))
                elif c.endswith("_at"):
                    cells.append(local(v))
                else:
                    cells.append(esc(v if v is not None else DASH))
            srows.append(cells)
        skipped = " · ".join(f"{esc(k.replace('_', ' '))}: {v}" for k, v in sec.skipped.items())
        parts.append(
            f"<section><h2>{esc(sec.title)}</h2><p class=note>{esc(sec.note)}</p>"
            + (table(["item", *[c.replace("_", " ") for c in cols]], srows,
                     set(range(1, len(cols) + 1))) if srows
               else '<p class="note">Nothing to show yet.</p>')
            + (f'<p class="note">{skipped}</p>' if skipped else "") + "</section>")
    return page(f"{rep.group.upper()} intelligence report", "".join(parts), rep.as_of)


def error_page(title: str, message: str) -> str:
    return page(title, f"<section><p>{esc(message)}</p></section>",
                datetime.now(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%S.%fZ"))


# ---- post creator ----------------------------------------------------------------------------
def _basis_line(b: dict[str, Any]) -> str:
    if not b:
        return ""
    bits = []
    vol, soc = b.get("volume"), b.get("social")
    if vol:
        bits.append(f"{vol['n24']} mentions across {vol['sources']} sources in 24h")
    if soc:
        bits.append(f"{soc['engagement']:,} reactions on {soc['posts']} Mastodon posts")
    top = [e["engagement"] for e in b.get("exemplars", [])]
    if top:
        bits.append(f"opening style ({esc(b.get('style'))}) copied from the posts that got the"
                    f" most reactions (best: {max(top)})")
    return '<p class="note">Why this topic: ' + "; ".join(bits) + ".</p>" if bits else ""


def _draft_card(title: str, text: str, note_html: str) -> str:
    n = len(text)
    return (f'<div class="card"><p><b>{esc(title)}</b> '
            f'<span class="meta">{n}/280 chars</span></p>'
            f'<textarea readonly rows=4 aria-label="post text">{esc(text)}</textarea>'
            f"{note_html}</div>")


def drafts_page(v: DraftsView, as_of: str) -> str:
    sug = "".join(
        _draft_card(c.name, c.text, _basis_line(c.basis)) for c in v.suggested
    ) or '<p class="note">Nothing is rising or engaging enough right now to suggest a post.</p>'
    queued = "".join(
        _draft_card(f"#{d['id']} \u00b7 {d['status']}", d["text"],
                    f'<p class="note">{esc(d["claim"])} · {d["sources"]} sources · by'
                    f' {esc(d["by"])} · {local(d["created_at"])}</p>' + _basis_line(d["basis"]))
        for d in v.queued) or '<p class="note">The queue is empty.</p>'
    body = (
        '<div class="steps"><div><b>1. Pick</b><br>a suggestion below</div>'
        "<div><b>2. Copy</b><br>click inside the box, then Cmd+A and Cmd+C</div>"
        "<div><b>3. Post</b><br>paste it into X yourself</div></div>"
        "<section><h2>Suggested posts (not saved)</h2>"
        '<p class="note">Written from real numbers: our own mention counts and reactions on'
        " public Mastodon posts about the same topic. The opening style follows what is getting"
        " engagement; no wording is copied. Posts never contain advice, links, @mentions or"
        f" $tickers.</p>{sug}"
        '<p class="note">To save suggestions to the approval queue, run <code>uv run liquidmexx'
        " compose --save</code> in the terminal.</p></section>"
        f"<section><h2>Approval queue</h2><p>posting mode {pill(v.mode, v.mode == 'off')}</p>"
        '<p class="note">Nothing posts automatically. Approving happens only in the terminal'
        f" (<code>liquidmexx queue</code>).</p>{queued}</section>")
    return page("Trend Intelligence \u00b7 Post Creator", body, as_of, dark=True)


# ---- help ------------------------------------------------------------------------------------
def help_page(as_of: str) -> str:
    stages = "".join(f"<dt>{_stage(k)}</dt><dd>{esc(v)}</dd>" for k, v in STAGE_HELP.items())
    body = (
        "<section><h2>What this is</h2><p>A local dashboard that watches AI and crypto sources"
        " (GitHub, Hacker News, Hugging Face, arXiv, news feeds, Polymarket, DeFi data and"
        " Mastodon), finds topics that are heating up, and helps you write posts about them."
        " It only reads stored data. It cannot post anything.</p></section>"
        '<div class="steps"><div><b>1. Look</b><br>Overview: what is rising, and the 8 tiles'
        "</div><div><b>2. Write</b><br>Post Creator: copy a ready post</div>"
        "<div><b>3. Post</b><br>paste it into X yourself</div></div>"
        "<section><h2>The 8 tiles</h2><dl>"
        "<dt>Items monitored</dt><dd>How much new material the collectors found in 24 hours."
        "</dd><dt>Emerging trends</dt><dd>Topics whose mentions are growing.</dd>"
        "<dt>Trend velocity</dt><dd>Total mentions this hour compared with the hour before."
        "</dd><dt>Engagement acceleration</dt><dd>Whether likes, reposts and replies are"
        " arriving faster or slower than an hour ago (Mastodon posts for now).</dd>"
        "<dt>Influential accounts</dt><dd>Accounts whose posts got real reactions.</dd>"
        "<dt>Viral probability</dt><dd>Always n/a for now. We have no method that has proven"
        " itself, and we will not show a made-up number.</dd>"
        "<dt>Anomaly alerts</dt><dd>Sudden spikes, and topics where one source supplies most of"
        " the buzz.</dd><dt>Narrative shifts</dt><dd>How many topics are surging (up arrow) and"
        " fading (down arrow).</dd></dl></section>"
        f"<section><h2>Topic status words</h2><dl>{stages}</dl>"
        '<p class="note">These are simple rules on mention counts. They describe what already'
        " happened. They are not forecasts.</p></section>"
        "<section><h2>What is switched off, and why</h2><ul>"
        "<li><b>X data:</b> X charges per post read and the account has no credit. Mastodon"
        " stands in for engagement signals.</li>"
        "<li><b>Posting:</b> off. Posts are drafted for you to copy; approving happens only in"
        " the terminal.</li>"
        "<li><b>Claude-written posts:</b> off. Drafts are written by fixed templates from real"
        " numbers.</li></ul></section>"
        "<section><h2>Handy terminal commands</h2><p><code>uv run liquidmexx compose</code>"
        " preview post drafts<br><code>uv run liquidmexx compose --save</code> put them in the"
        " approval queue<br><code>uv run liquidmexx queue list</code> see the queue<br>"
        "<code>uv run liquidmexx status</code> counts, last runs and spend</p></section>")
    return page("Trend Intelligence \u00b7 Help", body, as_of, dark=True)
