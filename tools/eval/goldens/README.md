# Eval goldens

Each golden is a directory with a captured, **redacted** `summary.json`, a
`label.yaml` (ground truth), and a `README.md` (one paragraph on the case).

## Current state (11a seed set)

10 **synthetic** seeds (`source: synthetic`) encoding the regressions fixed in
Steps 1–9b (terminal cause at the failed step, the uninformative-job → pipeline
compile regression, benign Docker pull output, and one representative per
category). They exercise the deterministic classifier and the report pipeline
without any real repo data.

## Growing the set (eval-spec §7.2) — operational, not done here

Target **30–50 real goldens** that cover the real failure distribution:

1. Capture from a real failed run with `python -m tools.rca.cli capture …`, then
   collect a `summary.json`. Every captured file must pass
   `python -m tools.eval.check_goldens_redacted` (reuses `redact.py`).
2. Add `label.yaml` from the **fix commit** — the true category, infra/code,
   flakiness, expected short-circuit, and the human root cause / acceptable
   fixes (judgement fields, unused by the deterministic metrics).
3. Mark `source: real`, set `captured_from_run` and `labelled_at`. Goldens older
   than 180 days surface in `stale_goldens` and should be re-reviewed.
4. Keep the distribution honest — do not over-index on one category.

The baseline (`tools/eval/eval-baseline.json`) is refreshed **deliberately** by a
maintainer, never auto-updated by CI.
