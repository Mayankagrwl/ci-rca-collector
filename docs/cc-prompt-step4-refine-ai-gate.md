# Claude Code task — Step 4: refine the AI gate (send less, safely)

## Context

`ci-rca-collector`. Read `AGENTS.md` and `docs/ci-rca-collector-spec-v8.md` first. Steps 1–3 are merged:

- Step 1: collection anchors on the GitHub failed step; `FailedJob.primary_failure_line` set.
- Step 2: `is_grounded(result, anchor)` + `failed_step_anchor_text(summary)` + `focused_evidence`; ungrounded AI answers trigger one focused re-ask; `AnalysisRecord.grounded` + confidence cap in `finalize_user_card`.
- Step 3: `terminal_cause_present(summary)` + `TERMINAL_CAUSE_PATTERNS` / `SYMPTOM_PATTERNS`; symptom/teardown demotion; terminal cause outranks R14/blast-radius.

This is Step 4. Do **only** what this document scopes.

## The problem this slice fixes

Today `analyze.py :: decide_stgpt_call` **ignores** `requires_analysis` and `summary` (`_ = (summary, requires_analysis)`) and returns `True` whenever mode=collect + analyze≠false + a key/from_completion is present. So the model is called on **every** failure — including ones the collector already solved (runner-infra, same-SHA flake, a clean high-confidence signature with a terminal cause). That is unnecessary cost and latency.

But naive gating is unsafe: a low-evidence deterministic verdict (R14 "workflow changed" / R18 residual) can be confidently wrong. So the gate must key on **evidence quality at the failed step**, not on a rule's self-reported `requires_analysis`.

## Goal of this slice

Make `decide_stgpt_call` skip the model **only when the deterministic answer is trustworthy and anchored**, keep calling it otherwise, and expose the choice as a policy input.

1. Add an `analyze-policy` control: `auto` (default) | `always` | `never`.
   - `always` = today's behaviour (call whenever possible) — keep it for A/B and emergencies.
   - `never` = never call; always render the deterministic card.
   - `auto` = the tiers below.
2. **`auto` — skip the model (deterministic is trustworthy) only when anchored:**
   - a hard short-circuit verdict: `infra_runner`, `infra_widespread`, `flake_same_sha_passed`, `no_failed_jobs`; **or**
   - a **signature** category (`SIGNATURE_CATEGORIES` from `diagnose.py`) that fired **high-confidence**, `terminal_cause_present(summary)` is True, and the deterministic card `is_grounded(...)` on the failed-step anchor; **or**
   - `history.match == "exact"` with a non-empty `history.previous_resolution`.
3. **`auto` — never let these suppress the model:** R14 / R18 or any verdict without a terminal cause at the failed step. `requires_analysis == False` **alone** is not sufficient. (This is the Run-B guard: a config-changed guess must not skip the model.)
4. `from_completion` replay always calls (so stubbed tests are deterministic), regardless of policy tiers.
5. Surface the decision: the skip `reason_code` already flows into the rendered "AI diagnosis" section via `skipped_record` — add the new reason codes, and (if trivial) emit an `analyze-decision` GitHub output.

## Where to change (verify against current code first)

- **`tools/rca/analyze.py`**
  - `decide_stgpt_call(...)`: add a `policy: str | None = "auto"` parameter. Keep the existing early returns (`mode_not_collect`, `analyze_disabled`, `missing_stgpt_key`). Then:
    - `policy == "always"` → `(True, None)`.
    - `policy == "never"` → `(False, "policy_never")`.
    - `policy == "auto"`:
      - if `from_completion is not None` → `(True, None)`.
      - if `summary.verdict.short_circuit` in the hard set → `(False, "short_circuit")`.
      - if `_deterministic_sufficient(summary)` → `(False, "deterministic_sufficient")`.
      - if history exact + previous_resolution → `(False, "history_resolution")`.
      - else `(True, None)`.
  - Add `_deterministic_sufficient(summary) -> bool` (lazy-import from `diagnose` as other helpers here already do):
    - `summary.classification.confidence == "high"` **and** `summary.classification.category in SIGNATURE_CATEGORIES` **and** `terminal_cause_present(summary)` **and** `is_grounded(analysis_result_from_summary(summary, source="deterministic"), failed_step_anchor_text(summary))` **and** the winning `diagnosis.rule_id` is **not** in `{"R14","R18"}`. Any miss → False (→ the model is called).
  - Extend `SKIP_REASONS` with `policy_never` and `history_resolution` human strings. Keep `skipped_record` behaviour (it already builds a grounded deterministic card + sets `model_called=False`).
