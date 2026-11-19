# Data sources

All sources are public, read-only and HTTPS. Each one can be turned on or off in `config/sources.yaml`.
**X discovery and search are not used**: X stays off at launch and is reserved for publishing and the account's own metrics in later stages.

| Source | Endpoint | Auth | Limits / terms | Stored |
|---|---|---|---|---|
| GitHub | REST `search/repositories`, `repos/{o}/{r}`, `stargazers` (backfill) | optional `GITHUB_TOKEN` | 60/h unauth, 5000/h with token; search 10 or 30/min | repos; stars, forks, open_issues |
| Hacker News | `hn.algolia.com/api/v1/search_by_date` | none | public API, paced at 1 req/s | stories; points, comments |
| Hugging Face | `huggingface.co/api/models`, `/spaces?sort=trendingScore` | none | public API | models/spaces; likes, downloads, trending_score |
| arXiv | `export.arxiv.org/api/query` | none | arXiv terms: ≤1 request / 3 s | papers (cs.AI, CL, LG, MA, CR) |
| MCP Registry | `registry.modelcontextprotocol.io/v0/servers` | none | public; incremental via `updated_since` | MCP servers; update counts |
| DeFiLlama | `api.llama.fi/protocols`, `/v2/chains` | none | public | protocols with TVL ≥ $1M (CEXs excluded); TVL, mcap; chain TVL |
| Polymarket | `gamma-api.polymarket.com/events` | none | public market data, **read-only, never trades** | events; 24h volume, liquidity, open interest |
| Solana | JSON-RPC `getRecentPerformanceSamples` | optional `SOLANA_RPC_URL` | public RPC is rate-limited | network TPS, non-vote TPS |
| RSS | CoinDesk, The Block, Blockworks, Decrypt, The Defiant, Solana, Polymarket News, HF blog, OpenAI, Google AI, Simon Willison, Latent Space | none | publisher feeds | articles (HTML stripped) |
| Reddit | OAuth `oauth.reddit.com/r/{sub}/new` | client id/secret + UA | **off by default**; needs an approved Reddit API app; never scrapes | posts; score, comments |

## Known data-quality caveats

- **First-run catalogs.** DeFiLlama and the MCP Registry return their existing catalog on the first run. Those items are flagged as `is_initial_snapshot` and must not count as new activity.
- **GitHub spam.** "New repo" searches surface clone farms: the same repo name under many throwaway accounts. They're flagged in `v_repo_quality`. GitHub stars are a weak signal on their own.
- **Gaps from rate limits.** `solana.com/news/rss.xml` often returns 429. Such failures are recorded as `partial` runs, and the source is retried on its next interval.
- **Star backfill** counts only *current* stargazers. People who starred and later un-starred are invisible, so it's a reconstruction, labelled `is_backfill = 1`.
- **HN backfill** dates stories by their public `created_at`. Their past points totals are unknown and are never fabricated.
- **MCP Registry** fetches at most 10 pages (1,000 servers) per run. A burst larger than that between runs would be partly missed.
