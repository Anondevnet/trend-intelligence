"""Stage 9 dashboard: loopback only, GET only, read-only database, real stored numbers."""

from __future__ import annotations

import hashlib
import http.client
import sqlite3
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from liquidmexx import cli
from liquidmexx.config import load_budgets, load_publishing
from liquidmexx.costs.budget import BudgetGuard, LLMUsage
from liquidmexx.dashboard import HOST, make_server
from liquidmexx.dashboard.data import connect_readonly, overview
from liquidmexx.dashboard.render import overview_page, report_page
from liquidmexx.dashboard.server import load_config, make_handler, render_path
from liquidmexx.db import connect, migrate
from liquidmexx.intel import build_report
from liquidmexx.publishing import create_draft
from tests.test_intel import FEEDS, narrative, raw
from tests.test_scoring import CRED, T, World, at, cfg


def seed(conn: sqlite3.Connection) -> None:
    w = World(conn)
    pay = narrative(w, "ai-agents/agent-payments", "ai-agents")
    mix = narrative(w, "ai-agents/ai-agents", "ai-agents")
    for src in ("mcp_registry", "hackernews", "rss"):
        w.prime(src)
    for i in range(9):
        raw(w, "mcp_registry", "mcp_server", f"s{i}", at(-1), trend=pay)
    raw(w, "rss", "article", "hf_blog:x", at(-1), trend=pay)
    raw(w, "rss", "article", "hf_blog:y", at(-2), trend=mix,
        title="Agents <script>alert(1)</script>")
    raw(w, "mcp_registry", "mcp_server", "m1", at(-2), trend=mix)
    create_draft(conn, load_publishing(), text="Draft <b>bold</b> & co", sources=[],
                 claim_status="verified", actor="system:test", now=at(-1))
    BudgetGuard(conn, load_budgets()).record(
        "anthropic", "messages", "test", cost_usd=0.0123, ts=at(-1), tier="haiku",
        llm=LLMUsage("claude-haiku-5-5", 10, 10))


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "dash.db"
    conn = connect(path)
    migrate(conn)
    seed(conn)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    return path


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def page(db: Path, path: str) -> tuple[int, str]:
    conn = connect_readonly(db)
    try:
        status, _ctype, body = render_path(path, conn, load_config(), T)
    finally:
        conn.close()
    return status, body


# ---- read-only database ----------------------------------------------------------------------
def test_readonly_connection_cannot_write_or_create(db: Path, tmp_path: Path) -> None:
    conn = connect_readonly(db)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("CREATE TABLE evil(x)")
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO sources(id, kind) VALUES ('x','y')")
    conn.close()
    with pytest.raises(FileNotFoundError):
        connect_readonly(tmp_path / "missing.db")
    assert not (tmp_path / "missing.db").exists()


def test_pages_leave_the_database_unchanged(db: Path) -> None:
    before = sha(db)
    for path in ("/", "/intel/crypto", "/intel/ai", "/style.css", "/nope"):
        page(db, path)
    assert sha(db) == before


# ---- content ---------------------------------------------------------------------------------
def test_overview_shows_stored_numbers_and_flags(db: Path) -> None:
    conn = connect_readonly(db)
    ov = overview(conn, load_config(), T)
    conn.close()
    rows = {t.slug: t for t in ov.trends}
    assert rows["ai-agents/agent-payments"].window_mentions == 10.0
    assert rows["ai-agents/agent-payments"].top_source_share == 0.9
    assert rows["ai-agents/ai-agents"].top_source_share == 0.5
    assert ov.posting.mode == "off" and ov.posting.queue_counts
    anthropic = next(s for s in ov.spend if s.provider == "anthropic")
    assert anthropic.spent_today == pytest.approx(0.0123) and anthropic.cap == 2.0
    assert ("haiku", pytest.approx(0.0123), 0.2) in anthropic.tiers
    assert ov.readiness.lock_problem is None and not ov.readiness.ready
    html = overview_page(ov)
    assert "90% of this activity is from mcp_registry alone" in html
    assert "50% of this activity" not in html
    assert "$0.0123" in html and "$2.0000" in html
    assert "Draft &lt;b&gt;bold&lt;/b&gt; &amp; co" in html and "<b>bold" not in html


def test_report_pages_escape_collected_text(db: Path) -> None:
    status, body = page(db, "/intel/ai")
    assert status == 200
    assert "<script>" not in body and "&lt;script&gt;" in body
    assert "90% of this activity is from mcp_registry alone" in body


