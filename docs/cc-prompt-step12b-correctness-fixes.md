# Claude Code task — Step 12b: pre-delivery correctness fixes

## Context

`ci-rca-collector`. Read `AGENTS.md`, `docs/ci-rca-collector-spec-v8.md`, `docs/rca-eval-spec-v1.md`, and `docs/rca-delivery-spec-v1.3.md` first. Steps 1–11b are merged. This slice fixes defects found by a full-code audit **before** Phase 3 delivery renders anything, because each one would surface directly in a PR comment. It is independent of Step 12 (`github_api.py`) and can land before or after it.

Do **only** what this document scopes. **Nothing is hardcoded to a project, message, or vendor** — every fix is table- or structure-driven.

## Defects (all reproduced on the committed goldens)

Running `python -m tools.eval.run_eval --goldens tools/eval/goldens --no-llm` gives category accuracy **0.9**; the single miss is `artifactory-version-exists` — the case this project started from:

```
primary_failure_line: This release already exists on Artifactory. You need to update package.json
terminal_cause_present: True
diagnose -> rule R18 | category unknown | confidence low | requires_analysis True
one_liner:  "Checking if version 3.1.21 exists in Artifactory"      <- the echo BEFORE the cause
gate:       decide_stgpt_call -> (True, None)                         <- model always called
```

**D1 — A terminal cause never becomes a verdict.** Step 3's `TERMINAL_CAUSE_PATTERNS` and the classify taxonomy (`CLASSIFY_RULES`) are disconnected: a line can be a self-explaining terminal cause yet map to no category, so the chain falls through to R18 `unknown`/`low`. Consequences: Step 4's `_deterministic_sufficient` can never fire for it (model always called), and a delivery comment would read "CI failure — unknown" with the root cause omitted under the default `medium` threshold.

**D2 — The headline cites the wrong line.** `diagnose.specific_log_cause()` never looks at `FailedJob.primary_failure_line` (Step 1); it scans error lines/windows and matches `_SPECIFIC_CAUSE_RE`, which still contains a **bare `artifactory`** alternative. Step 9 removed bare `artifactory` from `TERMINAL_CAUSE_PATTERNS` but not from this regex, so the echo line "Checking if version … in Artifactory" wins. `_copy_from_specific_line` also keys its fix text on the literal substring `"artifactory"`.

**D3 — Source mislabel.** In the Step 2 grounding gate (`analyze.py`, the block after `if focused != evidence:`), when the model answer is ungrounded and the deterministic card is grounded, the result is replaced by `analysis_result_from_summary(summary, source="hybrid")` while `status` stays `ok`. The card is 100% deterministic (model text only in notes), yet it is labelled `hybrid`, and `outputs.diagnosis_source()` returns `"ai"` for any `ok`/`cached` status without looking at `result.source`. The `diagnosis-source` action output therefore disagrees with the rendered card.

**D4 — `AGENTS.md` contradicts the current project.** It says "This repo implements **Phase 1 only**" and "**No LLM calls, no PR comments, no issue creation**". Phase 2 (STGPT analyze) and eval are shipped, and Phase 3 delivery is being built. Every step prompt says "follow AGENTS.md", so from Step 15 onward the rules would forbid the work. Its protected-module list and dependency list are also stale.

## Goal

### 1. Terminal cause → category (fixes D1), gap-filling only

