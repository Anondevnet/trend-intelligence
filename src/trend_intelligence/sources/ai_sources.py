"""AI-ecosystem sources: Hugging Face Hub, arXiv, MCP Registry."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import feedparser

from trend_intelligence.sources.base import CollectContext, CollectResult, SourceAdapter, strip_html
from trend_intelligence.sources.http import SourceHTTPError
from trend_intelligence.store import Observation, RawItem
from trend_intelligence.timeutil import parse_iso, to_iso


class HuggingFaceAdapter(SourceAdapter):
    id = "huggingface"
    kind = "ai_hub"
    min_interval_s = 1.0

    def collect(self, ctx: CollectContext) -> CollectResult:
        result = CollectResult()
        for kind, limit_key, path in (("model", "models_limit", "models"),
                                      ("space", "spaces_limit", "spaces")):
            try:
                rows: list[dict[str, Any]] = ctx.client.get_json(
                    f"https://huggingface.co/api/{path}",
                    params={"sort": "trendingScore", "limit": int(ctx.params.get(limit_key, 50))},
                )
            except SourceHTTPError as exc:
                result.errors.append(f"{path}: {exc}")
                continue
            for row in rows:
                rid = row.get("id") or row.get("modelId")
                if not rid:
                    continue
                key = str(rid).lower()
                tags = row.get("tags") or []
                result.items.append(RawItem(
                    item_type=kind,
                    external_id=key,
                    title=str(rid),
                    url=f"https://huggingface.co/{'spaces/' if kind == 'space' else ''}{rid}",
                    body=" ".join([row.get("pipeline_tag") or "", *[str(t) for t in tags[:25]]]),
                    author=str(rid).split("/")[0],
                    published_at=row.get("createdAt"),
                    payload={"tags": tags[:40], "pipeline_tag": row.get("pipeline_tag")},
                ))
                ref = (kind, key)
                for metric, field in (("likes", "likes"), ("downloads", "downloads"),
                                      ("trending_score", "trendingScore")):
                    if row.get(field) is not None:
                        result.observations.append(
                            Observation(kind, key, metric, float(row[field]), item_ref=ref))
        return result


class ArxivAdapter(SourceAdapter):
    id = "arxiv"
    kind = "research"
    min_interval_s = 3.1  # arXiv API terms: no more than one request every 3 seconds

    def collect(self, ctx: CollectContext) -> CollectResult:
        result = CollectResult()
        cats = ctx.params.get("categories") or ["cs.AI"]
        query = " OR ".join(f"cat:{c}" for c in cats)
        text = ctx.client.get_text(
            "https://export.arxiv.org/api/query",
            params={"search_query": query, "sortBy": "submittedDate", "sortOrder": "descending",
                    "max_results": int(ctx.params.get("max_results", 100))},
        )
        feed = feedparser.parse(text)
        for entry in feed.entries:
            raw_id = str(entry.get("id", ""))
            arxiv_id = raw_id.rsplit("/abs/", 1)[-1]
            if not arxiv_id:
                continue
            authors = ", ".join(a.get("name", "") for a in entry.get("authors", [])[:8])
            tags = [t.get("term") for t in entry.get("tags", []) if t.get("term")]
            result.items.append(RawItem(
                item_type="paper",
                external_id=arxiv_id.split("v")[0] if "v" in arxiv_id else arxiv_id,
                title=strip_html(entry.get("title"), 500),
                url=raw_id.replace("http://", "https://"),
                body=strip_html(entry.get("summary"), 4000),
                author=authors,
                published_at=entry.get("published"),
                payload={"categories": tags},
            ))
        return result


class McpRegistryAdapter(SourceAdapter):
    id = "mcp_registry"
    kind = "ai_registry"
    min_interval_s = 1.0

    def collect(self, ctx: CollectContext) -> CollectResult:
        result = CollectResult()
        since = ctx.last_success_at or to_iso(parse_iso(ctx.fetched_at) - timedelta(days=7))
        cursor: str | None = None
        count = 0
        for _ in range(int(ctx.params.get("max_pages", 10))):
            params: dict[str, Any] = {"limit": int(ctx.params.get("page_size", 100)),
                                      "updated_since": since, "version": "latest"}
            if cursor:
                params["cursor"] = cursor
            data = ctx.client.get_json("https://registry.modelcontextprotocol.io/v0/servers",
                                       params=params)
            for entry in data.get("servers", []):
                server = entry.get("server") or {}
                name = server.get("name")
                if not name:
                    continue
                meta = (entry.get("_meta") or {}).get(
                    "io.modelcontextprotocol.registry/official") or {}
                repo = (server.get("repository") or {}).get("url")
                result.items.append(RawItem(
                    item_type="mcp_server",
                    external_id=str(name).lower(),
                    title=server.get("title") or name,
                    url=repo or (server.get("websiteUrl")),
                    body=server.get("description") or "",
                    author=str(name).split("/")[0],
                    published_at=meta.get("publishedAt"),
                    payload={"name": name, "version": server.get("version"), "repository": repo},
                ))
                count += 1
            cursor = (data.get("metadata") or {}).get("nextCursor")
            if not cursor:
                break
        result.observations.append(
            Observation("mcp_registry", "all", "servers_updated_since_last_run", float(count)))
        return result
