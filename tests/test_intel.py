"""Stage 7 intelligence reports: evidence labels, point-in-time movers, reproducibility."""

from __future__ import annotations

import inspect
import socket
import sqlite3
from typing import Any

import pytest

from liquidmexx import cli
from liquidmexx.config import load_intel, rss_feed_categories
from liquidmexx.intel import build_report, classify, is_rumor, metric_movers, new_items, render
from liquidmexx.intel import movers as movers_mod
from liquidmexx.intel import reports as reports_mod
from liquidmexx.store import Observation, RawItem
from tests.test_scoring import CRED, T, World, at, cfg

FEEDS = {"coindesk": "crypto", "hf_blog": "ai"}


def icfg(**over: Any) -> dict[str, Any]:
    c = load_intel()
    c.update(over)
    return c


def narrative(w: World, slug: str, category: str) -> int:
    cur = w.conn.execute(
        "INSERT INTO trends(slug, name, category, kind, origin, created_at)"
        " VALUES (?,?,?,'narrative','taxonomy',?)", (slug, slug, category, T))
    return int(cur.lastrowid or 0)


def obs(w: World, source: str, stype: str, key: str, metric: str, value: float, when: str,
        *, available: str | None = None, ref: tuple[str, str] | None = None) -> None:
    w.store.ensure_source(source, "test")
    w.store.add_observations(source, [Observation(
        stype, key, metric, value, observed_at=when, available_at=available or when,
        item_ref=ref)], run_id=None, fetched_at=when)


def raw(w: World, source: str, item_type: str, ext: str, when: str, *, title: str = "t",
        body: str | None = None, published: str | None = None, run: int | None = None,
        trend: int | None = None) -> int:
    rid, _ = w.store.insert_raw_item(
        source, RawItem(item_type, ext, title=title, body=body, url=f"https://x.test/{ext}",
                        published_at=published),
        run if run is not None else w.run(source), seen_at=when, available_at=when)
    if trend is not None:
        w.conn.execute(
            "INSERT INTO trend_mentions(trend_id, raw_item_id, source_id, available_at,"
            " linked_at, linker_version) VALUES (?,?,?,?,?, 'v1')",
            (trend, rid, source, when, when))
    return rid


def report(conn: sqlite3.Connection, group: str, as_of: str = T, **over: Any) -> Any:
    return build_report(conn, group, as_of, icfg(**over), cfg(), CRED, FEEDS)


# ---- evidence labels -------------------------------------------------------------------------
def test_evidence_classes_and_claim_status() -> None:
    c = icfg()
    assert classify("defillama", "anything", c).claim_status == "verified"
    assert classify("rss", "Coinbase adds Solana trading", c).claim_status == "speculative"
    assert classify("hackernews", "Show HN: a tool", c).evidence_class == "discussion"
    assert classify("rss", "Exchange reportedly plans token", c).claim_status == "rumor"
    assert classify("hackernews", "Unconfirmed: big partnership", c).claim_status == "rumor"
    # a measured value or a market is never relabelled by wording
    assert classify("github", "rumor-mill repo", c).claim_status == "verified"
    assert classify("polymarket", "Will the rumor be true?", c).evidence_class == "market_odds"
    assert classify("unknown_source", "x", c).claim_status == "speculative"


def test_rumor_terms_are_whole_words() -> None:
    c = icfg()
    assert is_rumor("Docs leaked ahead of launch", c)
    assert is_rumor("Sources  say the deal is close", c)
    assert not is_rumor("Leakage current in batteries", c)
    assert not is_rumor("Bleak outlook", c)


def test_shipped_intel_config_is_valid_and_rejects_double_labels(tmp_path: Any) -> None:
    c = load_intel()
    assert set(c["groups"]) == {"crypto", "ai"}
    assert set(rss_feed_categories().values()) >= {"crypto", "ai"}
    bad = (cli.get_settings().config_dir / "intel.yaml").read_text().replace(
        "reported_sources: [rss, arxiv]", "reported_sources: [rss, arxiv, github]")
    (tmp_path / "intel.yaml").write_text(bad)
    with pytest.raises(ValueError, match="github"):
        load_intel(tmp_path)


