# Claude Code task — Step 9b: tighten benign patterns + benign-filter the classify layer

## Context

`ci-rca-collector`. Read `AGENTS.md` first. Step 9 added `config.BENIGN_LINE_PATTERNS` + `extract.is_benign_line`, applied across cause selection (`_primary_failure_line`, `terminal_cause_present`, `specific_log_cause`, citations, exit-only detection, R17). A review found two follow-ups. This is a small correctness hardening slice — **no new features, no hardcoding**. Do only what is scoped.

## The problems

**A. One benign pattern is too broad (same failure mode as the `already exists` regression).**
`BENIGN_LINE_PATTERNS` contains:

```
r"\busing\b[^\n]*\bservice\b"
```

This marks **any** line containing "using" … "service" as benign — including a genuine error such as `ERROR: connection failed using the auth service (500)` or `Failed using the payment service`. Because benign lines are dropped from all cause selection **and** count toward "uninformative job", a real error line matching this can be silently suppressed and the true cause missed. It was only meant to catch the setup echo `Using <name> service:`.

**B. The classify layer is not benign-filtered (consistency gap).**
`classify.classify_lines` / `classify.classify_union` (Stage 5 — they set `category`, which drives the Step 4 AI gate and the Step 6 evidence profile) do **not** skip benign lines, while cause selection now does. No current benign pattern overlaps a `CLASSIFY_RULE`, so it is latent, but it is an inconsistency that will bite when the tables grow (e.g. a future benign/progress line that also matches `error:`).

## Goal

1. **Tighten `\busing\b[^\n]*\bservice\b`** to the setup-echo shape only — anchor it to the start of the (timestamp-stripped) line and/or require the trailing colon, e.g. `^\s*using\s+\S+\s+service\b\s*:?` (match "Using unit test service: …", not an error that merely contains both words). Verify against the intended benign line and against a real error containing "using"+"service".
2. **Audit the rest of `BENIGN_LINE_PATTERNS`** for the same over-breadth and tighten any that can match a genuine error line. Guiding rule: a benign pattern must match **normal/progress/echo** output only. In particular re-check `\blevel=warning\b` and `\bno services to build\b` — keep them (they are correct for the uninformative-job path) but confirm they can't swallow an adjacent error. Keep each pattern documented with a one-line comment on exactly what normal output it targets.
3. **Apply the benign filter in the classify layer.** In `classify.classify_lines` and `classify.classify_union`, skip lines for which `is_benign_line(line)` is True before rule matching, so a benign/progress line can never set the category. Import `is_benign_line` from `extract` (keep `classify.py` source-agnostic — `extract`/`is_benign_line` have no GitHub imports, so this is fine). Do not change rule order or confidence semantics otherwise.

## Where to change (verify against current code first)

- **`tools/rca/config.py`** — replace the over-broad `using…service` regex with the anchored shape; tighten any other pattern the audit flags; keep comments.
- **`tools/rca/classify.py`** — `is_benign_line`-skip in `classify_lines` and `classify_union`. Guard the import so a circular/edge import can't crash classification (fall back to no-skip on ImportError).

No changes to `diagnose.py`, `prompt.py`, `analyze.py`, `extract.py` logic beyond what these two require.

## Constraints (AGENTS.md)

- `classify.py`, `extract.py`, `budget.py`, `redact.py`, `drain_index.py`, `history.py`, `pipeline_logs.py` stay free of GitHub imports / `job_id`/`run_id`.
- Never fail the workflow: a bad pattern or import must degrade to "not benign" (i.e. do not suppress a line on error), never raise.
- No hardcoded project names, filenames, or messages — only generic pattern tightening.
- **No regressions:** the Step 9 regression test (uninformative Java job with `Using unit test service` / `No services to build` / `Already exists 0B` → compile cause from pipeline) and the Artifactory case must both still pass. The `Using … service:` echo must still be benign so that job stays "uninformative".

## Acceptance criteria

Add/extend tests under `tests/rca/`:

1. **Echo still benign.** `Using unit test service: maven_unit_test_x` → `is_benign_line` True (the Step 9 uninformative-job path still works).
2. **Real error no longer swallowed.** `ERROR: connection failed using the auth service (500)` → `is_benign_line` **False**, and it is eligible as `primary_failure_line` / a cause / a citation.
3. **Classify skips benign.** A cleaned line list where a benign progress line precedes a real signal: `classify_lines` / `classify_union` pick the real category, and a benign-only list classifies as `unknown` (not miscategorized by a progress line).
4. **No regression.** The Step 9 uninformative-job regression test and the Artifactory terminal-cause test stay green.
5. Suite green: `pytest tests/rca -q`, and the acceptance-35 grep stays empty:
   `rg -n "job_id|run_id|github_api" tools/rca/cleaner.py tools/rca/drain_index.py tools/rca/budget.py tools/rca/redact.py tools/rca/history.py`

## Out of scope

- Any change to gating, evidence profiles, telemetry, or the Step 9 uninformative-job/anchoring logic.
- Adding new benign categories beyond tightening existing ones (unless the audit finds a clearly-normal line already in the list that is dangerously broad).

## Deliverable

A small diff over `config.py` and `classify.py`, plus tests, with `pytest tests/rca -q` green. Summarize which patterns you tightened and show the before/after for the `using … service` case (echo benign, error not benign).
```
Short command to run it in Claude Code (from the repo root):

Read docs/cc-prompt-step9b-benign-pattern-tightening.md and implement exactly that slice — nothing outside its scope. Follow AGENTS.md, add the tests it specifies (including the Step 9 and Artifactory no-regression guards), and make sure `pytest tests/rca -q` is green before you finish.
```
