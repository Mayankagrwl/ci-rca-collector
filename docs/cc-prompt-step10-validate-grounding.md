# Claude Code task — Step 10: `validate.py` production grounding (RECONCILE, do not rebuild)

## Context

`ci-rca-collector`. Read `AGENTS.md`, `docs/ci-rca-collector-spec-v8.md`, and `docs/rca-eval-spec-v1.md` (§1, §4, §5, §6, §10) first. Steps 1–9b are merged. This implements the **production** half of the eval spec (grounding + validation/cost telemetry as first-class, carried into `summary.json`).

**CRITICAL — this is a consolidation of existing behaviour, not new parallel logic.** Much of eval-spec §4–§6 already exists under different names. Reuse it. Do **not** create a second grounding implementation that can drift from the real one (that divergence is exactly the class of bug we fixed in Step 9). The eval spec was written against an idealized model shape (`RCA.evidence: list[str]`); the real model is `AnalysisResult.citations: list[AnalysisCitation{quote, source, line}]` — map to the real fields.

## What already exists (reuse — do NOT duplicate)

- `analyze.is_grounded(result, anchor_text)` — citation ∈ failed-step anchor.
- `prompt.failed_step_anchor_text(summary)` — the failed-step anchor (Step 2/6/9).
- `analyze._interpret_completion` — already rejects citations that aren't substrings of the sent evidence (anti-fabrication), and marks `cannot_determine`.
- `AnalysisRecord.grounded`, `AnalysisResult.citations`, `.suspected_files`, `.suspected_stage`.
- Step 2 **focused re-ask** on ungrounded, and the Step 5 confidence cap / `needs-review` in `finalize_user_card` (`_finalize_card` + `_stamp_grounding`).
- `telemetry.py :: append_telemetry` (Step 8) + outputs `diagnosis-grounded`, `analyze-decision`, short-circuit.
- `redact.py`, `changes.commits`, `changes.files`, extracted stack traces, failed job names.

## Goal — add `tools/rca/validate.py` that consolidates + extends

1. **`GroundingResult` model** (in `models.py`), matching eval-spec §4.1 but mapped to real fields:
   `grounded: bool`, `citation_count: int`, `ungrounded_citations: list[str]`, `grounding_rate: float`, `component_grounded: bool | None`, `shas_grounded: bool | None`, `checks_run: list[str]`.
2. **`validate.check_grounding(result: AnalysisResult, prompt_evidence: str, summary: Summary) -> GroundingResult`** — a pure scoring function (no model calls):
   - **Fabrication check (the rate):** each `citation.quote` must appear in `prompt_evidence` after **normalisation (eval-spec §4.2)**: collapse whitespace runs to one space, strip leading/trailing whitespace + `.,;:"'` and backticks, lowercase, strip ANSI. Apply to **both** sides before comparing. `grounding_rate` = found / citation_count; `grounded` = all found; `citation_count == 0` ⇒ treat as rate 0. **No fuzzy matching beyond §4.2** (no Levenshtein/token-overlap). This normalisation is the real improvement — our current raw substring check produces false-ungrounded on whitespace/case/punctuation differences.
   - **`component_grounded` (§4.3):** does `suspected_files` / a named component appear in `changes.files`, an extracted stack trace, or a failed job name? `None` when none of that data exists — never `False` on absence.
   - **`shas_grounded` (§4.3):** extract `\b[0-9a-f]{7,40}\b` from `root_cause` + `suggested_fix`; each must be in `changes.commits`. `None` when there is no changes context.
   - Record which checks ran in `checks_run`.
3. **Keep the two grounding notions distinct and complementary** — do not merge or replace:
   - existing **failed-step anchor** grounding (`is_grounded`) → drives the Step 5 confidence cap / `needs-review` (leave as-is);
   - new **prompt-evidence** grounding (`GroundingResult.grounding_rate`) → drives the display policy below (anti-fabrication).
4. **Display policy (eval-spec §4.4) — RECONCILED with Step 5 (fall back, never blank):**
   - `rate == 1.0` → publish normally.
   - `0.5 <= rate < 1.0` → **strip the ungrounded citations** from the displayed result, keep it, add a visible warning note.
   - `rate < 0.5` (incl. `citation_count == 0`) → **fall back to the deterministic card** (Step 5 behaviour: render the section as `needs-review` with a warning), keeping the model text in notes. **Do NOT suppress the section** — Step 5's "always render one AI Diagnosis section" invariant holds. (Leave a `# TODO: eval-spec §4.4 literal 'suppress' is available as a future config option` comment; default is fallback.)
   - `collection_notes` must always state what happened and why.