# ---- point-in-time movers --------------------------------------------------------------------
def test_movers_compare_now_with_value_known_at_lookback(conn: sqlite3.Connection) -> None:
    w = World(conn)
    c = icfg()
    obs(w, "defillama", "protocol", "p1", "tvl_usd", 100.0, at(-25))
    obs(w, "defillama", "protocol", "p1", "tvl_usd", 150.0, at(-1))
    obs(w, "defillama", "protocol", "p1", "tvl_usd", 999.0, at(1))          # after T: ignored
    # observed before the lookback point but only known later: not usable as "then"
    obs(w, "defillama", "protocol", "p2", "tvl_usd", 10.0, at(-26), available=at(-2))
    obs(w, "defillama", "protocol", "p2", "tvl_usd", 20.0, at(-1))
    obs(w, "defillama", "protocol", "p3", "tvl_usd", 50.0, at(-10))         # stale at T
    obs(w, "defillama", "protocol", "p4", "tvl_usd", 0.0, at(-24))
    obs(w, "defillama", "protocol", "p4", "tvl_usd", 5.0, at(-1))
    got = {m.subject_key: m for m in metric_movers(conn, "defillama", "protocol", "tvl_usd",
                                                    T, c)}
    assert set(got) == {"p1", "p2", "p4"}
    assert got["p1"].now == 150.0 and got["p1"].then == 100.0 and got["p1"].pct == 0.5
    assert got["p2"].then is None and got["p2"].change is None
    assert got["p4"].change == 5.0 and got["p4"].pct is None


def test_new_items_skip_first_run_backfill_and_out_of_window(conn: sqlite3.Connection) -> None:
    w = World(conn)
    first = w.run("defillama")
    raw(w, "defillama", "protocol", "catalog", at(-2), run=first)          # first-run catalog
    raw(w, "defillama", "protocol", "old", at(-30))
    raw(w, "defillama", "protocol", "new", at(-3))
    raw(w, "defillama", "protocol", "future", at(2))
    w.store.insert_raw_item("defillama", RawItem("protocol", "bf", title="t"), w.run("defillama"),
                            seen_at=at(-1), available_at=at(-1), is_backfill=True)
    assert [r["external_id"] for r in new_items(conn, "defillama", "protocol", T, 24)] == ["new"]


# ---- reports ---------------------------------------------------------------------------------
def crypto_world(conn: sqlite3.Connection) -> World:
    w = World(conn)
    sol = narrative(w, "solana/solana-ecosystem", "solana")
    narrative(w, "depin/depin", "depin")
    narrative(w, "ai-agents/ai-agents", "ai-agents")
    for src in ("rss", "github", "defillama", "polymarket"):
        w.prime(src)
    raw(w, "rss", "article", "coindesk:a1", at(-2), title="Solana fees fall", trend=sol)
    raw(w, "rss", "article", "coindesk:a2", at(-1), title="Exchange reportedly lists SOL",
        trend=sol)
    raw(w, "rss", "article", "hf_blog:b1", at(-1), title="New open model")
    raw(w, "rss", "article", "coindesk:old", at(-40), title="Old news", trend=sol)
    for i, (key, now, then) in enumerate([("big", 2e7, 1e7), ("small", 5e6, 1e6)]):
        raw(w, "defillama", "protocol", key, at(-48 - i), title=key.title())
        obs(w, "defillama", "protocol", key, "tvl_usd", then, at(-24), ref=("protocol", key))
        obs(w, "defillama", "protocol", key, "tvl_usd", now, at(-1), ref=("protocol", key))
    raw(w, "polymarket", "market", "pm:1", at(-5), title="Will Solana flip ETH?")
    raw(w, "polymarket", "market", "pm:2", at(-5), title="Who wins the election?")
    for key in ("pm:1", "pm:2"):
        obs(w, "polymarket", "market", key, "volume_24h_usd", 5e5, at(-1),
            ref=("market", key))
    for owner in ("a", "b", "c"):
        raw(w, "github", "repo", f"{owner}/solana-bot", at(-30), body="solana trading bot",
            published=at(-24 * 10))
        obs(w, "github", "repo", f"{owner}/solana-bot", "stars", 30.0, at(-1),
            ref=("repo", f"{owner}/solana-bot"))
    raw(w, "github", "repo", "dev/sol-sdk", at(-30), body="solana sdk", published=at(-24 * 4))
    obs(w, "github", "repo", "dev/sol-sdk", "stars", 100.0, at(-24), ref=("repo", "dev/sol-sdk"))
    obs(w, "github", "repo", "dev/sol-sdk", "stars", 160.0, at(-1), ref=("repo", "dev/sol-sdk"))
    raw(w, "github", "repo", "dev/llm-kit", at(-30), body="llm agents", published=at(-24))
    obs(w, "github", "repo", "dev/llm-kit", "stars", 500.0, at(-1), ref=("repo", "dev/llm-kit"))
    return w


def section(rep: Any, prefix: str) -> Any:
    return next(s for s in rep.sections if s.title.startswith(prefix))


