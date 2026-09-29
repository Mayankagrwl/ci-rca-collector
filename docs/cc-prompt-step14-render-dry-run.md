# Claude Code task — Step 14: comment rendering + `deliver --dry-run` + `DeliveryReport` write-back

## Context

`ci-rca-collector`. Read `AGENTS.md` and `docs/rca-delivery-spec-v1.3.md` (**§3, §7, §7.1–§7.3, §11, §13, §14, §15**) first.

**Prerequisite — stop and report if missing:** Step 13 merged (`tools/rca/deliver/{targets,severity,suppress}.py`, `ROUTING_TABLE`, `evaluate()`, `ref_is_tag`).

This step makes Phase 3 **visible without posting anything**. It renders the PR/commit comment body and adds a `deliver` CLI subcommand that, in this step, **only** writes `rca/delivery-preview.md` and a `delivery` block into `summary.json`. That is rollout stage 1 of v1.3 §18: a week of reading previews before anything can post.

**Zero GitHub writes.** Read-only lookups (`get_repo`, `ref_is_tag`, `get_run`, `list_runs`) are allowed, and the command must also work fully offline.

Do **only** what this document scopes.

## Verified facts to build on (do not re-derive)

- Step 13 API: `resolve_context(summary, *, client, repo, env, now) -> (DeliveryContext, notes)`; `route(context, inputs) -> RoutePlan`; `should_open_issue(context, summary, inputs)`; `default_branch_state(summary, *, client, repo, default_branch, now)`; `severity(context, state, summary, ...)`; `suppress.evaluate(summary, record, context, inputs, *, existing=None, now=None) -> SuppressionDecision` (Stage A sets `suppressed_by`; Stage C sets `unverified_banner` / `omit_root_cause`). `DeliveryInputs` holds every v1.3 §13 input with defaults.
- `summary.json` is written by collect; `analyze` then merges an extra top-level `"analysis"` key into it and also writes `rca/analysis.json`. `Summary` itself has no `analysis` field (Pydantic ignores the extra key), so load the analysis record separately: `--analysis` path if present, else `summary.json["analysis"]`, else none.
- Available fields: `Summary.run.{run_id, run_attempt, html_url, workflow_name}`, `FailedJob.{failed_step_name, primary_failure_line, failed_step_excerpt.lines}`, `Summary.history.seen_count` (times seen **before** this run), `Summary.classification.category`, `Summary.diagnosis.{one_liner, fix_one_liner}`, `Summary.fingerprint_coarse`, `AnalysisRecord.{result, grounding}`.
- Reuse, do not reimplement: `analyze.display_status()`, `prompt.failed_step_anchor_text()` (already includes the pipeline first-error window when the job log is uninformative — Step 9), `extract.is_benign_line()`, `redact.redact_text()`, `tools/eval/labels.VALID_CATEGORIES` **as a test oracle only** (production code must not import `tools/eval`).

## Deliverables

### 1. `tools/rca/deliver/render_comment.py` — pure

`render_comment(summary, record, context, decision) -> str`, following v1.3 §7 exactly:

```
<!-- rca-bot:fp=<fingerprint_coarse> -->
[Unverified banner, only if decision.unverified_banner]
### CI failure — <category in human words>

**<headline>**                       <- omitted entirely when decision.omit_root_cause
Confidence: <label> · `<failed step name>` · [run <run_id>](<html_url>)

**Suggested fix** — <one line>       <- omitted when omit_root_cause, unless the fix is generic and non-claiming

<details><summary>Evidence</summary>

<fence>text
<evidence lines>
<fence>
</details>

---
<sub>Seen N× · [full report](<html_url>) · reply `/resolved <what fixed it>`</sub>
```

