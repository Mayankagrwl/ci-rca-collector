# Claude Code task — Step 16: recurring-failure issues, index issue, `IssuesHistoryStore`, migration

## Context

`ci-rca-collector`. Read `AGENTS.md` and `docs/rca-delivery-spec-v1.3.md` (**§4.2, §8.0–§8.2, §11, §14, §15, §17**) first.

**Prerequisite — stop and report if missing:** Step 15 merged. That means `deliver/sticky.py`, `pr_comment.py`, `commit_comment.py`, the `--live` flag, `DeliveryBudget`, and `tests/rca/deliver/_fake_github.py` all exist.

This step makes the bot open and maintain **one GitHub issue per failure fingerprint**. Issues are opened for broken default branches, tags and schedules always, and for other triggers on recurrence. It also keeps an **index issue** that maps fingerprint → issue number, and adds a one-shot **migration** from the cache history store. Per v1.3 §8.0, issues are **delivery-owned**: collect keeps `history-backend: cache` and is not touched.

Do **only** what this document scopes.

## Verified facts to build on (do not re-derive)

- `history.HistoryStore` Protocol: `get(fingerprint) -> FailureRecord | None`, `find_by_coarse(coarse) -> list[FailureRecord]`, `upsert(record) -> None`, `close() -> None`. `CacheHistoryStore(root).all_records()` exists. `history.py` must **not** import GitHub code, so the new store lives in `deliver/`.
- `FailureRecord.absorb(other)` merges idempotently: an overlapping `run_ids` means no count bump. `run_ids` is capped at 20.
- Collect builds its `FailureRecord` **inline** (`cli.py`, near the `store.upsert(record)` call). There is no reusable builder, and its `last_summary` is a Drain template or bare category.
- `GitHubClient` (Step 12): `list_issues(repo, labels=None, state="open")`, `create_issue(repo, title, body, labels=None, assignees=None)`, `update_issue(repo, number, **fields)` (drops `None` fields), `add_labels(...) -> {"labels": [...]}`, `create_issue_comment`, `list_issue_comments`. `create_*` never retries on timeout. 403 raises `GitHubAPIError(status_code=403)`.
- Step 13: `should_open_issue(context, summary, inputs)`, `route(...).issue ∈ {"always", "on_recurrence", None}`.
- Step 15: live writes only with `--live` (and not `--offline` / `--dry-run`); `DeliveryBudget` (30s, injectable clock); preview always written; `render_comment` output is posted verbatim.

## Deliverables

### 1. `deliver/issues.py`

**Record builder.** `record_from_summary(summary, headline, now) -> FailureRecord` for the current run:

| Field | Source |
|---|---|
| `fingerprint`, `fingerprint_coarse` | `summary` |
| `category` | `summary.classification.category` |
| `templates` | `summary.drain.templates[*].template`, when present |
| `masking_config_hash` | `summary.drain` |
| `branches` | `[run.head_branch]` |
| `run_ids` | `[run.run_id]` |
| `first_seen`, `last_seen` | `now` |
| `last_summary` | the **rendered headline** (human text, redacted), not a Drain template |

**Issue body format** (bot-owned):

```
<!-- rca-issue:fp=<fine fingerprint> -->
### <category human words>: <headline>
<one-paragraph prose: fix line, first/last seen, count, branches, latest run link>
_This issue is maintained by the RCA bot. Comment below; edits to this body are overwritten._

<!-- rca-record -->
<fence>json
<FailureRecord JSON>
<fence>
```

- On read, parse **only** the fenced JSON block that follows `<!-- rca-record -->`. A missing or unparseable block means "no record", plus a note. Never crash.
- **Title:** `[RCA] <category human words>: <headline>`, capped at about 120 chars.
- Title, prose, and every comment go through the same safety as comments: redaction first, `@mention` neutralising, HTML escaping for forks, and no `\bR\d+\b`. Reuse `render_comment` helpers; don't duplicate them.
- **Body cap 65,536 characters:** assert before writing. If over, shrink the prose, then `templates`, then `last_summary`, in that order. The JSON block must stay valid and parseable.

