# Claude Code task — Step 2: citation-grounding gate (answer must cite the failed step)

## Context

`ci-rca-collector`. Read `AGENTS.md` and `docs/ci-rca-collector-spec-v8.md` first. Step 1 is already merged: the collector now centers the first-error window on the GitHub-designated failed step and pins `FailedJob.primary_failure_line`; `prompt.py :: build_evidence` emits `primary_failure_line` as the first mandatory evidence block, before `failed_step_excerpt`.

This is Step 2 of the precision fix. Do **only** what this document scopes.

## The problem this slice fixes

Across real runs of the same failure, every **correct** diagnosis cited the failed step's own line; every **wrong** one — deterministic *and* AI — cited something else:

- AI, full log → root cause = a later "Not enough permissions to delete/overwrite" line, cited from `log_templates`. Wrong: that's a follow-on symptom, not the gate that stopped the job.
- Deterministic → generic "workflow changed", cited from `pipeline_logs` / a curl line. Wrong.
- Correct run → cited `This release already exists … update package.json` from the failed step.

The tool already checks that citation quotes are verbatim substrings of the evidence. It does **not** check the converse: that the answer is anchored to the *failed step*. Add that check. It is the single highest-value guard — it catches both wrong cases and is recoverable.

**This is an illustration. Do not hardcode any failure string, package name, or "permission"/"already exists" text.** Grounding is judged structurally (does a citation fall inside the failed-step evidence?), not by matching specific words.

## Goal of this slice

1. Define **grounding**: a result is grounded when at least one of its citation quotes is a non-empty verbatim substring of the failed-step *anchor text* = `primary_failure_line` + `failed_step_excerpt` lines + the `first_error`/`merged` window content of the primary failed job. Judge by the quote text itself, not the model's self-reported `source` (the model can mislabel).
2. In the **AI path**, when the model returns an otherwise-valid (`ok`) result that is **not** grounded, and the evidence contained `log_templates` or `pipeline_logs`, **re-ask exactly once** with a *focused* evidence pack that omits those two sections. Keep the grounded result if the retry produces one.
3. If, after the focused re-ask, the answer is still ungrounded but the **deterministic** card *is* grounded, prefer the deterministic card (mark `source = "deterministic"` or `"hybrid"`, keep the model text in `notes`).
4. **Confidence reflects grounding, not the model's self-report:** cap final `confidence` at `medium` whenever the chosen result is not grounded. Reserve `high` for grounded + cited answers.
5. Record a `grounded: bool` on the analysis record for later measurement.

## Where to change (verify signatures against current code before editing)

- **`tools/rca/prompt.py`**
  - Add a way to build a **focused** evidence string that omits the `log_templates` and `pipeline_logs` sections while keeping the mandatory `primary_failure_line` and `failed_step_excerpt`. Cleanest: give each entry in `_priority_sections` a section key and add an `exclude: set[str] | None` parameter to `build_evidence`, plus a small `focused_evidence(summary, *, cap_tokens=None)` helper that excludes `{"log_templates", "pipeline_logs"}`. Do not change the default (non-focused) ordering or output.
  - Add a helper that returns the failed-step anchor text described in Goal (1), e.g. `failed_step_anchor_text(summary) -> str`, reusing `_primary_failure_line_lines`, `_excerpt_lines`, `_job_first_error_lines`. This is what the grounding check greps against.

- **`tools/rca/analyze.py`**
  - Add `is_grounded(result: AnalysisResult, anchor_text: str) -> bool`: True iff any `citation.quote` (non-empty) is a substring of `anchor_text`.
  - Wire the gate in `analyze_summary` (it already has `summary`, `evidence`, and the `caller`). After `_run_personas` returns a result with `status == "ok"`:
    - compute `anchor = failed_step_anchor_text(summary)`; if `is_grounded` → done.
    - else, if the original evidence included templates/pipeline (i.e. `focused_evidence` differs from `evidence`), do a **single** focused re-ask — one persona, one call (do **not** re-run the whole two-persona ladder). Add a small helper such as `_ask_once(caller, persona, focused_evidence) -> AnalysisResult | None` that reuses `build_messages` + `_interpret_completion`. If that result is grounded, use it.
    - if still ungrounded: if the deterministic card (`analysis_result_from_summary(summary)`) is grounded, prefer it (`source="deterministic"`/`"hybrid"`, original model text into `notes`); otherwise keep the model result.
  - Set `record.grounded` and cap `confidence` to at most `medium` when the final chosen result is not grounded. Do this in one place so it applies whether the source is `ai`, `hybrid`, or `deterministic`. `finalize_user_card` is the natural home for the confidence cap.
  - Keep the existing behaviour for `skipped` / `gated` / `failed` records unchanged except for setting `grounded` where a result exists.

