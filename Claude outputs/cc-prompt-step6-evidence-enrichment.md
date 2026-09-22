# Claude Code task — Step 6: category-aware evidence enrichment (stack traces / junit / code_context)

## Context

`ci-rca-collector`. Read `AGENTS.md` and `docs/ci-rca-collector-spec-v8.md` first. Steps 1–5 are merged:

- Step 1: failed-step anchoring; `FailedJob.primary_failure_line`.
- Step 2: `is_grounded` / `failed_step_anchor_text` / `focused_evidence`; `build_evidence(..., exclude=...)`; `_priority_sections` returns `(key, mandatory, lines)`.
- Step 3: `terminal_cause_present`; when true, `build_evidence` already excludes `log_templates` + `pipeline_logs`.
- Step 4: `decide_stgpt_call` (auto policy) — the model now runs **only on genuinely ambiguous failures**.
- Step 5: one consistent "AI Diagnosis" section.

This is Step 6. Do **only** what this document scopes.

## The problem this slice fixes

`prompt.py :: build_evidence` packs `primary_failure_line`, `failed_step_excerpt`, `first_error_window`, `DETERMINISTIC_HINT`, `log_templates`, `pipeline_logs`, `change_context`. But the SYSTEM_PROMPT tells the model it may cite `stack_traces`, `junit`, and `code_context` — and these are **collected** (`FailedJob.stack_traces`, `summary.junit`, `summary.code_context.hunks`) yet **never packed**. Because citations must be verbatim substrings of the evidence, any citation to those sources is auto-rejected, and the model is starved of exactly the high-signal evidence (the exception frame, the failing test + message, the source hunk) that resolves the ambiguous failures Step 4 now routes to it.

Since Step 4 means the model only runs on the hard cases, those calls must carry the richest *relevant* evidence — but still bounded, and still category-aware so infra/dependency cases don't get bloated with irrelevant stack traces.

**No hardcoded failure strings.** Selection is by category and by the existing collected structures only.

## Goal of this slice

1. Add three evidence sections to `build_evidence`, built from data already on `Summary`:
   - `stack_traces` — from the primary failed job's `stack_traces` (headline + truncated content already limited to top/bottom frames).
   - `junit` — from `summary.junit.failures` (classname::name + message/body, capped).
   - `code_context` — from `summary.code_context.hunks` (path + line range + content).
   Each respects its per-section cap in `config.SECTION_TOKEN_CAPS` (`stack_traces`, `junit`, `code_context` already exist there); trim per section (reuse `budget.trim_middle`) so one large block can't eat the whole pack.
2. Make evidence **category-aware** via an evidence profile keyed on `summary.classification.category`:
   - **code categories** (`compile`, `crash`, `test_failure`): after the mandatory `primary_failure_line` + `failed_step_excerpt` + `first_error_window`, prioritize `stack_traces` → `junit` → `code_context`, then `deterministic_hint`, then the rest.
   - **dependency**: include `first_error_window`, `deterministic_hint`, `change_context` (lockfile/manifest), `log_templates`; do **not** add `stack_traces` (usually noise for install failures).
   - **infra categories** (`oom`, `timeout`, `disk_space`, `image_pull`, `auth`, `network_dns`, `infra_runner`): minimal — `primary_failure_line` + `failed_step_excerpt` + `first_error_window` + `deterministic_hint` only. (Step 4 rarely sends these to the model anyway.)
   - **unknown / residual (R18) / default**: the ambiguous bucket — include everything relevant (`stack_traces`, `junit`, `code_context`, `log_templates`, `pipeline_logs`, `change_context`), since this is where the model needs the most.
   Keep `primary_failure_line` and `failed_step_excerpt` mandatory in every profile. Keep the Step 2 `exclude` mechanism and Step 3 terminal-cause exclusion working (terminal cause still drops `log_templates`/`pipeline_logs`).
3. **Grounding correctness:** extend `failed_step_anchor_text(summary)` to also include the **primary failed job's `stack_traces` content**, because for a crash/compile the failing frame is the failed step's own output. This prevents a correct stack-frame-based answer from being marked ungrounded → `needs-review`. Do not otherwise change Step 2's grounding logic.

## Where to change (verify against current code first)

