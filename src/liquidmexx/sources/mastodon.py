"""Mastodon public hashtag timelines: free, no login, read-only.

Gives the social-engagement signals (likes, reposts, replies) that the dashboard's social tiles
need, without any paid API. Only the public hashtag timeline endpoint is called, with GET.

These posts are stored and shown on the dashboard but are deliberately NOT linked to trends:
the Stage 6 detector comparison is locked to the sources it was registered against.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from liquidmexx.sources.base import CollectContext, CollectResult, SourceAdapter, strip_html
from liquidmexx.sources.http import SourceHTTPError
from liquidmexx.store import Observation, RawItem

METRICS = (("favourites_count", "likes"), ("reblogs_count", "reposts"),
           ("replies_count", "replies"), ("quotes_count", "quotes"))


def status_item(instance: str, status: dict[str, Any]) -> RawItem:
    sid = f"{instance}:{status['id']}"
    text = strip_html(status.get("content"), 1000)
    account = status.get("account") or {}
    acct = account.get("acct")
    author = None if not acct else (acct if "@" in acct else f"{acct}@{instance}")
    return RawItem(
        item_type="post",
        external_id=sid,
        title=text[:200],
        url=status.get("url") or status.get("uri"),
        body=text,
        author=author,
        published_at=status.get("created_at"),
        payload={"lang": status.get("language"), "followers": account.get("followers_count")},
    )


def status_observations(instance: str, status: dict[str, Any]) -> list[Observation]:
    key, ref = f"mastodon:{instance}:{status['id']}", ("post", f"{instance}:{status['id']}")
    return [Observation("post", key, name, float(status[field]), item_ref=ref)
            for field, name in METRICS if field in status]


class MastodonAdapter(SourceAdapter):
    id = "mastodon"
    kind = "social"
    min_interval_s = 1.0

    def collect(self, ctx: CollectContext) -> CollectResult:
        result = CollectResult()
        p = ctx.params
        instance = str(p.get("instance", "mastodon.social"))
        limit = min(int(p.get("limit_per_tag", 40)), 40)
        langs = set(p.get("languages") or ["en"])
        tags = [t for group in (p.get("tags") or {}).values() for t in group]
        seen: set[str] = set()
        for tag in tags[: int(p.get("max_tags_per_run", 12))]:
            try:
                data = ctx.client.get_json(
                    f"https://{instance}/api/v1/timelines/tag/{quote(tag, safe='')}",
                    params={"limit": limit})
            except SourceHTTPError as exc:
                result.errors.append(f"mastodon: {exc}")
                if exc.status_code in (401, 403, 429):
                    break  # blocked or rate limited: more calls won't help
                continue
            for status in data if isinstance(data, list) else []:
                if status.get("reblog") or str(status.get("id")) in seen:
                    continue
                if status.get("language") and status["language"] not in langs:
                    continue
                seen.add(str(status["id"]))
                result.items.append(status_item(instance, status))
                result.observations.extend(status_observations(instance, status))
        return result
