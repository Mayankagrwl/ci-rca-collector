# Claude Code task — Step 12: GitHub write layer (Phase 3 prerequisite)

## Context

`ci-rca-collector`. Read `AGENTS.md`, `docs/ci-rca-collector-spec-v8.md`, and
`docs/rca-delivery-spec-v1.2.md` (**§0 Prerequisites**, §1.2, §14) first. Steps 1–11b are merged.

This is **Step 12 of Phase 3 (Delivery)** and it is a pure prerequisite: `github_api.py` today is
**read-only and cannot send a request body**, so no delivery channel can be built until this lands.

**This slice adds capability only. It changes no existing behaviour, wires nothing into `collect`,
and writes nothing to GitHub at runtime.** Do not create `tools/rca/deliver/`, do not add the
`deliver` CLI subcommand, do not touch `action.yml` or any workflow — those are Steps 13+.

## Verified starting state (do not re-derive)

- `GitHubClient.__init__(*, api_url=None, token=None, timeout=_DEFAULT_TIMEOUT, transport=None, sleep=time.sleep)`
  — `transport` accepts an `httpx.MockTransport` and `sleep` is injectable, so everything here is
  testable offline with no network and no real sleeping.
- `_request(self, method: str, url: str, *, follow_redirects: bool = False, headers: dict[str,str] | None = None) -> httpx.Response`
  — **no body parameter.** Retries on timeout / connect error / 5xx via `self._sleep(2**attempt)` up
  to `_MAX_ATTEMPTS`; handles 403 (rate-limit exhausted → raise; `retry-after` → sleep once and
  retry; otherwise raise `GitHubAPIError(status_code=403)`); calls `_record_rate_headers`.
- All existing methods are GET: `get_run`, `list_jobs`, `list_runs`, `compare`, `get_pull`,
  `list_commit_pulls`, `list_artifacts`, `download_artifact_zip`, `get_file`, `list_commit_files`,
  `get_job_log`. Typical shape: `response = self._request("GET", f"repos/{repo}/...")` then a
  `response.status_code >= 400` check.
- `_next_link(link_header)` exists for pagination — reuse it for the new list methods.
- `GitHubAPIError(message, *, status_code=None)`.
- Enterprise base URL is already handled by `base_url` in the constructor — **never** hardcode
  `api.github.com` in a new method.

## Goal

### 1. Body support in `_request`

Add an optional JSON body (e.g. `json: Any | None = None`) passed through to
`self._client.request(...)`. Omit the kwarg entirely when it is `None` so GET behaviour is
byte-identical to today.

### 2. Retry safety for non-idempotent writes (important)

The existing retry loop retries on **timeout**. For a `POST` that creates a comment or issue, a
timeout may mean *the server already created the resource* — a blind retry produces duplicates.

Add an explicit idempotency control to `_request` (for example `retry_on_timeout: bool = True`, or
an `idempotent: bool` flag) and have the **creating** methods (`create_issue_comment`,
`create_commit_comment`, `create_issue`, `create_reaction`) opt **out** of timeout retries. Keep 5xx
and `retry-after` handling as-is for all methods. Document the reasoning in a short comment so it is
not "optimised" away later.

### 3. New read method

```
get_repo(repo) -> dict[str, Any]          # GET /repos/{repo}; delivery reads default_branch
```

### 4. New write / lookup methods

All through `_request`, all honouring the existing host/token resolution and rate-limit handling:

```
list_issue_comments(repo, issue_number)            -> list[dict]   # paginated; PR comments live here
create_issue_comment(repo, issue_number, body)     -> dict
update_issue_comment(repo, comment_id, body)       -> dict         # PATCH /repos/{repo}/issues/comments/{id}
list_commit_comments(repo, sha)                    -> list[dict]   # paginated
create_commit_comment(repo, sha, body)             -> dict
update_commit_comment(repo, comment_id, body)      -> dict
list_issues(repo, labels=None, state="open")       -> list[dict]   # paginated
create_issue(repo, title, body, labels=None, assignees=None) -> dict
update_issue(repo, number, **fields)               -> dict         # body / state / labels
add_labels(repo, issue_number, labels)             -> dict
create_reaction(repo, comment_id, content)         -> dict         # best-effort; see below
```