- **Category human words:** one fixed map covering **every** category the collector can emit, including `release` ("release version already published") and `unknown` ("unclassified failure"). A missing key must never render a raw category name (fall back to a generic phrase).
- **Headline precedence (v1.3 §7.1), in order:** (1) `decision.omit_root_cause` → no root-cause line from either source; (2) `display_status ∈ {ok, cached}` and `result.source ∈ {ai, hybrid, mixed}` → `record.result.root_cause`; (3) otherwise → `summary.diagnosis.one_liner`, fix from `fix_one_liner`; (4) `display_status == "needs-review"` → post with the banner and the deterministic text, never as confirmed. The confidence label comes from the same source as the headline.
- **Evidence block (v1.3 §7.2), max 8 lines, deduplicated:** `primary_failure_line` first; then `failed_step_excerpt.lines` **excluding benign lines** (`is_benign_line`); then grounded citation quotes that also appear in `failed_step_anchor_text(summary)`. Never lead with a `log_templates` or pipeline line that isn't in that anchor. This keeps the Step 9 Java case (cause only in the pipeline log) and the Docker "Already exists 0B" case correct.
- **"Seen N×":** `N = seen_count + 1` (this run included).
- **Run link:** `[run <run_id>](<html_url>)`, adding ` (attempt <n>)` when `run_attempt > 1`. Do not paste `summary.md`.
- **Sticky marker:** the first line is exactly `<!-- rca-bot:fp=<fingerprint_coarse> -->` (Step 15 finds comments by it).
- **Safety (v1.3 §11, plus two additions):**
  - Redact the **whole body before truncating** (`redact_text`), so truncation can never split a redaction.
  - Choose a code fence longer than any backtick run inside the evidence, so log text can't break out of the fence.
  - When `context.is_fork`, HTML-escape model- and log-derived text outside the fence.
  - **Neutralise `@mentions`** in model- and log-derived text for all triggers (for example insert a zero-width space after `@`). Log lines and model prose must never ping real users. This is not in v1.3; it is required here.
- **Hard cap 4000 characters.** First shrink the evidence block, then drop `<details>` entirely; never cut the marker, heading, headline, or footer. Also cap the headline and the fix at a sensible one-line length (~300 chars each).
- **No rule ids:** the final body must never match `\bR\d+\b` in prose (outside the marker).

### 2. `DeliveryReport` on `Summary` (v1.3 §15)

Add `DeliveryReport` to `models.py` exactly as v1.3 §15, plus `notes: list[str] = []` for resolver notes, and `delivery: DeliveryReport | None = None` on `Summary`. Old `summary.json` files must still load.

### 3. `deliver` CLI subcommand (dry-run only in this step)

```
python -m tools.rca.cli deliver --summary rca/summary.json [--analysis rca/analysis.json] --out rca \
    [--dry-run] [--offline] [--repo owner/name] \
    [--comment-on-pr ...] [--comment-on-commit ...] [--comment-on-branch-push ...] \
    [--create-issues ...] [--issue-threshold N] [--allow-fork-issues ...] \
    [--confidence-threshold low|medium|high] [--quiet-window-minutes N] \
    [--platform-team ...] [--default-notify ...]
```

- Build `DeliveryInputs` from the flags (names mirror v1.3 §13; defaults from the model). Step 19 will map action inputs onto these flags.
- Flow: load summary + analysis → `resolve_context` → `route` → `should_open_issue` → `default_branch_state` (only for `push_default` / `tag`) → `severity` → `suppress.evaluate(existing=None)` → `render_comment` if the plan has a comment and Stage A did not suppress.
- Client: `GitHubClient(timeout=15)` via the existing host/token resolution. **`--offline`** skips every API call (the Step 13 fallbacks handle it: `RCA_DEFAULT_BRANCH` / `main`, not-a-tag, history-based state) and records notes.
- **Writes, all local:**
  - `rca/delivery-preview.md`: a short header (trigger, severity, route plan — which channels *would* be used — `suppressed_by`, modifiers, whether an issue would be opened, notes), then the comment body **verbatim** between clear markers, so it is byte-identical to what Step 15 will post. When no comment would be posted, say why.
  - `summary.json` write-back (v1.3 §15 rule): re-read the file as a dict, set **only** the `delivery` key (`dry_run=True`, `delivered_to=[]`), write it back. Never regenerate the file, never drop or alter `analysis`.
  - When `GITHUB_OUTPUT` is set, append `severity`, `suppressed-by`, `delivered-to`, `comment-url`, `issue-url` (add a small `DELIVERY_OUTPUT_KEYS` writer in `outputs.py`; empty values are fine).