- In `config.py`, introduce `TERMINAL_CAUSE_RULES: list[{pattern, category}]` as the single source of truth and **derive** `TERMINAL_CAUSE_PATTERNS` from it, so every existing consumer (`diagnose._TERMINAL_CAUSE_RES`, `terminal_cause_present`, Step 3 exclusion) is unchanged.
- Map each entry to an **existing** category where one fits (`ERESOLVE`/`No matching distribution`/`Could not find a version`/`ModuleNotFoundError`/`Cannot find module` → `dependency`; `error TS…`/`cannot find symbol` → `compile`; `AssertionError`/`FAILED <name>` → `test_failure`; `ENOSPC` → `disk_space`).
- Add **one new category `release`** for release/version-gate failures — the version/tag/artifact/package "already exists", `version exists`, `must update`, `you need to update` entries. Reason: none of the existing categories is honest here, and mapping to `dependency` would make `user_facing()` print "Package install failed: a package was not found", which is wrong. Entries with no good fit (e.g. quality-gate / coverage) map to `None` and are not gap-filled.
- Wire `release` everywhere a category is enumerated: `classify._CODE_CATEGORIES` (side = `code`), `diagnose.SIGNATURE_CATEGORIES`, `config.EVIDENCE_PROFILES` (lean profile like `dependency`: excerpt + first-error + hint + change_context), `diagnose.user_facing()` copy (root cause = the terminal line itself; fix = bump the release/package version and republish), `tools/eval/labels.py` `VALID_CATEGORIES` (derive it to include terminal-cause categories, not only `CLASSIFY_RULES`), and the `category` output description in `action.yml`.
- In `diagnose.diagnose()`, add a **gap-filling** rule that runs **only when the chain would otherwise return R14 or R18** (i.e. no category rule matched): if `terminal_cause_present(summary)` and the first non-benign anchor line that matches a terminal rule (prefer `primary_failure_line`) maps to a non-`None` category → return that category, `confidence="high"`, `requires_analysis=False`, `is_infra_vs_code=_side(category)`, `one_liner` = that line. **Do not change any outcome where a category rule already fired.**
- Give it a new rule id (e.g. `R19`) with an entry in `_FIX_BY_RULE`, and **widen every rule-id regex** so the new id can never leak into user text: `_contains_rule_id` and the two `re.sub` calls in `user_facing()` currently match only `R1`–`R18` (`\bR(?:1[0-8]|[1-9])\b`). Use a form that covers any `R<number>`.

### 2. Headline line (fixes D2)

- `specific_log_cause()` must consider `primary_failure_line` **first** (when present, non-benign, and it matches a cause/terminal pattern) before scanning error lines and windows.
- Remove the bare `artifactory` alternative from `_SPECIFIC_CAUSE_RE`; rely on the contextual release shapes.
- `_copy_from_specific_line()` must select the release fix by the release/version-exists **shape** (reuse the `release` terminal patterns), not the literal vendor word.

### 3. Source labelling (fixes D3)

- In the Step 2 grounding gate, when the deterministic card replaces an ungrounded model answer, label it `source="deterministic"` (the model text is already preserved in `notes`). `hybrid` stays reserved for model prose re-anchored on the failed step. Update any test asserting `"hybrid"` for this path, with a one-line justification in the test.
- `outputs.diagnosis_source()`: when `record.result` is present, derive from `record.result.source` (`deterministic` → `"deterministic"`; `ai`/`hybrid`/`mixed` → `"ai"`), before the status-based fallback. Keep the output enum `deterministic | ai | gated | none` unchanged.

### 4. Rewrite `AGENTS.md` (fixes D4)

Keep it short and rule-shaped. It must state:
- Phases: 1 collect (shipped), 2 analyze via STGPT (shipped), eval harness (shipped), 3 delivery (in progress, per `docs/rca-delivery-spec-v1.3.md`).
- **Permissions:** collect stays `actions: read`, `contents: read`. GitHub writes (comments, issues, labels, reactions) happen **only** in `tools/rca/deliver/` via `github_api.py`, only in the separate delivery job, and only when `deliver` is enabled.
- **Source-agnostic modules** (no GitHub imports, no `job_id`/`run_id`): `cleaner.py`, `drain_index.py`, `budget.py`, `redact.py`, `history.py`, `extract.py`, `classify.py`, `pipeline_logs.py`.
- All GitHub HTTP goes through `github_api.py`; no hardcoded `api.github.com`.
- `tools/rca/` never imports `tools/eval/`.
- Never print rule ids (`R<n>`) in any user-facing channel.
- Keep the existing token / never-fail rules.
- Dependencies: `pydantic>=2`, `httpx`, `drain3`, `python-dateutil`, `pyyaml`. No PyGithub.
- Build discipline: one slice per step prompt under `docs/cc-prompt-step*.md`; keep `pytest tests/rca -q` and `pytest tests/eval -q` green; the acceptance-35 grep stays empty.