Notes:
- Paginated list methods follow `_next_link` like the existing ones, with a sane page cap.
- `create_reaction` targets an issue **comment** reaction and needs the reactions Accept header;
  it must be best-effort — on any non-2xx, return `{}` / `None` rather than raising, because the
  spec says reactions must never block delivery.
- A 4xx other than 403 should surface as a `GitHubAPIError` carrying `status_code` (mirror how the
  existing GET methods handle `>= 400`), so the caller can record it.

### 5. Error propagation (do not swallow)

Keep raising `GitHubAPIError` with `status_code` set on 403 / 4xx (except the best-effort reaction).
Delivery — not `github_api.py` — is responsible for catching it and recording `delivery.errors`
(v1.2 §14, AC #22). Do not add try/except that hides failures here.

### 6. Timeout

Per-call timeout stays constructor-driven. Do not change `_DEFAULT_TIMEOUT`; delivery will construct
its own client with `timeout=15` later (v1.2 §14). Just make sure nothing in the new code pins a
different timeout.

## Constraints (AGENTS.md)

- `github_api.py` remains the **only** module performing GitHub HTTP. Do not add HTTP to
  `deliver/`-to-be, `history.py`, or any source-agnostic module.
- `cleaner.py`, `drain_index.py`, `budget.py`, `redact.py`, `history.py`, `extract.py`,
  `classify.py`, `pipeline_logs.py` stay GitHub-agnostic — untouched by this slice.
- The collector must never fail the workflow; this slice adds no new call sites, so collect
  behaviour must be **provably unchanged**.
- No hardcoded `api.github.com`. No secrets in logs or error messages (existing redaction/logging
  discipline applies).

## Acceptance criteria

Add tests under `tests/rca/` using `httpx.MockTransport` and an injected `sleep` (no network, no
real delays):

1. **GET unchanged.** An existing GET method's request (URL, headers, no body) is byte-identical to
   before the change — add a characterisation test asserting no `json` body is sent for GETs.
2. `get_repo` returns the parsed payload; `default_branch` is readable from it.
3. Each new write method issues the correct **method + path + JSON body** (assert against the mock
   transport's captured request).
4. **Duplicate-safety:** a `create_*` call whose first attempt times out does **not** retry (exactly
   one request is issued, and the error propagates); a GET that times out still retries as before.
5. 5xx still retries and then raises `GitHubAPIError` with `status_code`.
6. 403 raises `GitHubAPIError(status_code=403)` and is **not** swallowed.
7. `create_reaction` on a non-2xx returns empty/None instead of raising.
8. Paginated list methods follow `_next_link` across two pages and concatenate results.
9. Enterprise: a client constructed with a GHES `api_url` sends requests to that base — no
   `api.github.com` anywhere in the new code (`rg -n "api\.github\.com" tools/rca/` finds only
   pre-existing default-resolution code, not new methods).
10. Suite green: `pytest tests/rca -q` and `pytest tests/eval -q`; acceptance-35 grep stays empty:
    `rg -n "job_id|run_id|github_api" tools/rca/cleaner.py tools/rca/drain_index.py tools/rca/budget.py tools/rca/redact.py tools/rca/history.py`

## Out of scope (later steps)

- `tools/rca/deliver/**`, the `deliver` CLI subcommand, `DeliveryContext`, suppression, severity,
  comment rendering (Steps 13–14).
- Any actual posting, issue creation, `action.yml` inputs/outputs, permissions, workflow changes.
- `DeliveryReport` on `Summary` (Step 14 write-back).
- `IssuesHistoryStore` (Step 16).

## Deliverable

A focused diff to `tools/rca/github_api.py` plus tests, with `pytest tests/rca -q` and
`pytest tests/eval -q` green. Summarize the new methods, and explicitly state how you proved collect
behaviour is unchanged and how duplicate-safe write retries are enforced.
```
Short command to run it in Claude Code (from the repo root):

Read docs/cc-prompt-step12-github-write-layer.md and implement exactly that slice — capability only, no wiring into collect, no deliver package. Follow AGENTS.md, add the tests it specifies, and make sure `pytest tests/rca -q` and `pytest tests/eval -q` are green before you finish.
```