- **`tools/rca/prompt.py`**
  - Add `_stack_trace_lines(summary)`, `_junit_lines(summary)`, `_code_context_lines(summary)` returning `["### stack_traces", ...]` etc., each trimmed to its `SECTION_TOKEN_CAPS` cap.
  - Replace the fixed `_priority_sections` with a category-aware ordering: a small profile map (define in `prompt.py` or `config.py`) `category → ordered list of section keys`, with a sensible default (the "unknown/residual" full set). Keep returning `(key, mandatory, lines)` so `exclude=` still works.
  - Extend `failed_step_anchor_text` to append the primary job's stack-trace content.
- **`tools/rca/config.py`** — optional: hold `EVIDENCE_PROFILES` (category → section-key order) and any `JUNIT_EVIDENCE_CAP` constant here; export via `__all__`. Reuse the existing `SECTION_TOKEN_CAPS`.
- No changes to `models.py`, `analyze.py` gating, `diagnose.py`, or collection modules.

## Constraints (AGENTS.md)

- `prompt.py` is Phase 2 and may read `Summary` fields; do not add GitHub imports or `job_id`/`run_id` to the source-agnostic modules.
- Respect the total `cap_tokens` budget (`ANALYZE_EVIDENCE_CAP` / `TOKEN_BUDGET_ANALYZE`) — the new sections are non-mandatory and must stop packing at the cap, exactly like the existing optional sections. Mandatory sections (`primary_failure_line`, `failed_step_excerpt`) are never dropped.
- Analyze must never fail: a missing/None `junit` / `code_context` / empty `stack_traces` yields an empty section, never an error.
- Backwards-compatible: the default profile must keep producing valid evidence for summaries with none of the new data; the focused re-ask path (`focused_evidence`) and terminal-cause exclusion must still behave as in Steps 2–3.
- No hardcoded failure strings — selection is by category and collected structures only.

## Acceptance criteria

Add/extend tests under `tests/rca/` (generic fixtures):

1. **Code category packs the rich sections.** A `compile`/`crash`/`test_failure` summary with `stack_traces` + `junit` + `code_context` produces an `<EVIDENCE>` containing `### stack_traces`, `### junit`, `### code_context`, ordered before `log_templates`, after the mandatory excerpt.
2. **Dependency stays lean.** A `dependency` summary does **not** include `### stack_traces`; it still includes `change_context`/`log_templates`.
3. **Infra minimal.** An `oom`/`timeout` summary includes only the mandatory + `first_error_window` + `deterministic_hint`; no stack/junit/code/templates/pipeline.
4. **Unknown/R18 includes everything relevant** (stack_traces, junit, code_context, templates, pipeline unless terminal-cause-excluded).
5. **Budget respected.** With a very small `cap_tokens`, mandatory sections still present, optional sections dropped in order; no section exceeds its `SECTION_TOKEN_CAPS` entry.
6. **Grounding on stack frame.** A crash whose model answer cites a stack-trace line is `is_grounded == True` (because `failed_step_anchor_text` now includes stack traces) and renders `Status: ok`, not `needs-review`.
7. **Terminal-cause + focused paths unchanged.** With `terminal_cause_present`, `log_templates`/`pipeline_logs` are still excluded; `focused_evidence` still drops them.
8. Suite green: `pytest tests/rca -q`, and the acceptance-35 grep stays empty:
   `rg -n "job_id|run_id|github_api" tools/rca/cleaner.py tools/rca/drain_index.py tools/rca/budget.py tools/rca/redact.py tools/rca/history.py`

## Out of scope (do NOT do here)

- Collection-side changes (error-centered trimming, gating the pipeline-artifact *download*, JUnit collapse-by-message at collection time) — that is the collection-hardening slice.
- Telemetry persistence / new measurement outputs — later slice.
- Single-persona default / cost ceilings — later, optional.

## Deliverable

A focused diff, mostly in `prompt.py` (and optionally `config.py`), plus tests, with `pytest tests/rca -q` green. Summarize what changed and show an example `<EVIDENCE>` block for a `crash` and for a `dependency` summary so the category-aware difference is visible.
```
Short command to run it in Claude Code (from the repo root):

Read docs/cc-prompt-step6-evidence-enrichment.md and implement exactly that slice — nothing outside its scope. Follow AGENTS.md, add the tests it specifies, and make sure `pytest tests/rca -q` is green before you finish.
```
