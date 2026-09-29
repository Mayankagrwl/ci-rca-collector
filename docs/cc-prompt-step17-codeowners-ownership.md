# Claude Code task — Step 17: ownership (CODEOWNERS, last match wins), issue assignees, and two Step 16 follow-ups

## Context

`ci-rca-collector`. Read `AGENTS.md` and `docs/rca-delivery-spec-v1.3.md` (**§9, §11, §14, §15, §17 AC #15–16**) first.

**Prerequisite — stop and report if missing:** Step 16 merged. That means `deliver/issues.py` with `IssuesHistoryStore`, the `issue_ref` footer, `--migrate-history`, and `GitHubClient.get_issue`.

This step decides **who owns a failure** and records it. It also assigns fingerprint issues to user owners. Notifications to those owners are Step 18. PR and commit comment bodies are **unchanged**.

Do **only** what this document scopes. Nothing is hardcoded to a project, path, team or user.

## Verified facts to build on (do not re-derive)

- `GitHubClient.get_file(repo, path, ref) -> str | None`: 404 and 403 return `None` and never raise. There is **no** assignee method yet.
- `DeliveryContext` has `trigger`, `pr_number`, `is_fork`, `branch`, `default_branch`, `actor`, `commit_sha`, and **no** PR base ref.
- `DeliveryInputs.platform_team` and `.default_notify` exist, both empty strings by default.
- `DeliveryReport.owner_resolved_by: str | None` exists and is always `None` today.
- `Summary.classification.is_infra_vs_code ∈ {infra, code, unknown}`.
- `Summary.verdict.short_circuit ∈ {infra_runner, infra_widespread, flake_same_sha_passed, no_failed_jobs, None}`.
- `DeterministicDiagnosis.suspected_files` and `AnalysisResult.suspected_files` are lists of strings. Since 14b these can be **absolute runner paths** such as `/work/src/test/TestConfig.java`.
- `ChangeContext.files` is a list of repo-relative strings with **no per-file change counts**.
- `analyze.display_status()` decides whether model output is trusted (`ok` / `cached`).
- Step 16 issue flow: `_deliver_issue` runs **before** `_deliver_comment`. `IssuesHistoryStore.sync(...)` → `_create` / `_sync`, then `_self_heal`. `sweep_stale` and `store.close()` (the index flush) are also called inside `_deliver_issue`.

## Part 0 — Step 16 follow-ups (small; do these first)

**F1 — Housekeeping starves the comment.** `sweep_stale` (a full label scan plus up to 10 writes) and the index flush in `store.close()` currently run inside `_deliver_issue`, which runs **before** the comment. Both share the 30s budget, so on a slow API the sticky comment, the most important write, can be skipped with `delivery budget exceeded`.

- Keep issue **sync** before the comment, because the `tracked in #N` footer needs it.
- Move `sweep_stale` and `store.close()` to **after** `_deliver_comment`, still inside the budget and still never raising.
- Test: with a fake clock that crosses 30s during the sweep, the comment was already posted.

**F2 — Occurrence lost on duplicate self-heal.** When `_self_heal` closes **our** freshly created issue as `Duplicate of #N` (N lower), this run's occurrence is never recorded on #N.

- After self-heal, if the kept issue is not the one we created, run the normal update path on #N: absorb, rewrite the body, and add a recurrence comment.
- `delivered_to` then contains `issue:duplicate-closed` plus `issue:updated`.
- Test: #N's count goes up by one.

**F3 — Cosmetic.** In the issue body, put `**Suggested fix** — …` on its own paragraph, separate from the first-/last-seen sentence.

## Deliverables

### 1. `tools/rca/deliver/owners.py` — pure (no `github_api`, no `httpx`, no I/O)

**Parser.** `parse_codeowners(text) -> list[OwnerRule]`, with `OwnerRule(pattern, owners: tuple[str, ...], line_no)`, following GitHub's CODEOWNERS syntax:

- Blank lines and `#` comments are skipped. Inline ` #` comments are stripped. `\#` at the start of a pattern is a literal `#`.
- Owners are `@user`, `@org/team`, or an email address. Keep them as written.
- A pattern with **no owners** is a valid rule meaning "explicitly unowned". It still participates in last-match-wins.
- Unsupported syntax (a `!` negation, `[...]` character ranges) or an invalid line is **skipped with a note, never fatal**. This mirrors GitHub, which ignores invalid lines.
- Handle CRLF and a BOM.

**Matcher.** `owners_for(path, rules) -> OwnerMatch | None`. **The last matching rule wins** (v1.3 §9, AC #16). Pattern semantics follow gitignore:

| Pattern | Meaning |
|---|---|
| leading `/` | anchored to the repo root |
| a `/` elsewhere, except trailing | also anchored |
| no `/` | matches at any depth |
| trailing `/` | everything under that directory, recursively |
| `*` | does not cross `/` |
| `**` | crosses `/` |
| `dir/*` | direct children only |

- Matching is case-sensitive. Implement it with a small translator to regex; **no new dependency**.
- `OwnerMatch` carries `owners`, `pattern`, `line_no`, and `catch_all: bool`. `catch_all` is true for `*`, `**`, `/**`, `/*`.

**Path normalisation.** `repo_relative_candidates(path, *, workspace: str | None) -> list[str]`, longest first:

- Convert backslashes to `/` and drop any `file://` scheme.
- If `workspace` is set and prefixes the path, strip it and return that single candidate.
- A path that is already relative, with no `..`, is returned as-is.
- Otherwise return every suffix obtained by dropping leading segments, longest first. For example `/work/src/test/A.java` gives `work/src/test/A.java`, `src/test/A.java`, `test/A.java`, `A.java`.

`resolve_path_owners(path, rules, *, workspace, known_files)` picks one candidate:

1. A candidate that equals, or is a suffix-aligned match of, an entry in `known_files` (`changes.files`). Use that repo path.
2. Else the **longest** candidate whose match is **not** `catch_all`.
3. Else the catch-all match, if any.

This is generic, with no project knowledge. Anchored patterns like `/src/` only match the right suffix, and unanchored ones like `*.java` match the longest.

**Resolver.** `resolve_owner(summary, analysis, context, inputs, rules, *, workspace) -> OwnerResolution`, returning `owners: list[str]`, `resolved_by`, and `notes`. The precedence follows v1.3 §9 exactly, and the first step that yields owners wins:

| # | Step | `resolved_by` |
|---|---|---|
| 1 | **Infra:** `is_infra_vs_code == "infra"`, or `verdict.short_circuit` is `infra_runner` / `infra_widespread`. Use `inputs.platform_team` (split on commas/whitespace). If that is empty, go straight to step 5. Infra **never** falls to the actor or to CODEOWNERS (AC #15). | `platform_team` |
| 2 | **Suspected files:** `analysis.result.suspected_files` only when `display_status ∈ {ok, cached}`, then `diagnosis.suspected_files`. Deduplicate, cap at 20 paths. Take the union of owners in file order. | `codeowners_suspected` |
| 3 | **Changed files:** `changes.files` in given order (there are no counts, so "most-changed first" becomes file order; note that), capped at 50. Same union. | `codeowners_changes` |
| 4 | **Actor:** `context.actor`, skipped when it ends in `[bot]` or the trigger is `schedule`, since the cron's actor isn't the owner. | `actor` |
| 5 | **Default:** `inputs.default_notify`. | `default_notify` |
| — | Otherwise: `owners=[]`. | `none` |

- Owners are deduplicated, preserving order, and capped at 10.
- A path whose last match is an **explicitly unowned** rule contributes nothing.

### 2. Loading CODEOWNERS (delivery-side, in `cli.py` or a thin `deliver/owners_io.py`)

- Try `.github/CODEOWNERS`, then `CODEOWNERS`, then `docs/CODEOWNERS` via `get_file`. The first **found** file wins, even if it's empty.
- Read at **`context.default_branch`**, never the PR head. This way a fork PR cannot edit CODEOWNERS to change who is blamed (v1.3 §11).
- Skip files over 3 MB with a note.
- Read at most once per run, inside the Step 15 `DeliveryBudget`.
- Offline: no fetch, and steps 2–3 are skipped with a note.
- Dry-run: the reads are allowed.
- `workspace` comes from `RCA_WORKSPACE`, then `GITHUB_WORKSPACE`, then `None`.

### 3. Use the result

- **Report:**
  - `DeliveryReport.owner_resolved_by` is set on every run that reaches routing.
  - Add `owners: list[str] = []` to `DeliveryReport`, optional so old files load.
  - The preview gains `- owner: <owners or none> (resolved by <resolved_by>)`. Replace the Step 16 "owner resolution is Step 17" note.
- **Issue body:** add one line `Owner: \`@org/team\`, \`@user\``. Use code spans so nobody is pinged; pings are Step 18's job. Omit the line when there are no owners. Redaction and safety rules apply as before.
- **Issue assignees**, best-effort, live only:
  - Add `GitHubClient.add_assignees(repo, number, assignees) -> dict` (`POST repos/{repo}/issues/{n}/assignees`, `idempotent=True`).
  - Assignable logins are owners that are `@user`: strip the `@`, exclude `org/team` and emails, cap at 10.
  - Assign **after** create, as a separate call. Never pass assignees to `create_issue`, so an unassignable login can never fail the create.
  - On update, assign only when the issue currently has **no** assignees. Never remove or replace a human's choice.
  - Any failure (422, 403) becomes a note (`could not assign …`), not a delivery error, and never blocks the comment.
  - `delivered_to` gains `issue:assigned` when it lands.
- **Comments:** PR and commit comment bodies stay **byte-identical** to Step 16 output.

## Constraints

- `owners.py` stays pure and GitHub-agnostic. All HTTP goes through `GitHubClient`. No new dependencies. No `action.yml` or workflow changes (Step 19).
- Collect is untouched. `render_comment`, `severity`, and `suppress` stay pure and unchanged in behaviour.
- The acceptance-35 grep stays empty. `tools/rca` never imports `tools/eval`. Never print `R<n>` rule ids.

## Acceptance criteria

Test pure logic directly, and wiring against the Step 15/16 fake GitHub, extended with the `contents` and `assignees` endpoints. No network.

1. **Last match wins (AC #16):** `* @a`, then `/src/ @b`, then `*.java @c` → `src/X.java` resolves to `@c`, and `src/x.py` resolves to `@b`.
2. **Explicitly unowned:** `/src/ @b`, then `/src/gen/` → `src/gen/x.py` has no owner, and the resolver moves on to the next file or step.
3. **Pattern semantics:** leading `/`, a middle `/`, a trailing `/`, `*` vs `**`, `dir/*` (direct children only), and a slash-less pattern at any depth. Also comments, inline comments, `\#`, CRLF, BOM, and a skipped `!`/`[...]` line with a note.
4. **Absolute runner path:** `/work/src/test/TestConfig.java` with `/src/ @b` resolves to `@b` via suffix candidates. With `GITHUB_WORKSPACE=/work` it resolves by stripping the prefix. When `changes.files` contains `src/test/TestConfig.java`, that path is used.
5. **Infra (AC #15):** an infra verdict with `--platform-team @org/platform` gives `owners=["@org/platform"]` and `resolved_by=platform_team`, **even when** CODEOWNERS and an actor exist. With no platform team it gives `default_notify` or `none`, never the actor.
6. **Precedence:** trusted analysis suspected files beat diagnosis files, which beat `changes.files`, which beat the actor, which beats `default_notify`. Untrusted analysis (`needs-review` / `failed`) files are ignored. A `[bot]` actor and the `schedule` trigger skip the actor step.
7. **File lookup:** `.github/CODEOWNERS` beats a root `CODEOWNERS`. It is read at `default_branch` (assert the `ref` query), and at most three GETs are made. Offline makes zero requests.
8. **Assignees:** a create is followed by `add_assignees` with only user logins; teams and emails are excluded. A 422 on assign is a note, the issue still exists, and the command exits 0. An existing issue that already has assignees is left alone. Dry-run makes zero assign calls.
9. **Report and preview:** `owner_resolved_by` and `owners` are written back, and the preview shows the owner line. Comment bodies for every golden are byte-identical to before this step.
10. **Part 0:** F1 (the comment is posted before the sweep uses up the budget), F2 (the kept issue's count is bumped after a duplicate close), and F3 (the fix sits on its own paragraph) are all tested.
11. **Safety:** no owner string in any body can ping anyone (all are in code spans). No `\bR\d+\b`. Redaction still applies.
12. **Suites and checks:** `pytest tests/rca -q` and `pytest tests/eval -q` are green, the acceptance-35 grep is empty, and `run_eval --no-llm` stays at 1.0.

## Out of scope

- Sending anything to owners: email, chat, or `@`-pings (Step 18).
- Reactions and `/resolved` (Step 18).
- `action.yml` inputs/outputs, the delivery job and permissions (Step 19).
- Git blame. Per-file change counts in collect.

## Deliverable

`deliver/owners.py` (plus a thin loader), `GitHubClient.add_assignees`, CLI wiring, the `DeliveryReport.owners` field, the Part 0 fixes, the fake-GitHub extension, and tests, with both suites green. Show:

- the preview owner line for an infra golden and for `java-compile-in-pipeline` (with a sample CODEOWNERS);
- the fake-server transcript for create → assign;
- the issue body's owner line.
