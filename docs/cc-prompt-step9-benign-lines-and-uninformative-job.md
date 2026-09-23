# Claude Code task — Step 9 (regression fix): benign-line filtering + uninformative-job → pipeline-sourced cause

## Context

`ci-rca-collector`. Read `AGENTS.md` and `docs/ci-rca-collector-spec-v8.md` first. Steps 1–8 are merged. A real regression was found: a failure that was diagnosed **correctly before Step 1** is now diagnosed **wrongly**, caused by the interaction of Steps 1/3.

Do **only** what this document scopes. **Do not hardcode any project, filename, or message. Fix it generally.**

## The failure and the exact mechanism (reproduce this, generalized)

A Java/Maven CI job. The GitHub-designated failed step is **"Run unit tests"**, but that step's **job log is uninformative** — it contains only setup/echo/warning/normal-progress lines and an exit code, e.g.:

```
Using unit test service: <name>
time="…" level=warning msg="No services to build"
<hex> Already exists 0B          # normal Docker image-layer pull output
Error: Process completed with exit code 1.
```

The **real root cause** lives only in the **pipeline/docker logs** (a Maven compile error):

```
[ERROR] COMPILATION ERROR :
[ERROR] /…/TestConfig.java:[19,40] ';' expected
[ERROR] Failed to execute goal …:maven-compiler-plugin:…:testCompile …
```

What the current code does wrong:

1. `config.TERMINAL_CAUSE_PATTERNS` includes bare `already exists` and `artifactory`. The **normal Docker layer line `<hex> Already exists 0B` matches `already exists`**, so `diagnose.terminal_cause_present(summary)` returns True — a **false positive** on benign pull output.
2. Because a terminal cause is "present", Step 3 makes `build_evidence` **exclude `log_templates` + `pipeline_logs`** — deleting the only evidence that contains the compile error — and `extract._primary_failure_line` picks the benign "Already exists 0B" line.
3. The deterministic card anchors on "Already exists" and emits an Artifactory-style "bump the package version" fix; the model, given only benign evidence, says `cannot_determine`, so the grounding fallback ships the wrong deterministic card.

The two general defects:

- **A. Normal/informational output is treated as a failure cause.** Docker/registry **pull progress** (`Already exists`, `Pull complete`, `Downloading`, `Verifying Checksum`, `Extracting`, `Digest: sha256:…`, `Status: Image is up to date`, layer ids), orchestration **warnings/echoes** (`level=warning`, `No services to build`, `Using … service`), and normal completions must **never** be a terminal cause, `primary_failure_line`, first-error anchor, or citation.
- **B. When the failed step's own log is uninformative, the cause is in the pipeline logs** and must be used — never excluded. The current `diagnose._job_logs_are_exit_only` does not treat a setup/warning/progress-only job as uninformative, so the pipeline-over-job path (R17) doesn't fire.

## Goal of this slice