def test_crypto_report_contents(conn: sqlite3.Connection) -> None:
    crypto_world(conn)
    rep = report(conn, "crypto")
    assert [n.slug for n in rep.narratives] == ["solana/solana-ecosystem"]
    sol = rep.narratives[0]
    assert sol.weighted_mentions == 2.0 and sol.latest_score is None
    assert [e.claim_status for e in sol.evidence] == ["rumor", "speculative"]
    assert rep.quiet_narratives == ["depin/depin"]          # AI narratives are not in crypto
    tvl = section(rep, "Protocol TVL")
    assert [r["subject"] for r in tvl.rows] == ["big"]       # "small" is under the minimum
    assert tvl.rows[0]["pct"] == 1.0 and tvl.rows[0]["claim_status"] == "verified"
    markets = section(rep, "Polymarket")
    assert [r["subject"] for r in markets.rows] == ["pm:1"]  # election market is not crypto
    repos = section(rep, "GitHub")
    assert [r["subject"] for r in repos.rows] == [
        "dev/sol-sdk", "a/solana-bot", "b/solana-bot", "c/solana-bot"]   # farms sort last
    assert repos.rows[0]["change"] == 60.0 and repos.rows[0]["stars_per_day"] == 40.0
    assert [r["clone_farm"] for r in repos.rows] == [False, True, True, True]
    assert "dev/llm-kit" not in [r["subject"] for r in repos.rows]
    heads = section(rep, "Headlines")
    assert [r["subject"] for r in heads.rows] == ["coindesk:a2", "coindesk:a1"]
    text = render(rep)
    assert "not investment advice" in text and "Not collected" in text


def test_ai_report_uses_ai_feeds_and_repos(conn: sqlite3.Connection) -> None:
    crypto_world(conn)
    rep = report(conn, "ai")
    assert [r["subject"] for r in section(rep, "GitHub").rows] == ["dev/llm-kit"]
    assert [r["subject"] for r in section(rep, "Headlines").rows] == ["hf_blog:b1"]
    assert "depin/depin" not in rep.quiet_narratives


def test_single_source_concentration_is_flagged(conn: sqlite3.Connection) -> None:
    w = World(conn)
    pay = narrative(w, "ai-agents/agent-payments", "ai-agents")
    w.prime("mcp_registry")
    w.prime("hackernews")
    for i in range(9):
        raw(w, "mcp_registry", "mcp_server", f"s{i}", at(-1), trend=pay)
    raw(w, "hackernews", "story", "h1", at(-1), trend=pay)
    w.points("hackernews", "h1", 50, at(-1))
    rep = report(conn, "ai")
    assert rep.narratives[0].top_source_share == 0.9
    assert "90% of this activity is from mcp_registry alone" in render(rep)


def test_report_ignores_later_data_and_is_reproducible(conn: sqlite3.Connection) -> None:
    w = crypto_world(conn)
    before = report(conn, "crypto").to_json()
    sol = int(conn.execute("SELECT id FROM trends WHERE slug='solana/solana-ecosystem'")
              .fetchone()[0])
    raw(w, "rss", "article", "coindesk:late", at(3), title="Later", trend=sol)
    obs(w, "defillama", "protocol", "big", "tvl_usd", 9e9, at(2), ref=("protocol", "big"))
    obs(w, "github", "repo", "dev/sol-sdk", "stars", 9999.0, at(-2), available=at(1))
    assert report(conn, "crypto").to_json() == before


def test_report_uses_latest_score_run_known_at_as_of(conn: sqlite3.Connection) -> None:
    crypto_world(conn)
    sol = int(conn.execute("SELECT id FROM trends WHERE slug='solana/solana-ecosystem'")
              .fetchone()[0])
    for run_as_of, score in ((at(-5), 11.0), (at(5), 99.0)):
        cur = conn.execute(
            "INSERT INTO scoring_runs(as_of, scorer_version, linker_version, config_hash,"
            " config_json, trends_scored, computed_at) VALUES (?,?,?,?,?,?,?)",
            (run_as_of, "s2", "v1", run_as_of, "{}", 1, run_as_of))
        conn.execute("INSERT INTO trend_scores(run_id, trend_id, as_of, score, confidence,"
                     " components, evidence) VALUES (?,?,?,?,?,?,?)",
                     (cur.lastrowid, sol, run_as_of, score, 1.0, "{}", "{}"))
    assert report(conn, "crypto").narratives[0].latest_score == 11.0


def test_unknown_group_is_rejected(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="unknown report group"):
        report(conn, "memes")


def test_reports_make_no_network_or_llm_calls(conn: sqlite3.Connection,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_: Any, **__: Any) -> None:
        raise AssertionError("network access during an intel report")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    crypto_world(conn)
    assert report(conn, "crypto").sections and report(conn, "ai").sections
    for mod in (reports_mod, movers_mod):
        src = inspect.getsource(mod)
        assert "httpx" not in src and "anthropic" not in src


def test_cli_intel_on_empty_database(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["intel", "crypto", "--as-of", T]) == 0
    out = capsys.readouterr().out
    assert "crypto intelligence as of" in out and "(no eligible mentions in the window)" in out
    assert cli.main(["intel", "ai", "--json"]) == 0
    assert '"group": "ai"' in capsys.readouterr().out
