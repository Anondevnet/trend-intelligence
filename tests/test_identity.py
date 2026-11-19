from __future__ import annotations

import sqlite3

from liquidmexx.config import load_taxonomy
from liquidmexx.store import RawItem, Store
from liquidmexx.timeutil import now_iso
from liquidmexx.trends.identity import (
    extract_entity_refs,
    link_new_items,
    load_rules,
    match_text,
    seed_taxonomy,
)

TAXONOMY = {"categories": {
    "mcp": [{"name": "Model Context Protocol", "aliases": ["mcp server"], "exact": ["MCP"],
             "requires_context": ["ai", "llm", "agent"]}],
    "solana": [{"name": "Solana ecosystem", "aliases": ["solana"]}],
    "security": [{"name": "Crypto security incidents", "aliases": ["exploit", "hack"],
                  "requires_context": ["defi", "crypto"]}],
}}


def slugs(conn: sqlite3.Connection, hits: dict[int, int]) -> set[str]:
    return {conn.execute("SELECT slug FROM trends WHERE id=?", (t,)).fetchone()[0] for t in hits}


def test_seed_is_idempotent_and_real_taxonomy_loads(conn: sqlite3.Connection) -> None:
    assert seed_taxonomy(conn, TAXONOMY) > 0
    assert seed_taxonomy(conn, TAXONOMY) == 0
    assert seed_taxonomy(conn, load_taxonomy()) > 0


def test_phrase_exact_and_context_rules(conn: sqlite3.Connection) -> None:
    seed_taxonomy(conn, TAXONOMY)
    rules = load_rules(conn)
    assert slugs(conn, match_text("New MCP tools for AI agents", rules)) == {
        "mcp/model-context-protocol"}
    assert match_text("MCP tools", rules) == {}                       # no AI context
    assert match_text("mcp tools for ai", rules) == {}                # exact is case-sensitive
    assert slugs(conn, match_text("Running an MCP-server for AI", rules)) == {
        "mcp/model-context-protocol"}                                 # hyphen/space tolerant
    assert match_text("Solanaceae plants", rules) == {}               # word boundary
    assert match_text("Life hack for parents", rules) == {}           # requires crypto context
    assert slugs(conn, match_text("DeFi exploit drains pool", rules)) == {
        "security/crypto-security-incidents"}


def test_entity_refs_from_urls() -> None:
    text = ("see https://github.com/Acme/Agent-X.git and github.com/topics/llm and "
            "https://huggingface.co/Org/Model-7B plus https://huggingface.co/spaces/a/b")
    assert extract_entity_refs(text) == [("repo", "acme/agent-x"), ("model", "org/model-7b")]


def test_linker_inherits_availability_and_is_incremental(conn: sqlite3.Connection) -> None:
    seed_taxonomy(conn, TAXONOMY)
    store = Store(conn)
    store.ensure_source("hackernews", "c")
    run = store.start_run("hackernews")
    store.insert_raw_item("hackernews", RawItem(
        "story", "1", title="Solana MCP server for AI agents", url="https://github.com/a/b"),
        run, "2026-11-10T00:00:00.000000Z")
    assert link_new_items(conn) == 3  # solana, mcp, entity repo a/b
    assert link_new_items(conn) == 0  # watermark: nothing re-linked
    avail = {r[0] for r in conn.execute("SELECT available_at FROM trend_mentions")}
    assert avail == {"2026-11-10T00:00:00.000000Z"}
    first = conn.execute("SELECT first_detected_at FROM trends WHERE slug='solana/solana-ecosystem'"
                         ).fetchone()[0]
    assert first == "2026-11-10T00:00:00.000000Z"
    # A later item does not move first detection forward.
    store.insert_raw_item("hackernews", RawItem("story", "2", title="solana again"), run, now_iso())
    link_new_items(conn)
    assert conn.execute("SELECT first_detected_at FROM trends WHERE slug='solana/solana-ecosystem'"
                        ).fetchone()[0] == first


def test_entities_only_confirmed_from_discussion_sources(conn: sqlite3.Connection) -> None:
    seed_taxonomy(conn, TAXONOMY)
    store = Store(conn)
    store.ensure_source("github", "code")
    run = store.start_run("github")
    store.insert_raw_item("github", RawItem("repo", "x/y", title="x/y",
                                            url="https://github.com/x/y"), run, now_iso())
    link_new_items(conn)
    assert conn.execute("SELECT COUNT(*) FROM trends WHERE kind='entity'").fetchone()[0] == 0
