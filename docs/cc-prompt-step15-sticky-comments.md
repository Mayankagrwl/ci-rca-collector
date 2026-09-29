# Claude Code task — Step 15: sticky PR comments + commit comments (first GitHub writes)

## Context

`ci-rca-collector`. Read `AGENTS.md` and `docs/rca-delivery-spec-v1.3.md` (**§3, §4.1, §4.2, §5 Stage A/B, §7.3, §11, §14, §15, §17**) first.

**Prerequisites — stop and report if missing:** Steps 12–14 merged, and Step 14b merged. The render tests should already reflect 14b's headlines.

This is the **first step that writes to GitHub**. It finds an existing RCA comment by its marker and edits it, or creates a new one, on a PR (sticky PR comment) or a commit (default-branch / tag / opted-in branch pushes). It also applies the `ci:flaky` label. Issues are Step 16. Everything else stays as built.

Do **only** what this document scopes.

## Verified facts to build on (do not re-derive)

- `GitHubClient` (Step 12): `list_issue_comments(repo, n)`, `create_issue_comment(repo, n, body)`, `update_issue_comment(repo, comment_id, body)`, `list_commit_comments(repo, sha)`, `create_commit_comment(repo, sha, body)`, `update_commit_comment(repo, comment_id, body)`, `add_labels(repo, n, labels) -> {"labels": [...]}`. The `create_*` methods do **not** retry on timeout (`idempotent=False`). 5xx / `retry-after` are retried once inside `_request`. 403 raises `GitHubAPIError(status_code=403)`.
- `deliver.ExistingComment(NamedTuple)` has `fingerprint_coarse`, `branch`, `updated_at`. `suppress.evaluate(..., existing=...)` turns it into `dedupe="edit"` (B1) or `"update_quiet"` (B2).
- `render_comment(summary, record, context, decision)` produces the body. Its first line is exactly `<!-- rca-bot:fp=<fingerprint_coarse> -->`.
- The `deliver` CLI currently always runs as a dry run (note "live delivery is enabled in Step 15"). It writes `delivery-preview.md`, the `delivery` block (copy in `--out`), and `DELIVERY_OUTPUT_KEYS`. `--offline` uses `_OfflineClient`.

## Deliverables

### 1. Opt-in live mode (safety change to the CLI)

- Live writes happen **only** with an explicit `--live` flag and not `--offline`. With no flag the command stays a dry run, and `--dry-run` stays as an explicit alias. This makes it impossible to post by accident from a laptop, which v1.3's "absence of `--dry-run` means live" would allow. Step 19 maps `deliver: true` → `--live`.
- In dry-run (not offline) mode, **read** existing comments too, so the preview shows "would create" vs "would edit" vs "unchanged" accurately. Offline still makes no calls.

### 2. Extend `ExistingComment`

Add optional fields with defaults, so Step 13 code and tests keep working: `comment_id: int | None = None`, `html_url: str | None = None`, `body: str = ""`.

### 3. Sticky finder (`deliver/sticky.py`)

`find_existing(client, repo, *, target_kind: "pr" | "commit", target: int | str, fingerprint_coarse, branch) -> tuple[ExistingComment | None, list[str]]`:

- List comments on the target (paginated), and match only where the body's **first line** is exactly the marker, after stripping a BOM and surrounding whitespace. A human quoting our comment in a `>` blockquote must not match.
- Match by marker, **never by author**, because the bot identity differs between `GITHUB_TOKEN` and a PAT.
- If several match, choose the most recently updated and add a note naming the duplicates. Never delete anything.
- Never raises; a lookup error returns `(None, [note])`.

### 4. Writers (`deliver/pr_comment.py`, `deliver/commit_comment.py`, shared logic in `sticky.py`)

`post_or_update(client, repo, target, body, existing, *, clock, budget) -> WriteResult` (fields `action: "created" | "updated" | "unchanged" | "skipped" | "failed"`, `url`, `error`):

- **Unchanged:** if `existing.body` equals `body` byte-for-byte, make no write. `action="unchanged"`, and the URL is the existing one. This avoids pointless "edited" markers on re-runs.
- **Edit** when `existing` is present, **create** when it isn't.
- **Duplicate-safety for creates:** if a create fails with a timeout or connection error, list the target **once more**. If a comment with our marker and this exact body now exists, treat it as `created` (it landed). Otherwise record the failure. Never issue a second create in the same run.
- **Errors are recorded, never raised.** A 403 produces a message naming the missing scope: `pull-requests: write` for PR comments, `contents: write` for commit comments. After one failed write on a channel, stop that channel for this run (v1.3 §5 A5).
- **Budget:** the whole delivery has 30 seconds of wall-clock (v1.3 §14), with an injectable clock. Before each write check the remaining budget. If it's exceeded, skip the write with `error="delivery budget exceeded"`.

