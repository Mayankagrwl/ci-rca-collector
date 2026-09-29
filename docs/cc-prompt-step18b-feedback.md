# Claude Code task — Step 18b: feedback (`/resolved`, reactions, workflow stub) and two Step 18a follow-ups

## Context

`ci-rca-collector`. Read `AGENTS.md` and `docs/rca-delivery-spec-v1.3.md` (**§5 A2/A3, §10, §11, §12, §17 AC #9, #18, #19, #25**) first.

**Prerequisite — stop and report if missing:** Step 18a merged. That means `deliver/notify.py` (`elect_leader`, `plan_notification`, `ChatSender`, `SmtpSender`, `Scrubber`), `IssueResult.previous_updated_at`, and the notifications section in the preview.

Do **only** what this document scopes. Nothing is hardcoded to a project, team or user.

## Verified facts to build on (do not re-derive)

- **Step 18a leader election.** `elect_leader(runs, *, run_id, workflow, branch, now, window_minutes)` takes **every** failed run returned by `list_runs(status="failure")` inside the window and lets the lowest id send. The CLI calls it for the platform paths (A2 `notify_platform_once`, A3 `infra_runner`).
- `IssuesHistoryStore` handles:
  - the index and label scan;
  - `_update` (absorb, rewrite, reopen, recurrence comment);
  - `sweep_stale`, which **skips records that have a `resolution`**;
  - `_self_heal` (lowest number wins).
- `FailureRecord` has `resolution`, `resolution_author`, `resolution_run_id`, `human_verified`. `absorb()` keeps an existing resolution (`self.resolution or other.resolution`).
- `GitHubClient`:
  - `create_reaction(repo, comment_id, content)` reacts to an **issue comment**. It is best-effort, never raises, and returns `{}` on failure.
  - `list_issue_comments`, `get_issue`, `update_issue`, `create_issue_comment` all exist.
- **The sticky marker.** The first line of an RCA PR comment is `<!-- rca-bot:fp=<coarse> -->`, and `deliver.sticky.find_existing` matches it. Comment objects returned by GitHub include a `reactions` rollup (`"+1"`, `"-1"`, …) at no extra cost.
- `render_comment.safe_text()` redacts, defuses `@mentions` and rule ids, and clips.

## Part 0 — Step 18a follow-ups (do these first)

### F1 — The platform leader can be a run that never sends (real bug; affects A3 today)

`elect_leader` elects among **all** failed runs in the window. It cannot know which of them are on the platform path. Reproduced:

- Run 100 is an ordinary compile failure 20 minutes ago. Runs 101 and 102 are runner-infra failures.
- Both 101 and 102 compute `leader_id=100` and stay silent.
- Run 100 never takes the platform path, so **no platform notification is sent at all**.

On a busy repo with a 60-minute window this happens almost every time, so A3 platform alerts are effectively lost. AC #9 passes only because every fake run in the test is infra.

**Fix: coordinate through a GitHub-held marker instead of guessing from the run list.** This is stateless on our side and duplicate-safe.

**The marker issue:**

| Field | Value |
|---|---|
| Label | `rca-platform-incident` |
| Title | `[RCA] platform incident: <kind in human words>`, where kind is `infra_widespread` or `infra_runner` |
| Body first line | `<!-- rca-incident:kind=<kind> -->` |
| Body prose | workflows and branches seen in the window (from `list_runs`, informational only), plus the maintained-by-bot line |

It is **not** labelled `rca-fingerprint`, so the fingerprint sweep and index ignore it.

**For a platform-path run:**

1. List open `rca-platform-incident` issues and keep those whose marker kind matches.
2. **If one has `updated_at` inside `quiet_window_minutes`:** this burst was already notified.
   - Add one short comment to it: `Also failed: [run N](url) · <workflow> · <branch>`, made safe. This bumps its `updated_at`, so one continuous outage produces one message.
   - Do not send. Add the note `platform notified via #N`.
3. **Otherwise:**
   - Create a new incident issue.
   - Re-list, and keep open incidents of this kind created within the window.
   - The **lowest issue number** wins. If ours is not the lowest, close ours with `Duplicate of #N` and don't send.
   - If ours is the lowest, **send**. Then close older, outside-window incidents of the same kind with `Superseded by #N` (at most 5, live only).
4. **Fail open:**
   - When `create_issues` is false, or any list/create call fails (403 records `issues: write`), send anyway with a note (`burst dedupe unavailable`).
   - In dry-run, preview `would open/join incident`.
   - Offline makes no calls.

Remove `elect_leader`'s use from the CLI. Keep the function only if tests still need it; otherwise delete it together with its tests.

**Tests:**

- The reproduction above sends exactly one message.
- Five simultaneous `infra_widespread` runs with two concurrent creates → one message, and one incident is closed as a duplicate.
- A run 30 minutes after the last incident activity (with a 60-minute window) joins it with a comment and doesn't send.
- A run 90 minutes after sends again, with a new incident, and the old one is superseded.
- A 403 → sends, with a note.

### F2 — A bare known file still borrows ownership (residual of Step 17)

Reproduced: `/work/services/billing/pom.xml` with `changes.files=["pom.xml"]` and CODEOWNERS `/pom.xml @root` + `/services/billing/ @billing` resolves to **`@root`**. The root `pom.xml` is a suffix of the candidate, but it is a different file.

**Fix:** a known file with **one** segment matches only when the candidate *is* that original relative path, not merely ends with it. Known files with two or more segments keep the suffix rule. The billing case then resolves to `@billing` through the longest non-catch-all candidate.

**Tests:** that case, plus a root `pom.xml` given as a relative path, which still maps to `@root`.

## Deliverables

### 1. `deliver/feedback.py` — the parser is pure

`parse_resolved(body) -> ResolvedCommand | None`, returning `target_issue: int | None` and `text: str`:

- The **first non-blank line** must start with `/resolved`, followed by whitespace or end of line. The command is case-sensitive.
- It does **not** count when it is inside a `>` quote, inside a code fence, or not the first line.
- An optional `#<number>` token right after the command targets a specific fingerprint issue: `/resolved #123 bumped the base image`.
- `text` is the rest of that line plus any following lines, whitespace-collapsed, redacted and made safe with `safe_text`, and capped at about 300 characters.
- Empty text means `None`, with the reason recorded by the caller. Never raises.

### 2. `feedback` CLI subcommand (the `/resolved` handler)

```
python -m tools.rca.cli feedback --event "$GITHUB_EVENT_PATH" [--repo owner/name] [--live|--dry-run] [--offline] --out rca
```

**Input:** an `issue_comment` event payload (`action == "created"`), read **from the file only**.

**Skip, with a note and exit 0, when:**

- the action isn't `created`;
- the comment author is a bot (`[bot]` or `type == "Bot"`);
- `parse_resolved` returns `None`;
- the author's `author_association` is not in the allowed set: default `OWNER, MEMBER, COLLABORATOR`, overridable via `RCA_RESOLVE_ASSOCIATIONS` (comma list). Anyone can comment on public repos, so this is required.

**Resolve the target fingerprint issue(s):**

| Where the comment is | Target |
|---|---|
| On a fingerprint issue (body marker `rca-issue:fp=`) | That issue. A `#N` token is ignored, with a note. |
| On a PR, with `#N` | Issue N, only if it is an RCA fingerprint issue; otherwise skip with a note. |
| On a PR, without `#N` | The PR's RCA sticky comments (by marker). With one, use its coarse fingerprint. With several, use the **most recently updated** and say so in the note. Then find the fine-fingerprint issues for that coarse fingerprint via `IssuesHistoryStore.find_by_coarse` / label scan and apply to each open one (usually one). |
| Anywhere else | Skip with a note. |

**Apply (live only):**

1. On the record, set `resolution=<text>`, `resolution_author=<login>`, `human_verified=True`, and `resolution_run_id=None` (a comment has no run).
2. Rewrite the body. The JSON must stay parseable and the body stays under the 65,536-character cap.
3. **Close** the issue with `state_reason: "completed"`. It already reopens automatically on recurrence (Step 16).
4. React 👍 (`+1`) on the `/resolved` comment, best-effort.
5. **Idempotent:** a re-delivered event with the same text and author makes no body write, but the reaction is still attempted.

**Output:** write `rca/feedback-report.md` (what was parsed, the target, and what happened). Never fail the workflow (exit 0 unless `--strict`). Dry-run reads only; offline makes no calls.

**Security (AC #18):** the comment text is untrusted. It is only ever read from the event JSON file, never passed through argv, env interpolation, or a `run:` script.

### 3. Recurrence after a resolution (small lifecycle fix)

When a resolved issue recurs, the Step 16 update path reopens it.

- **Keep** the resolution fields as history. Add one prose line: `Previously resolved by @<author>: <text> — recurred since.` The author is rendered in a code span so nobody is pinged.
- **Change `sweep_stale`:** skip only issues that are **closed**. An open issue with a resolution (it recurred) is swept after 14 days like any other.
- Tests for both.

### 4. Reactions on the RCA comment (cheap signal)

- When `find_existing` returns the sticky comment, read its `reactions` rollup from the list payload. **No extra API call.**
- Record it on `DeliveryReport` as `feedback: dict[str, int] = {}` (for example `{"up": 3, "down": 1}`). This is optional and defaults empty, so old files load.
- Show it on one preview line.
- Do **not** store it on `FailureRecord`: per-target counts summed across runs would double-count.
- Never block delivery on it.

### 5. Workflow stub (documentation, not active in this repo)

Add `docs/examples/rca-feedback.yml`, a consumer-repo workflow:

```yaml
on:
  issue_comment:
    types: [created]
permissions:
  contents: read
  issues: write
  pull-requests: write
jobs:
  resolved:
    if: startsWith(github.event.comment.body, '/resolved') && !endsWith(github.event.comment.user.login, '[bot]')
    runs-on: ubuntu-latest
    steps:
      # check out the RCA tool (pinned), never the PR head
      - run: python -m tools.rca.cli feedback --event "$GITHUB_EVENT_PATH" --live --out rca
        env:
          GITHUB_TOKEN: ${{ github.token }}
```

Also add a short section in `docs/` explaining:

- the comment text is read from the event file only;
- the PR's code is never checked out;
- why `author_association` is enforced;
- the `#N` targeting.

Wiring this into `action.yml` is Step 19.

## Constraints

- `feedback.py`'s parser stays pure. All GitHub HTTP goes through `GitHubClient`. No new dependencies. No `action.yml` or CI workflow changes (Step 19). Collect is untouched.
- Comment bodies stay byte-identical to Step 18a output. The issue body changes only through the recurrence-after-resolution line.
- The acceptance-35 grep stays empty. `tools/rca` never imports `tools/eval`. Never print `R<n>`.

## Acceptance criteria

All tests are offline, against the fake GitHub (extend it with event fixtures, `state_reason`, and reactions rollups).

1. **Parser (AC #19):**
   - `/resolved bumped base image` → text set.
   - `/resolved #42 fixed flaky port` → target 42.
   - `> /resolved x`, a fenced block, `/resolvedx`, `/resolved` with no text, and `/resolved` on the second line → `None`.
   - Secrets are redacted, `@mentions` defused, and the text is capped.
2. **On a fingerprint issue:** the record gets `human_verified=true` plus resolution and author, the issue is closed as completed, 👍 is added, and the body JSON parses. A re-run makes no second body write.
3. **On a PR:** one sticky comment → its coarse fingerprint's open issues are resolved. Two sticky comments → the most recent one is used, with a note. `#N` pointing at a non-RCA issue → skipped.
4. **Authorisation:** `CONTRIBUTOR` / `NONE` / bot authors → no writes, with a note. `RCA_RESOLVE_ASSOCIATIONS=OWNER` narrows the set.
5. **Recurrence after resolution:** the issue reopens, the "Previously resolved by `@x`" line is present, and after 14 days without recurrence the sweep closes it.
6. **Reactions:** the preview shows `feedback: 👍 3 · 👎 1` from the rollup, with zero extra GETs.
7. **Part 0:** the F1 and F2 tests listed above, including the exact 100/101/102 reproduction.
8. **Safety (AC #18, #25):** no comment text reaches argv or env in the stub. The report and bodies contain no secret pattern, no `\bR\d+\b`, and no live `@mention`.
9. **Modes:** dry-run makes zero writes, offline makes zero requests, and every path exits 0.
10. **Suites and checks:** `pytest tests/rca -q` and `pytest tests/eval -q` are green, the acceptance-35 grep is empty, and `run_eval --no-llm` stays at 1.0.

## Out of scope

- Inferred resolution ("fingerprint gone and pipeline green"). It needs the success-path trigger that arrives with the delivery job in Step 19; list it as deferred.
- `action.yml` inputs/outputs, the delivery job and permissions (Step 19).
- Populating `run.concurrent_*` in collect (a known separate gap).

## Deliverable

`deliver/feedback.py`, the `feedback` subcommand, the incident-issue platform dedupe (replacing leader election), the F2 fix, the lifecycle change, the `DeliveryReport.feedback` field, `docs/examples/rca-feedback.yml` plus its docs section, and tests, with both suites green. Show:

- the fake-server transcript for `/resolved` on a PR (list comments → issue update + close → reaction);
- the F1 reproduction producing exactly one message;
- a feedback report.
