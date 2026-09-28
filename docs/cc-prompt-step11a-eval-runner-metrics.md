# Claude Code task — Step 11a: offline eval harness core (labels + metrics + runner + seed goldens)

## Context

`ci-rca-collector`. Read `AGENTS.md`, `docs/ci-rca-collector-spec-v8.md`, and `docs/rca-eval-spec-v1.md` (§2, §3, §7, §8, §10, §11) first. Steps 1–10 are merged: Step 10 added production grounding + `AnalysisRecord.grounding` (`GroundingResult`), `.validation_telemetry` (`ValidationTelemetry`), `.call_telemetry` (`CallTelemetry`).

This is the **offline** half of the eval spec, part 1 of 2. **11a = labels + metrics + runner + seed goldens.** **11b (later) = `report.py` + baseline diffing + `eval.yml` + growing the golden set.** Do only 11a.

**RECONCILE, do not rebuild.** The analysis path already exists — the runner *replays goldens through it*, it does not reimplement analysis or grounding. Reuse:
- `analyze.analyze_summary(summary, ...)` — the Phase 2 analysis path; returns an `AnalysisRecord` already carrying `grounding` / `validation_telemetry` / `call_telemetry` (Step 10). Read metrics off those — do **not** recompute grounding/schema/cost.
- `diagnose.diagnose(summary)` + `diagnose.apply_verdict(summary, verdict)` — the deterministic rule engine, which runs on the `Summary` object. Re-running it on a golden's `summary.json` reflects the **current** `config.py`/`diagnose.py` rules, so `--no-llm` genuinely tests rule changes without re-collecting.
- `models.Summary` (load golden `summary.json`), `redact.py` (golden capture), `config.PROMPT_VERSION` / `config.COLLECTOR_VERSION` (provenance).

Dependency direction (eval-spec §10): `tools/eval/` may import from `tools/rca/`; `tools/rca/` must **never** import from `tools/eval/`.

## Deliverables (11a only)

Create `tools/eval/__init__.py`, `labels.py`, `metrics.py`, `run_eval.py`, and seed goldens under `tools/eval/goldens/`.

### 1. `tools/eval/labels.py` — label schema + loader (eval-spec §7.1)

- Pydantic `GoldenLabel` model with exactly the §7.1 fields: `schema_version`, `slug`, `source` (`real|synthetic`), `captured_from_run` (int|None), `captured_at` (date), `true_category` (must be one of the Stage-5 categories — validate against the real category set), `is_infra_vs_code` (`infra|code`), `is_flaky` (bool), `expect_short_circuit` (`infra_runner|infra_widespread|flake_same_sha_passed|null`), plus the judgement-only fields recorded but unused here (`root_cause`, `acceptable_fixes`, `labelled_by`, `labelled_at`, `notes`).
- `load_label(path) -> GoldenLabel` and `load_goldens(dir) -> list[(slug, Summary, GoldenLabel, Path)]`: read `label.yaml` + `summary.json` per golden dir. **A malformed `label.yaml` fails loudly and names the file** (eval-spec §11.14) — never skipped silently.
- Use `pyyaml` (add to `requirements.txt` if not present) and `Summary.model_validate_json`.

### 2. `tools/eval/metrics.py` — aggregation (eval-spec §8.2)

- `Stat` (mean, stdev, min, max) and `ClassificationStat` (precision, recall, f1) Pydantic models.
- `EvalMetrics` per §8.2: `n_goldens`, `n_runs`, `category_accuracy: Stat`, a **confusion matrix** for category (§8.2 — full matrix, not just a scalar; e.g. `category_confusion: dict[str, dict[str, int]]`), `infra_vs_code_accuracy: Stat`, `flake_detection: ClassificationStat` (**precision/recall/F1**, not accuracy — minority class, §8.2), `short_circuit_accuracy: Stat`, `grounding_rate: Stat`, `fully_grounded_share: Stat`, `schema_first_try_rate: Stat`, `repair_success_rate: Stat`, `fallback_rate: Stat`, `mean_prompt_tokens: Stat`, `mean_latency_ms: Stat`, `short_circuit_share: Stat`, `median_golden_age_days: int`, `stale_goldens: list[str]`.
- **Where each metric comes from (reconcile — read existing fields):**
  - category / infra_vs_code / flake / short_circuit → compare the **re-run** `diagnose.diagnose(summary)` verdict (category, is_infra_vs_code, is_flaky, short_circuit) against the label. These are deterministic → the same every run → `Stat` with stdev 0.
  - grounding_rate / fully_grounded_share → from each run's `AnalysisRecord.grounding` (`GroundingResult.grounding_rate`, `.grounded`).
  - schema_first_try_rate / repair_success_rate / fallback_rate → from `AnalysisRecord.validation_telemetry`.
  - mean_prompt_tokens / mean_latency_ms / short_circuit_share → from `AnalysisRecord.call_telemetry`.
  - median_golden_age_days / stale_goldens → from label `labelled_at`; **stale = older than 180 days** (eval-spec §7.3, §11.13).
