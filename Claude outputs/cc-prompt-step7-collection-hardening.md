# Claude Code task — Step 7: collection-side hardening

## Context

`ci-rca-collector`. Read `AGENTS.md` and `docs/ci-rca-collector-spec-v8.md` first. Steps 1–6 are merged (failed-step anchoring, grounding gate, terminal-cause/symptom demotion, refined AI gate, consistent output section, category-aware evidence). This slice raises the quality of the *collected* evidence that feeds both the deterministic path and the model. It is a set of **independent, low-risk** improvements — implement them together but keep each isolated and separately tested.

Do **only** what this document scopes. No changes to gating (`decide_stgpt_call`), grounding, or the evidence profiles from Step 6.

## Improvements (each independent)

### 7a. Error-centered trimming (highest value)

`budget.trim_middle` keeps the head + tail and elides the middle. For a long `first_error` / `merged` window the actual causal line can sit in the elided middle and be lost. Add an **error-centered** trim used for those windows: keep a band of lines around the window's error anchor instead of head+tail.

- Add a function such as `trim_around(text, cap_tokens, *, anchor_index=None)` in `budget.py` that keeps `cap`-sized content centered on `anchor_index` (fallback: reuse `extract._first_error_index`-style detection, or accept the anchor from the caller). Keep the existing `trim_middle` for tail/stack/annotation sections.
- In `apply_budget`, use error-centered trimming for `first_error` and `merged` `LogWindow`s; keep `tail` on `trim_middle`. Never trim `failed_step_excerpt` or `primary_failure_line` (already mandatory/untrimmed).
- `budget.py` must stay source-agnostic (no GitHub imports).

### 7b. JUnit collapse-by-message

When many tests fail with the **same** message (one root cause, many assertions), the `JUNIT_FAILURE_CAP` (5) can drop the signal. Collapse failures by normalized message before capping.

- In `junit.py` (`parse_junit_xml` / `merge_junit_reports`), group failures whose `message` (or first body line) is equal after whitespace-normalization; keep one representative per group with a `count`, and only then apply `JUNIT_FAILURE_CAP`. Preserve distinct messages over near-duplicates.
- Keep `JUnitReport.total_failures` as the true count; the collapse only affects the retained `failures` list. If adding a per-group count needs a model field, add `JUnitFailure.count: int = 1` (optional + defaulted).

### 7c. Stack-frame relevance ordering

When multiple stack traces / frames exist, prioritize the ones whose paths intersect `changes.files` (you already compute `application_stack_in_diff` in `diagnose.py`), so the most relevant frame leads.

- Where stack traces are assembled for a `FailedJob` (extract/collect), order traces so that a trace naming a changed application source path comes first. Do not drop the others; only reorder. Keep `extract.py` source-agnostic — pass changed paths in as a plain list, or do the ordering at the collect layer that already has `changes`.

### 7d. Redaction entropy backstop

`redact.py` is allow-list regex — solid for known token shapes but a bare high-entropy secret with no keyword (a long hex/base64 blob) can pass. Add an entropy-based backstop.

- In `redact.py`, after the existing patterns, redact standalone tokens that are long (e.g. ≥ 32 chars), high-entropy, and not obviously a hash/sha/path/URL. Be conservative: require a length + charset + Shannon-entropy threshold so normal identifiers, SHAs already shown intentionally, and file paths are **not** clobbered. Keep it a separate pass with its own unit tests; it must run before anything is written (it already does via `redact_summary` at `_emit`).
- `redact.py` stays source-agnostic.

### 7e. (Optional, flag-guarded) Gate the pipeline-artifact download on a job-log pre-verdict

Today the collect path downloads up to `MAX_PIPELINE_LOG_ARTIFACTS` pipeline/docker zips **before** diagnosis. When the **job log alone** already yields a high-confidence terminal cause at the failed step, those zips are only symptoms (Step 3) — downloading them wastes latency/bandwidth and adds noise.

- Add a cheap pre-classification from the **job logs only** (reuse `classify_failure` / `terminal_cause_present`-style check on the cleaned job log). When it finds a high-confidence signature **with** a terminal cause at the failed step **and** the failed step is not itself a docker/pipeline step, skip the pipeline-log (not JUnit) download.
- Make this **opt-in and off by default** via an input/env (e.g. `RCA_GATE_PIPELINE_DOWNLOAD`, default false) so current behaviour is unchanged unless enabled. Record a `collection_notes` line when a download was skipped and why.
- If this proves fiddly, ship 7a–7d and leave 7e as a `TODO` — do not reorder the collect pipeline in a risky way to force it in.

## Constraints (AGENTS.md)

- `budget.py`, `redact.py`, `junit.py`, `cleaner.py`, `drain_index.py`, `history.py` must not import GitHub modules or mention `job_id`/`run_id`. Stack-frame ordering that needs `changes` is done at the collect layer, not inside `extract.py`.
- Collector must never fail the workflow: every new step degrades gracefully (empty/oddly-encoded input → no-op, never raises), exit 0 unless `--strict`.
- Non-destructive and backwards-compatible: `summary.json` schema stays valid for old fixtures (new model fields optional + defaulted). The default behaviour of 7e is unchanged (off).
- No hardcoded failure strings.

## Acceptance criteria

Add tests under `tests/rca/` for each part independently:

1. **7a:** a `first_error` window whose causal line is in the middle survives error-centered trimming under a small cap (the causal line is present; head+tail-only trimming would have dropped it). `tail` windows still use middle-elision.
2. **7b:** 12 failures sharing one message collapse to a single retained failure with `count=12` (or the group representation you chose); distinct messages are preserved; `total_failures` unchanged.
3. **7c:** with two stack traces, the one naming a path in `changes.files` is ordered first; both remain.
4. **7d:** a bare 40-char high-entropy token is redacted; a normal file path, a 40-char SHA shown intentionally in an excerpt, and ordinary identifiers are **not** redacted (guard against over-redaction).
5. **7e (if implemented):** with the flag on and a job-log terminal cause present, pipeline zips are not downloaded and a note is recorded; with the flag off, behaviour is unchanged. (If deferred, assert nothing changed and a TODO exists.)
6. Suite green: `pytest tests/rca -q`, and the acceptance-35 grep stays empty:
   `rg -n "job_id|run_id|github_api" tools/rca/cleaner.py tools/rca/drain_index.py tools/rca/budget.py tools/rca/redact.py tools/rca/history.py`

## Out of scope

- Telemetry/measurement persistence — that is Step 8.
- Any change to the AI gate, grounding, terminal-cause tables, or evidence profiles.
- Single-persona default / cost ceilings.

## Deliverable

A focused diff across `budget.py`, `junit.py`, `redact.py`, the collect layer (for 7c and optional 7e), and `models.py` (only optional defaulted fields), plus per-part tests, with `pytest tests/rca -q` green. Summarize each part and show the before/after for 7a (a trimmed window keeping the causal line) and 7d (an entropy redaction).
```
Short command to run it in Claude Code (from the repo root):

Read docs/cc-prompt-step7-collection-hardening.md and implement exactly that slice — nothing outside its scope. Follow AGENTS.md, add the per-part tests it specifies, and make sure `pytest tests/rca -q` is green before you finish.
```
