"""Hacker News via the public Algolia search API."""

from __future__ import annotations

from typing import Any

from liquidmexx.sources.base import CollectContext, CollectResult, SourceAdapter, strip_html
from liquidmexx.store import Observation, RawItem
from liquidmexx.timeutil import parse_iso

API = "https://hn.algolia.com/api/v1"


def story_item(hit: dict[str, Any]) -> RawItem:
    oid = str(hit["objectID"])
    return RawItem(
        item_type="story",
        external_id=oid,
        title=hit.get("title") or hit.get("story_title"),
        url=hit.get("url") or f"https://news.ycombinator.com/item?id={oid}",
        body=strip_html(hit.get("story_text")),
        author=hit.get("author"),
        published_at=hit.get("created_at"),
        payload={"hn_url": f"https://news.ycombinator.com/item?id={oid}", "url": hit.get("url")},
    )


def story_observations(hit: dict[str, Any]) -> list[Observation]:
    key = f"hn:{hit['objectID']}"
    ref = ("story", str(hit["objectID"]))
    return [
        Observation("story", key, "points", float(hit.get("points") or 0), item_ref=ref),
        Observation("story", key, "comments", float(hit.get("num_comments") or 0), item_ref=ref),
    ]


class HackerNewsAdapter(SourceAdapter):
    id = "hackernews"
    kind = "community"
    min_interval_s = 1.0

    def collect(self, ctx: CollectContext) -> CollectResult:
        result = CollectResult()
        p = ctx.params
        now_s = int(parse_iso(ctx.fetched_at).timestamp())
        lookback = int(p.get("lookback_minutes", 120)) * 60
        if ctx.last_success_at:
            lookback = max(lookback, now_s - int(parse_iso(ctx.last_success_at).timestamp()) + 600)
        lookback = min(lookback, 3 * 24 * 3600)

        page = 0
        while page < 10:
            data = ctx.client.get_json(
                f"{API}/search_by_date",
                params={"tags": "story", "numericFilters": f"created_at_i>{now_s - lookback}",
                        "hitsPerPage": 500, "page": page},
            )
            for hit in data.get("hits", []):
                result.items.append(story_item(hit))
            page += 1
            if page >= int(data.get("nbPages", 0)):
                break

        # Re-measure points/comments of recent stories with traction (velocity).
        horizon = now_s - int(p.get("refresh_max_age_hours", 72)) * 3600
        min_points = int(p.get("min_points_refresh", 5))
        data = ctx.client.get_json(
            f"{API}/search_by_date",
            params={"tags": "story",
                    "numericFilters": f"created_at_i>{horizon},points>={min_points}",
                    "hitsPerPage": int(p.get("refresh_max_stories", 200))},
        )
        known = {i.external_id for i in result.items}
        for hit in data.get("hits", []):
            if str(hit["objectID"]) not in known:
                result.items.append(story_item(hit))
                known.add(str(hit["objectID"]))
            result.observations.extend(story_observations(hit))
        return result

    @staticmethod
    def backfill_window(ctx: CollectContext, start_s: int, end_s: int,
                        min_points: int = 3) -> CollectResult:
        """Stories created in [start, end). available_at = HN created_at (genuinely public then)."""
        result = CollectResult()
        page = 0
        while page < 20:
            data = ctx.client.get_json(
                f"{API}/search_by_date",
                params={"tags": "story",
                        "numericFilters": f"created_at_i>={start_s},created_at_i<{end_s},"
                                          f"points>={min_points}",
                        "hitsPerPage": 1000, "page": page},
            )
            for hit in data.get("hits", []):
                result.items.append(story_item(hit))
            page += 1
            if page >= int(data.get("nbPages", 0)):
                break
        return result
