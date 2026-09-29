# RCA feedback: `/resolved`

Humans close the loop on a recurring CI failure by commenting:

```text
/resolved bumped the base image to 3.19
/resolved #123 fixed the flaky port allocation
```

The `feedback` subcommand (`python -m tools.rca.cli feedback --event "$GITHUB_EVENT_PATH" --live`)
records the text as the fingerprint issue's resolution (`human_verified=true`, author = the
commenter), closes the issue as completed, and reacts 👍 on the comment. If the failure recurs, the
issue reopens automatically and keeps the resolution as history ("Previously resolved by … —
recurred since"). A re-delivered event with the same text and author makes no second write.

An example consumer workflow is in [`examples/rca-feedback.yml`](examples/rca-feedback.yml).
Wiring it into `action.yml` is a later step.

## Where a comment applies

| Comment on | Applies to |
|---|---|
| An RCA fingerprint issue | That issue (a `#N` token is ignored). |
| A pull request, with `#N` | Issue N — only if it is an RCA fingerprint issue. |
| A pull request, without `#N` | The fingerprint issue(s) behind the PR's RCA comment (the most recently updated one if there are several). |
| Anything else | Nothing (skipped with a note). |

The command must be the **first non-blank line** (`/resolved`, case-sensitive). Quoted (`>`) or
fenced text never counts, and a command without text is ignored.

## Security

- **The comment text is read from the event file only.** It never reaches argv, `env:` or a `run:`
  script, so it cannot inject shell or workflow expressions.
- **The pull request's code is never checked out.** The workflow checks out only the pinned RCA
  tool; nothing from the PR runs with the `issues: write` token.
- **`author_association` is enforced.** Anyone can comment on a public repository, so only
  `OWNER`, `MEMBER` and `COLLABORATOR` may resolve failures by default. Narrow or widen the set
  with `RCA_RESOLVE_ASSOCIATIONS` (comma list, e.g. `OWNER`). Bot authors are always ignored.
- **The stored text is made safe**: redacted, `@mentions` and rule ids defused, capped at ~300
  characters. The resolution author is shown in a code span, so nobody is pinged.

## Reactions

👍 / 👎 reactions on the sticky RCA comment are read from the comment payload the delivery run
already fetches (no extra API call) and reported as `delivery.feedback` (`{"up": n, "down": n}`)
and one preview line. They are per comment, never summed onto the issue record.

## Deferred

Inferred resolution ("the fingerprint disappeared and the pipeline is green") needs a
success-path trigger, which arrives with the delivery job in a later step.
