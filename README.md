# SCP-JP Daily Monitor v5.2.0

The production monitor retrieves new SCP-JP articles from the Crom GraphQL API
and publishes monitoring JSON through Raw GitHub around 06:17 JST every day.
The existing runtime hotfix and `hotfix_version` remain at 5.1.2.

## Scope

Included content: SCP reports, Tales, GoI formats, artwork and music, hubs and
sites, collaborations and setting collections, essays, news, external wiki
archives, and translations of those categories.

Excluded content: author/translator pages, components, themes, `fragment` /
`deleted`, hidden pages, and administrative or technical pages without an
included work tag.

## Files

```text
.github/workflows/scp-jp-monitor.yml
VERSION
config/baseline.json
scripts/scp_jp_monitor.py
scripts/scp_jp_hotfix_v512.py
scripts/run_monitor_v512.py
scripts/publish_monitor_feed.py
tests/test_monitor.py
tests/test_hotfix_v512.py
tests/test_publish_monitor_feed.py
docs/scheduled-task-prompt.md
docs/operations.md
docs/v5.1.2-hotfix.md
```

The v5.1.2 launcher applies the existing hotfix to the v5.1.1 core. Older v2-v4.2
probes may remain; the production workflow does not use them.

## Daily flow

```text
06:17 JST (scheduled; Actions may be delayed)
GitHub Actions
  1. Load previous state from monitor-state.
  2. Test, compile, and generate monitor-output/public/ through Crom.
  3. Capture and upload diagnostics.
  4. Publish health.json / delta.json / latest.json to monitor-feed.
  5. Persist state.json to monitor-state only after Raw publication succeeds.

12:40 JST
ChatGPT Scheduled Task
  Check health freshness (36 hours), matching health/delta generations,
  and previously unreported notification_id values; notify only for new items.
```

The six-hour margin accommodates Actions delays. Generation or Raw publication
failure leaves the previous state in place and fails the workflow. State
persistence failure also fails the workflow and can cause repeated candidates
on the next run. The task's remembered `notification_id` values prevent
duplicate notifications.

The incremental cutoff is `max(now - 30 days, bootstrap_since_jst)`. Confirmed
articles whose revision count and tags are unchanged reuse stored snapshots;
only new, revised, or pending articles need detailed Crom queries. HTTP 429
honors `Retry-After`; public diagnostics do not include response bodies.

## Setup and normal execution

Place the project files at the root of `iniwa/scp-jp-crom-probe`, then commit and
push. The workflow's `GITHUB_TOKEN` requires `contents: write` for both managed
branches. Repository/organization policy and branch protection must permit
these pushes.

Run **SCP-JP daily monitor** from Actions with:

```text
window_days: 30
now: empty
force_bootstrap: false
```

On a pull request, only the verification job runs. Publication requires a
successful build on the repository default branch and a non-PR event.

## Public endpoints

The Raw GitHub URLs expose the generated files at the `monitor-feed` root:

- https://raw.githubusercontent.com/iniwa/scp-jp-crom-probe/monitor-feed/health.json
- https://raw.githubusercontent.com/iniwa/scp-jp-crom-probe/monitor-feed/delta.json
- https://raw.githubusercontent.com/iniwa/scp-jp-crom-probe/monitor-feed/latest.json

`publish_monitor_feed.py` parses all files, requires matching nonempty
`health.generated_at_jst` and `delta.generated_at_jst`, and checks the latest
timestamp if present. It copies the original bytes to an external temporary
directory, validates again, and fetches an existing feed or creates an orphan
branch. An isolated Git worktree replaces stale tracked content with only the
three JSON files, validates the copies and staged bytes, then commits all three
atomically as `github-actions[bot]`. Unchanged bytes create no commit. Pushes
use Git's normal fast-forward checks.

Separate HTTP requests can straddle a branch update. Consumers should validate
timestamps and freshness, then re-fetch if the generations differ. The
[Scheduled Task prompt](docs/scheduled-task-prompt.md) uses the Raw URLs.

## JSON contracts

- `health.json`: last retrieval status (`ok`, `degraded`, or `error`),
  `generated_at_jst`, `hotfix_version`, `counts.details_fetched`, and
  `counts.details_reused`. `generated_date_jst` remains an audit value; a
  different calendar date alone is not a failure.
- `delta.json`: notification candidates retained for 168 hours (7 days), with
  stable `notification_id` derived from `wikidot_id`, `is_new_this_run`,
  `edition` (`jp_original` / `translation`), genre, article title, subtitle,
  `summary_basis`, `content_warnings`, and translation credits/source metadata.
  Already reported IDs must not be reported again.
- `latest.json`: confirmed articles from the last 30 days for audit and gap checks.

Snapshots expire after 14 days; deduplication IDs are retained. Individual
unsynchronized pages stay in `pending_pages` for retry. Unknown/conflicting
classifications are not finalized in state. These cases can produce `degraded`
while confirmed content is published. A global Crom failure, malformed data,
or limit failure fails generation and leaves prior public feeds intact. No
new items with an `ok` status means no notification. A malformed timestamp or
a generation more than 36 hours old is a task verification failure.

## Baseline and migration

`config/baseline.json` contains nine already introduced JP originals.
Translations are not baseline items, so initial bootstrap can report
translations created since 2026-07-26 and subsequent unreported articles.
Historical initial expectations were `ok`, bootstrap mode, at least nine JP
originals and sixteen translations, and no pending pages; current counts vary.

The v5.1.1 recovery procedure remains historical: after upgrading from v5.0 or
v5.1, a single `force_bootstrap: true` run reconstructs notification candidates
from the baseline, excludes articles before 2026-07-26 00:00 JST, and recovers
candidates that expired under the former 72-hour retention. Follow it with a
normal run and check `new_this_run_ids` for unexpectedly old articles. Existing
task memory prevents repeated notifications. See [operations](docs/operations.md).

For v5.1.2 and v5.2.0, use normal execution; do not force bootstrap. Previously
expired snapshots may be fetched on the first normal run, then reused. See
[the preserved v5.1.2 hotfix notes](docs/v5.1.2-hotfix.md).

Do not edit or reset `monitor-state` routinely. Deliberately deleting it causes
bootstrap on the next run and can recreate translation notification candidates.

## Local validation

```bash
python -m unittest discover -s tests -p 'test_*.py' -v
python -m compileall -q scripts/scp_jp_monitor.py scripts/scp_jp_hotfix_v512.py scripts/run_monitor_v512.py scripts/publish_monitor_feed.py
```

Publication tests use local bare Git repositories without Crom or GitHub access.
Live generation requires external network access. See
[operations](docs/operations.md) for publication verification and failure handling.
