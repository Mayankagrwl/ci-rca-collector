# Claude Code task — Step 1: anchor RCA evidence on the GitHub-designated failed step

## Context

This repo (`ci-rca-collector`) collects GitHub Actions failure context and produces a root-cause diagnosis, deterministically (`collect`) and optionally via an LLM (`analyze`). Read `AGENTS.md` and `docs/ci-rca-collector-spec-v8.md` before changing anything.

We are fixing a **precision** problem, in the smallest safe slice. Do **only** what this document scopes. Do not implement the citation-grounding gate, the terminal-cause table, or the analyze-gating changes — those are later slices.

## The problem this slice fixes

For one real failure, the job's failed step was **"Check Version in Artifactory"**, whose log contained:

```
Checking if version 3.1.21 exists in Artifactory
This release already exists on Artifactory. You need to update package.json
Error: Process completed with exit code 1.
```

That "already exists … update package.json" line is the true root cause. But the deterministic path produced a **generic wrong** answer ("The CI workflow or action definition changed … revert the pipeline edit", rule R14), because the causal line never reached `error_lines` / the `first_error` window — the window centered on an earlier `curl … manifest.json` check instead, and `specific_log_cause()` returned nothing, so R14's blast-radius text won by default.

Root issue: **the first-error window and error-line extraction scan the whole cleaned log via `_first_error_index`, ignoring which step GitHub says actually failed.** GitHub already names the failed step; the excerpt uses it (`_pick_failed_group`) but the windows and error-lines do not.

**This is an illustration, not a special case. Do not hardcode anything about Artifactory, "already exists", package.json, or this specific pipeline.** The fix must be general and driven by the existing regexes.

## Goal of this slice

Make the failed step the anchor of collection so that, whenever GitHub identifies a failed step:

1. The `first_error` window is centered **within the failed step's log group** (only fall back to a whole-log scan when there is no identifiable failed-step group).
2. `error_lines` used downstream include the failed step's own lines, so `specific_log_cause()` / `user_facing()` see the real cause even when a low-evidence rule (R14/R18) fires.
3. A new `primary_failure_line` is captured — the first semantic/error cause line inside the failed-step group — and surfaced on the model and in the analyze evidence pack as the first item.

## Where to change (verify signatures before editing)

- **`tools/rca/extract.py`** — the core of this slice.
  - `extract_from_lines(lines, *, failed_step_name=None)` already receives `failed_step_name`. Thread it into window/error-line building.
  - Today `_windows(lines)` centers on `_first_error_index(lines)` over the whole log, and `_error_lines(lines)` scans every record. Change these so that, when `failed_step_name` resolves to a GitHub log group (reuse `_github_groups` / `_pick_failed_group`), the first-error window and the error-line scan are **scoped to that group first**. If no group matches, keep today's whole-log behaviour (backwards compatible).
  - Add a helper that returns `primary_failure_line`: the first line inside the failed-step group matching the existing cause regexes (`_SEMANTIC_CAUSE`, then `_WINDOW_ERROR`, then `ERROR_LINE`). Reuse those constants — do not invent new patterns in this slice. Put it on the `Extracted` dataclass.
  - Keep `failed_step_excerpt_lines` behaviour, but ensure it and the new logic agree on the same group.
- **`tools/rca/models.py`** — add an optional `primary_failure_line: str | None = None` to `FailedJob` (and/or `FailedStepExcerpt`). Optional + defaulted so existing `summary.json` fixtures still validate.
- **`tools/rca/cli.py` (collect path)** — verify `failed_step_name` is reliably populated from the GitHub job's failed step (the step with `conclusion == "failure"`) and passed into `extract_from_lines`. If it is already, leave it; if it's missing or best-effort, make it robust. Wire the new `primary_failure_line` from `Extracted` onto the `FailedJob`.
- **`tools/rca/prompt.py`** — in `build_evidence` / `_priority_sections`, prepend a small mandatory `primary_failure_line` block **before** `failed_step_excerpt` when present. Keep it tiny and untrimmed. Do not otherwise change the evidence order in this slice.

## Constraints (from AGENTS.md — do not violate)

- `extract.py`, `cleaner.py`, `budget.py`, `redact.py`, `drain_index.py`, `history.py` must **not** import GitHub modules or mention `job_id` / `run_id`. Passing `failed_step_name` as a plain string is fine and keeps `extract.py` source-agnostic — keep it that way.
- The collector must never fail the workflow: catch exceptions, keep `primary_failure_line` optional, exit 0 unless `--strict`.
- No hardcoded failure strings. Everything keys off the existing regex constants and the GitHub-provided step name.

## Acceptance criteria

Add a fixture + tests under `tests/rca/` that reproduce the failure mode above **generically** (a made-up "Check Version" style step; no real repo data):

1. A cleaned job log containing GitHub groups where the **failed step group** holds a semantic-cause line (e.g. a line matching `_SEMANTIC_CAUSE`, followed by `Process completed with exit code 1`), **and** an earlier, unrelated group holds a different error-ish line (e.g. a `curl … manifest.json` check) that a naive whole-log `_first_error_index` would center on.
2. Assert:
   - `failed_step_excerpt` contains the semantic-cause line.
   - the `first_error` (or `merged`) window is centered inside the failed-step group and contains the semantic-cause line, **not** the earlier unrelated line.
   - `primary_failure_line` equals the semantic-cause line.
   - Building a `Summary` from this and calling `diagnose`/`specific_log_cause` yields a root cause derived from the semantic-cause line — i.e. the deterministic answer is no longer the generic R14 "workflow changed" text.
3. A regression test for the no-group / no-`failed_step_name` case proving today's whole-log behaviour is unchanged.
4. Run the existing suite: `pytest tests/rca -q` must stay green, and the acceptance-35 grep must stay empty:
   `rg -n "job_id|run_id|github_api" tools/rca/cleaner.py tools/rca/drain_index.py tools/rca/budget.py tools/rca/redact.py tools/rca/history.py`

## Out of scope (do NOT do these here)

- Citation-grounding gate (final answer must cite the failed step) — next slice.
- `TERMINAL_CAUSE_PATTERNS` table and symptom demotion (withholding pipeline_logs / templates) — later slice.
- Any change to `decide_stgpt_call` / when the model is called — later slice.
- Adding stack_traces / junit / code_context to the evidence pack — later slice.

## Note for a later slice (record, don't act on now)

Inside the pipeline/docker log zips, individual log files carry docker sub-step names like `docker up test`, `docker down test`, `docker flyway` (naming is approximate, not exact). That will help attribute pipeline streams to the failed stage and demote follow-on symptoms in the terminal-cause slice. Leave a `TODO` comment noting this where pipeline streams are matched (`pipeline_logs.py` / `diagnose._sorted_pipeline_streams`), but do not build stage/sub-step parsing in this slice.

## Deliverable

A focused diff limited to the files above, plus the new fixture and tests, with `pytest tests/rca -q` green. Summarize what changed and show the new test output.
