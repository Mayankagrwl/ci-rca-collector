# Claude Code task — Step 5: one consistent "AI Diagnosis" section, always

## Context

`ci-rca-collector`. Read `AGENTS.md` and `docs/ci-rca-collector-spec-v8.md` first. Steps 1–4 are merged:

- Step 1: failed-step anchoring; `FailedJob.primary_failure_line`.
- Step 2: `is_grounded` / `failed_step_anchor_text` / `focused_evidence`; `AnalysisRecord.grounded`; confidence cap in `finalize_user_card` (`_finalize_card` + `_stamp_grounding`).
- Step 3: `terminal_cause_present`; symptom/teardown demotion.
- Step 4: `decide_stgpt_call` now skips the model under `analyze-policy=auto` when the deterministic answer is anchored — so many runs render a deterministic card with **Model called: no**.

This is Step 5, the final core slice. Do **only** what this document scopes. It is a rendering/normalization slice — no changes to gating, evidence selection, or collection.

## The problem this slice fixes

The rendered section (`_diagnosis_markdown`) shows the raw internal `record.status`, so users see dead-end-looking states like **Status: skipped** / **failed** even though a full deterministic root cause + fix is present. Now that Step 4 skips the model far more often, this reads like a gap rather than a diagnosis. The requirement: **one "AI Diagnosis" section renders every time, with the same fields, never blank, whether the answer came from the model or from code.**

## Goal of this slice

1. **Normalize the user-facing status.** Map the internal `record.status` to a display status via a helper `display_status(record) -> str`:
   - `ok` → `ok`
   - `cached` → `cached`
   - `skipped`, `gated` → `deterministic`
   - `unvalidated`, `parse_error`, `citation_invalid`, `bridge_error`, `failed`, `unusable` → `needs-review`
   Keep the raw internal status available in `**Notes:**` (e.g. `internal_status=failed`) for debugging. Do **not** change the machine `analysis-status` GitHub output (consumers depend on it) — only the rendered markdown Status line and, optionally, a new `diagnosis-display-status` output.

2. **Never blank.** Guarantee that after `finalize_user_card`, the record's `result` has non-empty `root_cause` **and** `suggested_fix` for **every** status. `_finalize_card` already fills the deterministic card in most paths; add a final guard so that if either field is somehow empty (including `result is None`), it is filled from `analysis_result_from_summary(summary, source="deterministic")`, and if that too is empty, a last-resort generic line (reuse `fix_for_rule` / the verdict one-liner). Add an invariant test: *for any status, both fields are non-empty.*

3. **Source is always one of `ai | deterministic | hybrid | cached`.** When the model was not called → `deterministic`. When a cache hit → keep the stored source but the Status is `cached`. `hybrid` (model prose re-anchored on the failed step) already exists from Steps 2–3 — keep it. Never render an empty/unknown source.

4. **Confidence reflects grounding (already capped in `_stamp_grounding`).** Additionally, when display status is `needs-review`, render confidence as at most `low`/`medium` and include a short `**Review:** answer not grounded to the failed step` note, so a human knows to look. Do not silently show `high` on a needs-review card.

5. **Consistent header + idempotent re-render.** Standardize the section header to `## AI Diagnosis` (title case) and update the marker in `_upsert_diagnosis` to match, so re-running analyze **replaces** the section instead of appending a second one. Add a test that rendering twice yields exactly one section.

## Required section shape (every time)

```
## AI Diagnosis
**Status:** ok | cached | deterministic | needs-review
**Model called:** yes | no
**Reason code:** <when skipped/gated>            # keep existing behaviour
**Source:** ai | deterministic | hybrid | cached
**Confidence:** high | medium | low
**Root cause:** <always non-empty>
**Suggested fix:** <always non-empty>
**Citations:**
- `<quote>` (source[:line])                       # or "- (none)"
**Notes:** <existing notes; include internal_status=…, review hint when needs-review>
```

## Where to change (verify against current code first)

- **`tools/rca/analyze.py`**
  - Add `display_status(record: AnalysisRecord) -> str` with the mapping above.
  - `_diagnosis_markdown`: render `display_status(record)` on the Status line; keep `Model called`, `Reason code`, `Persona`, `Cache` behaviour; ensure `Source` and non-empty `Root cause`/`Suggested fix` always print; add the needs-review review hint; append `internal_status=<record.status>` into Notes.
  - `_upsert_diagnosis`: change the `marker` to the new header string so replace-not-append still works.
  - `finalize_user_card` / `_finalize_card`: add the never-blank guard (fill from deterministic card, then last-resort generic).
- **`tools/rca/models.py`** — optional: add `display_status: str | None = None` if you prefer to store it; not required if computed at render. Keep optional + defaulted.
- **`tools/rca/outputs.py`** — optional and only if trivial: add a `diagnosis-display-status` GitHub output. Leave the existing `analysis-status` output unchanged.

## Constraints (AGENTS.md)

- Rendering-only slice: do **not** change `decide_stgpt_call`, evidence selection, terminal-cause logic, or any collection module. No new GitHub imports or `job_id`/`run_id` in the source-agnostic modules.
- Never fail the workflow: rendering must not raise on a partial/empty record; fall back to a minimal valid section.
- Backwards-compatible: keep the machine `analysis-status` output and existing reason codes intact. Existing consumers of `steps.analyze.outputs.*` must not break.
- No hardcoded failure strings.

## Acceptance criteria

Add/extend tests under `tests/rca/`:

1. **Status mapping.** For records with internal status `ok`, `cached`, `skipped`, `gated`, `unvalidated`, `failed`, `bridge_error`, the rendered `**Status:**` line is `ok`, `cached`, `deterministic`, `deterministic`, `needs-review`, `needs-review`, `needs-review` respectively.
2. **Skipped reads as a real diagnosis.** A Step-4 `deterministic_sufficient` skip renders `Status: deterministic`, `Model called: no`, non-empty `Root cause` and `Suggested fix`, `Source: deterministic`.
3. **Never blank.** For every status above, `Root cause` and `Suggested fix` are non-empty; add a fixture with `result=None` and assert the guard fills both.
4. **Needs-review.** A `failed`/`bridge_error` record with a deterministic fallback renders `Status: needs-review`, confidence not `high`, a review hint in Notes, and preserves any model text already in notes.
5. **Idempotent header.** Rendering the section twice over the same markdown yields exactly one `## AI Diagnosis` section.
6. **Outputs unchanged.** The machine `analysis-status` output still emits the internal status values.
7. Suite green: `pytest tests/rca -q`, and the acceptance-35 grep stays empty:
   `rg -n "job_id|run_id|github_api" tools/rca/cleaner.py tools/rca/drain_index.py tools/rca/budget.py tools/rca/redact.py tools/rca/history.py`

## Out of scope (do NOT do here)

- Adding stack_traces / junit / code_context to the evidence pack — next slice (evidence enrichment).
- Any collection-side change (trimming, artifact-download gating, JUnit collapse) — later slice.
- Telemetry persistence — later slice.

## Deliverable

A focused diff, mostly in `analyze.py` (and optionally `models.py` / `outputs.py`), plus tests, with `pytest tests/rca -q` green. Summarize what changed and show a rendered example of the section for a `deterministic_sufficient` skip and for a `needs-review` case.
```
Short command to run it in Claude Code (from the repo root):

Read docs/cc-prompt-step5-consistent-diagnosis-section.md and implement exactly that slice — nothing outside its scope. Follow AGENTS.md, add the tests it specifies, and make sure `pytest tests/rca -q` is green before you finish.
```
