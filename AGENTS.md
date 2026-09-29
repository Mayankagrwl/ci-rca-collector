# CI RCA Collector — agent rules

## Phases

| Phase | Scope | Status |
|---|---|---|
| 1 — collect | Collection + deterministic diagnosis + rendering (`docs/ci-rca-collector-spec-v8.md`) | shipped |
| 2 — analyze | STGPT analysis of the collected summary | shipped |
| eval | Offline eval harness under `tools/eval/` (`docs/rca-eval-spec-v1.md`) | shipped |
| 3 — delivery | PR / commit comments, issues, labels, reactions (`docs/rca-delivery-spec-v1.3.md`) | in progress |

## Build discipline

1. One slice per step prompt under `docs/cc-prompt-step*.md`. Implement exactly that slice; do not build later steps early.
2. Keep `pytest tests/rca -q` and `pytest tests/eval -q` green.
3. The acceptance-35 grep stays empty:
   `rg -n "job_id|run_id|github_api" tools/rca/cleaner.py tools/rca/drain_index.py tools/rca/budget.py tools/rca/redact.py tools/rca/history.py`
4. Schema lives only in `tools/rca/models.py`.

## Non-negotiables

1. **Permissions.** Collect stays `actions: read`, `contents: read`. GitHub writes (comments, issues, labels, reactions) happen **only** in `tools/rca/deliver/` via `github_api.py`, **only** in the separate delivery job, and **only** when `deliver` is enabled.
2. **Source-agnostic modules** never import GitHub-specific modules and never mention `job_id` / `run_id`: `cleaner.py`, `drain_index.py`, `budget.py`, `redact.py`, `history.py`, `extract.py`, `classify.py`, `pipeline_logs.py`.
3. **All GitHub HTTP goes through `tools/rca/github_api.py`**, which reads host/token from env (see `config.py`). Never hardcode `api.github.com`.
4. `tools/rca/` never imports `tools/eval/`.
5. Never print rule ids (`R<n>`) in any user-facing channel (summary, card, action outputs, comments, issues).
6. Never log tokens. Never write tokens into `summary.json` / `summary.md` or any delivered text.
7. Never fail the workflow: catch exceptions, emit a partial summary, exit 0 unless `--strict`.
8. Nothing is hardcoded to a project, message, or vendor — rules are table- or structure-driven (`config.py`).
9. Dependencies: `pydantic>=2`, `httpx`, `drain3`, `python-dateutil`, `pyyaml`. No PyGithub.

## Phase 3 packaging (action.yml / rca.yml)

- **Collect stays read-only** (AC #28): the collect job and `mode: collect | train` run with `actions: read`, `contents: read` only.
- **Write scopes exist only** on the separate `deliver` job in `.github/workflows/rca.yml` and in the consumer feedback / migrate workflows (`docs/examples/`).
- **`deliver: false` (the default) is a dry run**: zero GitHub writes; `rca/delivery-preview.md` shows exactly what would be posted.
- **Secrets arrive via env only**: `smtp-url` / `chat-webhook-url` → `RCA_SMTP_URL` / `RCA_CHAT_WEBHOOK_URL`, masked; never CLI arguments.
- **Every value reaches `run:` through `env:`** — never `${{ }}` inside a `run:` body (injection guard, AC #18).

## GitHub.com vs GitHub Enterprise

Resolve API base in this order:

1. `--api-url` CLI flag
2. `RCA_GITHUB_API_URL`
3. `GITHUB_API_URL` (Actions sets this automatically)
4. Derive from `GITHUB_SERVER_URL` / `GH_HOST` / `RCA_GITHUB_HOST`
5. Default `https://api.github.com`

Enterprise rule: if the server host is **not** `github.com`, API base is `{server}/api/v3`.

Token resolution order:

1. `--token` CLI flag
2. `RCA_GITHUB_TOKEN`
3. `COMMON_ACTIONS_PAT`
4. `GITHUB_TOKEN`
5. `GH_TOKEN`
