# CI RCA Collector

A GitHub Action that collects the context of a failed GitHub Actions run, reaches a
deterministic diagnosis (optionally refined by an LLM), and — in Phase 3 — delivers it where
people already look: a sticky PR or commit comment, a recurring-failure issue, and optional
email / chat notifications.

- `action.yml` — the composite action (`mode: collect | train | deliver | feedback | migrate-history`).
- `.github/workflows/rca.yml` — the reusable workflow: a read-only `collect` job and a separate,
  write-scoped `deliver` job.
- Specs: `docs/ci-rca-collector-spec-v8.md` (collect), `docs/rca-delivery-spec-v1.3.md` (delivery).
- Agent rules: `AGENTS.md`.

## Delivery (Phase 3)

Delivery runs in its own job after collect, consuming only the uploaded `rca/` artifact. Nothing
from the failing run is checked out, and model / log text is escaped before posting.

### Rollout (delivery spec v1.3 §18)

1. **Dry run (default).** `deliver: false` makes zero GitHub writes. Each failure's job summary
   shows `rca/delivery-preview.md` — the exact comment, issue action and notifications that
   *would* happen. Read these for about a week.
2. **Turn on delivery.** Set `deliver: true` in the caller (`with: deliver: true`). Comments,
   issues and labels are now written; notifications go out only if `smtp-url` /
   `chat-webhook-url` secrets are configured.
3. **Tune.** Adjust `confidence-threshold`, `issue-threshold`, `quiet-window-minutes`,
   `platform-team`, `default-notify`, and add a `CODEOWNERS` for ownership.

### Caller permissions

A reusable workflow can only narrow the caller's token. For delivery to write, the **caller** must
grant:

```yaml
permissions:
  actions: read
  contents: write        # commit comments
  pull-requests: write
  issues: write
```

Without them delivery records `403 … needs <scope>` and still exits 0. Collect runs read-only
(`actions: read`, `contents: read`) regardless.

### Examples

- [`docs/examples/rca-consumer.yml`](docs/examples/rca-consumer.yml) — `workflow_run` trigger
  calling `rca.yml` (starts at `deliver: false`).
- [`docs/examples/rca-feedback.yml`](docs/examples/rca-feedback.yml) — handles `/resolved`
  comments (`mode: feedback`); see [`docs/rca-feedback.md`](docs/rca-feedback.md).
- [`docs/examples/rca-migrate.yml`](docs/examples/rca-migrate.yml) — one-shot migration of the
  cache history into issues (`mode: migrate-history`), once per workflow.

### Deferred

- **Inferred resolution** — closing an issue when the fingerprint is gone and the pipeline is
  green needs a success-path delivery trigger.
- **`run.concurrent_*` in collect** — the widespread-failure rule runs before the code rules, so
  populating it needs its own design.
- **Auto-fix PRs** and the other items deferred in delivery spec v1.3 §19.