- **`tools/rca/cli.py`** — `_cmd_analyze`: add an `--analyze-policy` arg (default `auto`), thread it into `decide_stgpt_call(..., policy=args.analyze_policy)`. Leave the existing `--analyze-enabled` master switch as-is (policy only refines when analyze is on).
- **`action.yml`** — add an `analyze-policy` input (default `'auto'`), forward it as `RCA_ANALYZE_POLICY` env and `--analyze-policy` in the analyze step, mirroring how `analyze` is wired today. Document the three values in the input description.
- **`tools/rca/outputs.py`** (only if trivial) — emit `analyze-decision` (`called | skipped:<reason> | cached`) alongside the existing analysis outputs; otherwise leave a `TODO` for the measurement slice.

## Constraints (AGENTS.md)

- Do not add GitHub imports or `job_id`/`run_id` to `cleaner.py`, `drain_index.py`, `budget.py`, `redact.py`, `history.py`, `pipeline_logs.py`.
- Analyze/collect must never fail the workflow: a gate exception must fall back to calling the model (fail open toward analysis), not crash. Wrap `_deterministic_sufficient` so any error returns False.
- Default must be safe: `auto`. Do not change the meaning of the existing `analyze` input.
- No hardcoded failure strings — the gate uses the Step 3 tables and existing category/rule metadata only.
- Backwards-compatible: existing `from_completion` tests must still call the model; update any existing test that assumed unconditional calling.

## Acceptance criteria

Add tests under `tests/rca/` (no network; use small `Summary` fixtures and the `from_completion` stub where a call is expected):

1. **Short-circuit → skip.** `verdict.short_circuit = "infra_runner"` → `decide_stgpt_call(policy="auto")` returns `(False, "short_circuit")`; `skipped_record` yields a deterministic, grounded card with `model_called=False`.
2. **Anchored signature → skip.** High-confidence signature category + `terminal_cause_present` + grounded deterministic card → `(False, "deterministic_sufficient")`.
3. **Run-B guard → CALL.** Category config-ish, `diagnosis.rule_id="R14"`, `requires_analysis=False`, **no** terminal cause → `(True, None)`. Also R18/unknown → `(True, None)`.
4. **Policy switches.** `policy="always"` → `(True, None)` for a summary that `auto` would skip; `policy="never"` → `(False, "policy_never")`.
5. **Replay.** `from_completion` present → `(True, None)` under `auto` even for an otherwise-skippable summary.
6. **Fail-open.** If `_deterministic_sufficient` raises internally, the gate returns a call (True), never propagates.
7. **CLI/action wiring.** `--analyze-policy` parses and reaches `decide_stgpt_call`; default is `auto`.
8. Suite green: `pytest tests/rca -q`, and the acceptance-35 grep stays empty:
   `rg -n "job_id|run_id|github_api" tools/rca/cleaner.py tools/rca/drain_index.py tools/rca/budget.py tools/rca/redact.py tools/rca/history.py`

## Out of scope (do NOT do here)

- The "always render one AI Diagnosis section / rename `skipped`→`deterministic`/`needs-review` / add `hybrid` source / needs-review confidence" output invariant — that is Step 5.
- Adding stack_traces / junit / code_context to the evidence pack — later slice.
- Per-repo cost ceilings / recurrence-widening of the cache — later, optional.

## Deliverable

A focused diff over `analyze.py`, `cli.py`, `action.yml` (and `outputs.py` only if trivial), plus new fixtures/tests, with `pytest tests/rca -q` green. Summarize what changed and show the new test output, including a before/after count of how many of the existing test fixtures now skip the model under `auto`.
```
Short command to run it in Claude Code (from the repo root):

Read docs/cc-prompt-step4-refine-ai-gate.md and implement exactly that slice — nothing outside its scope. Follow AGENTS.md, add the fixtures and tests it specifies, and make sure `pytest tests/rca -q` is green before you finish.
```
