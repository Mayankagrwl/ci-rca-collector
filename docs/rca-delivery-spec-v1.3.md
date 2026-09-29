# Build Spec: Phase 3 — Delivery

**Spec version 1.3** · Companion to `docs/ci-rca-collector-spec-v8.md` and `docs/rca-eval-spec-v1.md`

> **How to use this file:** commit beside the other specs and reference it as
> `#file:docs/rca-delivery-spec-v1.3.md`. Build in the order under "Build order".
>
> **Changes from v1.2** (full-code audit, 2026-09-29):
> - **§4** — tag pushes had no data source (`RunMeta` has only `event` + `head_branch`); added
>   `ref_is_tag` (§0). `default_branch` fallback fixed: `GITHUB_DEFAULT_BRANCH` is not a standard
>   Actions variable — use `RCA_DEFAULT_BRANCH` set from the `workflow_run` payload.
> - **§4.2** — `DefaultBranchState` scoped to the failing workflow; `red_since` defined precisely.
> - **§5** — v1.2's single "first match wins" table could not express rules that must combine
>   (e.g. an edited comment that is also Unverified and below threshold). Split into hard
>   suppressions / dedupe / cumulative presentation modifiers.
> - **§6** — severity defined for `schedule` and `unknown` (previously missing).
> - **§1.2 / §7** — new category `release` (collector Step 12b) needs human words; the rule-id rule
>   now covers any `R<n>` (Step 12b adds `R19`).
>
> **Changes from v1.1** (all driven by verification against the collector as of 2026-09-28,
> Steps 1–11b merged):
> - **New §0** — prerequisites. `github_api.py` is currently **read-only and cannot send a request
>   body**; there is no `get_repo`. These must be built first; v1.1 assumed they existed.
> - **§8 rewritten** — the `history-backend: issues` swap as written contradicted "collect stays
>   read-only". Issues are now **delivery-owned**; collect keeps `cache`.
> - **§5/§7 reconciled** — explicit precedence for headline vs confidence threshold, and delivery
>   no longer re-derives grounding (Step 10's `validate.py` already finalized it).
> - **§4.2** — `DefaultBranchState` now has a defined data source.
> - **§3** — `permissions:` belongs to the caller workflow; a composite action cannot declare it.
> - Idempotency/quiet-window state source made explicit (no new state store).

---

## 0. Prerequisites — build these first (NEW in v1.2)

v1.1's §1.2 table said "all writes go through `github_api.py`". Verified: **`github_api.py` has no
write capability at all.**

| Reality today | Needed for delivery |
|---|---|
| `GitHubClient._request(method, url, *, follow_redirects, headers)` — **no body/`json` parameter** | Add optional `json` body support |
| Methods are all GET: `get_run`, `list_jobs`, `list_runs`, `compare`, `get_pull`, `list_commit_pulls`, `list_artifacts`, `download_artifact_zip`, `get_file`, `list_commit_files`, `get_job_log` | Add the write/lookup methods below |
| No `get_repo` | `get_repo(repo)` → `default_branch` (§4, AC #4) |
| `_request` **raises** `GitHubAPIError` on 403 | Delivery catches it and records `delivery.errors` (AC #22, §3) |

Methods to add (all through the existing host/token resolution — no hardcoded `api.github.com`):

```
get_repo(repo)                                   -> dict            # default_branch
ref_is_tag(repo, name)                           -> bool            # GET /repos/{repo}/git/ref/tags/{name}; tag trigger (§4) — added in Step 13
list_issue_comments(repo, issue_number)          -> list[dict]      # PR comments live here
create_issue_comment(repo, issue_number, body)   -> dict
update_issue_comment(repo, comment_id, body)     -> dict
list_commit_comments(repo, sha)                  -> list[dict]
create_commit_comment(repo, sha, body)           -> dict
update_commit_comment(repo, comment_id, body)    -> dict
list_issues(repo, labels=None, state="open")     -> list[dict]
create_issue(repo, title, body, labels, assignees=None) -> dict
update_issue(repo, number, **fields)             -> dict            # body/state/labels
add_labels(repo, issue_number, labels)           -> dict
create_reaction(repo, comment_id, content)       -> dict            # best-effort, never blocking
```

Constraints: every write is redacted (§14), respects the 15s per-call timeout, and surfaces 403 as
a recorded error rather than an exception escaping delivery.

---

## 1. Objective and scope

Phases 1 and 2 produce a diagnosis that nobody sees unless they open the workflow Summary tab.
Phase 3 delivers that diagnosis where people already look.

**In scope:** target resolution, suppression, sticky PR comments, commit comments for direct
pushes, recurring-failure issues, optional notification routing, CODEOWNERS ownership,
`/resolved` feedback, and a **delivery-owned** issues mirror of failure history.

**Out of scope:** auto-fix PRs, merge blocking, dashboards, PagerDuty, and any change to
collection, Drain3, or the STGPT prompt. Delivery reads `summary.json` / `analysis.json`. It does
not re-diagnose and **does not re-run grounding**.

### 1.1 Governing principle

**Quiet by default. Loud only when the bot knows something the author does not already see in the
red check.**

"Your test failed" is worse than silence. Suppression is the feature.

### 1.2 What already exists (verified — do not rebuild)

| Verified in the repo | Delivery must use it |
|---|---|
| `AnalysisResult.confidence` is `high` \| `medium` \| `low` | Do not compare to `0.5`. Map threshold to those labels. |
| `AnalysisRecord.grounding` (`GroundingResult`: `grounded`, `grounding_rate`, `ungrounded_citations`) — Step 10 | Read it. Unverified banner when `grounding_rate < 1.0` or `grounded is False`. **Never recompute.** |
| `AnalysisRecord.validation_telemetry` / `.call_telemetry` — Step 10 | Available for the report; not required for delivery logic. |
| `FailedJob.failed_step_excerpt` **and `FailedJob.primary_failure_line`** (Step 1) | Headline evidence. `primary_failure_line` is the single best one-liner — prefer it for the evidence block's first line. |
| `DeterministicDiagnosis.one_liner` / `.fix_one_liner`; rule ids `R<n>` (`R1`–`R19` after Step 12b) | Never print rule ids in any channel. |
| Category `release` (Step 12b) for release / version-gate failures, e.g. "release already exists" | Needs human words in the comment headline (§7). |
| `AnalysisRecord.status` ∈ `ok, cached, skipped, gated, unvalidated, failed, parse_error, citation_invalid, bridge_error, unusable`; `analyze.display_status()` → `ok \| cached \| deterministic \| needs-review` (Step 5) | Use `display_status()`, not raw status, for user-facing wording. |
| `AnalysisResult.source` ∈ `ai \| deterministic \| hybrid \| mixed \| cached` | Drives whether the headline is an AI claim. |
| `HistoryStore` Protocol: `get`, `find_by_coarse`, `upsert`, `close`; `open_store(backend, path)`; `CacheHistoryStore`, `NoneHistoryStore` | Protocol signature confirmed correct. See §8 for the issues decision. |
| `FailureRecord` fields incl. `resolution`, `resolution_author`, `human_verified`, `run_ids` (capped 20), `branches` | `/resolved` writes these. |
| `RunMeta`: `run_id`, `run_attempt`, `event`, `actor`, `head_sha`, `head_branch`, `pr_number`, `pr_url`, `is_fork`, `head_repo`, `html_url`, `concurrent_failures/workflows/branches` | Feeds `DeliveryContext`. |
| `ChangeContext.pr_is_draft`, `.pr_title`, `.pr_labels`, `.files`, `.commits` | Draft suppression, CODEOWNERS. |
| `HistoryContext.seen_count`, `.match`, `.branches_seen`, `.cross_branch`, `.last_success_sha/at` | Issue threshold, recurrence. |
| `github_api.py` host/token resolution (Enterprise-safe) | All writes go through it — **after §0 adds write support**. |
| `redact.py` | Every channel passes through it before write. |
| `validate.py` + `telemetry.py` (Steps 8/10) | Read. Do not duplicate eval. |
| `tools/eval/` golden set + `report.py` (Steps 11a/11b) | Delivery changes must not regress eval. |

**Known pre-existing inconsistency:** `action.yml` exposes `history-redis-url` but `open_store`
implements only `cache` and `none`. Phase 3 does not implement `redis`; document it as unsupported
rather than silently degrading.

---

## 2. Module layout

```
tools/rca/
  deliver/
    __init__.py
    targets.py          # workflow_run context -> DeliveryContext + channels
    severity.py
    suppress.py
    render_comment.py   # comment body, distinct from render.py / summary.md
    pr_comment.py
    commit_comment.py
    issues.py           # recurring issues + IssuesHistoryStore (delivery-owned)
    notify.py           # email / chat, off by default
    owners.py           # CODEOWNERS, last-match wins
    feedback.py         # /resolved parser and reactions
  cli.py                # new subcommand: deliver
```

**Decision (was an open option in v1.1):** the issues-backed store lives in **`deliver/issues.py`**,
not in `history.py`. AGENTS.md forbids `history.py` from importing GitHub-specific modules; keeping
it out entirely is cleaner than injecting a writer. `history.py` is unchanged by Phase 3 except, if
desired, an `open_store` passthrough that accepts an already-constructed store.

`cleaner.py`, `drain_index.py`, `budget.py`, `redact.py`, `history.py`, `extract.py`, `classify.py`,
`pipeline_logs.py` stay GitHub-agnostic. `deliver/*` may import `github_api` and `models`.

---

## 3. Packaging — separate from collect

**Do not add `contents: write` / `pull-requests: write` / `issues: write` to the collect job.**
Collect stays `actions: read`, `contents: read`. A bug in delivery must not be able to push.

**A composite action cannot declare `permissions`.** The block below belongs in the **caller /
reusable workflow** (`.github/workflows/rca.yml` and consumer stubs), not in `action.yml`:

```yaml
# caller workflow — delivery job only
permissions:
  actions: read
  contents: write          # commit comments; new in Phase 3
  pull-requests: write
  issues: write
```

Preferred shape: a **separate job** that consumes the `rca/` artifact from the collect job. A second
composite step in the same job is acceptable only when the caller already granted write scopes to
that job. Document this escalation. Org-restricted tokens fail delivery with "resource not
accessible" — record in `delivery.errors`, keep the job summary, exit 0.

New CLI:

```text
python -m tools.rca.cli deliver \
  --summary rca/summary.json \
  --analysis rca/analysis.json \
  --out rca \
  --dry-run            # deliver:false
```

Idempotent on `(run_id, channel)`. Re-run updates, never duplicates. Note `run_attempt > 1` is a
re-run of the **same** `run_id` — the sticky marker must match and update, not duplicate.

---

## 4. Delivery context

Do not assume a PR. Do not assume the default branch is `main`.

```python
class DeliveryContext(BaseModel):
    trigger: Literal[
        "pull_request", "push_default", "push_branch",
        "schedule", "dispatch", "tag", "merge_group", "unknown"
    ]
    pr_number: int | None = None
    pr_is_draft: bool = False
    is_fork: bool = False
    branch: str
    default_branch: str          # get_repo(repo)["default_branch"] — see §0
    is_default_branch: bool
    actor: str
    commit_sha: str
```

Sources: `pr_number`/`is_fork`/`actor`/`commit_sha`(=`head_sha`)/`branch`(=`head_branch`) from
`Summary.run`; `pr_is_draft` from `Summary.changes.pr_is_draft`; `default_branch` from `get_repo`.
`default_branch` fallback chain: `get_repo` → env `RCA_DEFAULT_BRANCH` (the caller sets it from `github.event.repository.default_branch`, present in every `workflow_run` payload) → `main`, recording a note at each fallback — never crash. (v1.2 named `GITHUB_DEFAULT_BRANCH`; that is not a standard Actions variable.)

**Tag detection (v1.3).** `RunMeta` records only `event` and `head_branch`; for a tag push, `head_branch` is the tag name. For `event == "push"` only, call `ref_is_tag(repo, head_branch)` (`GET /repos/{repo}/git/ref/tags/{name}`: 200 → tag, 404 → not a tag). On lookup failure treat it as not a tag and record a note.

First match wins: resolved `run.pr_number` (including the fork SHA fallback) → `pull_request`;
`merge_group`; push tag; push to `default_branch` → `push_default`; other push → `push_branch`;
`schedule`; `workflow_dispatch` → `dispatch`; else `unknown`.

### 4.1 Routing matrix

| Trigger | Job summary | Comment | Issue | Notify |
|---|---|---|---|---|
| PR, ready | always | sticky PR comment | on recurrence (`seen_count >= issue-threshold`) | no |
| PR, draft | always | none | no | no |
| PR, fork | always | PR comment, escaped (§11) | no unless `allow-fork-issues` | no |
| **push_default** | always | **commit comment** | **always** | team + actor, if webhook/SMTP set |
| push_branch | always | commit comment only if `comment-on-branch-push` | on recurrence | no |
| merge_group | always | **none** | on recurrence | actor if notify configured |
| tag | always | commit comment | always | team if configured |
| schedule | always | none | always | owning team if configured |
| dispatch | always | none | no | actor only if configured |
| unknown | always | none | no | no |

Job summary is always written by collect. Delivery must not depend on it succeeding.

### 4.2 Broken default branch

Highest severity. No PR to hold the thread.

1. Commit comment: `POST /repos/{owner}/{repo}/commits/{sha}/comments`. Needs `contents: write`.
2. Always open or update an issue, even on first occurrence.
3. Notify pusher and owning team **only if** a channel is configured. Missing SMTP is not an error.
4. `DefaultBranchState`: `red_since`, `red_duration_hours`, `consecutive_failures`, `last_green_sha`.
   Over 1 hour or ≥ 3 consecutive failures → severity `critical`.
5. Same fingerprint already tracked → update issue and commit comment. Do not open a new issue every run.

**Data source for `DefaultBranchState` (v1.2, refined in v1.3):** derive at delivery time, read-only
(`actions: read`):

1. Scope to the failing workflow: `get_run(repo, run_id)["workflow_id"]`, then
   `list_runs(repo, workflow_id=…, branch=default_branch, status="completed")`. If `workflow_id` is
   unavailable, use `list_runs(repo, branch=default_branch, status="completed")` filtered by
   `name == Summary.run.workflow_name`.
2. Walk newest → oldest, capped at 50 runs. `consecutive_failures` = count of leading runs whose
   `conclusion != "success"` (the current run counts as the first).
3. `last_green_sha` = `head_sha` of the first success encountered.
4. `red_since` = `created_at` of the **oldest** failing run in that leading streak (the first run
   after the last green); if the streak reaches the cap, use the oldest run seen.
   `red_duration_hours` = now − `red_since`.
5. When the run list is unavailable, fall back to `Summary.history.last_success_at` /
   `.last_success_sha`; `consecutive_failures` is then `None`, and only `red_duration_hours` can
   make severity `critical`.

### 4.3 Non-default branch without a PR

Job summary only. `comment-on-branch-push` default `false`. If enabled, still suppress when the
same fingerprint was commented on that branch inside `quiet-window-minutes` (default 60).

---

## 5. Suppression

Evaluate before any write, in three stages. (v1.3: v1.2's single "first match wins" table could not
express rules that must combine — e.g. an edited comment that is also Unverified and below threshold.)

**Stage A — hard suppressions.** First match wins; stop; record `delivery.suppressed_by`.

| # | Condition | Action | `suppressed_by` |
|---|---|---|---|
| A1 | `classification.is_flaky` or `short_circuit == flake_same_sha_passed` | No comment. `ci:flaky` label if a PR exists. | `flaky` |
| A2 | `short_circuit == infra_widespread` | One platform notification if configured. No per-PR comments. | `infra_widespread` |
| A3 | `short_circuit == infra_runner` | Job summary + optional platform notify. No author comment. | `infra_runner` |
| A4 | Draft PR | Job summary only. | `draft` |
| A5 | Delivery error for this `(run_id, channel)` after one retry | Stop. Record `delivery.errors`. Enforced by the writer (Step 15). | `delivery_error` |

**Stage B — dedupe.** Not a suppression; `suppressed_by` stays `null`.

| # | Condition | Action |
|---|---|---|
| B1 | Same coarse fingerprint already commented on this target (PR or commit) | Edit that comment in place. |
| B2 | Same fingerprint, same branch, inside the quiet window | Update in place. No new chat/email. |

**Stage C — presentation modifiers.** Always evaluated; cumulative; never suppress.

| # | Condition | Effect |
|---|---|---|
| C1 | `analysis.grounding` present and (`grounding_rate < 1.0` or `grounded is False`), or `display_status == needs-review` | Unverified banner. |
| C2 | Confidence below threshold | Post evidence + category. **Omit every root-cause claim** (§7.1). |

**Confidence threshold.** Input `confidence-threshold` default `medium`.

| Setting | Post a root-cause claim when |
|---|---|
| `low` | any of low / medium / high |
| `medium` (default) | medium or high |
| `high` | high only |

`AnalysisResult.confidence` is a label. Do not invent a float. When the model was skipped and only a
deterministic card exists (the common path after the Step 4 gate), the deterministic card's
`confidence` is used unchanged — `high` counts as `high`, `medium` as `medium`.

**Do not re-apply Step 10's policy (new in v1.2).** `validate.py` already stripped ungrounded
citations at `0.5 ≤ rate < 1.0` and already replaced the model answer with the deterministic card
below `0.5` (status `unvalidated` → `needs-review`). Delivery reads the **post-validate** record:
modifier C1 only decides the banner. Never strip or re-rank citations again.

**Quiet-window / idempotency state (new in v1.2).** No new state store. Derive from what GitHub
already holds: the sticky comment found by marker carries `created_at`/`updated_at` — that is the
quiet-window clock for that `(target, fingerprint_coarse)`. For notifications, the fingerprint
issue's `updated_at` serves the same role. When neither exists, there is nothing to suppress.

---

## 6. Severity

| Severity | When | Channels |
|---|---|---|
| `critical` | `push_default` or `tag`, and (red > 1h or ≥ 3 consecutive failures) | commit comment + issue + notify if configured |
| `high` | `push_default`, `tag`, `merge_group` | commit comment (**not for `merge_group`** — see §4.1) + issue + notify if configured |
| `normal` | ready PR (incl. fork), `schedule` | PR comment (none for `schedule`; its issue/notify come from §4.1 / §10) |
| `low` | branch push, draft, dispatch, `unknown` | job summary |
| `info` | flaky, or widespread already notified | job summary only |

Expose `severity` as an action output.

---

## 7. Comment body

Not `summary.md`. Five-second scan. Hard cap **4000 characters**. Truncate `<details>`, never the
headline. **Redact before truncating**, so truncation can never split a redaction.

```markdown
<!-- rca-bot:fp=<fingerprint_coarse> -->
### CI failure — <category, human words>

**<one-line root cause>**
Confidence: high · `<failed step name>` · [run #N](url)

**Suggested fix** — <one line>

<details><summary>Evidence</summary>

```text
<cited lines only, primary_failure_line / failed_step_excerpt first>
```
</details>

---
<sub>Seen N× · [full report](artifact or run summary) · reply `/resolved <what fixed it>`</sub>
```

Category human words come from one fixed map in `render_comment.py` covering every category, including `release` (e.g. "release version already published") and `unknown` ("unclassified failure").

### 7.1 Headline precedence (new in v1.2 — resolves the v1.1 §5/§7 conflict)

Evaluate in order:

1. **Confidence below threshold** → no root-cause line at all, from **either** source. Headline is
   `CI failure — <category>`; body is the evidence block + suggested fix only if the fix is generic
   and non-claiming. (v1.1 §5 rule 7 said "omit the root-cause sentence" while §7 still fell back to
   the deterministic `one_liner`, which *is* a root-cause claim. Threshold wins.)
2. **Confidence passes, `display_status` is `ok`/`cached`, `source` is `ai`/`hybrid`/`mixed`** →
   use `analysis.result.root_cause`.
3. **Confidence passes, otherwise** (source `deterministic`, or `display_status` is `deterministic`)
   → use `diagnosis.one_liner`; fix from `diagnosis.fix_one_liner`.
4. **`display_status == needs-review`** → post with the Unverified banner and the deterministic
   text; never present it as confirmed.

Never include rule ids (any `R<n>`, e.g. `R8`, `R14`, `R18`, `R19`) in any channel.

### 7.2 Evidence block

- `primary_failure_line` first when present (Step 1 — the single most causal line), then
  `failed_step_excerpt.lines` (max 8 total).
- Then grounded citations whose `source` is `failed_step_excerpt` or `first_error_window`.
- Do **not** lead with a pipeline-log template or a `log_templates` citation from a step that did
  not fail (this is the exact failure mode Steps 2–3 and 9 fixed; delivery must not reintroduce it).
- Link to the run Summary / uploaded `rca/` artifact. Do not paste `summary.md`.

### 7.3 Sticky key

`(pr_number, fingerprint_coarse)` for PRs; `(sha, fingerprint_coarse)` for commit comments. The
marker is the HTML comment above. **Find by listing comments and matching the marker, not by
author** (the bot identity differs between `GITHUB_TOKEN` and a PAT). Different coarse fingerprint
→ new comment.

---

## 8. Issues — delivery-owned (REWRITTEN in v1.2)

### 8.0 Why this changed

v1.1 §8.1 made `issues` a `history-backend` swap. But `HistoryStore.upsert()` is called **during
collect**, so selecting `issues` would require `issues: write` on the **collect** job — directly
contradicting §3 and acceptance #28. Resolution:

- **Collect keeps `history-backend: cache`.** Unchanged, read-only, fast, no API cost.
- **Delivery owns the issues mirror.** `deliver/issues.py` writes the fingerprint issue and the
  index issue using the delivery job's `issues: write` token.
- `IssuesHistoryStore` still implements the `HistoryStore` protocol (`get`, `find_by_coarse`,
  `upsert`, `close`) so it is testable and reusable — it is simply **constructed by delivery** with
  an injected `GitHubClient` + repo, never by `open_store(backend, path)`.
- If a future phase wants collect to *read* history from issues, that needs only `issues: read` and
  can be added then. Do not do it now.

`action.yml`'s `history-backend` input keeps `cache` as default and gains no `issues` value in
Phase 3; delivery exposes `create-issues` instead.

### 8.1 `IssuesHistoryStore` (in `deliver/issues.py`)

- One issue per **fine** fingerprint, title `[RCA] <category>: <one-line summary>`.
- Labels: `rca-fingerprint`, `rca:<category>`, plus `rca:infra` or `rca:flaky` when applicable.
- Body: short prose, then a fenced `json` block of `FailureRecord`. On read, parse **only** that block.
- **Index issue** titled `[RCA] fingerprint index` maps fingerprint → issue number. Do not use the
  Search API as the primary lookup (30 req/min, eventually consistent → duplicate issues).
  Primary lookup is the index issue; label listing is the fallback; Search is never required (AC #21).
- Body cap 65,536. Assert before write. Keep `run_ids` capped at 20 (already enforced by
  `FailureRecord.absorb`).
- **Migration:** a one-shot `deliver --migrate-history` reads the cache store's `all_records()` and
  upserts into issues, idempotent by fingerprint. Never destructive to the cache.
- `GITHUB_TOKEN` cannot trigger workflows from issue events. That is desirable.

### 8.2 When to open an issue

- `push_default` or `tag` or `schedule`: always.
- Other triggers: `seen_count >= issue-threshold` (default 3).
- Fork PRs: no, unless `allow-fork-issues=true`.
- On recurrence: comment + bump `count`. Same fingerprint does not open a second issue.
- Close after 14 days with no recurrence. Reopen on recurrence.

Assignment: owning team from §9, not an individual, unless no team resolves and `default-notify` is
a user.

---

## 9. Ownership

Parse CODEOWNERS from `.github/CODEOWNERS`, `CODEOWNERS`, `docs/CODEOWNERS` (first file found, via
`get_file`). **Last matching pattern wins** (not gitignore order).

1. `is_infra_vs_code == infra` or an infra short-circuit → `platform-team` input. Never the pusher.
2. Else owners of suspected files (`analysis.result.suspected_files` or `diagnosis.suspected_files`).
3. Else owners of blame-range files (`changes.files`), most-changed first.
4. Else `run.actor`.
5. Else `default-notify`.

Record `delivery.owner_resolved_by`.

---

## 10. Notifications

Off unless `smtp-url` or `chat-webhook-url` is set. Missing channel is not a failure.

- Email subject: `[CI] <severity>: <workflow> failed on <branch>`. No log text in the subject.
- Chat: severity, one-line cause, run link, owner. No evidence blocks.
- Only `high` and `critical`, plus `schedule` at any severity.
- Widespread infra: exactly one message naming workflows, not one per run.
- Quiet window per fingerprint per channel (default 60 min), clocked per §5.
- All bodies through `redact.py`.

---

## 11. Fork safety

- Never interpolate model or log text into a `run:` script. Pass via `env` or a file.
- Delivery stays on `workflow_run` in the base repo, not inside the CI job.
- `is_fork`: strip/escape HTML in model text; do not treat PR title or branch name as trusted.
- No issues from fork failures unless `allow-fork-issues=true`.

---

## 12. Feedback

- Comment starting with `/resolved ` on the PR comment thread or the fingerprint issue sets
  `resolution`, `resolution_author`, `human_verified=true`. React 👍 on that comment.
- Reactions on the RCA comment: 👍 useful, 👎 not useful. Store counts on the record if cheap;
  do not block delivery on the reactions API.
- Inferred resolution (fingerprint gone and pipeline green): store diff summary with
  `human_verified=false`. Never render it as confirmed.

A `/resolved` handler can be a separate `issue_comment` / `pull_request_review_comment` workflow in
the consumer repo. **Do not require it for the first ship** — ship the parser plus a documented
workflow stub, and test the parser (AC #19).

---

## 13. Action interface

Inputs (all optional):

```yaml
deliver:                 { default: 'false' }   # true only after dry-run week
comment-on-pr:           { default: 'true' }
comment-on-commit:       { default: 'true' }
comment-on-branch-push:  { default: 'false' }
create-issues:           { default: 'true' }
issue-threshold:         { default: '3' }
allow-fork-issues:       { default: 'false' }
confidence-threshold:    { default: 'medium' }  # low | medium | high
quiet-window-minutes:    { default: '60' }
platform-team:           { default: '' }
default-notify:          { default: '' }
smtp-url:                { default: '' }
chat-webhook-url:        { default: '' }
```

Outputs: `delivered-to`, `comment-url`, `issue-url`, `severity`, `suppressed-by`.

`deliver: false` renders the comment to `rca/delivery-preview.md` and logs channels. **Zero GitHub
writes.** The preview must be byte-identical to what would be posted, so the dry-run week is
meaningful.

`history-backend` is unchanged (`cache`); see §8.0.

---

## 14. Non-functional

- Delivery errors never fail the workflow and never delete `summary.md` / `summary.json`.
- One retry on 5xx / `retry-after`. Then record and continue. `GitHubAPIError` (incl. 403) is caught
  and recorded, never propagated.
- Per-call timeout 15s. Delivery budget 30s total — the retry/backoff must respect the budget.
- Redact every channel, before truncation.
- Enterprise: same API base as collect (`GITHUB_API_URL` / `RCA_GITHUB_API_URL`).

---

## 15. Schema additions

Optional on `Summary` (defaults so old files load):

```python
class DeliveryReport(BaseModel):
    trigger: str
    severity: str
    suppressed_by: str | None = None
    delivered_to: list[str] = []
    comment_url: str | None = None
    issue_url: str | None = None
    owner_resolved_by: str | None = None
    errors: list[str] = []
    dry_run: bool = False

class Summary(BaseModel):
    delivery: DeliveryReport | None = None
```

**Write-back rule (new in v1.2):** delivery runs after collect and after `analyze` merged its
`analysis` block into `summary.json`. When writing `delivery`, re-read `summary.json`, set only the
`delivery` key, and write back — **never** regenerate the file or clobber `analysis`.

---

## 16. Build order

0. **§0 API write layer** — `json` body support in `_request`, `get_repo`, and the write methods.
   No behaviour change to collect. *(New in v1.2 — everything else depends on it.)*
0b. **Correctness fixes (collector Step 12b):** terminal cause → category (`release`), headline prefers
   `primary_failure_line`, source labelling, `AGENTS.md` updated for Phases 1–3. *(New in v1.3.)*
1. `DeliveryContext` + default-branch lookup + routing matrix + `DefaultBranchState`, unit tests, no writes.
2. `suppress.py` + `severity.py`. Tests for flake, widespread, draft, confidence label, quiet window.
3. `render_comment.py` with `primary_failure_line`/failed-step excerpt, 4000-char cap, unverified
   banner, headline precedence (§7.1), no rule ids.
4. Dry-run CLI `deliver --dry-run` writing `rca/delivery-preview.md` + `DeliveryReport` write-back.
5. Sticky PR comment (marker, update in place).
6. Commit comment + default-branch issue + dedupe.
7. `IssuesHistoryStore` + index issue + `--migrate-history` (idempotent).
8. CODEOWNERS last-match + infra → platform-team.
9. Notify (optional). `/resolved` parser + documented workflow stub.
10. Wire action inputs/outputs, separate delivery job + permissions, `deliver` default **false**.

---

## 17. Acceptance criteria

1. Ready PR, one coarse fingerprint → one comment; second run edits it.
2. Different coarse fingerprint in the same PR → second comment.
3. Push to default branch, no PR → commit comment + issue. Notify only if a channel is set.
4. Default branch read from the repo API. A `master` repo routes as `push_default`.
5. Push to a non-default branch → job summary only when `comment-on-branch-push` is false.
6. Default branch red > 1h → `severity=critical`, `red_duration_hours` and `consecutive_failures` set
   from the run-list walk (§4.2).
7. Same fingerprint on a still-red default branch updates the issue; does not open another.
8. Flaky → no comment; `ci:flaky` label if a PR exists.
9. Five simultaneous `infra_widespread` runs → one notification, zero PR comments.
10. Confidence `low` with threshold `medium` → evidence posted, **no root-cause line from either
    source** (§7.1 rule 1).
11. `grounding_rate < 1.0` → Unverified banner present; citations are **not** re-stripped by delivery.
12. Every suppression writes `delivery.suppressed_by`.
13. Schedule failure → issue, no commit comment.
14. `workflow_dispatch` → actor notify only if configured; no issue.
15. Infra verdict routes to `platform-team`, not `run.actor`.
16. CODEOWNERS last-match wins.
17. Fork PR does not open an issue by default.
18. No model text interpolated into `run:`.
19. `/resolved` parser sets `human_verified` (handler may be a follow-up workflow; test the parser).
20. Cache → issues migration is idempotent.
21. Search API unavailable → index issue still finds the fingerprint issue.
22. SMTP failure recorded; PR comment still posted. A 403 from a restricted token is recorded in
    `delivery.errors` and the step exits 0.
23. `deliver: false` → preview file byte-identical to the would-be comment, zero GitHub writes.
24. Comment ≤ 4000 chars; headline intact; redaction applied before truncation.
25. No secret pattern in comment, issue, email, or chat.
26. Comment evidence leads with `primary_failure_line` / `failed_step_excerpt` when present, and does
    not lead with an unrelated pipeline-log template.
27. Enterprise API base is honored (no hardcoded github.com).
28. **Collect job token is unchanged** (`actions: read`, `contents: read`); write scopes exist only
    on the delivery job. `history-backend` remains `cache` for collect.
29. `summary.json` write-back sets only `delivery` and preserves the `analysis` block.
30. `pytest tests/rca -q` and `pytest tests/eval -q` stay green; eval metrics do not regress.
31. A tag push is detected via `ref_is_tag` and routes as `tag`; a failed lookup routes as a branch push and records a note.
32. `DefaultBranchState` is scoped to the failing workflow; unrelated workflows on the default branch do not count toward `consecutive_failures`.
33. Stage C modifiers combine: an edited (B1) comment can carry both the Unverified banner (C1) and an omitted root cause (C2).
34. A release-gate failure (category `release`) renders with human words, never as "unknown".

---

## 18. Rollout

1. `deliver: false` for one week. Read `delivery-preview.md`. Count comments you would not have wanted.
2. PR comments only, one pilot repo, two weeks. Tune `confidence-threshold` and quiet window.
3. Default-branch commit comments + issues; enable `create-issues` and run `--migrate-history` once.
4. Notifications last.

---

## 19. Deferred

Auto-fix PRs, merge blocking, dashboards, PagerDuty/Opsgenie, CD/Kubernetes delivery, `redis`
history backend, collect-side reads from the issues store.
