# Stage 6 comparison: checklist and outcome plans

The comparison is **pre-registered**. `config/detectors.yaml` was locked in commit `5daa245`
(`config/detectors.lock`, sha256 `aba394f7…`) before any comparison ran. Settings do not change
after results are seen, and every verdict is reported as printed, including when nothing beats
`volume_only`.

## When

- Earliest: **2026-11-26** (America/Detroit). Live collection started 2026-11-19.
- `liquidmexx compare` also checks for itself. It refuses (exit 2, saves nothing) until at least
  2 sources have 7 or more distinct UTC days of live, non-backfill collection. Don't work around
  that refusal. If it refuses on 2026-11-26, wait and check again the next day.

## Checklist for 2026-11-26

1. **Working tree is clean and the lock is intact.**
   ```bash
   cd ~/trend-intelligence
   git status --short                       # expect no output
   git log --oneline -- config/detectors.yaml config/detectors.lock   # last change is 5daa245
   shasum -a 256 config/detectors.yaml      # must equal the sha256 line in config/detectors.lock
   ```
2. **Collection is healthy.**
   ```bash
   uv run liquidmexx status
   ```
   - Each source shows 7+ live days ("0 more day(s) needed") for at least 2 sources.
   - No source has been stuck in `error` for days. If one has, write it down. It goes into the
     report as a data-quality caveat, not a reason to change settings.
   - `api_usage` spend is within caps, and posting shows `mode=off`, kill switch reported.
3. **Back up the database first.**
   ```bash
   uv run liquidmexx backup
   ```
4. **Run the comparison once, all horizons, saving the runs.**
   ```bash
   uv run liquidmexx compare 2>&1 | tee data/logs/compare-2026-11-26.txt
   ```
   - Exit 3 means the lock doesn't match. Stop. Do not edit the lock to make it pass.
   - Exit 2 means not enough live data yet. Nothing was saved. Try again the next day.
5. **Report the result as printed.** For every horizon and detector, record the verdict, hits@10,
   recall, FPR, walk-forward F1 and every listed reason. Include the config hash and data
   watermark from the first output line, and the saved backtest run ids.
   No re-runs with other horizons or settings to get a nicer answer.

## What to expect

With only 7 live days, the most likely result is **INSUFFICIENT DATA** for most rows:

- The 30-day horizon cannot have a complete outcome window yet.
- The 1-day and 7-day horizons still lean on the Hacker News backfill. A backfill share above 0.5
  makes a row INSUFFICIENT by design, because that backfill is hindsight-biased.
- Fewer than 7 evaluation dates or 20 positive labels also gives INSUFFICIENT DATA.

That outcome is a valid result and is reported as such. It is not a failure to fix by tuning.

## Plan for each outcome

The verdicts are defined in [`architecture.md`](architecture.md#detector-comparison-stage-6).

### A. INSUFFICIENT DATA (most or all rows)

- Report it as is, with the reasons printed for each row.
- Nothing changes in code or config. Scores stay descriptive only and do not drive posting.
- Proposal, **needs the owner's approval** because the standing rule says "run it once": re-run
  the same locked comparison on a fixed schedule as live data accumulates (for example weekly,
  starting 2026-12-03), and report every run, not only the best one.

### B. No real edge: every detector is NOT BETTER THAN BASELINE

- Report it plainly: nothing beats `volume_only`. That matches the Stage 5 result, where the
  scorer lost to volume.
- `volume_only` becomes the reference ranking for drafts and digests. The scorer, hotlist,
  signal_sum and the ensemble stay descriptive only.
- No tuning to the observed results. Any new idea is a new detector or a new setting, written
  down and locked in a new commit **before** the next comparison, with its reason in
  `config/detectors.lock`, and tested on dates after the lock.
- Stage 7+ (narratives, Claude drafts) can still proceed. It must not claim that a trend is
  "predicted" or "early".

### C. Real edge: one or more detectors BEAT BASELINE

- Report the winning rows **and** all the losing ones, with the sign-test p-value,
  wins/losses and walk-forward F1. Note the small sample and the multiple detectors compared.
- Treat it as a candidate, not a proof. Confirm it on the next locked run on fresh dates before
  any detector ranks content.
- Even when confirmed, a detector only orders the draft queue. Every post still needs human
  approval, the quality gate and the posting safeguards in
  [`publishing-safety.md`](publishing-safety.md). Nothing auto-posts.

### D. Mixed (beats baseline at one horizon only, or only some detectors)

- Report every row. Use a detector only at the horizon where it beat the baseline, subject to
  the same confirmation run as in C.

## Not allowed at any point

- Editing `config/detectors.yaml` or `config/detectors.lock` after seeing results.
- Re-running with different horizons, k or thresholds and reporting only the best run.
- Deleting or editing saved backtest runs, or any history rows.
