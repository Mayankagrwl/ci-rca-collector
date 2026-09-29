# Claude Code task — Step 18a: notifications (email + chat) and three Step 17 follow-ups

## Context

`ci-rca-collector`. Read `AGENTS.md` and `docs/rca-delivery-spec-v1.3.md` (**§5 A2/A3/B2 and the quiet-window paragraph, §6, §9, §10, §11, §14, §17 AC #9, #14, #22, #25**) first.

**Prerequisite — stop and report if missing:** Step 17 merged. That means `deliver/owners.py`, `deliver/owners_io.py`, `GitHubClient.add_assignees`, `DeliveryReport.owners` / `owner_resolved_by`, and `_finish_issue` running after the comment.

Step 18 is split in two:

- **18a (this document):** notifications.
- **18b (next):** feedback, meaning the `/resolved` parser, reactions, and the workflow stub.

Do **only** what this document scopes. Nothing is hardcoded to a project, provider, host, team or user.

## Verified facts to build on (do not re-derive)

- `DeliveryInputs` already has `smtp_url` and `chat_webhook_url` (both `""`), `quiet_window_minutes=60`, `platform_team`, and `default_notify`. The CLI does **not** wire the two URL inputs yet.
- `suppress.evaluate()`:
  - Stage A: `infra_widespread` sets `notify_platform_once=True`; `infra_runner` sets `suppressed_by="infra_runner"`.
  - Stage B: `dedupe="update_quiet"` means the same fingerprint and branch inside the quiet window, measured on the sticky comment's `updated_at`.
- `severity.severity(context, state, summary, *, widespread_already_notified=False)` returns critical, high, normal, low or info.
- `RoutePlan.notify` holds intent audiences from `ROUTING_TABLE`: `"team"`, `"actor"`, `"owning_team"`. The preview currently says "channels are decided in Step 18".
- Step 17 gives `outcome["owners"]` (entries like `@user`, `@org/team`, or email addresses) and `owner_resolved_by`.
- `render_comment.card_headline(summary, record, decision)` returns the (headline, fix) pair as the comment states it, or a `None` headline when the root cause is omitted. `render_comment.safe_text()` redacts, defuses `@mentions` and rule ids, and escapes for forks.
- `GitHubClient.list_runs(repo, *, head_sha=None, branch=None, status=None, per_page=50, workflow_id=None)` is read-only.
- The flow is ownership → issue sync → comment → `_finish_issue` (stale sweep and index flush), all within one `DeliveryBudget` (30s).

## Part 0 — Step 17 follow-ups (do these first)

**F1 — Wrong owner from a same-named file (real bug).** `resolve_path_owners` maps a candidate to a known changed file when `k.endswith("/" + cand)`. Because candidates go all the way down to the bare basename, any common file name matches an unrelated file. Reproduced:

- `/work/services/billing/pom.xml` with `changes.files=["services/auth/pom.xml"]` resolves to `@auth`.
- A bare `pom.xml` also resolves to `@auth`.

In monorepos (`pom.xml`, `package.json`, `build.gradle`, `Dockerfile`, `__init__.py`, `index.ts` …) this blames the wrong team. Fix it generically:

- A known file matches when **it** is a suffix of the candidate (`cand == k` or `cand.endswith("/" + k)`). That direction is always safe.
- The reverse direction (`k.endswith("/" + cand)`) is allowed **only** for the original, un-stripped relative path, **only** when it has ≥ 2 segments, and **only** when exactly **one** known file matches. Otherwise it is ambiguous: skip it and add a note.
- Tests:
  - Both reproductions above resolve to `@billing` and `@default` respectively, not `@auth`.
  - An unambiguous `test/A.java` with known `src/test/A.java` still maps.
  - Two known files with the same suffix → no mapping, with a note.

**F2 — CODEOWNERS between 1 and 3 MB is read as empty.** The contents API returns `content: ""` (with `encoding: "none"`) for files over 1 MB, and `get_file(..., allow_empty=True)` then returns `""`, which is treated as an **existing empty** CODEOWNERS.

- Return `None` in that case and let the loader record a note (`too large for the contents API; ignored`). A genuinely empty file (`size == 0`) still returns `""`.
- Test both.

**F3 — `issue:assigned` claimed when GitHub silently dropped the login.** `POST /issues/{n}/assignees` returns 201 but silently ignores logins that can't be assigned.

- Compare the response's `assignees[*].login` with the requested logins.
- Record `issue:assigned` only if at least one requested login is present.
- Add a note naming each dropped login.
- Test a partial drop and a full drop.

## Deliverables

### 1. `tools/rca/deliver/notify.py` — decision and message building are pure; senders are injectable

**When to notify** (v1.3 §10). All of these must hold:

1. At least one channel is configured. **A missing channel is not an error.**
2. **Live** mode only. In dry-run the preview shows what would be sent; offline makes no network calls at all.
3. Stage A did not suppress the run, **or** it is the platform path described in the next list.
4. `severity ∈ {high, critical}`, **or** `trigger == "schedule"` at any severity.
5. The run is not inside the quiet window, measured per fingerprint:
   - Treat `decision.dedupe == "update_quiet"` as quiet.
   - Otherwise use the fingerprint issue's `updated_at` **as it was before this run's sync**. Capture it in `IssueResult` (add `previous_updated_at: datetime | None`).
   - If neither exists, nothing is quiet.
   - Email and chat share this one clock. Document that as a simplification.

**Platform path:**

- **A2 (`infra_widespread`, `notify_platform_once`)** sends exactly **one** message per burst, using **stateless leader election**.
  - Read `list_runs(repo, status="failure", per_page=100)` once and keep runs whose `created_at` falls within the last `quiet_window_minutes`.
  - Only the run with the **lowest `id`** among them (this run included) sends.
  - The message names the distinct workflows and branches in that set.
  - Non-leaders add the note `platform notified by run <id>`.
  - If the lookup fails, **send anyway** and add a note. A duplicate is better than a missed outage.
  - Severity gating does not apply to A2.
- **A3 (`infra_runner`)** goes to the platform audience only when rule 4 holds, using the same leader election so that a burst of runner failures produces one message.
- **Flaky, draft and `delivery_error` runs** never notify.

**Audience** (from `RoutePlan.notify` plus Step 17 owners, never hardcoded):

| Case | Audience |
|---|---|
| Platform path | `platform_team`, then `default_notify` |
| `"team"` / `"owning_team"` | `outcome["owners"]`, then `default_notify` |
| `"actor"` | `@<context.actor>`, unless it ends in `[bot]` |
| Otherwise | none |

- **Email recipients** are the entries in that audience that are email addresses. GitHub handles cannot be emailed: list them in the body as plain text and add a note (`no email address for @x`). No recipients means no email, plus a note.
- **Chat** always posts to the configured webhook when the gates pass. Owners appear as plain text, never as provider-specific mention syntax.

**Content** (build once, then derive per channel):

- **Email subject:** `[CI] <severity>: <workflow> failed on <branch>`.
  - No log or model text in the subject.
  - Strip CR/LF and other control characters from every header value, because workflow and branch names can come from forks. This prevents header injection.
  - Cap the subject at about 200 characters.
- **Email body** (plain text, UTF-8, `email.message.EmailMessage`): severity, the one-line cause, fix, run link, issue link if any, owners, and `resolved by <owner_resolved_by>`.
  - The one-line cause is `card_headline`. When the root cause is omitted (C2), use the category human words only, never a root-cause claim.
  - Add `Unverified` when `decision.unverified_banner` is set.
  - **No evidence block, no log lines.**
- **Chat text:** the same facts on 3–5 short lines.
  - Escape `&`, `<` and `>` in all dynamic text. This neutralises `<!channel>` / `<@U…>`-style broadcast syntax across providers.
  - `@mentions` stay defused via `safe_text`.
- Everything goes through redaction **before** truncation. The safety rules match comments: no `\bR\d+\b`, no secret patterns.
- For A2, the widespread message is **one** summary naming workflows and branches, not a per-run cause.

**Senders:**

- **Chat:** `ChatSender(url, *, transport=None, timeout)`.
  - Uses `httpx` (already a dependency); a transport can be injected for tests.
  - POSTs JSON `{<field>: text}`. `<field>` defaults to `text` (works for Slack, Mattermost, Google Chat and similar) and can be overridden by `RCA_CHAT_PAYLOAD_FIELD` (for example `content`).
  - Only `https://` URLs are accepted; anything else is a configuration note, not a send.
  - No redirects. One attempt, no retry.
  - A 2xx is success; anything else is an error.
- **Email:** `SmtpSender(url, *, smtp_factory=None, timeout)` with stdlib `smtplib`. No new dependency.
  - URL form: `smtp://[user:pass@]host[:port]?from=<addr>` uses STARTTLS and **requires** it (fail if the server doesn't offer it). `smtps://…` uses implicit TLS.
  - Plaintext is allowed **only** with an explicit `?tls=none`, documented as insecure for internal relays.
  - `from` is required; if it's missing, record a configuration note.
  - The SMTP factory can be injected for tests.
- **Secrets:** the webhook URL and SMTP URL are credentials.
  - Read them from **environment only**: `RCA_SMTP_URL` and `RCA_CHAT_WEBHOOK_URL`, falling back to the `DeliveryInputs` values when set programmatically.
  - **No CLI flags for them**, because argv leaks into process lists and logs.
  - Never log or write them anywhere, including notes, errors, the preview, `summary.json`, and exception text.
  - Scrub both values, their hosts' userinfo, and any query string from every recorded error. `httpx` and `smtplib` exceptions include URLs.
- **Budget:** each send's timeout is `min(10s, remaining budget)`. When the budget is exhausted, skip the send and record `delivery budget exceeded`.

### 2. CLI wiring

- **Order:** ownership → issue sync → comment → **notifications** → `_finish_issue`. Notifications are the lowest-priority write before housekeeping.
- **`delivered_to`** gains `email` and/or `chat` when a send landed.
- **Errors:** failures go to `delivery.errors` (for example `chat webhook failed (status 500)` or `SMTP send failed: …`), scrubbed. They never block the comment or the issue, which have already happened. Exit 0 unless `--strict` (AC #22).
- **Preview:** replace "channels are decided in Step 18" with a `Notifications` section. It lists, per channel: `would send` / `sent` / `skipped (<reason>)`, the email subject, the recipients (addresses shown, since they're already known to the repo owner), and the chat text verbatim. The chat text must be byte-identical to what would be POSTed.
- **`suppressed_by` rule:** a notification failure alone never sets `suppressed_by="delivery_error"` when the comment or issue landed. It only counts when notification was the **only** channel attempted (for example `schedule` with no issue, or the A2 platform path) and nothing was delivered.

## Constraints

- Message building and gating stay pure. Only `ChatSender` and `SmtpSender` do I/O, and both are injectable.
- GitHub HTTP still goes only through `GitHubClient`. The webhook is not GitHub, so it uses its own `httpx` client, never `GitHubClient` and never the GitHub token.
- **Never send the GitHub token to the webhook or SMTP host.**
- No new dependencies. No `action.yml` or workflow changes: Step 19 maps the `smtp-url` / `chat-webhook-url` inputs (from secrets) to the env vars. Collect is untouched.
- `render_comment`, `severity` and `suppress` stay pure. Comment bodies stay byte-identical to Step 17 output.

## Acceptance criteria

All tests are offline: a fake webhook through `httpx.MockTransport`, a fake SMTP factory, and the fake GitHub.

1. **No channel configured:** no send attempt and no error. The preview says `notifications: none configured`.
2. **`push_default` at high/critical with both channels:**
   - one chat POST and one email to the email-form owners and `default_notify` addresses;
   - `delivered_to` includes `commit_comment`, `issue:*`, `chat`, `email`;
   - the subject matches `[CI] high: <workflow> failed on <branch>`.
3. **Severity gate:** a `normal` PR failure sends nothing, while a `schedule` failure at `normal` sends (v1.3 §10).
4. **Quiet window:**
   - a second run within `quiet_window_minutes`, whether via `update_quiet` or a previous issue `updated_at`, sends nothing and adds a note;
   - after the window it sends again;
   - the issue's `updated_at` from **this** run's sync is never the clock.
5. **Five simultaneous `infra_widespread` runs (AC #9):**
   - exactly one chat message in total (only the lowest run id sends), naming the workflows and branches;
   - zero PR comments;
   - if the leader lookup fails, the run still sends, with a note.
6. **Infra runner:** a platform notification goes to `platform_team`, never to the actor (AC #15 spirit), and a burst produces one message.
7. **Flaky and draft:** no notification.
8. **Dispatch (AC #14):** notifies the actor only if a channel is configured. No issue.
9. **Failures (AC #22):**
   - SMTP raises → the error is recorded and scrubbed, the PR comment is still posted, and the exit code is 0;
   - webhook 500 → the error is recorded;
   - neither error contains the webhook URL, the SMTP password, or the query string.
10. **Transport security:**
    - `http://` webhook → rejected with a note, no send;
    - `smtp://` without STARTTLS support → failure recorded, nothing sent in clear;
    - `?tls=none` → sends in clear, with a note.
11. **Header injection:** a branch name containing `\r\nBcc: x@evil` produces a single-line subject and no extra header.
12. **Safety (AC #25):**
    - chat and email text contain no secret pattern and no `\bR\d+\b`;
    - `<!channel>`, `<@U123>`, `@everyone` and `@here` from log or model text are neutralised;
    - the omitted-root-cause (C2) case contains no root-cause claim.
13. **Dry-run and offline:** dry-run shows `would send` with zero webhook/SMTP calls; offline shows the same with zero network of any kind. `--live` is required to send.
14. **Part 0:** F1, F2 and F3 tests as described above.
15. **Suites and checks:** `pytest tests/rca -q` and `pytest tests/eval -q` are green, the acceptance-35 grep is empty, and `run_eval --no-llm` stays at 1.0.

## Out of scope

- `/resolved`, reactions, inferred resolution, and the workflow stub (Step 18b).
- `action.yml` inputs, secrets mapping, and the delivery job (Step 19).
- Populating `run.concurrent_*` in collect. It is a separate, known gap that needs its own design, because the widespread rule runs before code rules.

## Deliverable

`deliver/notify.py`, the CLI wiring, `IssueResult.previous_updated_at`, the Part 0 fixes, and tests, with both suites green. Show:

- the preview `Notifications` section for a `push_default` high failure and for the A2 leader and a non-leader;
- the exact chat JSON POSTed;
- the email headers and body, with addresses redacted in the transcript.