- Helpers: `stat(values) -> Stat`, `classification_stat(y_true, y_pred) -> ClassificationStat`, `confusion(y_true, y_pred) -> dict`. Standard library only (no numpy).

### 3. `tools/eval/run_eval.py` — the runner (eval-spec §8.1)

CLI:
```
python -m tools.eval.run_eval --goldens tools/eval/goldens --runs 3 --out eval-results.json [--filter category=dependency] [--no-llm]
```
- For each golden: load `summary.json` + label; **re-run `diagnose.diagnose` for the deterministic metrics** (reflects current rules); if **not** `--no-llm`, run `analyze.analyze_summary(summary, ...)` `--runs` times, collecting each `AnalysisRecord`.
- **`--runs` default 3; reject `runs < 2` when the model runs** (eval-spec §8.1 — a single run has unknown variance), but allow `runs == 1` under `--no-llm` (deterministic). Report mean **and** stdev for every metric.
- `--no-llm` must complete with **no network** and evaluate only the deterministic classification path (eval-spec §11.12).
- `--filter key=value` narrows goldens by label field (e.g. `category=dependency`).
- Write `eval-results.json` with the full `EvalMetrics` **plus provenance** (eval-spec §10): model/endpoint id (from env, redacted — never the key), `PROMPT_VERSION`, `COLLECTOR_VERSION`, git SHA (`git rev-parse HEAD`, tolerate absence), the `--runs` value, and any random seed used. Determinism: seed anything random and record it.
- Never leak secrets into the results file; run text fields through `redact.py` where they could carry captured content.

### 4. Seed goldens (eval-spec §7.2 — seed ~10, mark `source: synthetic`)

Under `tools/eval/goldens/<slug>/` create `summary.json` + `label.yaml` + `README.md` (one paragraph, §7.2) for ~10 cases, **reusing the fixtures already built across Steps 1–9b** so the golden set encodes the regressions we fixed:
- `artifactory-version-exists` (terminal cause at failed step → dependency/publish; expect deterministic).
- `java-compile-in-pipeline` (uninformative job, compile error only in pipeline logs → `compile`; the Step 9 regression).
- `docker-already-exists-benign` (benign pull line must NOT drive the verdict).
- plus representative one-per-category: `infra-runner` (expect_short_circuit: infra_runner), `flake-same-sha` (is_flaky, flake_same_sha_passed), `npm-eresolve` (dependency), `timeout`, `oom`, `test-failure-junit`, `image-pull`.
- Every golden's `summary.json` must be **redacted** (pass through `redact.py`); no golden may contain a value matching a `redact.py` secret pattern (eval-spec §11.19 — 11b adds the pre-commit check; for 11a just ensure the seeds are clean).

### Tests (`tests/eval/` — new)

Map eval-spec §11 items that 11a covers:
- `--runs 3` reports mean and stdev for every metric (#9).
- `--no-llm` completes with no network and reports classification metrics only (#12).
- A golden older than 180 days appears in `stale_goldens` (#13).
- A malformed `label.yaml` fails the run loudly and names the file (#14).
- Category metrics include a full confusion matrix (#15).
- Flake detection reports precision/recall/F1 (#16).
- `eval-results.json` records model, prompt hash, collector version, git SHA (#18).
- Deterministic metrics are stable across runs (stdev 0) while LLM metrics vary.

## Constraints (AGENTS.md + eval-spec §10)

- `tools/rca/` must not import from `tools/eval/`. `tools/eval/` may import from `tools/rca/` but must not reach into private internals it shouldn't — use the public functions above.
- `--no-llm` and deterministic paths must be reproducible and network-free.
- No hardcoded expected values baked into the runner — truth comes from the labels.
- Do not modify `tools/rca/` production code in this slice except a `requirements.txt` add (pyyaml) if needed. If a golden reveals a real collector bug, note it, don't fix it here.
- Suite green: `pytest tests/rca -q` (unchanged) and `pytest tests/eval -q` (new). Acceptance-35 grep stays empty.

## Out of scope (11b)

- `report.py`, baseline diffing, within-variance rendering, `--fail-under` (eval-spec §8.3).
- `.github/workflows/eval.yml` (§9).
- The redaction pre-commit check (§11.19) and growing goldens to 30+.

## Deliverable

`tools/eval/{__init__,labels,metrics,run_eval}.py` + ~10 seed goldens + `tests/eval/`, with `pytest tests/eval -q` green and `python -m tools.eval.run_eval --goldens tools/eval/goldens --no-llm --out /tmp/eval.json` producing a valid results file offline. Summarize what was reused from `tools/rca/` vs newly added, and show the deterministic metrics + one golden's confusion-matrix contribution.
```
Short command to run it in Claude Code (from the repo root):

Read docs/cc-prompt-step11a-eval-runner-metrics.md and implement exactly that slice — reconcile with existing code (replay through analyze_summary/diagnose; read grounding/telemetry off AnalysisRecord). Follow AGENTS.md, add the tests it specifies, and make sure `pytest tests/eval -q` and `pytest tests/rca -q` are green before you finish.
```