5. **Carry results into `summary.json`:** attach `GroundingResult` to the analysis record and merge into the `analysis` block of `summary.json` (so the offline suite can aggregate). Also surface a structured **`ValidationTelemetry`** (schema_valid_first_try, repair_attempted, repair_succeeded, parse_error≤200 chars, fallback_used) and **`CallTelemetry`** (prompt_tokens_est=len//4, completion_tokens_est, latency_ms, total_pipeline_ms, short_circuited, cache_hit) — **populate these from data already captured** (repair/parse from `_run_personas` notes, cache_hit from `AnalysisRecord.cache_hit`, short_circuited from the Step 4 decision, latency from the values already in `stgpt_responses`/notes). Prefer **extending the existing Step 8 telemetry record / `AnalysisRecord`** over inventing a parallel path.
6. **Wire into `analyze.py`** *after* the existing flow produces its final result (i.e. after Step 2's focused re-ask and Step 5 finalisation). `validate.check_grounding` is post-hoc scoring; it triggers **no new model call**.

## Reconciliation notes to honour (do not "fix" these to match the spec literally)

- **Keep the Step 2 focused re-ask.** Eval-spec §4.5 / acceptance #5 says "never retry on grounding failure / exactly one model call + one repair." Our focused re-ask exists to remove *symptom* evidence (templates/pipeline), not to re-roll a *fabrication*, and it demonstrably fixed real cases. Preserve it. `validate.py` is non-retrying scoring only. Add a short note in the PR/summary that this is a deliberate divergence from eval-spec #5, and that `CallTelemetry` will make the true call count measurable.
- **Do not import anything from `tools/eval/`** (§10) — `tools/eval/` doesn't exist yet and production must not depend on test code.

## Constraints (AGENTS.md + eval-spec §10)

- `validate.py` must **never crash the pipeline**: wrap all checks; on internal error, log, set grounding to `None`, add a `collection_notes` caveat, and publish the RCA. A broken validator must not suppress working diagnoses.
- Source-agnostic modules (`extract`, `budget`, `redact`, `drain_index`, `history`, `pipeline_logs`, `classify`, `cleaner`) keep no GitHub imports / `job_id`/`run_id`. `validate.py` may read `Summary`/`AnalysisResult` (Phase 2 layer).
- No hardcoded failure strings — normalisation and checks are generic.
- **No regressions:** the Steps 1–9b suite stays green.

## Acceptance criteria

Add tests under `tests/rca/` (map eval-spec §11 to the real model):

1. A citation not present in `prompt_evidence` → `ungrounded`, lowers `grounding_rate`.
2. A citation differing only in whitespace / trailing punctuation / case → **grounded** (normalisation works; this is the false-positive our old check had).
3. `rate < 0.5` → section renders as `needs-review` via the deterministic card (Step 5), **not** suppressed; warning in `collection_notes`.
4. `0.5 <= rate < 1.0` → ungrounded citations stripped, result kept, warning shown.
5. `check_grounding` runs **no** model call; the Step 2 focused re-ask still behaves as before (exactly one focused re-ask on failed-step-ungrounded, unchanged).
6. `component_grounded == False` when `suspected_files` names a path absent from `changes.files`, stack traces, and job names; `None` when there's no changes/stack/job data.
7. `shas_grounded` is `None` with no changes context; `False` when a cited SHA isn't in `changes.commits`.
8. An internal error inside `check_grounding` leaves the pipeline running, RCA published, grounding `None`, caveat noted.
9. `summary.json` `analysis` block carries `GroundingResult` + `ValidationTelemetry` + `CallTelemetry`, redacted.
10. Suite green: `pytest tests/rca -q`; acceptance-35 grep empty:
    `rg -n "job_id|run_id|github_api" tools/rca/cleaner.py tools/rca/drain_index.py tools/rca/budget.py tools/rca/redact.py tools/rca/history.py`

## Out of scope (this is the offline half — Step 11)

- `tools/eval/**` (labels, run_eval, metrics, report), goldens, `eval.yml`. Not now.
- LLM-as-judge, root-cause/fix-quality scoring (eval-spec §12).

## Deliverable

`tools/rca/validate.py` + `GroundingResult`/telemetry models + `analyze.py` wiring + tests, `pytest tests/rca -q` green. Summarize what was **reused vs newly added**, show a before/after where §4.2 normalisation flips a false-ungrounded citation to grounded, and confirm the Step 2 re-ask and Step 5 always-render invariant are unchanged.
```
Short command to run it in Claude Code (from the repo root):

Read docs/cc-prompt-step10-validate-grounding.md and implement exactly that slice — reconcile with existing code, do not build parallel grounding logic. Follow AGENTS.md, add the tests it specifies, and make sure `pytest tests/rca -q` is green before you finish.
```
