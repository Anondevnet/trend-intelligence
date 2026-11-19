# Publishing safety (Stage 10 groundwork)

Posting is **off** and draft-only. The codebase contains **no live X client**; a test fails if
any module references the X API hosts or an X client library. A live transport will be added only
in Stage 10 proper, with your explicit approval.

## What has to be true for a post to go out

`Publisher.publish` checks these in order and stops at the first failure. Every block is
written to the audit log with its reason.

1. **Kill switch off.** The switch is engaged if `data/POSTING_KILL_SWITCH` exists or
   `kill_switch: true` is set in `config/publishing.yaml`.
2. **Mode is not `off`.** The shipped value is `mode: "off"`.
3. **Live posting needs three things:**
   - `mode: "live"` in the config;
   - `LIQUIDMEXX_LIVE_POSTING=enabled` in `.env` (only that exact value counts);
   - a live transport, which does not exist yet.

   `dry_run` mode refuses a live transport.
4. **The draft is `approved`.** The approval must:
   - come from a `human:<name>` actor;
   - match the draft's exact content hash;
   - have no rejection after it;
   - be no older than `approval_ttl_hours` (24).
5. **The quality gate passes again at publish time.**
6. **Daily limits have room:** `max_posts_per_day` (3 publish attempts per UTC day) and, in
   live mode, the X spending cap in `budgets.yaml`.

Each draft gets **one** publish attempt, ever. The attempt is committed *before* the send. If
the process dies mid-send, the draft stays `publishing` and cannot be retried into a double
post. A failed send is final: post it again by creating a new draft.

## Enforced inside SQLite as well (migration 0007)

These guarantees hold even if the Python code is bypassed:

- `post_drafts` and `post_events` (the audit log) are append-only. An edit means a new draft
  that names the old one in `supersedes`.
- An `approved` event is refused unless:
  - the actor is `human:…`;
  - the hash matches the draft;
  - a passing gate run exists with no later failure.
- `publish_attempt`, `published_dry_run` and `published_live` are refused without a standing
  approval of the exact content.
- A unique index allows at most one attempt and one publish per draft.

## Quality gate (deterministic, no LLM)

A draft is blocked for any of the following:

- **Empty or too long:** more than 280 characters.
- **Links:** any link or domain, unless `allow_urls` is set. A URL post costs $0.20, more than
  the daily X cap.
- **Tags and tickers:** `@mentions` (no automated tagging) and `$TICKER` cashtags (no token
  promotion).
- **Financial advice:** "buy now", "NFA", "100x play", "guaranteed", "entry point" and similar.
- **Price predictions:** "will hit", "price target", "$10k by …", "EOY" and similar.
- **Sources:**
  - fewer than `min_sources` valid sources;
  - a `raw:<id>` that is not in `raw_items`;
  - a malformed URL.
- **Labels:**
  - a speculative or rumor post that doesn't say so (for example "Unconfirmed", "Rumor");
  - a sponsored post without a disclosure (`#ad`, `#sponsored`, …).
- **Duplicates:** an exact or near duplicate (word Jaccard ≥ 0.8) of a post published in the
  last 30 days.

The patterns live in `config/publishing.yaml`. The loader refuses `require_human_approval: false`.

## Commands

```bash
uv run liquidmexx queue add --claim verified --source raw:123 --text "..."   # gate runs, never approves
uv run liquidmexx queue list [--all]
uv run liquidmexx queue show 4              # text, sources, full audit trail
uv run liquidmexx queue approve 4           # interactive terminal only; you retype the id
uv run liquidmexx queue reject 4 --reason "..."   # also withdraws an approval
uv run liquidmexx queue publish 4           # dry-run transport only; blocked while mode is off
uv run liquidmexx posting status|kill|unkill [--reason ...]
```

`unkill` removes the kill-switch file. It does not turn posting on.
