# Claude Code task — Step 8: measurement / telemetry persistence

## Context

`ci-rca-collector`. Read `AGENTS.md` and `docs/ci-rca-collector-spec-v8.md` first. Steps 1–7 are merged. The measurement **outputs already exist**: `outputs.py` emits `analyze-decision` (`called | skipped:<reason> | cached`), `diagnosis-grounded`, and `diagnosis-display-status`, and `analyze_decision(record)` is implemented. So this slice is **only the persistence + summarization** half: accumulate one telemetry record per run so you can see, over weeks, which rules are safe to skip the model on and which categories the model actually improves.

Do **only** what this document scopes.

## Goal

1. **Append one telemetry record per run** to a JSON-lines file in the history directory (which is already cached across runs via `actions/cache`, so records accumulate per repo+workflow).
2. **Add a `telemetry` CLI subcommand** that reads the JSONL and prints/writes a compact aggregate summary.
3. Keep it fully optional and non-fatal: telemetry never fails the workflow and is a no-op when the history dir is unavailable.

## What to record (one line per run)

A flat JSON object, already redacted (no raw log text, no tokens — only categorical fields and short ids):

- `ts` (UTC ISO8601), `fingerprint`, `fingerprint_coarse`
- `category`, `confidence`, `is_infra_vs_code`, `short_circuit`, `requires_analysis`
- `rule_id` (from `summary.diagnosis`), `terminal_cause_present` (bool), `suspected_stage`
- `recurrence` / `seen_count` (from history)
- `model_called` (bool), `analyze_decision` (called|skipped:<reason>|cached), `analysis_status` (internal), `display_status`, `grounded` (bool|null), `source` (ai|deterministic|hybrid|cached), `rca_confidence`
- `collector_version`, `prompt_version`

No free-text root cause / fix / log lines — this is metrics, not content. Run it through the existing redaction helper anyway as a belt-and-suspenders step.

## Where to change (verify against current code first)

- **`tools/rca/history.py` or a new `tools/rca/telemetry.py`** — a small `append_telemetry(history_dir, record: dict)` that writes one JSON line to `<history_dir>/telemetry.jsonl`, creating the dir if needed, capping the file (e.g. keep the last N lines, N≈5000) so it can't grow unbounded, and swallowing all IO errors. Keep it source-agnostic (no GitHub imports). `CacheHistoryStore(root)` already roots at the history dir — reuse that root.
- **`tools/rca/cli.py`**
  - In the `analyze` command (`_cmd_analyze`), after `write_analysis` / `write_analysis_github_output`, build the record from `summary` + `record` (+ `terminal_cause_present(summary)` from `diagnose`, and `analyze_decision(record)` from `outputs`) and call `append_telemetry(history_dir, record)`. Wrap in try/except; never let it fail the step.
  - Add a `telemetry` subcommand: `--history-dir` (default same as analyze), optional `--out` (write JSON/markdown summary) and `--format json|md`. It reads `telemetry.jsonl` and aggregates.
- **`action.yml`** — after the analyze step, no new required inputs. Telemetry writes into the existing `history-dir` that is already cached; the cache save at job end persists it. (Optional: a `write-telemetry` input, default `'true'`.)

## Aggregate summary (the `telemetry` subcommand)

Print/emit counts and rates over the JSONL:

- total runs; runs by `category`; runs by `analyze_decision`
- `% model_called`, `% grounded` (of model-called), `% skipped:deterministic_sufficient`, `% skipped:short_circuit`
- for each `category`: model-called count, grounded rate, and how often the model result was kept vs the deterministic card (source ai/hybrid vs deterministic)
- `% needs-review` (display_status)

This is what tells you which categories/rules are reliable enough to force into Tier-0 skip and which the model genuinely improves.

## Constraints (AGENTS.md)

- `history.py` (and any new `telemetry.py`) must not import GitHub modules or mention `job_id`/`run_id`. Build the record in `cli.py` (which may read summary/record) and pass a plain dict down.
- Never fail the workflow: all telemetry IO is best-effort; a write/read/parse error is logged and swallowed, exit 0.
- Metrics only — never persist raw log content, root-cause/fix prose, file contents, or tokens. Redact the record before writing.
- Bounded growth: cap `telemetry.jsonl` length; corrupt/partial lines are skipped on read.
- Backwards-compatible: no change to `summary.json`, existing outputs, or the analyze/collect contracts.

## Acceptance criteria

Add tests under `tests/rca/`:

1. **Append + read round-trip.** `append_telemetry` writes a valid JSON line with all expected fields; reading it back parses; a corrupt line is skipped, not fatal.
2. **No content leakage.** The record contains only the categorical/id fields — assert it has no `root_cause`/`suggested_fix`/window text keys, and that a secret placed in the summary does not appear in the telemetry line.
3. **Cap.** Writing more than N records keeps only the last N.
4. **Aggregate.** The `telemetry` subcommand over a small fixture JSONL reports correct totals, `% model_called`, `% grounded`, and per-category breakdown.
5. **Best-effort.** With an unwritable/missing history dir, `append_telemetry` is a no-op and analyze still exits 0.
6. Suite green: `pytest tests/rca -q`, and the acceptance-35 grep stays empty:
   `rg -n "job_id|run_id|github_api" tools/rca/cleaner.py tools/rca/drain_index.py tools/rca/budget.py tools/rca/redact.py tools/rca/history.py`

## Out of scope

- Any collection or gating change (Steps 1–7 already merged).
- Dashboards / external metrics sinks — a later, optional integration; JSONL in the cached history dir is the store here.
- Changing the already-emitted GitHub outputs.

## Deliverable

A focused diff over `cli.py`, `history.py` (or new `telemetry.py`), and optionally `action.yml`, plus tests, with `pytest tests/rca -q` green. Summarize what changed and show an example telemetry line and an example aggregate summary.
```
Short command to run it in Claude Code (from the repo root):

Read docs/cc-prompt-step8-measurement-telemetry.md and implement exactly that slice — nothing outside its scope. Follow AGENTS.md, add the tests it specifies, and make sure `pytest tests/rca -q` is green before you finish.
```
