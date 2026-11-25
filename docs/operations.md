# Operations

## Normal daily execution

At 06:17 JST, Actions loads `monitor-state`, retrieves Crom data, generates the
feed, captures diagnostics, publishes to Raw GitHub, and saves state only after
Raw publication succeeds. The Scheduled Task runs around 12:40 JST. Actions may
start late. The task checks health, delta, and unreported IDs.

Normal manual inputs are `window_days: 30`, empty `now`, and
`force_bootstrap: false`. Candidates remain for 168 hours (7 days), snapshots
for 14 days, and deduplication IDs indefinitely. The incremental cutoff is
`max(now - 30 days, bootstrap_since_jst)`.

## Raw GitHub publication

The workflow calls `scripts/publish_monitor_feed.py` after diagnostic upload,
only after successful feed generation on the default branch and outside pull
requests. It validates generated JSON and
matching timestamps before remote branch inspection, stages exact copies
outside the source checkout, and uses an isolated Git worktree. Existing
`monitor-feed` history is retained; first publication creates an orphan branch.
All stale tracked files are replaced by the three root JSON files in one commit.
Copied JSON and staged bytes are validated before a non-force push. Unchanged
content produces no commit. The author is `github-actions[bot]`.

The source of truth remains `monitor-output/public/`; no JSON is regenerated,
normalized, or reconstructed for Raw. `monitor-state`, its `state.json`, logs,
configuration, and source files are never part of the public branch.

| File | Raw GitHub URL |
| --- | --- |
| health.json | https://raw.githubusercontent.com/iniwa/scp-jp-crom-probe/monitor-feed/health.json |
| delta.json | https://raw.githubusercontent.com/iniwa/scp-jp-crom-probe/monitor-feed/delta.json |
| latest.json | https://raw.githubusercontent.com/iniwa/scp-jp-crom-probe/monitor-feed/latest.json |

After a successful default-branch run:

1. Confirm the Raw publication and state persistence steps succeeded.
2. Retrieve the three Raw URLs and parse each response as JSON.
3. Confirm all three files share the same `generated_at_jst`.
4. Confirm `monitor-feed` tracks only the three JSON files at its root.

Independent requests can straddle an update. If timestamps differ, re-fetch
the files and check their freshness before using them. The
[Scheduled Task prompt](scheduled-task-prompt.md) lists the same Raw URLs.

## Failure handling

### Crom retrieval or generation failure

Check the `scp-jp-monitor-output` artifact: `monitor.log`, `monitor-debug.json`,
`monitor-summary.md`, `workflow-diagnostics.txt`, and `artifact-manifest.txt`.
The workflow fails before Raw publication or state persistence. The task
reports verification failure once the published timestamp is over 36 hours old.

### Raw publication failure

The publication step fails the workflow and prevents state persistence.
JSON validation failures leave the previous remote feed untouched.
Push rejection also preserves the remote branch; there is no force push or
remote deletion. Check the failed step, token `contents: write` permission,
branch protection, and connectivity before rerunning normally. Never copy
state or diagnostics onto `monitor-feed` to repair it.

### State persistence failure

The workflow fails after Raw serves the new generation. The next run can repeat
candidates from the prior state. The task's remembered `notification_id` values
prevent duplicate notifications. Check write permissions and branch protection.

## Historical v5.1.1 recovery

When migrating from v5.0/v5.1, run once with `force_bootstrap: true` after
updating main, retaining `window_days: 30` and empty `now`. This ignores old
state, rebuilds from the nine-item JP-original baseline, recovers translations
that expired under the former 72-hour retention, and excludes articles before
2026-07-26 00:00 JST. Verify:

1. The workflow and publication succeed, and status is `ok` or `degraded`.
2. `health.query.notification_retention_hours` and `delta.retention_hours` are 168.
3. `health.query.since_jst` is at least `2026-07-26T00:00:00+09:00`.
4. A subsequent normal run does not add pre-baseline articles to `new_this_run`.
5. The saved task prompt is applied as part of that historical migration.

Use `delta.new_this_run_ids` to distinguish newly reflected Crom articles from
unexpectedly old entries. Stop normal operation if pre-baseline articles appear.
The task's notification memory still prevents duplicate reports.

For v5.1.2/v5.2.0, do not force bootstrap. Repeated bootstrap can recreate
translation candidates. Complete reset by deleting `monitor-state` is an
intentional recovery operation, not routine maintenance.
