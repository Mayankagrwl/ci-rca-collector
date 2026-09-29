# Claude Code task — Step 13: delivery context, routing, default-branch state, severity, suppression (no writes)

## Context

`ci-rca-collector`. Read `AGENTS.md` and `docs/rca-delivery-spec-v1.3.md` (**§4, §4.1, §4.2, §5, §6, §8.2, §13**) first.

**Prerequisites — stop and report if either is missing:**
- **Step 12** merged: `GitHubClient.get_repo()` exists and `_request` accepts a JSON body.
- **Step 12b** merged: category `release` exists and `AGENTS.md` describes Phase 3.

This is the decision layer of Phase 3. It turns a finished `summary.json` + `analysis.json` into *what should happen* — trigger, channels, severity, suppression, modifiers — as **pure, table-driven, fully tested functions**. It performs **zero GitHub writes**. The only network calls are three read-only lookups in one resolver, behind an injectable client.

Do **only** what this document scopes.

## Verified facts to build on (do not re-derive)

- `Summary.run` (`RunMeta`): `run_id`, `event`, `actor`, `head_sha`, `head_branch`, `pr_number` (already fork-resolved by the collector), `is_fork`, `workflow_name`, `html_url`, `concurrent_failures`, `concurrent_workflows`, `concurrent_branches`.
- There is **no** tag information in `RunMeta` (verified). For a tag push, `head_branch` is the tag name.
- `Summary.changes.pr_is_draft` (may be `None` if changes weren't collected); `Summary.history.seen_count`, `.last_success_at`, `.last_success_sha`; `Summary.classification.is_flaky`, `.is_infra_vs_code`, `.category`, `.confidence`; `Summary.verdict.short_circuit`.
- The analysis record in `analysis.json` / `summary.json["analysis"]` is an `AnalysisRecord`: `.status`, `.result` (`AnalysisResult`: `.confidence`, `.source`, `.root_cause`), `.grounding` (`GroundingResult`: `.grounded`, `.grounding_rate`). Use `analyze.display_status(record)` for user-facing status. **Read the post-validate record; never recompute grounding** (v1.3 §5).
- `GitHubClient.get_run(repo, run_id)` returns the run payload (includes `workflow_id`); `list_runs(repo, *, head_sha=None, branch=None, status=None, per_page=50, workflow_id=None)`.

## Deliverables

Create `tools/rca/deliver/__init__.py`, `targets.py`, `severity.py`, `suppress.py`, plus one small read method in `github_api.py`.

### 1. `github_api.py` — one read-only addition

`ref_is_tag(repo, name) -> bool`: `GET repos/{repo}/git/ref/tags/{name}` (URL-encode `name`); 200 → `True`, 404 → `False`, anything else → raise `GitHubAPIError` with `status_code`. Read-only; follow the existing GET method pattern.

### 2. Models (in `deliver/`, not on `Summary` — `DeliveryReport` is Step 14)

- `DeliveryInputs` — every input from v1.3 §13 with its default (`deliver=False`, `comment_on_pr=True`, `comment_on_commit=True`, `comment_on_branch_push=False`, `create_issues=True`, `issue_threshold=3`, `allow_fork_issues=False`, `confidence_threshold="medium"`, `quiet_window_minutes=60`, `platform_team=""`, `default_notify=""`, `smtp_url=""`, `chat_webhook_url=""`). Validate `confidence_threshold ∈ {low, medium, high}`. (Parsing from action inputs is Step 19.)
- `DeliveryContext` — exactly v1.3 §4.
- `DefaultBranchState` — `red_since`, `red_duration_hours`, `consecutive_failures` (`int | None`), `last_green_sha`, plus `source: "run_list" | "history" | "none"`.
- `RoutePlan` — `job_summary: bool` (always `True`), `comment: "pr" | "commit" | None`, `issue: "always" | "on_recurrence" | None`, `notify: list[str]` (audiences such as `"team"`, `"actor"`, `"owning_team"`, `"platform"`), `notes: list[str]`.
- `SuppressionDecision` — `suppressed_by: str | None` (Stage A only), `dedupe: "edit" | "update_quiet" | None` (Stage B), `unverified_banner: bool` (C1), `omit_root_cause: bool` (C2), `add_flaky_label: bool`, `notify_platform_once: bool`, `reasons: list[str]`.

### 3. `targets.py`

- **`ReadClient` Protocol** with `get_repo`, `get_run`, `list_runs`, `ref_is_tag` — so tests inject a fake and `severity.py` / `suppress.py` never import `github_api`.
- **`resolve_context(summary, *, client, repo, env=os.environ, now=None) -> tuple[DeliveryContext, list[str]]`** — never raises; returns notes.
  - `default_branch`: `client.get_repo(repo)["default_branch"]` → env `RCA_DEFAULT_BRANCH` → `"main"`, noting each fallback.
  - Trigger, **first match wins** (v1.3 §4): `run.pr_number` present → `pull_request`; `event == "merge_group"` → `merge_group`; `event == "push"` and `client.ref_is_tag(repo, head_branch)` → `tag` (lookup failure → not a tag + note); `event == "push"` and `head_branch == default_branch` → `push_default`; `event == "push"` → `push_branch`; `event == "schedule"` → `schedule`; `event == "workflow_dispatch"` → `dispatch`; else `unknown`.
  - `pr_is_draft` from `summary.changes.pr_is_draft` (treat `None` as `False`); `commit_sha = run.head_sha`; `branch = run.head_branch`; `is_default_branch = branch == default_branch`.
- **`route(context, inputs) -> RoutePlan`** — encode the v1.3 §4.1 routing matrix **as a data table** (one row per trigger/variant), not an if-chain. Rows: PR ready, PR draft, PR fork, `push_default`, `push_branch` (comment only if `comment_on_branch_push`), `merge_group`, `tag`, `schedule`, `dispatch`, `unknown`. Honour `comment_on_pr` / `comment_on_commit` switches. Notify audiences are recorded as *intent*; whether a channel exists is decided in Step 18.
- **`should_open_issue(context, summary, inputs) -> bool`** — v1.3 §8.2: `push_default` / `tag` / `schedule` → always (if `create_issues`); other triggers → `seen_count >= issue_threshold`; fork PRs → `False` unless `allow_fork_issues`; draft / dispatch / unknown → `False`.
- **`default_branch_state(summary, *, client, repo, default_branch, now=None) -> DefaultBranchState`** — v1.3 §4.2 exactly: scope by `get_run(...)["workflow_id"]` (fallback: filter by `name == summary.run.workflow_name`), `status="completed"`, walk newest→oldest capped at 50, leading non-success streak (current run counts), `last_green_sha` from the first success, `red_since` = `created_at` of the **oldest** failing run in the streak, `red_duration_hours` from `now`. On lookup failure fall back to `summary.history.last_success_*` with `consecutive_failures=None` and `source="history"`. Never raises. Only called for `push_default` / `tag`.

### 4. `severity.py` — pure

`severity(context, state, summary, *, widespread_already_notified=False) -> "critical" | "high" | "normal" | "low" | "info"` per v1.3 §6:
- `info`: flaky (`is_flaky` or `flake_same_sha_passed`), or `infra_widespread` already notified.
- `critical`: `push_default` or `tag`, and (`red_duration_hours > 1` or `consecutive_failures >= 3`). `None` values never trigger critical.
- `high`: `push_default`, `tag`, `merge_group`.
- `normal`: ready PR (incl. fork), `schedule`.
- `low`: `push_branch`, draft PR, `dispatch`, `unknown`.

### 5. `suppress.py` — pure, three stages (v1.3 §5)

`evaluate(summary, record, context, inputs, *, existing=None, now=None) -> SuppressionDecision`, where `existing` is an optional small value `(fingerprint_coarse, branch, updated_at)` describing a previously posted comment. **Finding that comment is Step 15; here it is only a parameter.**

- **Stage A — hard suppressions, first match wins, set `suppressed_by`:** A1 `flaky` (set `add_flaky_label` when `context.pr_number`), A2 `infra_widespread` (set `notify_platform_once`), A3 `infra_runner`, A4 `draft`. Reserve the value `delivery_error` (A5) as a module constant — the writer enforces it in Step 15.
- **Stage B — dedupe, `suppressed_by` stays `None`:** B1 `existing` has the same `fingerprint_coarse` → `dedupe="edit"`; B2 same fingerprint and branch with `updated_at` inside `quiet_window_minutes` of `now` → `dedupe="update_quiet"`.
- **Stage C — cumulative modifiers, always evaluated (also after B):** C1 `unverified_banner` when `record.grounding` is present and (`grounding_rate < 1.0` or `grounded is False`), or `display_status(record) == "needs-review"`. C2 `omit_root_cause` when the confidence rank is below the threshold (`low < medium < high`). Confidence source: `record.result.confidence`; when there is no analysis result, use `summary.classification.confidence` (the deterministic card).
- Helper `within_quiet_window(updated_at, now, minutes) -> bool`.

## Constraints (AGENTS.md)

- **No GitHub writes.** No `deliver` CLI subcommand, no rendering, no `action.yml` or workflow changes (Steps 14, 19).
- `severity.py` and `suppress.py` import neither `github_api` nor `httpx`. Only `targets.py` touches a client, and only through the `ReadClient` Protocol.
- `deliver/*` may import `models`, `analyze.display_status`, and (in `targets.py`) `github_api` types. Source-agnostic modules stay untouched.
- Everything is deterministic: pass `now` explicitly in tests; no wall-clock reads in pure functions.
- Never raise out of `resolve_context` or `default_branch_state`; record notes instead.

## Acceptance criteria (tests under `tests/rca/deliver/`, fake client, no network)

1. **Trigger precedence** — one test per trigger, plus: a repo whose default branch is `master` routes a push to `master` as `push_default` (AC #4); a tag push routes as `tag`; a `ref_is_tag` failure routes as `push_branch` with a note (AC #31); `get_repo` failure falls back to `RCA_DEFAULT_BRANCH` and then `main` with notes.
2. **Routing matrix** — table-driven test covering all 10 rows of v1.3 §4.1, including the `comment_on_branch_push` and `comment_on_pr` / `comment_on_commit` switches.
3. **Issues** — `push_default` / `tag` / `schedule` always; PR below threshold no, at threshold yes; fork PR no by default and yes with `allow_fork_issues` (AC #17); `dispatch` no (AC #14); `create_issues=false` disables all.
4. **DefaultBranchState** — streak of 3 failures → `consecutive_failures=3`; red for 90 minutes → `red_duration_hours≈1.5` (AC #6); `red_since` is the **oldest** run in the streak; runs of another workflow are excluded (AC #32); run-list failure → `source="history"`, `consecutive_failures=None`.
5. **Severity** — every row of v1.3 §6, including `critical` via duration and via count, `None` values never critical, `schedule` → `normal`, `unknown` → `low`.
6. **Suppression** — flaky → `suppressed_by="flaky"` + `add_flaky_label` when a PR exists (AC #8); widespread → `notify_platform_once` and no comment (AC #9 decision part); runner; draft; every Stage A outcome sets `suppressed_by` (AC #12); B1 and B2 leave `suppressed_by=None`; confidence `low` with threshold `medium` → `omit_root_cause` (AC #10); `grounding_rate=0.8` → banner (AC #11); **C1 + C2 + B1 together** on one decision (AC #33).
7. **Purity** — `severity` and `suppress` modules import no `github_api` / `httpx` (assert via `sys.modules` or source inspection).
8. Suites green: `pytest tests/rca -q` and `pytest tests/eval -q`; acceptance-35 grep empty; eval `--no-llm` metrics unchanged from the Step 12b baseline.

## Out of scope

- Rendering, the `deliver` CLI, `delivery-preview.md`, `DeliveryReport` write-back (Step 14).
- Finding or writing comments, labels, issues (Steps 15–16). CODEOWNERS (Step 17). Notification channels (Step 18). Action wiring (Step 19).

## Deliverable

`tools/rca/deliver/{__init__,targets,severity,suppress}.py`, the `ref_is_tag` method, and `tests/rca/deliver/`, with both suites green. Summarize the routing table as implemented and show one worked example: a `push_default` failure red for 2h → context, route plan, state, severity `critical`, suppression decision.
```
Short command to run it in Claude Code (from the repo root):

Read docs/cc-prompt-step13-delivery-context-routing.md and implement exactly that slice — pure decision logic, zero GitHub writes. First confirm Steps 12 and 12b are merged and stop if not. Follow AGENTS.md, add the tests it specifies, and make sure `pytest tests/rca -q` and `pytest tests/eval -q` are green before you finish.
```
