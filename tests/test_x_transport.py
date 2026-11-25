"""Live X transport: one signed POST /2/tweets, no retry, secrets never leak. Offline only."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sqlite3
from pathlib import Path

import httpx
import pytest

from tests.test_publishing import NOW, approved, live_ready, publisher, src  # noqa: F401
from trend_intelligence.publishing.x_transport import (
    ENDPOINT,
    XTransport,
    credentials_from,
    oauth_header,
)
from trend_intelligence.settings import Settings

CREDS = {"api_key": "ck", "api_secret": "cs", "access_token": "at", "access_token_secret": "ats"}


def test_signature_matches_an_independent_hmac_sha1() -> None:
    header = oauth_header("POST", ENDPOINT, CREDS, nonce="n0nce", timestamp="1700000000")
    fields = dict(p.split("=", 1) for p in header.removeprefix("OAuth ").split(", "))
    assert fields["oauth_consumer_key"] == '"ck"' and fields["oauth_token"] == '"at"'
    base = ("POST&https%3A%2F%2Fapi.x.com%2F2%2Ftweets&oauth_consumer_key%3Dck%26oauth_nonce"
            "%3Dn0nce%26oauth_signature_method%3DHMAC-SHA1%26oauth_timestamp%3D1700000000"
            "%26oauth_token%3Dat%26oauth_version%3D1.0")
    want = base64.b64encode(hmac.new(b"cs&ats", base.encode(), hashlib.sha1).digest()).decode()
    got = fields["oauth_signature"].strip('"').replace("%2B", "+").replace(
        "%2F", "/").replace("%3D", "=")
    assert got == want
    assert "cs" not in header and "ats" not in header  # secrets sign but are never sent


def test_missing_credentials_mean_no_transport() -> None:
    assert credentials_from(Settings(_env_file=None)) is None  # type: ignore[call-arg]
    partial = Settings(_env_file=None, x_api_key="a", x_api_secret="b")  # type: ignore[call-arg]
    assert credentials_from(partial) is None
    full = Settings(_env_file=None, x_api_key="a", x_api_secret="b",  # type: ignore[call-arg]
                    x_access_token="c", x_access_token_secret="d")
    assert credentials_from(full) == {"api_key": "a", "api_secret": "b", "access_token": "c",
                                      "access_token_secret": "d"}


def test_send_posts_json_once_and_returns_the_id() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(201, json={"data": {"id": "123", "text": "hi"}})

    t = XTransport(CREDS, transport=httpx.MockTransport(handler), clock=lambda: 1700000000.0)
    assert t.live is True and t.send("hello") == "123"
    (req,) = seen
    assert (req.method, str(req.url)) == ("POST", ENDPOINT)
    assert json.loads(req.content) == {"text": "hello"}
    assert req.headers["authorization"].startswith("OAuth ")


@pytest.mark.parametrize("status,hint", [(402, "no X credit"), (403, "403"), (429, "429")])
def test_errors_raise_without_retrying_and_without_secrets(status: int, hint: str) -> None:
    calls: list[int] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(status, json={"title": "nope"})

    t = XTransport(CREDS, transport=httpx.MockTransport(handler))
    with pytest.raises(RuntimeError, match=hint) as exc:
        t.send("x")
    assert len(calls) == 1
    assert "ats" not in str(exc.value) and "Authorization" not in str(exc.value)


def test_publisher_posts_an_approved_draft_through_the_x_transport(
        conn: sqlite3.Connection, src: int, tmp_path: Path) -> None:  # noqa: F811
    from trend_intelligence.publishing import status_of
    bodies: list[bytes] = []

    def handler(req: httpx.Request) -> httpx.Response:
        bodies.append(req.content)
        return httpx.Response(201, json={"data": {"id": "999"}})

    did = approved(conn, src)
    pub = publisher(conn, tmp_path, "live",
                    XTransport(CREDS, transport=httpx.MockTransport(handler)), opt_in=True)
    out = pub.publish(did, "human:tester", now=NOW)
    assert out.ok and out.external_id == "999" and status_of(conn, did) == "published_live"
    again = pub.publish(did, "human:tester", now=NOW)
    assert not again.ok and len(bodies) == 1  # never posts the same draft twice


def test_a_failed_send_is_recorded_and_not_retried(
        conn: sqlite3.Connection, src: int, tmp_path: Path) -> None:  # noqa: F811
    from trend_intelligence.publishing import status_of
    calls: list[int] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(402, json={})

    did = approved(conn, src)
    pub = publisher(conn, tmp_path, "live",
                    XTransport(CREDS, transport=httpx.MockTransport(handler)), opt_in=True)
    out = pub.publish(did, "human:tester", now=NOW)
    assert not out.ok and "no X credit" in out.reason
    assert status_of(conn, did) != "approved"       # cannot be retried into a double post
    assert not pub.publish(did, "human:tester", now=NOW).ok and len(calls) == 1
