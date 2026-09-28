# Claude Code task — Step 11b: eval report + baseline diffing + CI wiring

## Context

`ci-rca-collector`. Read `AGENTS.md`, `docs/rca-eval-spec-v1.md` (§8.3, §9, §10, §11), and `docs/cc-prompt-step11a-eval-runner-metrics.md` first. Step 11a is merged: `tools/eval/{labels,metrics,run_eval}.py` + 10 seed goldens exist. `run_eval` writes `eval-results.json` shaped:

```json
{ "metrics": { ...EvalMetrics... }, "provenance": {...}, "filters": {...}, "no_llm": false }
```

This is 11b — the reporting + gating + CI half. **RECONCILE with 11a — reuse its models and results file; do not recompute metrics or re-implement the runner.**

## Deliverables

### 1. Small extension to `run_eval.py` — emit per-golden verdicts

The spec's "list every golden whose verdict changed vs baseline" (§8.3, acceptance #17) needs per-golden data; 11a stores only aggregates. Add a `per_golden` section to `eval-results.json` (a list, one entry per golden): `slug`, and the deterministic verdict re-run in 11a (`category`, `infra_vs_code`, `is_flaky`, `short_circuit`), each `*_correct` vs its label, and — when the model ran — that golden's mean `grounding_rate` and modal `display_status` across runs. Keep it flat and small. Do **not** change the existing aggregate `metrics` shape or the deterministic/LLM logic — just also record per-golden rows from data the runner already computes.

### 2. `tools/eval/report.py` (eval-spec §8.3)

```
python -m tools.eval.report --results eval-results.json [--baseline tools/eval/eval-baseline.json] \
  [--fail-under category_accuracy=0.70,grounding_rate=0.95] --format markdown
```

- Load `eval-results.json` (and optional baseline of the same shape). Reuse the `EvalMetrics`/`Stat` models from `metrics.py` — parse, don't recompute.
- **Markdown to stdout** (and the caller tees it to `$GITHUB_STEP_SUMMARY`); also write a JSON summary alongside for machine use.
- Render the headline metrics (category/infra/flake/short-circuit accuracy, grounding_rate, fully_grounded_share, schema_first_try_rate, fallback_rate, cost, short_circuit_share), the **category confusion matrix**, `median_golden_age_days`, and `stale_goldens`.
- **With `--baseline`, show per-metric deltas.** The one rule that matters most (eval-spec §8.3): **when `|delta| < stdev`, render it as `≈` and explicitly label it "within run-to-run variance"** — do not present it as a change. Use the noise band `max(current.stdev, baseline.stdev)` for the metric.
- **List every golden whose verdict changed vs baseline** (from the `per_golden` sections) — this is the section a human actually reviews. Include what changed (e.g. `category: compile → test_failure`).
- `--fail-under metric=threshold,...`: exit **non-zero** when any named metric's `.mean` is below its threshold, zero otherwise (eval-spec §11.11). Map friendly names (`category_accuracy`, `grounding_rate`, …) to the `EvalMetrics` fields' `.mean`.
- `--format` supports at least `markdown` (default); a `json` mode may just emit the machine summary.

### 3. `tools/eval/check_goldens_redacted.py` + pre-commit hook (eval-spec §11.19)

- A script that scans every file under `tools/eval/goldens/` for any value matching a `redact.py` secret pattern (reuse `redact.redact_text` / its pattern set — do not re-list patterns). Exit non-zero and name the offending file+pattern if any golden is unredacted.
- Wire it as a pre-commit hook (add/extend `.pre-commit-config.yaml`) and make it runnable standalone in CI.

### 4. `.github/workflows/eval.yml` (eval-spec §9)

Use the spec's §9 workflow as the template, adjusted to real module paths:
- `on: pull_request` filtered to `tools/rca/prompt.py`, `tools/rca/config.py`, `tools/rca/analyze.py`, `tools/rca/validate.py`, `tools/eval/**`; plus `workflow_dispatch` with a `runs` input (default `'3'`).
- Steps: checkout → setup-python 3.12 → `pip install -r requirements.txt` → `run_eval` (LLM creds from secrets `LLM_URL`/`LLM_KEY` mapped to the real env names the analyze path reads — `STGPT_API` / `STGPT_API_URL`; confirm against `config.resolve_stgpt_*`) → `report --baseline tools/eval/eval-baseline.json --fail-under grounding_rate=0.95,category_accuracy=0.70 --format markdown | tee -a "$GITHUB_STEP_SUMMARY"` → upload `eval-results.json` artifact (`if: always()`).
- Also run `check_goldens_redacted.py` as a step (fast, no network).
- Add a `tools/eval/eval-baseline.json` committed from a current `--no-llm` (or a representative) run, with a comment/README noting it is refreshed **deliberately**, never auto-updated (eval-spec §9 close).

### 5. Grow the golden set — note only

Add a short `tools/eval/goldens/README.md` documenting the §7.2 process (seed synthetic; grow to 30–50 real via the `capture` subcommand; label from the fix commit; cover the real distribution). Do **not** fabricate 30 goldens now — 11a's 10 synthetic seeds stand; real growth is operational.

## Constraints (AGENTS.md + eval-spec §10)

- `tools/rca/` must not import from `tools/eval/`. `report.py` and the redaction check may import from `tools/rca/` (e.g. `redact`).
- `report.py` and `check_goldens_redacted.py` must run with **no network**.
- No recomputation of metrics — parse 11a's results. No re-listing of redaction patterns — reuse `redact.py`.
- Never print secrets; the report is built from already-redacted results.
- Suite green: `pytest tests/eval -q` and `pytest tests/rca -q`.

## Acceptance criteria (eval-spec §11, the 11b items)

1. `report.py` renders a delta smaller than the metric's stdev as within-variance, not as a change (#10).
2. `--fail-under` exits non-zero when a threshold is breached and zero otherwise (#11).
3. The report lists every golden whose verdict changed relative to the baseline (#17), using the new `per_golden` section.
4. The report includes the category confusion matrix and flake precision/recall/F1 as rendered sections (#15, #16 surfaced).
5. `check_goldens_redacted.py` fails on a planted secret in a golden and passes on the clean seed set (#19).
6. `report.py` runs with no network and produces markdown + a machine JSON summary.
7. `eval.yml` is valid, triggers on the listed paths + `workflow_dispatch`, and uploads the results artifact.
8. Tests under `tests/eval/` cover the within-variance rule, `--fail-under` both directions, the changed-goldens diff, and the redaction check.
9. `pytest tests/eval -q` and `pytest tests/rca -q` green; acceptance-35 grep stays empty.

## Out of scope

- LLM-as-judge, root-cause/fix scoring (eval-spec §12).
- Actually collecting 30+ real goldens (operational; README documents it).

## Deliverable

`tools/eval/report.py` + `check_goldens_redacted.py` + the `per_golden` extension to `run_eval.py` + `.github/workflows/eval.yml` + `eval-baseline.json` + goldens `README.md` + `tests/eval/` additions, with `pytest tests/eval -q` and `pytest tests/rca -q` green. Show an example rendered report with a within-variance delta and a changed-golden line.
```
Short command to run it in Claude Code (from the repo root):

Read docs/cc-prompt-step11b-eval-report-ci.md and implement exactly that slice — reconcile with 11a (reuse EvalMetrics/results file; add per_golden; don't recompute). Follow AGENTS.md, add the tests it specifies, and make sure `pytest tests/eval -q` and `pytest tests/rca -q` are green before you finish.
```