**Index issue** titled exactly `[RCA] fingerprint index`, labelled `rca-index`. Its body holds the same `<!-- rca-record -->`-style fenced JSON, mapping `{fine_fingerprint: issue_number}`.
- **Lookup order:** index issue → fallback label scan (`list_issues(labels="rca-fingerprint", state="all")`, paginated with a sane cap, parsing each body's marker/record) → not found.
- **Never use the Search API** (v1.3 §8.1, AC #21).
- Create the index lazily on first need.
- **Batch index writes:** collect changes in memory and write the index once, in `close()`.

**`IssuesHistoryStore(client, repo, *, budget, clock)`** implements the `HistoryStore` Protocol:

| Method | Behaviour |
|---|---|
| `get` | Look up via the index, fall back to a label scan, parse the record. |
| `find_by_coarse` | Label scan filtered on `fingerprint_coarse`. |
| `upsert` | Create or update the fingerprint issue; queue the index change. |
| `close` | Flush the index. |

It never raises, recording notes and errors instead.

**Concurrency self-heal.** Two simultaneous runs can both miss the index and create two issues for one fingerprint. After creating, check the label scan for another open issue with the same fine fingerprint. If one exists, keep the **lowest** issue number, close the newer one with a comment `Duplicate of #N`, and point the index at #N. Never delete.

### 2. Issue lifecycle (v1.3 §8.2), in the live `deliver` flow

**Gate.** Open or maintain an issue only when `inputs.create_issues` and `should_open_issue(...)` hold, and Stage A did not suppress the run. `should_open_issue` already covers push_default, tag, schedule always, other triggers by recurrence threshold, and forks only with `allow_fork_issues`.

**Whether an issue exists for this fingerprint:**

- **None:** create it with labels `rca-fingerprint`, `rca:<category>`, plus `rca:infra` when `is_infra_vs_code == "infra"` and `rca:flaky` when flaky. Leave assignees empty; owner resolution is Step 17, and the preview notes it.
- **Exists:** `absorb` the new record. Re-running the same `run_id` must not bump the count. Rewrite the body, add a short recurrence comment (run link, branch, short SHA), and **reopen** the issue if it was closed.
- **Linking:** do the issue **before** the comment, so the Step 15 comment footer can say `· tracked in #N`. Add an optional `issue_ref` parameter to `render_comment`; the rendered output with no issue must stay byte-identical to today's.
- **Stale sweep** (opportunistic, live only, inside the budget, at most 5 issues per run): close open `rca-fingerprint` issues whose record `last_seen` is more than 14 days old and have no `resolution`, with a comment `No recurrence in 14 days — closing. Reopens automatically if it recurs.`
- **Failure handling:** issue failures never block the comment, and vice versa. A 403 records an error naming `issues: write`. Everything shares the Step 15 `DeliveryBudget`.
- **Report:**

  | Field | Values |
  |---|---|
  | `DeliveryReport.issue_url` | the issue's URL |
  | `delivered_to` | `issue:created`, `issue:updated`, `issue:reopened`, `issue:duplicate-closed` |
  | output `issue-url` | the issue's URL |

- **Dry-run** (not offline): read the index, labels and issues, and preview `would create issue` / `would update issue #N (count a→b)` / `would reopen #N`, with **zero** writes. Offline makes zero requests.

### 3. Migration: `deliver --migrate-history --history-dir <dir> [--migrate-limit 50] [--live]`

- Read `CacheHistoryStore(dir).all_records()` and create issues **only for fingerprints not already present**. Existing ones are left untouched and noted.
- This makes the command idempotent by construction (AC #20): running it again creates nothing new.
- Pace the creates with an injectable sleep of about 1s between them, to respect GitHub's content-creation secondary rate limits. Stop at `--migrate-limit`; a re-run continues where it left off. The 30s delivery budget doesn't apply to migration, but pacing does.
- Without `--live` it only lists what would be created. It never touches the cache directory.

## Constraints

- Collect, `history.py`, `open_store`, and `action.yml`'s `history-backend` default are **unchanged** (v1.3 §8.0). Don't import `github_api` from `history.py`.
- All HTTP goes through `GitHubClient`. No new dependencies. No `action.yml` or workflow changes (Step 19).
- `render_comment` stays pure, and its no-issue output stays byte-identical.

## Acceptance criteria

Test against Step 15's fake GitHub, extended with issue endpoints: list with `labels`/`state` filters and pagination, create/update/reopen, and issue comments. No network.

1. **First push_default failure:** one issue with the three labels, a parseable record, and an index entry. The commit comment footer says `tracked in #N` (AC #3).
2. **Same fingerprint, new run:** the issue is updated, the count goes a→a+1, a recurrence comment is added, and there is **no second issue** (AC #7). Re-running the **same** `run_id` leaves the count unchanged.
3. **Schedule failure:** an issue is created and there is no commit comment (AC #13).
4. **Workflow dispatch:** no issue (AC #14).
5. **Fork PR:** no issue by default; an issue with `--allow-fork-issues true` (AC #17).
6. **PR recurrence threshold:** a PR below `issue_threshold` gets no issue; at the threshold it gets one.
7. **Closed issue plus recurrence:** reopened.
8. **Index missing or corrupted:** the label-scan fallback still finds the existing issue, the index is repaired at `close()`, and there are zero Search API calls (AC #21).
9. **Two issues for one fingerprint:** the newer is closed as `Duplicate of #lowest`, and the index points to the lowest.
10. **Oversized record:** the body is ≤ 65,536 characters and the JSON still parses.
11. **Migration:** the first run creates issues only for new fingerprints; a second run creates **zero** (AC #20). `--migrate-limit` and pacing are honoured, and without `--live` there are zero writes.
12. **403 on `create_issue`:** an error names `issues: write`, the comment is still posted, and the command exits 0.
13. **Safety:** titles, bodies and comments have no secret pattern, no `\bR\d+\b`, and neutralised `@mentions` (AC #25).
14. **Stale sweep:** it closes a 15-day-old issue with the comment, leaves a 10-day-old one open, and closes at most 5 per run.
15. **Protocol conformance:** `IssuesHistoryStore` passes the same `get` / `find_by_coarse` / `upsert` / `close` behaviour tests as `CacheHistoryStore`.
16. **Suites and checks:** `pytest tests/rca -q` and `pytest tests/eval -q` are green, the acceptance-35 grep is empty, and `run_eval --no-llm` stays at 1.0.

## Out of scope

- CODEOWNERS and issue assignees (Step 17).
- Notifications, reactions, `/resolved` (Step 18).
- `action.yml` inputs/outputs, the delivery job and permissions (Step 19).
- Collect-side reads from issues.

## Deliverable

`deliver/issues.py`, CLI wiring for issues and `--migrate-history`, the `issue_ref` footer in `render_comment`, the fake-GitHub extension, and tests. Both suites must be green. Show the fake-server transcript for AC #1 → #2 (create, then update + recurrence comment), and the rendered issue body.
