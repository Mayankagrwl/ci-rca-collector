# Claude Code task — Step 19b: packaging fixes found in the Step 19 review

## Context

`ci-rca-collector`. Step 19 is merged (the `action.yml` modes deliver/feedback/migrate-history, the deliver job in `rca.yml`, `docs/examples/*`, `tests/rca/test_packaging_step19.py`).

**Stop and report if it is missing.**

This is a small, focused patch. Fix exactly these five items, add tests, and change nothing else.

## Fixes

### 1. Delivery can write with the org PAT instead of the job token (security)

`config_host.resolve_github_token()` order is `RCA_GITHUB_TOKEN` → `COMMON_ACTIONS_PAT` → `GITHUB_TOKEN` → `GH_TOKEN`. The Deliver, Feedback and Migrate steps pass both `GITHUB_TOKEN: inputs.github-token` and `COMMON_ACTIONS_PAT: env.COMMON_ACTIONS_PAT`.

When a consumer has `COMMON_ACTIONS_PAT` in the environment (common on GHES for action mirrors), every comment, issue and label is written with that PAT. That has two effects:

- The job's `permissions:` block no longer limits what delivery can do. This defeats v1.3 §3 ("a bug in delivery must not be able to push").
- Posts appear as the PAT's user, and the `github-token` input is silently ignored.

**Fix (in `action.yml` only, for the three write modes):**

- Pass `RCA_GITHUB_TOKEN: ${{ inputs.github-token }}` so the input always wins.
- Stop passing `COMMON_ACTIONS_PAT` to these three steps.
- Leave the collect/train steps exactly as they are.

Update the `github-token` input description: "delivery modes write with this token (default: the job's `GITHUB_TOKEN`, limited by the job's `permissions:`)."

**Test:** parse `action.yml`. The three write-mode steps set `RCA_GITHUB_TOKEN` from `inputs.github-token` and do **not** reference `COMMON_ACTIONS_PAT`. The collect step is unchanged.

### 2. The migration example can corrupt another workflow's history cache

`docs/examples/rca-migrate.yml` uses `actions/cache@v4` with the prefix restore key `rca-history-<repo>-`. Two things go wrong:

- When the exact key misses, it restores **another workflow's** history.
- The post-job save then stores that history under **this** workflow's exact key, poisoning the cache that `rca.yml`'s collect job restores next time.

**Fix:**

- Use `actions/cache/restore@v4` (restore only, never saves).
- Use the **exact** key and no `restore-keys`.
- Give it `id: restore`, and run the Migrate step only `if: steps.restore.outputs.cache-hit == 'true'`.
- Add a final step that writes "no history cache for <workflow>; nothing to migrate" to the step summary on a miss. The workflow name must reach it via `env:`, never `${{ }}` in `run:`.
- Add a comment: on GHES mirrors without `actions/cache/restore`, use `actions/cache@v4` with `lookup-only` removed **and** no `restore-keys`, and accept the save of an unchanged cache.

**Test:** parse the example. It uses `actions/cache/restore`, has no `restore-keys`, and the Migrate step is gated on `cache-hit`.

### 3. Re-running the RCA workflow can fail on the artifact upload

`actions/upload-artifact@v4` refuses to upload a second artifact with the same name in one run, and re-runs of failed jobs can hit that. The deliver job now depends on `rca-<run-id>`.

**Fix:** add `overwrite: true` to the collect job's upload step in `rca.yml`.

**Test:** the upload step has `overwrite: true`.

### 4. `/resolved` permission fallback ignores custom repository roles

`get_collaborator_permission` returns `role_name or permission`. With a custom role, `role_name` is the custom name (for example `dev-write`), while `permission` is the base level (`write`). The custom-role user is refused even though they have write access.

**Fix:** return both values, for example as a small tuple or dataclass. Allow when **either** `role_name` or `permission` is in `RCA_RESOLVE_PERMISSIONS`. Show both in the feedback report.

**Tests:**

- `role_name="dev-write", permission="write"` → allowed.
- `role_name="triage", permission="read"` → refused.
- 404 and errors → refused, as today.

### 5. `eval.yml` still interpolates `${{ }}` inside `run:`

The `Run eval` step contains `RUNS="${{ github.event.inputs.runs || '3' }}"`. A `workflow_dispatch` input lands in a shell script, violating the AC #18 rule that every other workflow follows.

**Fix:**

- Move it to `env: RCA_EVAL_RUNS: ${{ github.event.inputs.runs || '3' }}`.
- In the script, validate it as a positive integer (`[[ "$RCA_EVAL_RUNS" =~ ^[0-9]+$ ]]`, else use 3).

**Test:** extend the Step 19 injection-guard test to cover **every** file in `.github/workflows/` and `docs/examples/`, not a fixed list, and assert no `run:` body contains `${{`.

## Constraints

- No behaviour change for collect or train.
- No changes to `tools/rca` other than item 4.
- No new dependencies.

## Acceptance criteria

1. The tests for items 1–5 above pass.
2. `pytest tests -q` is green, the acceptance-35 grep is empty, and `run_eval --no-llm` stays at 1.0.
3. The offline self-test still passes locally:

   ```
   python -m tools.rca.cli deliver --summary tools/eval/goldens/artifactory-version-exists/summary.json --out <tmp> --dry-run --offline
   ```

   It writes `delivery-preview.md` and `severity=` to `$GITHUB_OUTPUT`, and leaves the golden untouched.

## Deliverable

A focused diff over `action.yml`, `.github/workflows/rca.yml`, `.github/workflows/eval.yml`, `docs/examples/rca-migrate.yml`, `tools/rca/github_api.py`, the feedback permission check, and tests. Show `git diff --stat` and the three write-mode `env:` blocks.
