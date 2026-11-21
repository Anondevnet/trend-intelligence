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
@media (max-width:800px){.grid2{grid-template-columns:1fr}}
svg.chart{width:100%;height:auto}
.stage{display:inline-block;padding:0 8px;border-radius:9px;font-size:12px;border:1px solid
var(--line)}.stage.Accelerating{color:#86efac}.stage.Emerging{color:#60a5fa}
body .card{border:1px solid var(--line);border-radius:8px;padding:10px;margin:8px 0;
background:var(--card)}
textarea{width:100%;font:inherit;color:var(--fg);background:var(--card);
border:1px solid var(--line);border-radius:6px;padding:8px;resize:vertical}
.stage.Cooling{color:#fdba74}.stage.Detection,.stage.Steady{color:var(--muted)}
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
        f"<header><h1>{esc(title)}</h1><nav><a href=/>Command Center</a>"
        "<a href=#emerging>Emerging Trends</a><a href=/drafts>Post Creator</a>"
        "<a href=/intel/crypto>Crypto report</a><a href=/intel/ai>AI report</a></nav>"
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
           " · X: no live transport")
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


def _command(c: Command) -> str:
    kpis = "".join(f'<div class="kpi {esc(k.tone)}"><div class="l">{esc(k.label)}</div>'
                   f'<div class="v">{esc(k.value)}</div><div class="s">{esc(k.sub)}</div></div>'
                   for k in c.kpis)
    rows = [(esc(v.slug), f'<span class="stage {esc(v.stage)}">{esc(v.stage)}</span>',
             str(v.n24), _fmt_growth(v.g1h), _fmt_growth(v.g6h), _fmt_growth(v.g24h),
             str(v.sources), local(v.first_seen)) for v in c.velocities]
    tbl = table(["trend", "stage", "mentions 24h", "growth 1h", "growth 6h", "growth 24h",
                 "sources", "first seen"], rows, {2, 3, 4, 5, 6}) if rows else \
        '<p class="note">No linked mentions in the last 48h yet.</p>'
    return (f'<div class="kpis">{kpis}</div><div class="grid2"><section><h2>Mentions per hour'
            f"</h2>{_chart(c.hourly)}<p class=note>Linked mentions from collected sources (no X"
            " data). Refreshes every 60s from stored data; collection runs every 15 min.</p>"
            '</section><section><h2>How to read this</h2><p class="note">Stages are simple'
            " rules on mention growth (Detection, Emerging, Accelerating, Steady, Cooling). They"
            " are descriptive, not validated predictions. Growth is shown for 1h, 6h and 24h"
            " because collection runs every 15 minutes, so 5-minute growth is not measurable."
            ' &quot;new&quot; means nothing to compare with yet. n/a tiles need data that is'
            " not collected.</p></section></div>"
            f'<section id=emerging><h2>Emerging trends</h2>{tbl}</section>')


def overview_page(ov: Overview) -> str:
    body = (_command(ov.command) + "<h2>System</h2>" + _readiness(ov) + _trends(ov)
            + _posting(ov) + _spend(ov) + _runs(ov))
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
    bits = [f"style: {esc(b.get('style'))} (from the most-engaged recent posts on the topic)"]
    vol, soc = b.get("volume"), b.get("social")
    if vol:
        bits.append(f"volume: {vol['n24']} mentions, {vol['sources']} sources, {esc(vol['stage'])}")
    if soc:
        bits.append(f"social: {soc['engagement']:,} engagement on {soc['posts']} posts")
    top = [e["engagement"] for e in b.get("exemplars", [])]
    if top:
        bits.append("top exemplar engagement: " + ", ".join(str(x) for x in top))
    return '<p class="note">' + " · ".join(bits) + "</p>"


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
        "<section><h2>Suggested posts (not saved)</h2>"
        '<p class="note">Written from measured activity: our own mention counts and the likes,'
        " reposts and replies on public Mastodon posts about the same topic. The opening style"
        " follows what is getting engagement; no wording is copied. Click a box and press"
        " Cmd+A, Cmd+C to copy, then post it yourself. No advice, links, @mentions or $tickers."
        f"</p>{sug}"
        '<p class="note">To save suggestions to the approval queue, run <code>uv run liquidmexx'
        " compose --save</code> in the terminal.</p></section>"
        f"<section><h2>Approval queue</h2><p>posting mode {pill(v.mode, v.mode == 'off')}</p>"
        '<p class="note">Nothing posts automatically. Approving happens only in the terminal'
        f" (<code>liquidmexx queue</code>).</p>{queued}</section>")
    return page("Trend Intelligence \u00b7 Post Creator", body, as_of, dark=True)