1. **Benign-line filter (defect A).** Add `config.BENIGN_LINE_PATTERNS` (case-insensitive regexes) covering container/registry pull progress, image-up-to-date/loaded, layer-digest lines, and non-error orchestration noise (`level=warning`, `No services to build`, `Using … service`, pure echo lines, `exited with code 0`). Add a helper `is_benign_line(line) -> bool`. A benign line **cannot** be selected as a cause anywhere, even if it also matches a cause/terminal pattern — **benign takes precedence**. Apply it in:
   - `extract._primary_failure_line` and `extract._first_error_index` / `_windows` (don't anchor the first-error window on a benign line),
   - `diagnose.terminal_cause_present` (evaluate terminal patterns only over **non-benign** anchor lines),
   - `diagnose.specific_log_cause` and `diagnose.display_citation_quotes` (never return/cite a benign line).
2. **Tighten the over-broad terminal patterns (defence in depth).** Replace bare `already exists` with a failure-context shape such as `(?:release|version|tag|artifact|image|package)\b[^\n]*already exists` **or** `already exists[^\n]*(?:you need to update|update (?:the )?package|overwrite|on\s+\w+)`, and **remove bare `artifactory`**. The original Artifactory line ("This release already exists on Artifactory. You need to update package.json") must still match; a bare Docker "`<hex> Already exists 0B`" must not.
3. **Uninformative-job → pipeline source (defect B).** Extend the "exit-only" notion (`diagnose._job_logs_are_exit_only`, or a new `_job_log_uninformative`) so a job whose non-benign, non-exit content is empty **counts as uninformative**. When the primary job is uninformative and pipeline streams have a real first error:
   - `terminal_cause_present` must be False for the job (so Step 3 does **not** exclude `pipeline_logs`/`log_templates`),
   - the pipeline-over-job rule (R17) fires and the deterministic cause is taken from the highest-priority **non-teardown** pipeline stream's first error (e.g. the compile error), not the job's benign output,
   - set `FailedJob.primary_failure_line` from that pipeline error when the job yields none.
4. **Grounding correctness for pipeline-sourced causes.** Extend `prompt.failed_step_anchor_text` so that **when the job is uninformative**, the anchor also includes the primary non-teardown pipeline stream's first-error window. This keeps a correct pipeline-cited answer `grounded` (Status `ok`) instead of `needs-review`.
5. **Safety net for cannot-determine.** In `analyze.finalize_user_card` / `_finalize_card`: when the model returns `cannot_determine` **and** the deterministic card is low-confidence and not grounded (or its cause is a benign line), do **not** present the deterministic card as a confident answer — render `needs-review` "cannot determine from current evidence" and keep the model's explanation in notes. (With 1–4 fixed this rarely triggers, but it prevents shipping a bogus card.)

## Where to change (verify against current code first)

- **`tools/rca/config.py`** — add `BENIGN_LINE_PATTERNS` + export; tighten `TERMINAL_CAUSE_PATTERNS` (drop bare `already exists`/`artifactory`, add the contextual shapes). Keep `SYMPTOM_PATTERNS` as-is.
- **`tools/rca/extract.py`** — `is_benign_line` usage in `_primary_failure_line`, `_first_error_index`/`_windows`; import the patterns from config. Keep `extract.py` source-agnostic (no GitHub imports).
- **`tools/rca/diagnose.py`** — benign-aware `terminal_cause_present`, `specific_log_cause`, `display_citation_quotes`; extend `_job_logs_are_exit_only`/add uninformative detection; make R17 (`_rule_pipeline_over_job`) fire for the uninformative-job case and source the cause + `primary_failure_line` from the non-teardown pipeline stream.
- **`tools/rca/prompt.py`** — extend `failed_step_anchor_text` with the pipeline first-error window when the job is uninformative.
- **`tools/rca/analyze.py`** — the cannot-determine safety net in `finalize_user_card`/`_finalize_card`.

## Constraints (AGENTS.md)

- Keep `extract.py`, `budget.py`, `redact.py`, `drain_index.py`, `history.py`, `pipeline_logs.py`, `cleaner.py` free of GitHub imports / `job_id`/`run_id`.
- Collector/analyze must never fail the workflow; all new checks degrade gracefully.
- **No hardcoded project names, filenames, or messages** — only generic pattern tables and structural rules (benign vs cause, informative vs uninformative, job vs pipeline).
- **No regressions:** the Steps 1–8 test suite must stay green, especially the Artifactory "release already exists → bump version" case and the Step 3 terminal-cause / symptom-demotion tests.

## Acceptance criteria

Add tests under `tests/rca/` (generic fixtures):

1. **Regression reproduced then fixed.** A summary where the failed job log is setup/warning/pull-progress + exit-only (including a `<hex> Already exists 0B` line) and a pipeline stream contains a compile error:
   - `terminal_cause_present` is **False** (benign line does not count);
   - `build_evidence` **includes** `pipeline_logs`/`log_templates` (not excluded);
   - the deterministic cause and `primary_failure_line` come from the **compile error**, not "Already exists";
   - a model answer citing the pipeline compile line is `grounded` and renders `Status: ok` with the compile root cause — not the "bump package version" card.
2. **Artifactory case still works.** "This release already exists on Artifactory. You need to update package.json" in the failed step still yields `terminal_cause_present=True`, the version/publish root cause, and (Step 4) `deterministic_sufficient` skip. No regression.
3. **Benign never cited.** `specific_log_cause` / `display_citation_quotes` / `primary_failure_line` never return a `BENIGN_LINE_PATTERNS` line; a first-error window is not centered on one.
4. **Safety net.** A model `cannot_determine` with a low-confidence, ungrounded deterministic card renders `needs-review` "cannot determine…", not a confident wrong card.
5. Suite green: `pytest tests/rca -q`, and the acceptance-35 grep stays empty:
   `rg -n "job_id|run_id|github_api" tools/rca/cleaner.py tools/rca/drain_index.py tools/rca/budget.py tools/rca/redact.py tools/rca/history.py`

## Out of scope

- Any new feature; this is a correctness fix. No changes to the AI gate policy, telemetry, or evidence profiles beyond what defects A/B require.

## Deliverable

A focused diff over `config.py`, `extract.py`, `diagnose.py`, `prompt.py`, `analyze.py`, plus tests reproducing the regression and guarding the Artifactory case, with `pytest tests/rca -q` green. Show the before/after `<EVIDENCE>` and rendered "AI Diagnosis" for the uninformative-job compile case.
```
Short command to run it in Claude Code (from the repo root):

Read docs/cc-prompt-step9-benign-lines-and-uninformative-job.md and implement exactly that slice — nothing outside its scope. Follow AGENTS.md, add the tests it specifies (including the Artifactory no-regression guard), and make sure `pytest tests/rca -q` is green before you finish.
```
