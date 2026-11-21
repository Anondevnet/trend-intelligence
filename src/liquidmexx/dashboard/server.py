"""Local read-only dashboard server (Python standard library only).

- Binds to 127.0.0.1 only; there is no option to listen on other interfaces.
- Answers GET and HEAD only; every other method gets 405. There are no forms or buttons.
- Rejects requests whose Host header is not this loopback address (DNS-rebinding guard).
- Opens the database read-only with ``query_only`` on, per request, and never migrates.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from liquidmexx.config import (
    load_budgets,
    load_detectors,
    load_intel,
    load_models,
    load_publishing,
    load_scoring,
    rss_feed_categories,
    source_credibility,
)
from liquidmexx.dashboard import render
from liquidmexx.dashboard.data import Config, connect_readonly, drafts_view, overview
from liquidmexx.intel.reports import build_report
from liquidmexx.settings import get_settings
from liquidmexx.timeutil import now_iso

log = logging.getLogger("liquidmexx.dashboard")

HOST = "127.0.0.1"
DEFAULT_PORT = 8741
SECURITY_HEADERS = {
    "Content-Security-Policy": ("default-src 'none'; style-src 'self'; base-uri 'none';"
                                " form-action 'none'; frame-ancestors 'none'"),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


def load_config() -> Config:
    s = get_settings()
    return Config(load_scoring(), source_credibility(), load_detectors(), load_intel(),
                  load_budgets(), load_publishing(), load_models(), rss_feed_categories(),
                  s.config_dir, s.data_dir, s.live_posting_opt_in, s.live_llm_opt_in)


def render_path(path: str, conn: sqlite3.Connection, cfg: Config, as_of: str
                ) -> tuple[int, str, str]:
    """(status, content type, body) for a request path. Pure reads only."""
    route = path.split("?", 1)[0].rstrip("/") or "/"
    if route == "/style.css":
        return 200, "text/css; charset=utf-8", render.CSS
    if route == "/":
        return 200, "text/html; charset=utf-8", render.overview_page(overview(conn, cfg, as_of))
    if route == "/drafts":
        return 200, "text/html; charset=utf-8", render.drafts_page(
            drafts_view(conn, cfg, as_of), as_of)
    if route in ("/intel/crypto", "/intel/ai"):
        group = route.rsplit("/", 1)[1]
        rep = build_report(conn, group, as_of, cfg.intel, cfg.scoring, cfg.credibility,
                           cfg.feed_categories)
        return 200, "text/html; charset=utf-8", render.report_page(rep)
    return 404, "text/html; charset=utf-8", render.error_page("Not found", "No such page.")


def make_handler(db_path: Path, port: int,
                 config_loader: Callable[[], Config] = load_config,
                 clock: Callable[[], str] = now_iso) -> type[BaseHTTPRequestHandler]:
    allowed_hosts = {f"{HOST}:{port}", f"localhost:{port}"}

    class Handler(BaseHTTPRequestHandler):
        server_version = "liquidmexx-dashboard"
        sys_version = ""

        def _send(self, status: int, ctype: str, body: str, *, head: bool = False,
                  extra: dict[str, str] | None = None) -> None:
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            for k, v in {**SECURITY_HEADERS, **(extra or {})}.items():
                self.send_header(k, v)
            self.end_headers()
            if not head:
                self.wfile.write(data)

        def _handle(self, head: bool) -> None:
            if self.headers.get("Host", "") not in allowed_hosts:
                self._send(421, "text/plain; charset=utf-8", "unexpected Host header\n",
                           head=head)
                return
            try:
                conn = connect_readonly(db_path)
            except (FileNotFoundError, sqlite3.Error) as exc:
                self._send(503, "text/html; charset=utf-8",
                           render.error_page("Database unavailable", str(exc)), head=head)
                return
            try:
                status, ctype, body = render_path(self.path, conn, config_loader(), clock())
            except Exception:
                log.exception("dashboard page failed: %s", self.path)
                status, ctype, body = 500, "text/html; charset=utf-8", render.error_page(
                    "Error", "This page failed to render. See the terminal log.")
            finally:
                conn.close()
            self._send(status, ctype, body, head=head)

        def do_GET(self) -> None:
            self._handle(head=False)

        def do_HEAD(self) -> None:
            self._handle(head=True)

        def _refuse(self) -> None:
            self._send(405, "text/plain; charset=utf-8", "read-only dashboard: GET only\n",
                       extra={"Allow": "GET, HEAD"})

        do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = _refuse

        def log_message(self, fmt: str, *args: object) -> None:
            log.info("%s %s", self.address_string(), fmt % args)

    return Handler


def make_server(port: int = DEFAULT_PORT, db_path: Path | None = None) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((HOST, port),
                               make_handler(db_path or get_settings().db_path, port))


def serve(port: int = DEFAULT_PORT) -> int:
    try:
        server = make_server(port)
    except OSError as exc:
        print(f"cannot listen on {HOST}:{port}: {exc.strerror or exc}. Another program may be"
              " using that port; try --port with a free one.")
        return 1
    print(f"Liquidmexx dashboard (read-only) at http://{HOST}:{port}/  - Ctrl+C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