def test_only_http_links_are_rendered(db: Path) -> None:
    conn = connect_readonly(db)
    rep = build_report(conn, "ai", T, load_config().intel, cfg(), CRED, FEEDS)
    conn.close()
    rep.narratives[0].evidence[0] = rep.narratives[0].evidence[0].__class__(
        "rss", "bad", "javascript:alert(1)", T, "reported", "speculative")
    html = report_page(rep)
    assert "javascript:" not in html


def test_pages_have_no_forms_buttons_or_scripts(db: Path) -> None:
    for path in ("/", "/intel/crypto", "/intel/ai"):
        status, body = page(db, path)
        assert status == 200
        lowered = body.lower()
        for tag in ("<form", "<button", "<input", "<script", "method="):
            assert tag not in lowered, (path, tag)


# ---- HTTP server -----------------------------------------------------------------------------
@pytest.fixture
def server(db: Path) -> Iterator[int]:
    srv = ThreadingHTTPServer((HOST, 0), BaseHTTPRequestHandler)
    port = srv.server_address[1]
    srv.RequestHandlerClass = make_handler(db, port, load_config, lambda: T)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield port
    srv.shutdown()
    srv.server_close()


def request(port: int, method: str, path: str, host: str | None = None
            ) -> tuple[int, dict[str, str], str]:
    conn = http.client.HTTPConnection(HOST, port, timeout=10)
    conn.putrequest(method, path, skip_host=True)
    conn.putheader("Host", host or f"{HOST}:{port}")
    conn.endheaders()
    resp = conn.getresponse()
    body = resp.read().decode("utf-8")
    headers = {k.lower(): v for k, v in resp.getheaders()}
    conn.close()
    return resp.status, headers, body


def test_server_serves_get_with_security_headers(server: int) -> None:
    status, headers, body = request(server, "GET", "/")
    assert status == 200 and "Liquidmexx dashboard" in body
    assert "form-action 'none'" in headers["content-security-policy"]
    assert headers["x-frame-options"] == "DENY" and headers["cache-control"] == "no-store"
    assert request(server, "GET", "/style.css")[0] == 200
    assert request(server, "GET", "/missing")[0] == 404
    assert request(server, "HEAD", "/")[0] == 200
    assert request(server, "GET", "/", host=f"localhost:{server}")[0] == 200


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
def test_server_refuses_every_write_method(server: int, db: Path, method: str) -> None:
    before = sha(db)
    status, headers, _ = request(server, method, "/")
    assert status == 405 and headers["allow"] == "GET, HEAD"
    assert sha(db) == before


def test_server_rejects_foreign_host_headers(server: int) -> None:
    for host in ("evil.example", f"evil.example:{server}", f"192.168.1.5:{server}"):
        assert request(server, "GET", "/", host=host)[0] == 421


def test_server_binds_loopback_only() -> None:
    srv = make_server(0)
    try:
        assert srv.server_address[0] == "127.0.0.1"
    finally:
        srv.server_close()


def test_missing_database_is_reported_not_created(tmp_path: Path) -> None:
    srv = ThreadingHTTPServer((HOST, 0), BaseHTTPRequestHandler)
    port = srv.server_address[1]
    missing = tmp_path / "none.db"
    srv.RequestHandlerClass = make_handler(missing, port, load_config, lambda: T)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        assert request(port, "GET", "/")[0] == 503
    finally:
        srv.shutdown()
        srv.server_close()
    assert not missing.exists()


def test_cli_has_no_host_option(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        cli.main(["dashboard", "--host", "192.168.1.5"])
    assert "unrecognized arguments" in capsys.readouterr().err


def test_dashboard_never_touches_network_or_llm_code() -> None:
    import inspect

    from liquidmexx.dashboard import data, render, server
    for mod in (data, render, server):
        src: Any = inspect.getsource(mod)
        for banned in ("import httpx", "import anthropic", "from anthropic", "liquidmexx.llm",
                       "liquidmexx.sources", "Publisher(", "urllib.request", "socket."):
            assert banned not in src, (mod.__name__, banned)


def test_busy_port_fails_with_a_message(capsys: pytest.CaptureFixture[str]) -> None:
    from liquidmexx.dashboard import serve

    holder = ThreadingHTTPServer((HOST, 0), BaseHTTPRequestHandler)
    try:
        assert serve(holder.server_address[1]) == 1
    finally:
        holder.server_close()
    assert "cannot listen on 127.0.0.1" in capsys.readouterr().out
