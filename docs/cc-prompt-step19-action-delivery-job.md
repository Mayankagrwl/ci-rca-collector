# Claude Code task — Step 19: packaging (action inputs/outputs, a separate delivery job with write scopes, feedback and migration entry points)

## Context

`ci-rca-collector`. Read `AGENTS.md` and `docs/rca-delivery-spec-v1.3.md` (**§3, §11, §13, §14, §17 AC #18, #23, #27, #28, §18 rollout**) first.

**Prerequisite — stop and report if missing:** Step 18b merged. That means:

- the `deliver/feedback.py` and `deliver/incidents.py` modules;
- the `feedback` subcommand;
- `docs/examples/rca-feedback.yml` and `docs/rca-feedback.md`.

This is the step that makes Phase 3 usable from a consumer repo. Nothing new is decided at runtime; this step **wires what exists**.

Do **only** what this document scopes. Nothing is hardcoded to an organisation, repository, branch or host.

## Verified facts to build on (do not re-derive)

**`action.yml`** is a composite action with `mode: collect | train`. Its steps are:

1. setup-python
2. pip install
3. cache-keys
4. two `actions/cache@v4` steps (Drain3, history)
5. `Run collector`
6. `Report analysis` (the analyze subcommand)
7. `Write job summary`

All values reach scripts through `env:`, never inline `${{ }}` in `run:` bodies. Keep it that way.

**`.github/workflows/rca.yml`** is a reusable `workflow_call` workflow:

- top-level `permissions: actions: read, contents: read`;
- a single `collect` job that uses `acme/ci-rca-collector@v1` and uploads `rca/` as `rca-<run-id>` on failure;
- input `default-branch` defaults to `'main'` (hardcoded — fix below).

**CLI subcommands:**

| Command | Flags |
|---|---|
| `deliver` | `--summary`, `--analysis`, `--out`, `--live` / `--dry-run` / `--offline`, `--repo`, the `DeliveryInputs` flags (`--comment-on-pr`, `--comment-on-commit`, `--comment-on-branch-push`, `--create-issues`, `--issue-threshold`, `--allow-fork-issues`, `--confidence-threshold`, `--quiet-window-minutes`, `--platform-team`, `--default-notify`), `--migrate-history --history-dir --migrate-limit` |
| `feedback` | `--event`, `--live` / `--dry-run` / `--offline`, `--out` |

**Environment the CLI reads:**

- `RCA_DEFAULT_BRANCH` (Step 13 fallback);
- `RCA_SMTP_URL`, `RCA_CHAT_WEBHOOK_URL`, `RCA_CHAT_PAYLOAD_FIELD` (Step 18a — never argv);
- `RCA_RESOLVE_ASSOCIATIONS` (Step 18b);
- `RCA_WORKSPACE` / `GITHUB_WORKSPACE` (Step 17).

The `deliver` CLI appends `severity`, `suppressed-by`, `delivered-to`, `comment-url`, `issue-url` to `$GITHUB_OUTPUT`.

## Part 0 — Step 18b follow-up

**F1 — Private org members are refused by `/resolved`.** The handler trusts only `author_association ∈ {OWNER, MEMBER, COLLABORATOR}`. GitHub reports members whose org membership is **private** as `CONTRIBUTOR` (or `NONE`) in `issue_comment` payloads, so legitimate maintainers can't resolve anything.

**Fix, generically:**

- Add `GitHubClient.get_collaborator_permission(repo, username) -> str | None` (`GET repos/{repo}/collaborators/{username}/permission`, read-only). It returns `role_name` or `permission`, and **404 → `None`**.
- In `feedback`:
  - An allowed association passes as today.
  - Otherwise, query the permission and allow when it is in `RCA_RESOLVE_PERMISSIONS` (default `admin,maintain,write`).
  - `None` or any error means refuse, with a note. **Never allow on error.**
  - Bots are still refused first.
  - Dry-run may read; offline doesn't call and refuses non-allowed associations with a note.

**Tests:** private member (`CONTRIBUTOR` + `write`) → allowed; `CONTRIBUTOR` + `read` → refused; 404 → refused; API error → refused; `OWNER` makes no permission call.

## Deliverables

### 1. `action.yml` — new modes and inputs, same single action

**`mode` becomes `collect | train | deliver | feedback | migrate-history`.**

- **`collect` / `train`:** unchanged behaviour and unchanged outputs. The only additions are the `if:` guards that skip the new steps.
- **`deliver`:** runs only setup-python, pip install, and a new **Deliver** step. It **skips** cache-keys, both caches, collect, analyze and the job summary.
- **`feedback`:** setup-python, pip install, and a new **Feedback** step.
- **`migrate-history`:** setup-python, pip install, and a new **Migrate** step (reads `history-dir`, which the caller restored).

**New inputs** (v1.3 §13; all optional, defaults exactly as in the spec):

| Input | Default |
|---|---|
| `deliver` | `'false'` |
| `comment-on-pr` | `'true'` |
| `comment-on-commit` | `'true'` |
| `comment-on-branch-push` | `'false'` |
| `create-issues` | `'true'` |
| `issue-threshold` | `'3'` |
| `allow-fork-issues` | `'false'` |
| `confidence-threshold` | `'medium'` |
| `quiet-window-minutes` | `'60'` |
| `platform-team` | `''` |
| `default-notify` | `''` |
| `smtp-url` | `''` |
| `chat-webhook-url` | `''` |
| `chat-payload-field` | `''` |
| `default-branch` | `''` |
| `migrate-limit` | `'50'` |

Descriptions must say that `smtp-url` / `chat-webhook-url` should come from **secrets**, and that `deliver: false` means a dry run with zero GitHub writes.

**Deliver step** (`id: deliver`, `if: inputs.mode == 'deliver'`):

- `env:` carries:
  - every input;
  - `GITHUB_TOKEN`, `GITHUB_API_URL` / `RCA_GITHUB_API_URL` / `GITHUB_SERVER_URL`, with the same resolution as collect (AC #27: no hardcoded host);
  - `RCA_SMTP_URL`, `RCA_CHAT_WEBHOOK_URL`, `RCA_CHAT_PAYLOAD_FIELD`;
  - `RCA_DEFAULT_BRANCH`: `inputs.default-branch`, falling back to `github.event.repository.default_branch`. The API lookup still wins inside the tool; this is only the fallback.
  - `PYTHONPATH`, and the SSL variables exactly as collect.
- **First line of the script:** mask the two URLs when they are non-empty (`echo "::add-mask::$RCA_SMTP_URL"`). Secrets are auto-masked, but a caller might pass a plain string.
- `deliver == 'true'` → `--live`; anything else → `--dry-run`.
- Boolean and value inputs map onto the existing flags through a bash array. Values are **never** interpolated into the script text.
- **Never fail the step:** the CLI already exits 0; keep `fail-on-error` → `--strict`.
- Append `rca/delivery-preview.md` to `$GITHUB_STEP_SUMMARY` when `write-job-summary` is true, under a `## RCA delivery` heading.

**Feedback step:** `feedback --event "$GITHUB_EVENT_PATH"`, with `--live` when `deliver == 'true'`, else `--dry-run`. Append `rca/feedback-report.md` to the step summary.

**Migrate step:** `deliver --migrate-history --history-dir "$RCA_HISTORY_DIR" --migrate-limit "$RCA_MIGRATE_LIMIT"`, with `--live` only when `deliver == 'true'`.

**New outputs:**

| Output | Source |
|---|---|
| `severity` | `steps.deliver.outputs.*` |
| `suppressed-by` | `steps.deliver.outputs.*` |
| `delivered-to` | `steps.deliver.outputs.*` |
| `comment-url` | `steps.deliver.outputs.*` |
| `issue-url` | `steps.deliver.outputs.*` |

**Also fix these stale descriptions:**

- `short-circuit` should list `infra_widespread` as well.
- `history-backend` should say only `cache | none` are supported and that issues are delivery-owned (v1.3 §8.0). If `open_store` doesn't accept `issues` / `redis`, the description must not advertise them.
- `diagnosis-source` should list the actual values the code emits. Check `outputs.py`; don't guess.

### 2. `.github/workflows/rca.yml` — a separate delivery job

**Collect job:**

- Add **job-level** `permissions: { actions: read, contents: read }`. The top-level block stays read-only.
- Keep the artifact upload exactly as today: only when `inputs.conclusion == 'failure'`.

**New `deliver` job:**

- `needs: collect`
- `if: inputs.conclusion == 'failure' && needs.collect.result == 'success'`
- `timeout-minutes: 5`
- job-level permissions:

  ```yaml
  permissions:
    actions: read
    contents: write        # commit comments
    pull-requests: write
    issues: write
  ```

- **Steps:**
  1. `actions/download-artifact@v4` for `rca-${{ inputs.run-id }}` into `rca/`.
  2. `uses: acme/ci-rca-collector@v1` (the same ref as the collect job, with the same comment explaining the pin), `mode: deliver`, passing every delivery input.
- **Nothing** from the failing run is checked out, and no workspace code runs. The `rca/` artifact is data only. Model and log text are already escaped by the renderer (§11).

**New `workflow_call` inputs:**

- `deliver`: boolean, default `false`.
- Every §13 knob (strings with the spec's defaults).
- `default-branch` now defaults to `''`, not `'main'`. The action falls back to `github.event.repository.default_branch`, then the API.

**New `workflow_call` secrets** (`required: false`): `smtp-url`, `chat-webhook-url`, passed to the action inputs.

**Outputs:** expose `severity`, `suppressed-by`, `delivered-to`, `comment-url`, `issue-url` from the deliver job.

**Permissions rule, documented in a comment at the top of the file:** a reusable workflow can only **narrow** the caller's token. The **caller** job must grant `contents: write`, `pull-requests: write` and `issues: write` for delivery to be able to write. Otherwise delivery records `403 … needs <scope>` and still exits 0.

### 3. Consumer examples (documentation, not active here)

- **`docs/examples/rca-consumer.yml`:** `on: workflow_run` (types `completed`, `workflows: [<your CI>]`), calling `rca.yml` with `conclusion: ${{ github.event.workflow_run.conclusion }}`, `deliver: false` (rollout stage 1), secrets wired, and the caller permissions block.
  - Comment the fork-safety reason for `workflow_run`: it runs in the base repo, never in the fork's context.
  - Comment that nothing from the PR head is ever checked out.
- **Update `docs/examples/rca-feedback.yml`** to `uses: acme/ci-rca-collector@v1` with `mode: feedback` and `deliver: 'true'`, instead of checking out the tool repository. Keep the `if:` pre-filter and the security comments.
- **`docs/examples/rca-migrate.yml`:** `workflow_dispatch` with inputs `workflow-name` and `live` (boolean, default false).
  - Restore the history cache with the **same key scheme as `rca.yml`** (`rca-history-<sanitised repo>-<sanitised workflow>`). Reuse the sanitize snippet, don't reinvent it.
  - Then run the action with `mode: migrate-history`.
  - Use `issues: write` only.
  - Note that each workflow has its own history cache, so run it once per workflow; `--migrate-limit` continues on re-runs.

### 4. `self-test.yml` — prove packaging offline

Add a job that installs the action's requirements and runs, against one committed golden:

```
python -m tools.rca.cli deliver --summary <golden>/summary.json --out rca-selftest --dry-run --offline
```

- Assert that `rca-selftest/delivery-preview.md` exists and that `$GITHUB_OUTPUT` received `severity=`.
- It needs no write permissions and makes no network calls. Use `permissions: contents: read`.

### 5. Documentation

- **`AGENTS.md`** gets a short Phase 3 packaging section:
  - collect stays read-only (AC #28);
  - write scopes exist only on the `deliver` job and the feedback/migrate workflows;
  - `deliver: false` means a dry run;
  - secrets arrive via env only;
  - every value reaches `run:` through `env:`.
- **README** gets a "Delivery (Phase 3)" section:
  - the rollout stages from v1.3 §18;
  - how to switch `deliver: true`;
  - the caller permissions;
  - the three example files.
- **Deferred list** (README or spec note):
  - inferred resolution (fingerprint gone and pipeline green);
  - populating `run.concurrent_*` in collect (the widespread rule precedes code rules, so it needs its own design);
  - auto-fix PRs and the other v1.3 §19 items.

## Constraints

- **Collect and train behaviour, outputs and permissions are unchanged.** Existing consumers who don't pass `mode: deliver` see no difference.
- No model, log, PR-title or branch text in any `run:` script. Only `env:` indirection, and only for inputs (AC #18).
- No new Python dependencies. `tools/rca` code changes are limited to Part 0 and anything strictly needed for the CLI wiring.
- The acceptance-35 grep stays empty. `tools/rca` never imports `tools/eval`.

## Acceptance criteria

Tests under `tests/` parse the YAML with `pyyaml`. No GitHub needed.

1. **`action.yml`:**
   - every v1.3 §13 input exists with the spec default;
   - the outputs `severity`, `suppressed-by`, `delivered-to`, `comment-url`, `issue-url` exist and reference `steps.deliver.outputs`;
   - every collect/train step is guarded so it doesn't run in `deliver` / `feedback` / `migrate-history` modes;
   - collect's steps and outputs are otherwise byte-identical to before (diff the collect/train steps).
2. **Injection guard (AC #18):**
   - no `run:` body in `action.yml`, `rca.yml` or the example workflows contains `${{`;
   - the only exception is the `if:` expressions, which are not scripts.
3. **Secrets:**
   - `smtp-url` / `chat-webhook-url` reach the script only as `RCA_SMTP_URL` / `RCA_CHAT_WEBHOOK_URL` env vars, and are masked when non-empty;
   - no step passes them as CLI arguments.
4. **`deliver` mapping:** a small bash harness sources the Deliver step's script with stub env.
   - `deliver: 'true'` → argv contains `--live` and never `--dry-run`;
   - `'false'` or empty → `--dry-run` and never `--live`;
   - each boolean and value input maps to its flag.
5. **`rca.yml` (AC #28):**
   - the collect job's permissions are exactly `actions: read, contents: read`;
   - the deliver job has exactly the four scopes above;
   - the top level is read-only;
   - the deliver job `needs: collect`, downloads `rca-<run-id>`, and uses `mode: deliver`;
   - `default-branch` defaults to `''`.
6. **Examples:**
   - `rca-consumer.yml` uses `workflow_run` and `deliver: false`;
   - `rca-feedback.yml` uses the action with `mode: feedback` and no checkout of PR code;
   - `rca-migrate.yml` restores the history cache with the same key scheme as `rca.yml`.
7. **Self-test:** running the offline dry-run command from deliverable 4 locally produces the preview and the `severity` output.
8. **Part 0 F1:** the tests listed above.
9. **Suites and checks:** `pytest tests/rca -q` and `pytest tests/eval -q` are green, the acceptance-35 grep is empty, and `run_eval --no-llm` stays at 1.0.

## Out of scope

- Inferred resolution.
- Collect-side `concurrent_*`.
- Publishing or tagging a release.
- Changing the `acme/ci-rca-collector@v1` pin to your organisation. Leave `acme` and add a clear `# replace with your org` comment; that's a consumer choice.

## Deliverable

`action.yml`, `.github/workflows/rca.yml`, `self-test.yml`, the three example workflows, the AGENTS.md and README sections, the Part 0 fix, and tests, with both suites green. Show:

- `git diff --stat`;
- the new Deliver step;
- the deliver job from `rca.yml`;
- the argv produced by the harness for `deliver: 'true'` and `'false'`.