### 5. Eval: relabel + deliberate baseline refresh

- Relabel `tools/eval/goldens/artifactory-version-exists/label.yaml` to `true_category: release`, `is_infra_vs_code: code`, and add a `notes:` line explaining the taxonomy change.
- Re-run `python -m tools.eval.run_eval --goldens tools/eval/goldens --no-llm --out tools/eval/eval-baseline.json` (and regenerate `eval-baseline.summary.json` the way 11b does). This is a **deliberate** refresh because the change intentionally improves the metric — say so in the summary. Category and infra-vs-code accuracy must be **1.0** on the 10 goldens.

## Constraints

- Gap-filling only: every golden and fixture whose verdict came from a category rule must be byte-for-byte unchanged in category/confidence. Only R14/R18 outcomes may change.
- Benign still wins: a benign line (e.g. Docker `<hex> Already exists 0B`) must never trigger the `release` rule — `docker-already-exists-benign` must not become `release`.
- Source-agnostic modules stay GitHub-free. No new dependencies.
- Never fail the workflow; the new rule must be exception-safe like its neighbours.

## Acceptance criteria

Add tests under `tests/rca/` (and `tests/eval/` where noted), driven by the **real goldens** where possible:

1. `artifactory-version-exists` golden: `diagnose` → category `release`, confidence `high`, `requires_analysis=False`, `one_liner` equals the `primary_failure_line` (not the "Checking if version …" echo).
2. Same golden after `apply_verdict`: `decide_stgpt_call(summary, stgpt_key_present=True)` → `(False, "deterministic_sufficient")`.
3. No `R<number>` token appears in `user_facing()` root cause or fix for the new rule (regex widened).
4. `docker-already-exists-benign` and `java-compile-in-pipeline` goldens keep their current categories; no golden whose verdict came from a category rule changes.
5. `specific_log_cause()` returns `primary_failure_line` when it is a cause line; a summary whose only "Artifactory" mention is an echo line does not select that echo.
6. Step 2 gate path: an ungrounded model answer replaced by a grounded deterministic card → `result.source == "deterministic"` and `diagnosis-source` output `"deterministic"`.
7. `AGENTS.md` no longer contains "Phase 1 only" or "no PR comments"; it names the delivery permission split and the full source-agnostic list (a simple test can assert these strings).
8. `run_eval --no-llm`: category accuracy 1.0, infra-vs-code accuracy 1.0; baseline refreshed.
9. Suites green: `pytest tests/rca -q` and `pytest tests/eval -q`; acceptance-35 grep empty.

## Out of scope

- `github_api.py` (Step 12) and anything under `tools/rca/deliver/` (Step 13+).
- Changing the STGPT prompt, Drain3, or collection.
- Any other new category.

## Deliverable

A focused diff over `config.py`, `classify.py`, `diagnose.py`, `analyze.py`, `outputs.py`, `action.yml` (description only), `tools/eval/labels.py`, the one golden label, the refreshed baseline, `AGENTS.md`, plus tests. Show the before/after `diagnose` output for the Artifactory golden and the new eval metrics.
```
Short command to run it in Claude Code (from the repo root):

Read docs/cc-prompt-step12b-correctness-fixes.md and implement exactly that slice — gap-filling only, nothing outside its scope. Follow the updated AGENTS.md rules it describes, add the tests it specifies, and make sure `pytest tests/rca -q` and `pytest tests/eval -q` are green before you finish.
```
