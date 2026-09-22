# Claude Code task — Step 3: terminal-cause table + symptom / teardown demotion

## Context

`ci-rca-collector`. Read `AGENTS.md` and `docs/ci-rca-collector-spec-v8.md` first. Steps 1 and 2 are merged:

- Step 1: collection anchors on the GitHub failed step; `FailedJob.primary_failure_line` is set; `prompt.py` emits it first.
- Step 2: `is_grounded` / `failed_step_anchor_text` / `focused_evidence` exist; an ungrounded AI answer triggers one focused re-ask with `log_templates` + `pipeline_logs` excluded; `AnalysisRecord.grounded` and a confidence cap live in `finalize_user_card`.

This is Step 3. Do **only** what this document scopes.

## The problem this slice fixes

When the failed step already carries a line that *by itself* explains the exit (a "terminal cause"), later-in-time noise still leaks into both paths and misleads them:

- the AI (on the first, non-focused call) picks a follow-on symptom — e.g. a `Not enough permissions to delete/overwrite …` line that appears during a later upload/teardown — as the root cause;
- the deterministic path lets a low-evidence rule (R14 "workflow changed" / blast-radius) or a symptom line win instead of the terminal cause.

Step 2 only removes templates/pipeline_logs on the *re-ask*, i.e. after a miss. Step 3 removes the noise **up front** whenever a terminal cause is present at the failed step, so the miss rarely happens — and makes the terminal cause win deterministically.

**This is an illustration. Do not hardcode Artifactory / "already exists" / "permissions" / package.json.** Everything is driven by extensible regex tables in `config.py` and by structural phase parsing. New terminal-cause or symptom shapes are added by editing those tables, not code.

## Goal of this slice

1. Add two config-driven tables:
   - `TERMINAL_CAUSE_PATTERNS` — a line that alone explains the exit (version/tag already exists, quality/coverage gate failed, compile `error TS…`/`cannot find symbol`, dependency `No matching distribution`/`ERESOLVE`/`ModuleNotFoundError`, `AssertionError`/`FAILED <name>`, `ENOSPC`, …). Seed it as a superset of the existing `_SEMANTIC_CAUSE` (extract.py) and `_SPECIFIC_CAUSE_RE` (diagnose.py) markers; keep those working, do not duplicate divergently — prefer referencing one shared source.
   - `SYMPTOM_PATTERNS` — follow-on lines that must never be the root cause when a terminal cause precedes them (permission-to-delete/overwrite, 401/403 on upload, connection reset during teardown, `Post job cleanup`, container `Stopping`/`exited with code 0`, orphan-process cleanup).
2. Define `terminal_cause_present(summary)` — True when the failed step's anchor (`primary_failure_line` + `failed_step_excerpt` + first-error window of the primary job) matches a `TERMINAL_CAUSE_PATTERNS` entry. Reuse `failed_step_anchor_text` from Step 2.
3. **When a terminal cause is present at the failed step:**
   - **Deterministic path (`diagnose.py`):** the terminal cause takes precedence — do not let R14 / config-only / blast-radius rewrite the one-liner or win the verdict, and drop `SYMPTOM_PATTERNS` lines and teardown-phase pipeline streams from cause selection (`specific_log_cause`, the union hit, and deterministic citations). Never drop the line/stream that *contains* the terminal cause itself.
   - **AI evidence pack (`prompt.py :: build_evidence`):** exclude `log_templates` and `pipeline_logs` up front (reuse the Step 2 `exclude=` mechanism), so the first model call already can't wander to a symptom.
4. **Pipeline stream phase/ordering (uses the zip step-names):** inside pipeline/docker log zips, individual log files are named after docker sub-steps, e.g. `docker up test`, `docker flyway`, `docker down test` (naming is approximate, not exact). Parse the inner file name (and artifact name) into a coarse **phase**:
   - `up`, `start`, `flyway`, `migrate`, `seed`, `init` → `setup`
   - `down`, `stop`, `rm`, `cleanup`, `teardown`, `prune` → `teardown`
   - otherwise → `run`
   Store it on the stream, and **demote `teardown` streams below the failed stage everywhere** streams are ordered/selected (`diagnose._sorted_pipeline_streams`, `prompt._pipeline_window_lines`, `pipeline_logs.rank_pipeline_artifacts`/`select_pipeline_names`). Teardown streams are the classic source of the follow-on permission/exit-0 noise.
5. **Non-destructive:** do not delete anything from `summary.json`. Humans still see all pipeline logs / templates in `summary.md`. Step 3 only changes what feeds *cause selection* and the *model evidence pack*.

## Where to change (verify against current code first)