### 5. CLI wiring

After Step 13's decision flow:

- Comment target from the route plan: `"pr"` → `context.pr_number`, `"commit"` → `context.commit_sha`.
- If Stage A suppressed, or there's no target, don't comment.
- Otherwise: `find_existing` → `evaluate(existing=...)` → `render_comment` → `post_or_update` (live) or describe the outcome (dry-run).
- **Flaky label:** when `decision.add_flaky_label` and `context.pr_number` are set, call `add_labels(repo, pr_number, ["ci:flaky"])` in live mode only. It's best-effort; record errors.
- **Different coarse fingerprint:** a different marker naturally creates a second comment (AC #2). Leave comments for older fingerprints untouched.
- **`DeliveryReport`:**
  - `dry_run` reflects the mode.
  - `delivered_to` contains `pr_comment` / `commit_comment` / `label:ci:flaky` for what actually happened.
  - `comment_url` is set.
  - `errors` and `notes` are filled.
  - `suppressed_by="delivery_error"` only when a write was attempted and nothing was delivered.
- Outputs `comment-url`, `delivered-to`, `suppressed-by`.
- The preview is **still written in live mode** as an audit trail. Its header says `LIVE` and states what was done (created/updated/unchanged/failed + URL). The body in it must be byte-identical to the posted body.
- Default-branch issues remain "pending Step 16" in the preview.
- Never fail the workflow: exit 0 unless `--strict`.

## Constraints

- All HTTP goes through `GitHubClient`. No new dependencies. No `action.yml` or workflow changes (Step 19). Collect is untouched.
- Redaction already happens in `render_comment`. Post exactly that string, with no re-rendering between preview and post.
- `render_comment.py`, `severity.py`, `suppress.py` stay pure.

## Acceptance criteria

Test against an **in-memory fake GitHub** served through `httpx.MockTransport` + a real `GitHubClient`, so request shapes are real (no network):

1. Ready PR with one coarse fingerprint: the first run creates one comment. The second run with changed text edits it (PATCH), and a third identical run makes **no** write (`unchanged`) (AC #1).
2. A different coarse fingerprint on the same PR creates a second comment, and the first is untouched (AC #2).
3. A `push_default` failure creates a commit comment on `head_sha`, and a re-run edits it. The preview notes the issue as pending Step 16 (AC #3, comment part).
4. `push_branch` with default inputs produces no comment. With `--comment-on-branch-push true` it creates a commit comment (AC #5).
5. A flaky PR gets no comment, gets the `ci:flaky` label, and records `suppressed_by="flaky"` (AC #8).
6. A 403 on create records an error naming `pull-requests: write`, sets `suppressed_by="delivery_error"`, exits 0, and still writes the preview and report (AC #22).
7. Duplicate-safety: a create that times out, followed by a re-list showing the comment, gives `created`, with exactly **one** POST. If the re-list shows nothing, the result is `failed`, still with one POST.
8. A marker inside a `>` blockquote in a human comment is not matched, and a new bot comment is created.
9. Budget: with a fake clock that is past 30s, the write is skipped with `delivery budget exceeded`.
10. Safety: without `--live` (default and `--dry-run`), the fake server receives **zero** POST/PATCH but may receive GETs. With `--offline` it receives zero requests (AC #23).
11. The posted body equals the preview body byte-for-byte, ≤ 4000 characters, and contains no secret pattern and no `\bR\d+\b` outside the marker (AC #24, #25).
12. `pytest tests/rca -q` and `pytest tests/eval -q` are green, the acceptance-35 grep is empty, and eval `--no-llm` is 1.0.

## Out of scope

Issues and the index issue, `IssuesHistoryStore`, migration (Step 16). CODEOWNERS (Step 17). Notifications, reactions, `/resolved` (Step 18). `action.yml` inputs/outputs, the separate delivery job and permissions (Step 19).

## Deliverable

`deliver/sticky.py`, `deliver/pr_comment.py`, `deliver/commit_comment.py`, the `--live` flag and wiring in `cli.py`, the `ExistingComment` extension, and tests, with both suites green. Show a transcript of the fake-server requests for AC #1 (create → edit → unchanged).