- **Without `--dry-run`** in this step: behave exactly as dry-run and record the note "live delivery is enabled in Step 15". Never write to GitHub.
- **Never fail the workflow:** catch everything, still write whatever preview/report is possible, exit 0 unless `--strict`.

## Constraints (AGENTS.md)

- No GitHub writes anywhere in this step. No `action.yml` or workflow changes (Step 19). Collect is untouched.
- `render_comment.py` is pure: no `github_api`, no `httpx`, no clock, no file I/O.
- `tools/rca/` must not import `tools/eval/`; tests may.
- No hardcoded project names or messages in rendering; everything comes from the summary/record and fixed generic copy.

## Acceptance criteria (tests under `tests/rca/deliver/`)

1. **Goldens render correctly (deterministic path, no model):** for every golden under `tools/eval/goldens/`, render a body and assert: ≤ 4000 chars; starts with the marker; no `\bR\d+\b` outside the marker; no raw category key.
   - `artifactory-version-exists` → heading uses the `release` human words (never "unknown") and the headline is the `primary_failure_line` (AC #34).
   - `java-compile-in-pipeline` → the evidence contains the compile-error line and does **not** lead with "No services to build" / "Using … service".
   - `docker-already-exists-benign` → "Already exists 0B" is neither the headline nor an evidence line.
2. **Headline precedence** — one test per §7.1 rule, including C2 omitting the root cause from both sources (AC #10) and needs-review rendering with the banner (AC #11).
3. **Cap** — a summary with a huge excerpt stays ≤ 4000 with marker, heading, headline, and footer intact; truncation happens after redaction (a secret near the cut point never appears partially) (AC #24).
4. **Safety** — a log line containing a triple-backtick run can't break the fence; `@someone` in model text and log text is neutralised; fork context HTML-escapes `<script>`-like text (AC #18 spirit).
5. **Every category has human words** — iterate `VALID_CATEGORIES` from `tools/eval/labels.py` (test-only import) plus `unknown`.
6. **CLI dry-run** — run `deliver --dry-run --offline` against a golden: preview written; the body inside it equals `render_comment(...)` byte-for-byte (AC #23); `summary.json` gains `delivery` with `dry_run=True` while `analysis` is preserved unchanged (AC #29); a fake/mock client records **no** non-GET request (zero writes).
7. **Suppressed run** — a flaky golden produces a preview stating `suppressed_by=flaky` and no comment body; the `delivery` block records it (AC #12).
8. **Resilience** — a corrupt `analysis.json` still produces a deterministic preview; an exception inside rendering still writes the report with an error note and exits 0.
9. Suites green: `pytest tests/rca -q`, `pytest tests/eval -q`; acceptance-35 grep empty; eval `--no-llm` still 1.0.

## Out of scope

- Finding or editing existing comments, posting, labels, reactions (Step 15). Issues (Step 16). CODEOWNERS / `owner_resolved_by` (Step 17 — leave it `None`). Notifications (Step 18). `action.yml` inputs/outputs and the delivery job (Step 19).

## Deliverable

`deliver/render_comment.py`, the `deliver` subcommand in `cli.py`, `DeliveryReport` in `models.py`, the delivery output writer in `outputs.py`, and tests, with both suites green. Show the rendered preview for `artifactory-version-exists` and for `java-compile-in-pipeline`.