- **`tools/rca/config.py`** — add `TERMINAL_CAUSE_PATTERNS`, `SYMPTOM_PATTERNS`, and a `PIPELINE_PHASE_TOKENS` map (token → setup/run/teardown). Export via `__all__`.
- **`tools/rca/models.py`** — add `phase: Literal["setup","run","teardown"] | None = None` (and optionally `step_name: str | None = None`) to `PipelineLogStream`. Optional + defaulted so existing fixtures validate.
- **`tools/rca/pipeline_logs.py`** — in `_stream_from_text` / `parse_pipeline_zip`, derive `phase` from the inner file name (fallback: artifact name). Apply teardown demotion in `rank_pipeline_artifacts` / `select_pipeline_names`. No GitHub imports (this module is source-agnostic — keep it so).
- **`tools/rca/diagnose.py`** — add `terminal_cause_present(summary)`; make the terminal cause outrank R14/blast-radius (guard `_rule_config_only` / `refine_blast_radius` / `_rule_r18` so they don't override a present terminal cause); filter `SYMPTOM_PATTERNS` lines and teardown streams out of `specific_log_cause`, `_union_hit`, `_all_text` used for cause selection, and `display_citation_quotes`.
- **`tools/rca/prompt.py`** — in `build_evidence`, when `terminal_cause_present(summary)`, add `{"log_templates","pipeline_logs"}` to the exclude set for the default pack. Keep `focused_evidence` unchanged. Demote teardown streams in `_pipeline_window_lines`.

## Constraints (AGENTS.md)

- `cleaner.py`, `drain_index.py`, `budget.py`, `redact.py`, `history.py`, and `pipeline_logs.py` must not import GitHub modules or mention `job_id`/`run_id`. Phase parsing is pure string work — fine.
- Collector/analyze must never fail the workflow: a table miss or an odd file name must degrade gracefully (phase `None` / `run`), never raise.
- Precedence guard must be **narrow**: only suppress R14/blast-radius/symptoms when a terminal cause is genuinely present at the failed step. When there is **no** terminal cause (e.g. the job log is exit-only and the real cause lives in a pipeline stream — `_job_logs_are_exit_only` + `_pipeline_has_first_error`, rule R17), change nothing: pipeline stays in the evidence and can be the cause.
- No hardcoded failure strings — only the regex tables and phase tokens.

## Acceptance criteria

Add fixtures + tests under `tests/rca/` (generic, no real repo data):

1. **Terminal cause + teardown symptom → terminal cause wins.** Failed step has a `TERMINAL_CAUSE_PATTERNS` line; a `docker down test` teardown stream carries a `SYMPTOM_PATTERNS` permission/403 line. Assert: deterministic root cause = the terminal cause (not R14, not the symptom); the symptom is not in citations; `build_evidence` output contains no `log_templates` / `pipeline_logs` sections; `focused`/anchor still present.
2. **Phase parsing + demotion.** Streams from files `docker up test`, `docker flyway`, `docker down test` get phases `setup`, `setup`(or run), `teardown`; ordering/selection puts `teardown` last (below the failed stage).
3. **No terminal cause → unchanged.** With no `TERMINAL_CAUSE_PATTERNS` match, `build_evidence` still includes `log_templates`/`pipeline_logs`, R14 can still fire, and the R17 "diagnose from pipeline when job is exit-only" path still works (pipeline not suppressed).
4. **Graceful degradation.** An unrecognised inner file name yields phase `None`/`run` and never raises; an empty/odd summary does not crash cause selection.
5. Suite green: `pytest tests/rca -q`, and the acceptance-35 grep stays empty:
   `rg -n "job_id|run_id|github_api" tools/rca/cleaner.py tools/rca/drain_index.py tools/rca/budget.py tools/rca/redact.py tools/rca/history.py`

## Out of scope (do NOT do here)

- Changing `decide_stgpt_call` / when the model is called — Step 4.
- The "always render one AI Diagnosis section / rename skipped→deterministic / add hybrid" output invariant — Step 5.
- Adding stack_traces / junit / code_context to the evidence pack — later slice.

## Deliverable

A focused diff over `config.py`, `models.py`, `pipeline_logs.py`, `diagnose.py`, `prompt.py`, plus new fixtures/tests, with `pytest tests/rca -q` green. Summarize what changed and show the new test output.
```
Short command to run it in Claude Code (from the repo root):

Read docs/cc-prompt-step3-terminal-cause-symptom-demotion.md and implement exactly that slice — nothing outside its scope. Follow AGENTS.md, add the fixtures and tests it specifies, and make sure `pytest tests/rca -q` is green before you finish.
```