- **`tools/rca/models.py`**
  - Add `grounded: bool | None = None` to `AnalysisRecord` (optional + defaulted so existing fixtures validate). Optionally surface it in `_diagnosis_markdown` notes.

- **`tools/rca/outputs.py`** (if trivial) — emit a `diagnosis-grounded` GitHub output alongside the existing analysis outputs. If it complicates the slice, leave a `TODO` and defer to the measurement slice.

## Constraints (AGENTS.md)

- Do not touch the source-agnostic modules’ contracts: `cleaner.py`, `drain_index.py`, `budget.py`, `redact.py`, `history.py` must not gain GitHub imports or `job_id`/`run_id`. `analyze.py` and `prompt.py` are Phase 2 and may read `Summary` fields.
- Analyze must never raise into the workflow; keep the existing try/except discipline. A grounding check or re-ask failure must fall back to the pre-Step-2 result, not crash.
- Bound cost: at most **one** extra model call for the focused re-ask. No new call when the first answer is already grounded, when there were no templates/pipeline sections to remove, or in `from_completion` replay once the queue is empty.
- No hardcoded failure text. Grounding is substring-in-anchor, nothing keyword-specific.

## Acceptance criteria

Add tests under `tests/rca/` using the existing `from_completion` stub (`analyze_summary(..., from_completion=[...])`) so no network is needed. Build small `Summary` fixtures with a populated `primary_failure_line` + `failed_step_excerpt` and some `drain` templates / `pipeline_logs`.

1. **Ungrounded → focused re-ask → grounded.** First stubbed completion cites only a template/pipeline line (a follow-on symptom); second stubbed completion cites the `primary_failure_line`. Assert: two completions consumed, final `grounded is True`, final root cause derived from the failed-step line, `source` in `{ai, hybrid}`.
2. **Grounded first time → no re-ask.** Stub one completion citing the failed-step line. Assert: only one completion consumed (queue still has the second), `grounded is True`, `confidence` may be `high`.
3. **Still ungrounded after re-ask, deterministic grounded → deterministic wins.** Both stubbed completions cite only symptoms; the deterministic card cites the failed-step line. Assert final `source` is `deterministic`/`hybrid`, model text preserved in `notes`, `confidence <= medium`.
4. **Confidence cap.** Any final result that is not grounded has `confidence != "high"`.
5. **No-op safety.** With no templates/pipeline sections present, an ungrounded answer triggers no extra call (nothing to remove) and does not crash.
6. Existing suite stays green: `pytest tests/rca -q`, and the acceptance-35 grep stays empty:
   `rg -n "job_id|run_id|github_api" tools/rca/cleaner.py tools/rca/drain_index.py tools/rca/budget.py tools/rca/redact.py tools/rca/history.py`

## Out of scope (do NOT do here)

- The `TERMINAL_CAUSE_PATTERNS` table and withholding templates/pipeline_logs *at collection time* — next slice. (Here we only omit them in the focused **re-ask** evidence, not from `summary.json`.)
- Changing `decide_stgpt_call` / when the model is called at all — later slice.
- Adding stack_traces / junit / code_context to the evidence pack — later slice.
- The "always render one AI Diagnosis section / rename skipped→deterministic" output invariant — later slice.

## Deliverable

A focused diff over `prompt.py`, `analyze.py`, `models.py` (and `outputs.py` only if trivial), plus new fixtures/tests, with `pytest tests/rca -q` green. Summarize what changed and show the new test output.
```
Short command to run it in Claude Code (from the repo root):

Read docs/cc-prompt-step2-citation-grounding-gate.md and implement exactly that slice — nothing outside its scope. Follow AGENTS.md, add the fixtures and tests it specifies, and make sure `pytest tests/rca -q` is green before you finish.
```
