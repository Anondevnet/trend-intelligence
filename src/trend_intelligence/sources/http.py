"""Polite HTTP client: per-source pacing, bounded retries, usage accounting."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlsplit

import httpx

from trend_intelligence.costs.budget import BudgetGuard

log = logging.getLogger(__name__)

USER_AGENT = "research-collector/0.1 (+python-httpx)"
RETRY_STATUSES = {429, 500, 502, 503, 504}


class SourceHTTPError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class PoliteClient:
    def __init__(
        self,
        provider: str,
        guard: BudgetGuard,
        *,
        min_interval_s: float = 1.0,
        max_retries: int = 3,
        timeout_s: float = 30.0,
        run_id: int | None = None,
        headers: Mapping[str, str] | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.provider = provider
        self.guard = guard
        self.min_interval_s = min_interval_s
        self.max_retries = max_retries
        self.run_id = run_id
        self.requests_made = 0
        self._sleep = sleep
        self._last_request = 0.0
        base_headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"}
        base_headers.update(headers or {})
        self._client = httpx.Client(
            timeout=timeout_s, headers=base_headers, follow_redirects=True, transport=transport
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> PoliteClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _pace(self) -> None:
        wait = self.min_interval_s - (time.monotonic() - self._last_request)
        if wait > 0:
            self._sleep(wait)
        self._last_request = time.monotonic()

    def request(
        self, method: str, url: str, *, params: Mapping[str, Any] | None = None,
        json: Any = None, data: Any = None, headers: Mapping[str, str] | None = None,
        auth: httpx.Auth | tuple[str, str] | None = None, operation: str = "read",
    ) -> httpx.Response:
        if not url.startswith("https://"):
            raise SourceHTTPError(f"refusing non-HTTPS URL for {self.provider}")
        endpoint = urlsplit(url).path or "/"
        attempt = 0
        while True:
            self._pace()
            self.requests_made += 1
            try:
                resp = self._client.request(
                    method, url, params=params, json=json, data=data, headers=headers, auth=auth
                )
            except httpx.TransportError as exc:
                self.guard.record(self.provider, endpoint, operation, status_code=None,
                                  run_id=self.run_id, detail=type(exc).__name__)
                if attempt >= self.max_retries:
                    raise SourceHTTPError(f"{self.provider}: network error {type(exc).__name__}"
                                          ) from exc
                attempt += 1
                self._sleep(min(2**attempt, 30))
                continue
            self.guard.record(self.provider, endpoint, operation, status_code=resp.status_code,
                              run_id=self.run_id)
            if resp.status_code in RETRY_STATUSES and attempt < self.max_retries:
                attempt += 1
                self._sleep(self._retry_delay(resp, attempt))
                continue
            if resp.status_code == 403 and resp.headers.get("x-ratelimit-remaining") == "0":
                raise SourceHTTPError(f"{self.provider}: rate limit exhausted", 403)
            if resp.status_code >= 400:
                raise SourceHTTPError(
                    f"{self.provider}: HTTP {resp.status_code} on {endpoint}", resp.status_code
                )
            return resp

    @staticmethod
    def _retry_delay(resp: httpx.Response, attempt: int) -> float:
        retry_after = resp.headers.get("retry-after")
        if retry_after and retry_after.isdigit():
            return min(float(retry_after), 120.0)
        return float(min(2**attempt, 60))

    def get_json(self, url: str, **kwargs: Any) -> Any:
        return self.request("GET", url, **kwargs).json()

    def get_text(self, url: str, **kwargs: Any) -> str:
        return self.request("GET", url, **kwargs).text
