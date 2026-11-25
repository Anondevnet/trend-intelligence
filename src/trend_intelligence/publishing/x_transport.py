"""The one place that can write to X: a single ``POST /2/tweets`` with OAuth 1.0a (user context).

Only ``Publisher`` calls it, and ``Publisher`` refuses unless every switch is on (approved draft,
quality gate, mode "live", TREND_INTEL_LIVE_POSTING=enabled, budget, daily cap, no kill switch).
There is no retry: a failed or ambiguous send is recorded by the publisher and never repeated,
so a flaky connection can never turn into a double post. Nothing else (likes, follows, replies,
deletes, media) is implemented.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from collections.abc import Callable
from urllib.parse import quote

import httpx

from trend_intelligence.settings import Settings

ENDPOINT = "https://api.x.com/2/tweets"


def _enc(value: str) -> str:
    return quote(value, safe="~")


def oauth_header(method: str, url: str, creds: dict[str, str], *, nonce: str, timestamp: str
                 ) -> str:
    """OAuth 1.0a HMAC-SHA1 Authorization header. A JSON body adds no signed parameters."""
    params = {
        "oauth_consumer_key": creds["api_key"],
        "oauth_nonce": nonce,
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": timestamp,
        "oauth_token": creds["access_token"],
        "oauth_version": "1.0",
    }
    norm = "&".join(f"{_enc(k)}={_enc(v)}" for k, v in sorted(params.items()))
    base = "&".join((method.upper(), _enc(url), _enc(norm)))
    key = f"{_enc(creds['api_secret'])}&{_enc(creds['access_token_secret'])}"
    sig = base64.b64encode(hmac.new(key.encode(), base.encode(), hashlib.sha1).digest()).decode()
    params["oauth_signature"] = sig
    return "OAuth " + ", ".join(f'{_enc(k)}="{_enc(v)}"' for k, v in sorted(params.items()))


def credentials_from(settings: Settings) -> dict[str, str] | None:
    """The four OAuth values, or None when any is missing. Never prints them."""
    out: dict[str, str] = {}
    for name in ("api_key", "api_secret", "access_token", "access_token_secret"):
        secret = getattr(settings, f"x_{name}")
        value = secret.get_secret_value() if secret else ""
        if not value:
            return None
        out[name] = value
    return out


class XTransport:
    """Publishes one text post. ``live`` is True: the publisher treats it as a real send."""

    def __init__(self, creds: dict[str, str], *, transport: httpx.BaseTransport | None = None,
                 clock: Callable[[], float] = time.time, timeout_s: float = 20.0) -> None:
        self._creds = creds
        self._clock = clock
        self._client = httpx.Client(timeout=timeout_s, transport=transport,
                                    follow_redirects=False)

    @property
    def live(self) -> bool:
        return True

    def send(self, text: str) -> str:
        auth = oauth_header("POST", ENDPOINT, self._creds, nonce=secrets.token_hex(16),
                            timestamp=str(int(self._clock())))
        resp = self._client.post(ENDPOINT, json={"text": text},
                                 headers={"Authorization": auth, "Accept": "application/json"})
        if resp.status_code != 201:
            hint = " (no X credit on the account)" if resp.status_code == 402 else ""
            raise RuntimeError(f"X returned HTTP {resp.status_code}{hint}: {resp.text[:200]}")
        try:
            return str(resp.json()["data"]["id"])
        except (ValueError, KeyError, TypeError) as exc:
            raise RuntimeError("X answered 201 but without a post id; check the account"
                               " before retrying") from exc
