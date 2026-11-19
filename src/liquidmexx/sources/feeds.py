"""RSS/Atom news feeds and Reddit (OAuth, app-only)."""

from __future__ import annotations

import calendar
import hashlib
from typing import Any

import feedparser
import httpx

from liquidmexx.settings import Settings
from liquidmexx.sources.base import CollectContext, CollectResult, SourceAdapter, strip_html
from liquidmexx.sources.http import SourceHTTPError
from liquidmexx.store import Observation, RawItem
from liquidmexx.timeutil import from_unix


def _entry_published(entry: Any) -> str | None:
    for key in ("published_parsed", "updated_parsed"):
        st = entry.get(key)
        if st:
            return from_unix(calendar.timegm(st))
    return None


class RssAdapter(SourceAdapter):
    id = "rss"
    kind = "news"
    min_interval_s = 1.0

    def collect(self, ctx: CollectContext) -> CollectResult:
        result = CollectResult()
        for feed in ctx.params.get("feeds") or []:
            fid, url = feed["id"], feed["url"]
            try:
                text = ctx.client.get_text(url)
            except SourceHTTPError as exc:
                result.errors.append(f"{fid}: {exc}")
                continue
            parsed = feedparser.parse(text)
            n = 0
            for entry in parsed.entries[:100]:
                link = entry.get("link") or ""
                ident = entry.get("id") or link or entry.get("title") or ""
                if not ident:
                    continue
                digest = hashlib.sha1(ident.encode(), usedforsecurity=False).hexdigest()
                ext = f"{fid}:{digest[:20]}"
                result.items.append(RawItem(
                    item_type="article",
                    external_id=ext,
                    title=strip_html(entry.get("title"), 500),
                    url=link or None,
                    body=strip_html(entry.get("summary"), 3000),
                    author=entry.get("author"),
                    published_at=_entry_published(entry),
                    payload={"feed": fid, "feed_category": feed.get("category")},
                ))
                n += 1
            result.observations.append(Observation("feed", fid, "entries_in_feed", float(n)))
        return result


class RedditAdapter(SourceAdapter):
    id = "reddit"
    kind = "community"
    min_interval_s = 1.1  # well under 100 QPM

    def availability(self, settings: Settings) -> tuple[bool, str]:
        if settings.reddit_client_id and settings.reddit_client_secret and \
                settings.reddit_user_agent:
            return True, "OAuth credentials present"
        return False, "needs REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET, REDDIT_USER_AGENT"

    def client_headers(self, settings: Settings) -> dict[str, str]:
        return {"User-Agent": settings.reddit_user_agent or "research-collector/0.1"}

    def collect(self, ctx: CollectContext) -> CollectResult:
        result = CollectResult()
        s = ctx.settings
        assert s.reddit_client_id and s.reddit_client_secret
        token_resp = ctx.client.request(
            "POST", "https://www.reddit.com/api/v1/access_token",
            data={"grant_type": "client_credentials"},
            auth=httpx.BasicAuth(s.reddit_client_id.get_secret_value(),
                                 s.reddit_client_secret.get_secret_value()),
            operation="auth",
        ).json()
        token = token_resp.get("access_token")
        if not token:
            result.errors.append("reddit auth failed")
            return result
        headers = {"Authorization": f"Bearer {token}"}
        for sub in ctx.params.get("subreddits") or []:
            try:
                data = ctx.client.get_json(f"https://oauth.reddit.com/r/{sub}/new",
                                           params={"limit": int(ctx.params.get("limit", 50))},
                                           headers=headers)
            except SourceHTTPError as exc:
                result.errors.append(f"r/{sub}: {exc}")
                continue
            for child in (data.get("data") or {}).get("children", []):
                post = child.get("data") or {}
                pid = post.get("id")
                if not pid or post.get("over_18"):
                    continue
                result.items.append(RawItem(
                    item_type="post",
                    external_id=str(pid),
                    title=post.get("title"),
                    url=f"https://www.reddit.com{post.get('permalink', '')}",
                    body=(post.get("selftext") or "")[:3000],
                    author=post.get("author"),
                    published_at=to_iso_unix(post.get("created_utc")),
                    payload={"subreddit": sub, "link": post.get("url")},
                ))
                key, ref = f"rd:{pid}", ("post", str(pid))
                result.observations += [
                    Observation("post", key, "score", float(post.get("score") or 0), item_ref=ref),
                    Observation("post", key, "comments", float(post.get("num_comments") or 0),
                                item_ref=ref),
                ]
        return result


def to_iso_unix(value: Any) -> str | None:
    return from_unix(float(value)) if isinstance(value, (int, float)) else None

