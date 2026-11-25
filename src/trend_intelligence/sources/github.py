"""GitHub REST API: newly created repos (search) + star/fork re-measurement for velocity.

Uses the official REST API only (no HTML scraping of github.com/trending).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from trend_intelligence.settings import Settings
from trend_intelligence.sources.base import CollectContext, CollectResult, SourceAdapter
from trend_intelligence.sources.http import SourceHTTPError
from trend_intelligence.store import Observation, RawItem
from trend_intelligence.timeutil import parse_iso, to_iso

API = "https://api.github.com"


def repo_key(full_name: str) -> str:
    return full_name.strip().lower()


def repo_item(repo: dict[str, Any], category: str | None = None) -> RawItem:
    topics = repo.get("topics") or []
    body = " ".join(filter(None, [repo.get("description") or "", " ".join(topics)]))
    return RawItem(
        item_type="repo",
        external_id=repo_key(repo["full_name"]),
        title=repo["full_name"],
        url=repo.get("html_url"),
        body=body,
        author=(repo.get("owner") or {}).get("login"),
        published_at=repo.get("created_at"),
        payload={
            "full_name": repo["full_name"],
            "description": repo.get("description"),
            "topics": topics,
            "language": repo.get("language"),
            "license": (repo.get("license") or {}).get("spdx_id"),
            "created_at": repo.get("created_at"),
            "pushed_at": repo.get("pushed_at"),
            "query_category": category,
            "fork": repo.get("fork"),
            "archived": repo.get("archived"),
        },
    )


def repo_observations(repo: dict[str, Any]) -> list[Observation]:
    key = repo_key(repo["full_name"])
    ref = ("repo", key)
    out = [
        Observation("repo", key, "stars", float(repo.get("stargazers_count", 0)), item_ref=ref),
        Observation("repo", key, "forks", float(repo.get("forks_count", 0)), item_ref=ref),
        Observation("repo", key, "open_issues", float(repo.get("open_issues_count", 0)),
                    item_ref=ref),
    ]
    if repo.get("subscribers_count") is not None:
        out.append(Observation("repo", key, "watchers", float(repo["subscribers_count"]),
                               item_ref=ref))
    return out


class GitHubAdapter(SourceAdapter):
    id = "github"
    kind = "code"

    def availability(self, settings: Settings) -> tuple[bool, str]:
        if settings.github_token:
            return True, "token present (5000 req/h)"
        return True, "no token: 60 req/h core, 10 req/min search -- refresh is reduced"

    def client_headers(self, settings: Settings) -> dict[str, str]:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if settings.github_token:
            headers["Authorization"] = f"Bearer {settings.github_token.get_secret_value()}"
        return headers

    def pacing(self, settings: Settings) -> float:
        # Search API: 30/min with token, 10/min without.
        return 2.2 if settings.github_token else 6.5

    def collect(self, ctx: CollectContext) -> CollectResult:
        result = CollectResult()
        p = ctx.params
        since = to_iso(parse_iso(ctx.fetched_at) - timedelta(days=int(p.get("new_repo_window_days",
                                                                             14))))[:10]
        min_stars = int(p.get("min_stars", 15))
        per_query = int(p.get("per_query", 30))
        seen: set[str] = set()
        for category, queries in (p.get("queries") or {}).items():
            for query in queries:
                q = f"{query} created:>{since} stars:>={min_stars} fork:false"
                try:
                    data = ctx.client.get_json(
                        f"{API}/search/repositories",
                        params={"q": q, "sort": "stars", "order": "desc", "per_page": per_query},
                    )
                except SourceHTTPError as exc:
                    result.errors.append(f"search '{query}': {exc}")
                    continue
                for repo in data.get("items", []):
                    key = repo_key(repo["full_name"])
                    if key in seen:
                        continue
                    seen.add(key)
                    result.items.append(repo_item(repo, category))
                    result.observations.extend(repo_observations(repo))

        # Velocity: re-measure previously seen repos not covered by this run's searches.
        limit = int(p.get("refresh_max_repos", 150))
        if not ctx.settings.github_token:
            limit = min(limit, 20)
        max_age = to_iso(parse_iso(ctx.fetched_at) - timedelta(days=int(p.get(
            "refresh_max_age_days", 45))))
        rows = ctx.store.conn.execute(
            "SELECT r.external_id, MAX(o.value) AS stars FROM raw_items r"
            " LEFT JOIN observations o ON o.subject_type='repo' AND o.subject_key=r.external_id"
            "  AND o.metric='stars'"
            " WHERE r.source_id='github' AND r.item_type='repo' AND r.first_seen_at >= ?"
            " GROUP BY r.external_id ORDER BY stars DESC LIMIT ?",
            (max_age, limit + len(seen)),
        ).fetchall()
        refreshed = 0
        for row in rows:
            if refreshed >= limit:
                break
            key = row["external_id"]
            if key in seen:
                continue
            try:
                repo = ctx.client.get_json(f"{API}/repos/{key}")
            except SourceHTTPError as exc:
                if exc.status_code in (404, 451):
                    continue  # deleted / blocked repo: nothing to measure
                result.errors.append(f"refresh {key}: {exc}")
                if exc.status_code in (403, 429):
                    break
                continue
            refreshed += 1
            result.observations.extend(repo_observations(repo))
        return result
