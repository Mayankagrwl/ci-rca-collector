# CI RCA Collector — agent rules

This repo implements **Phase 1 only** of `docs/ci-rca-collector-spec-v8.md`.

## Non-negotiables

1. Follow the spec's **build order** (§6). Do not implement later modules first.
2. Phase 1 is collection + rendering. **No LLM calls, no PR comments, no issue creation, no kubectl.**
3. `cleaner.py`, `drain_index.py`, `budget.py`, `redact.py`, `history.py` must **never** import GitHub-specific modules or mention `job_id` / `run_id`.
4. GitHub host is **not hardcoded**. All REST calls go through `tools/rca/github_api.py`, which reads host/token from env (see `config.py`).
5. Never log tokens. Never write tokens into `summary.json` / `summary.md`.
6. Collector must not fail the workflow: catch exceptions, emit partial summary, exit 0 unless `--strict`.
7. Keep dependencies minimal: `pydantic>=2`, `httpx`, `drain3`, `python-dateutil`. No PyGithub.

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

## What to implement now

Phase 1 action + CLI + tests/fixtures. Schema lives only in `tools/rca/models.py`.
