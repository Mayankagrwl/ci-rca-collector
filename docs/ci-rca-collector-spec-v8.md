# Build Spec: CI Failure Context Collector (Phase 1 — Collection Only)

**Spec version 8.0**

> **How to use this file:** Paste this whole document into GitHub Copilot Chat (or open it in the
> repo and reference it with `#file:ci-rca-collector-spec-v8.md`) and ask it to implement the modules
> in the order listed under "Build order". Each module has an explicit signature and contract, so
> Copilot can be asked to generate them one at a time.

### Changes from v7

An audit of evidence collection found seven categories missing. Two matter disproportionately on
ephemeral self-hosted runners.

- **New Stage 2 — blast-radius check.** One API call asks whether the rest of the repository is
  also failing right now. Three workflows down across two unrelated branches in 30 minutes is
  infrastructure, and no amount of log reading will say so. The negative case is recorded too:
  "nothing else failed" is evidence that the change is implicated.
- **Runner identity.** `runner_name`, `runner_group`, `labels`, plus image and OS extracted from
  the `Set up job` group. Failures concentrated on one runner pool or image tag are an
  infrastructure problem whatever the logs claim — and on a rolling ARC image, an image change is
  otherwise invisible.
- **Queue time** per job. Eleven minutes waiting for a runner is a capacity problem, not a broken
  test, and it belongs to a different team.
- **Full step table**, not just the failed step. Includes duration against the last green run
  (>3× suggests a hang) and cache-restore-miss detection.
- **Artifact inventory.** Parse what is parseable, list the rest. A `playwright-traces` artifact is
  often the most actionable line in the report, and v7 would never have mentioned it.
- **Timeout detection made explicit.** v7's `cancelled` exclusion would have silently discarded
  every timed-out job. Now matched on log signature rather than trusting the conclusion field.
- **Post-step failure guard.** v7 dropped `Post job cleanup` output as noise unconditionally; when
  the failure *is* in a post step that left an empty report.
- **Log volume** recorded as a signal — 40× normal output means looping, near-zero means it died
  before starting.
- New `StepInfo`, `ArtifactInfo`, `RunnerInfo` models; `infra_widespread` short-circuit added.
  Stages 2–8 renumbered.

### Changes in v7 (retained)

Change and commit context was under-specified. v6 computed a blame range and even rendered
"(3 commits)" in the report — while never fetching what those commits were.

- **Commits in the range are now collected.** Short SHA, subject line, author, files-changed count,
  capped at 10. Commit subjects are the cheapest high-signal evidence available and were missing.
- **Compare API replaces per-commit lookups.** `GET /compare/{base}...{head}` returns commits and
  cumulative files in one call. v6 analysed only the tip commit, missing the change that broke it
  whenever a PR carried more than one.
- **Blast-radius classification of changed paths.** `ci_config`, `container`, `lockfile` and five
  others. A build breaking the day the pipeline config changed is almost never coincidence, and
  this makes it visible without reading a diff.
- **Revert and merge detection** on commit subjects and parent counts.
- **First failing commit narrowing.** Where intermediate runs exist, the last pass and first fail
  bracket the culprit exactly.
- **PR metadata** — title, labels, draft status, 500-char body excerpt. The title often states the
  intent the diff only implies.
- **Range truncation and base-resolution fallbacks** handled explicitly, with `range_basis`
  recorded so a reader knows what the comparison actually means.
- New `CommitInfo` and `ChangeContext` models; `HistoryContext` gains `last_success_age_hours` and
  `first_failing_sha`. Change-context budget raised 700 → 900 tokens.

### Changes in v6 (retained)

Phase boundaries made explicit, plus four operational gaps found in a re-read.

- **§1 restructured** into objective, a four-phase table, and forward-compatibility rules.
  **Phases 1–3 are CI only; CD is Phase 4 and nothing before it** — now stated where someone
  reading top-down will see it, not only in the deferred-work section.
- **New §1.3 — source-agnostic interfaces.** An `EvidenceSource` / `SourceEvidence` boundary, and
  a rule that `cleaner`, `drain_index`, `budget`, `redact` and `history` must never import anything
  GitHub-specific. This is what decides whether Phase 4 is an addition or a rewrite.
- **Matrix builds (Stage 0).** v5 said "for each failed job" with no cap — 20 failed legs would
  have blown the budget. Now capped at 3, earliest-first, with identical-leg detection and
  `cancelled` legs excluded.
- **Pull-request context (Stage 0).** `workflow_run.pull_requests` is empty for fork PRs; a
  commit-to-PR fallback and an `is_fork` flag are now specified, captured in Phase 1 because
  Phase 3 cannot safely work without them.
- **API rate limits (Stage 4).** `GITHUB_TOKEN` gets 1,000 requests/hour per repo, not 5,000.
  Header-driven backoff and graceful degradation added.
- **Runtime budget and collect-storms (§13).** A 90-second collection target with a hard job
  timeout, and a concurrency group so simultaneous failures do not pile up.
- `RunMeta` added to the schema explicitly, carrying the PR and matrix fields.

### Changes in v5 (retained)

Two defects in v4's recurrence design, both of which would have shipped and then quietly produced
wrong results rather than errors.

- **Jaccard similarity corrected (§10.2).** v4 said to compare sets of *template IDs*. Drain3
  cluster IDs depend on insertion order into a state file, and `template_id` is a per-report
  ordinal — neither is stable across runs. Similarity now compares **masked template strings**
  (or per-template hashes of them). The old version would have produced plausible-looking
  similarity scores that were noise.
- **New §10.3 — masking config is schema.** Editing `drain3.ini` changes every template string,
  therefore every fingerprint, therefore orphans the entire history in one commit — silently, with
  every failure reporting as `new`. Adds a `masking_config_hash` stamped on each record, a
  degrade-to-similarity path on drift, and a re-fingerprinting migration route.
- `FailureRecord` gains `template_hashes` and `masking_config_hash`; `HistoryContext` gains
  `config_drift`; `DrainReport` gains `masking_config_hash`.
- §10.4 onward renumbered.

### Changes in v4 (retained)

v3 specified fingerprint history in a single line: a JSON map in `.rca-history/`, cached. That is
enough to prototype and not enough to build. v4 adds a full section on failure memory.

- **New §10 — Failure memory.** States plainly that **no model is trained**; recurrence is a lookup
  table the collector owns, injected into the report as evidence.
- **Dual fingerprints** (`fine` and `coarse`) so a reworded error message no longer defeats
  recurrence detection, with Jaccard similarity for near-misses and an explicit rejection of vector
  embeddings for this problem.
- **A real `FailureRecord` schema**, including the `resolution` field — the highest-value field and
  the only one that cannot be populated automatically.
- **Pluggable `HistoryStore` backends** — `cache` | `issues` | `redis` | `none` — behind one
  interface, selected by a new `history-backend` action input.
- **`actions/cache` is now labelled a Phase 1 expedient**, with its ~7-day eviction and 10 GB
  per-repo LRU cap documented, and a GitHub Issues backend specified as the Phase 3 destination.
- **Concurrency and idempotency rules** for parallel failing jobs racing on the store.

### Changes in v3 (retained)

v2 specified the collector as a Python module invoked inline by a single workflow. That works in one
repository and nowhere else. v3 packages it as a **reusable composite GitHub Action** plus an
optional **reusable workflow**, so any repo in the org can adopt it with a few lines of YAML.

- **New §5 — Packaging as a GitHub Action.** Full `action.yml` contract: inputs, outputs, composite
  steps, and the `${{ github.action_path }}` mechanism that makes the collector's own code available
  without the caller checking out anything.
- **Action outputs** (`category`, `fingerprint`, `is-flaky`, `short-circuit`, `requires-analysis`,
  `summary-path`) so callers can branch on the result — this is the hook Phase 2 and Phase 3 plug into.
- **Reusable workflow variant** (`on: workflow_call`) for callers who want caching, artifact upload,
  and job summary bundled in.
- **Repo layout restructured** into an action repo and a thin consumer stub.
- **Cross-repo concerns documented:** cache scoping, private-repo action access, version pinning,
  and self-hosted runners without `setup-python`.

**The `workflow_run` trigger cannot be inherited.** It must be declared in each consuming repo, on
that repo's default branch. So every consumer keeps a small local stub; only the logic is shared.

### Changes in v2 (retained)

The log-template handling was substantially reworked. Earlier drafts had two weakly-specified
sections ("Distinct Error Lines" and "Novel Log Lines"); v2 merged them into one **Log Templates**
section with a precise table shape and deterministic ranking.

- **Tier-based ranking (T1–T5)** replaces implicit "relevance" ordering. No weighted scores.
- **Baseline counts** are now carried alongside current counts, enabling **depletion detection** —
  a template appearing far *less* than usual is a signal v1 would have discarded as noise.
- **Novelty flag and first-occurrence line number** per template. Sequence is what separates cause
  from symptom; clustering destroys it, so it must be carried explicitly.
- **Extracted variables** (`host=db`, `port=5432`) and numeric ranges per template.
- **Variables must be redacted** — a masked token still surfaces as a variable value.
- **Fingerprint excludes counts.** Hash template strings only, or recurrence detection breaks
  whenever load varies.

---

## 1. Objective, scope and phases

### 1.1 What this builds

A **reusable GitHub Action** that, when a GitHub Actions CI workflow fails, collects everything
needed to diagnose the failure and emits **two artifacts**:

| File | Purpose |
|---|---|
| `rca/summary.json` | Machine-readable, stable schema. This is the future input to an LLM. |
| `rca/summary.md` | Human-readable "Log Summary". Rendered to the job summary and uploaded as an artifact. |

It must be consumable from any repository in the org with a few lines of YAML:

```yaml
- uses: acme/ci-rca-collector@v1
  with:
    run-id: ${{ github.event.workflow_run.id }}
```

**Phase 1 scope is collection and rendering only. No LLM call is made.** The tool must produce a
complete, correct `summary.md` that a human can read and say "yes, that's enough to diagnose this."
That is the acceptance bar.

### 1.2 The four phases

| | Phase | Covers | Ships | Adds |
|---|---|---|---|---|
| **1** | **Foundation** | **CI** | Evidence collection, noise reduction, heuristic classification, recurrence memory, a readable report | The report. No AI anywhere. |
| **2** | **Analysis** | **CI** | An LLM call, gated on `requires-analysis`, with validated structured output | Plain-language root cause for novel failures |
| **3** | **Delivery** | **CI** | PR comments, issues, email; history moves to a durable backend | The answer arrives where people already look |
| **4** | **Deployments** | **CD** | Kubernetes deploy and runtime failures | Same report shape, wider coverage |

**Phases 1–3 are CI only.** CD is Phase 4 and nothing before it. Per-phase deliverables are in §15.

Each phase extends the same action rather than replacing it, and `summary.json` is the interface
between them. Phase 1 is useful standing alone; every later phase is additive.

**Explicitly out of scope for Phase 1:** any HTTP call to an inference endpoint, prompt templates,
JSON-schema-constrained model output, PR comments, email, issue creation, and anything requiring
cluster access — no kubeconfig, no `kubectl`, no Kubernetes API calls.

### 1.3 Forward compatibility — what Phase 1 must keep source-agnostic

CD is deferred, but Phase 4 is an *addition* only if Phase 1 is built as a pipeline with a CI
collector plugged into it, rather than as CI-specific code with a report bolted on. Three
interfaces carry the boundary. Define them in Phase 1 even though only the CI implementation exists.

```python
class EvidenceSource(Protocol):
    """One thing that can be investigated: a failed CI run, or later a failed rollout."""
    kind: Literal["ci_run", "k8s_rollout"]

    def collect(self) -> SourceEvidence: ...
    """Return raw text streams plus structured facts. Must not know about
    reports, budgets, prompts, or rendering."""

class SourceEvidence(BaseModel):
    kind: str
    streams: list[TextStream]        # named log-like text; Drain3 consumes these
    facts: dict[str, Any]            # structured, source-specific (job table / pod table)
    identity: SourceIdentity         # what to fingerprint on and how to link back
```

The rules that make Phase 4 cheap:

1. **`cleaner.py`, `drain_index.py`, `budget.py`, `redact.py` and `history.py` must never import
   anything GitHub-specific.** They operate on `TextStream` and template strings. A `kubectl logs`
   stream must flow through them unchanged. If any of them needs a `job_id`, the abstraction has
   already leaked.
2. **`render.py` renders sections that may be absent**, driven by which keys `Summary` carries —
   not by a hardcoded CI running order. Phase 4 adds a `kubernetes` block and the renderer picks it
   up without modification.
3. **Fingerprinting is source-agnostic** by construction, since it operates on masked template
   strings (§10.2). A CrashLoopBackOff fingerprints by the same code path as a failed test.

Conversely, these are *expected* to be CI-specific and need no abstraction in Phase 1:
`github_api.py`, `changes.py`, the runner-health checks in `classify.py`, and the `workflow_run`
wiring. Phase 4 adds siblings rather than generalising these.

The reserved `kubernetes: None` key in `Summary` (§11) is the cheap half of this. The interface
discipline above is the half that actually determines whether Phase 4 is a week or a rewrite.

---

## 2. Environment context (affects design — do not ignore)

- CI runs on **GitHub Actions**.
- Runners are **ephemeral self-hosted pods** created on the fly (ARC-style). The pod is destroyed
  when the job ends, and it can also be evicted or OOM-killed mid-job.
- CD deploys to Kubernetes via manifests. **CD is out of scope for Phase 1**, but the schema must
  reserve space for it (see `kubernetes` key, always `null` in Phase 1).

Two consequences that drive the architecture:

1. **The collector must NOT run as an `if: failure()` step inside the CI job.** If the runner pod
   dies, that step never executes. It runs as a **separate workflow triggered by `workflow_run`**,
   on a fresh runner, pulling everything through the GitHub REST API.
2. **Runner-infrastructure failures must be detected and reported as such.** A pod eviction looks
   exactly like a build failure in the logs and would otherwise produce a confident, wrong diagnosis.

---

## 3. Authentication — answering the token question

**No PAT or manually stored API key is required for same-repository use.**

GitHub automatically creates a `GITHUB_TOKEN` for every workflow run. You consume it as:

```yaml
env:
  GH_TOKEN: ${{ github.token }}          # gh CLI reads GH_TOKEN
  GITHUB_TOKEN: ${{ github.token }}      # PyGithub / requests read GITHUB_TOKEN
```

Requirements and gotchas the implementation must respect:

- The workflow **must** declare `permissions: { actions: read, contents: read }`. Reading another
  run's logs requires the `actions: read` scope, and many orgs default the token to restricted
  permissions.
- The token is scoped to the current repository only. If you later need cross-repo reads, that is
  when a PAT or GitHub App becomes necessary — not now.
- The token is valid only for the duration of the run; it cannot be cached or reused.
- A `GITHUB_TOKEN` cannot trigger further workflow runs. Irrelevant now, relevant in Phase 3 when
  posting comments.
- For local development, the tool must accept a PAT via the same `GITHUB_TOKEN` env var so it can
  run against real historical runs from a laptop.

Never log the token. Never write it into `summary.json` or `summary.md`.

---

## 4. Repository layout

Two repositories are involved.

### 4.1 The action repository — `acme/ci-rca-collector`

```
action.yml                      # composite action definition (repo root — required)
requirements.txt
drain3.ini
tools/rca/
  __init__.py
  cli.py                        # entrypoint: python -m tools.rca.cli
  config.py                     # budgets, regex tables, tunables
  models.py                     # Pydantic models = the schema, single source of truth
  github_api.py                 # REST client: runs, jobs, logs, history
  cleaner.py                    # ANSI/timestamp stripping, multi-line joining
  classify.py                   # regex pre-classification + runner-health checks
  drain_index.py                # Drain3 wrapper: baseline train + novelty diff
  extract.py                    # error windows, stack traces, JUnit, annotations
  changes.py                    # changed files, diffstat, lockfile deltas
  history.py                    # last success, same-SHA reruns, fingerprint store
  budget.py                     # token estimation + trimming
  render.py                     # summary.md renderer
  redact.py                     # secret scrubbing
  outputs.py                    # writes GITHUB_OUTPUT / GITHUB_STEP_SUMMARY
.github/workflows/
  rca.yml                       # OPTIONAL reusable workflow (on: workflow_call)
  self-test.yml                 # dogfoods the action against tests/rca/fixtures
tests/rca/
  fixtures/                     # captured raw logs from real failures
  test_*.py
```

`action.yml` **must** live at the repository root. GitHub resolves `uses: acme/ci-rca-collector@v1`
to `action.yml` or `action.yaml` at the root of the default branch or the pinned ref. An action in a
subdirectory requires the longer `acme/ci-rca-collector/path/to/action@v1` form; keep it at root.

### 4.2 The consuming repository

```
.github/workflows/
  ci.yml                        # existing CI (unchanged)
  rca-collect.yml               # thin stub — MUST exist here, see §5.5
  test-failures.yml             # optional: deliberately failing jobs, for testing
```

The consumer holds no Python. The stub exists solely because the `workflow_run` trigger cannot be
inherited from a reusable workflow or action — it must be declared locally, on the default branch.

---

## 5. Packaging as a GitHub Action

### 5.1 Why a composite action

Three packaging options were considered:

| Option | Verdict |
|---|---|
| **Composite action** | **Chosen.** Runs natively on any runner, no image pull, transparent steps in the log, and the action's own files are available via `${{ github.action_path }}`. |
| Docker container action | Rejected. Image pull cost on every ephemeral runner pod, Linux-only, and opaque logs. |
| JavaScript action | Rejected. Would mean rewriting the collector in TypeScript for no gain. |

A **reusable workflow** is offered *in addition* (§5.4), not instead — the two solve different
problems and callers may want either.

### 5.2 The `${{ github.action_path }}` mechanism

This is the detail that makes a composite action work here, and it is easy to get wrong.

When a caller runs `uses: acme/ci-rca-collector@v1`, GitHub downloads the action's repository to a
temporary directory and exposes that path as `${{ github.action_path }}`. **The caller does not need
`actions/checkout` for the action's code**, and the action must not call `actions/checkout` itself —
doing so would clobber the caller's workspace.

Consequences the implementation must respect:

- Reference the collector as `${{ github.action_path }}/tools/rca/cli.py`, never a relative path.
- Set `PYTHONPATH="${{ github.action_path }}"` so `python -m tools.rca.cli` resolves.
- `drain3.ini` and `requirements.txt` are read from `${{ github.action_path }}`, not the workspace.
- Output files go to the **caller's** workspace (`$GITHUB_WORKSPACE`), so the caller can upload them.
- `github.action_path` is only defined inside composite action steps. It is empty in a caller's
  workflow — do not reference it from the stub.

### 5.3 `action.yml` contract

```yaml
name: 'CI Failure Context Collector'
description: 'Collects and summarises GitHub Actions failure context for root-cause analysis.'
author: 'acme'

branding:
  icon: 'search'
  color: 'orange'

inputs:
  run-id:
    description: 'Workflow run ID to analyse. Usually github.event.workflow_run.id.'
    required: true
  repository:
    description: 'owner/repo to analyse.'
    required: false
    default: ${{ github.repository }}
  mode:
    description: 'collect | train. "train" builds the Drain3 baseline from a successful run.'
    required: false
    default: 'collect'
  github-token:
    description: 'Token with actions:read and contents:read.'
    required: false
    default: ${{ github.token }}
  output-dir:
    description: 'Directory for summary.json and summary.md, relative to the workspace.'
    required: false
    default: 'rca'
  token-budget:
    description: 'Total evidence token budget. See §8.'
    required: false
    default: '6000'
  drain-cache-dir:
    description: 'Drain3 baseline state directory. Caller is responsible for caching it.'
    required: false
    default: '.drain'
  history-dir:
    description: 'Fingerprint history directory, used by the cache backend. Caller caches it.'
    required: false
    default: '.rca-history'
  history-backend:
    description: 'Where failure memory is stored: cache | issues | redis | none. See §10.4.'
    required: false
    default: 'cache'
  history-redis-url:
    description: 'Connection URL when history-backend is redis. Pass via a secret.'
    required: false
    default: ''
  python-version:
    description: 'Python to set up. Ignored when setup-python is false.'
    required: false
    default: '3.12'
  setup-python:
    description: 'Set false on self-hosted images that already ship a suitable Python.'
    required: false
    default: 'true'
  write-job-summary:
    description: 'Append summary.md to $GITHUB_STEP_SUMMARY.'
    required: false
    default: 'true'
  fail-on-error:
    description: 'Fail the step if collection itself errors. Default false — see §13.'
    required: false
    default: 'false'

outputs:
  summary-path:
    description: 'Path to summary.md.'
    value: ${{ steps.collect.outputs.summary-path }}
  json-path:
    description: 'Path to summary.json.'
    value: ${{ steps.collect.outputs.json-path }}
  category:
    description: 'Heuristic classification: oom, dependency, compile, test_failure, etc.'
    value: ${{ steps.collect.outputs.category }}
  confidence:
    description: 'high | medium | low.'
    value: ${{ steps.collect.outputs.confidence }}
  is-infra-vs-code:
    description: 'infra | code | unknown.'
    value: ${{ steps.collect.outputs.is-infra-vs-code }}
  is-flaky:
    description: 'true when the same SHA passed in another run.'
    value: ${{ steps.collect.outputs.is-flaky }}
  short-circuit:
    description: 'infra_runner | flake_same_sha_passed | no_failed_jobs | empty string.'
    value: ${{ steps.collect.outputs.short-circuit }}
  requires-analysis:
    description: 'false when a deterministic verdict was reached. Phase 2 gates the LLM call on this.'
    value: ${{ steps.collect.outputs.requires-analysis }}
  fingerprint:
    description: '16-hex-char failure fingerprint.'
    value: ${{ steps.collect.outputs.fingerprint }}
  seen-count:
    description: 'Times this fingerprint has been seen before.'
    value: ${{ steps.collect.outputs.seen-count }}
  recurrence:
    description: 'new | similar | exact. See §10.2.'
    value: ${{ steps.collect.outputs.recurrence }}
  failed-job-count:
    description: 'Number of failed jobs found.'
    value: ${{ steps.collect.outputs.failed-job-count }}

runs:
  using: 'composite'
  steps:
    - name: Set up Python
      if: inputs.setup-python == 'true'
      uses: actions/setup-python@v5
      with:
        python-version: ${{ inputs.python-version }}

    - name: Install collector dependencies
      shell: bash
      run: pip install --disable-pip-version-check -r "$GITHUB_ACTION_PATH/requirements.txt"

    - name: Run collector
      id: collect
      shell: bash
      env:
        GITHUB_TOKEN: ${{ inputs.github-token }}
        PYTHONPATH: ${{ github.action_path }}
        RCA_DRAIN_CONFIG: ${{ github.action_path }}/drain3.ini
        RCA_REDIS_URL: ${{ inputs.history-redis-url }}
      run: |
        python -m tools.rca.cli "${{ inputs.mode }}" \
          --run-id "${{ inputs.run-id }}" \
          --repo "${{ inputs.repository }}" \
          --out "${{ inputs.output-dir }}" \
          --token-budget "${{ inputs.token-budget }}" \
          --drain-dir "${{ inputs.drain-cache-dir }}" \
          --history-dir "${{ inputs.history-dir }}" \
          --history-backend "${{ inputs.history-backend }}" \
          ${{ inputs.fail-on-error == 'true' && '--strict' || '' }}

    - name: Write job summary
      if: inputs.write-job-summary == 'true' && inputs.mode == 'collect'
      shell: bash
      run: |
        if [ -f "${{ inputs.output-dir }}/summary.md" ]; then
          cat "${{ inputs.output-dir }}/summary.md" >> "$GITHUB_STEP_SUMMARY"
        fi
```

Implementation notes:

- Every composite step **requires `shell:`**. Omitting it is the most common composite-action error.
- Use `$GITHUB_ACTION_PATH` (the env var) inside `run:` blocks and `${{ github.action_path }}` in
  `env:` and `with:` blocks. Both resolve to the same path.
- **Never interpolate `${{ inputs.* }}` directly into a `run:` body in a public action** — it is a
  script-injection vector. For internal use with trusted callers the risk is low, but prefer passing
  values through `env:` and referencing `$VAR` where practical. Treat `run-id` and `repository` as
  trusted only because they come from the `workflow_run` payload.
- `outputs.py` writes each key/value to `$GITHUB_OUTPUT` as `key=value`. Multi-line values need the
  heredoc form (`key<<EOF`); none of the outputs above are multi-line, so keep them single-line and
  keep `summary.md` as a file path rather than a value.
- Booleans in Action outputs are **strings**. Callers must compare with `== 'true'`, and the spec's
  example workflows must do the same.

### 5.4 Optional reusable workflow — `.github/workflows/rca.yml`

For callers who want caching, artifact upload, and job summary bundled rather than assembled.

```yaml
name: RCA Collect (reusable)

on:
  workflow_call:
    inputs:
      run-id:        { required: true,  type: string }
      workflow-name: { required: true,  type: string }
      conclusion:    { required: true,  type: string }
      head-branch:   { required: false, type: string, default: '' }
      default-branch:{ required: false, type: string, default: 'main' }
      runs-on:       { required: false, type: string, default: 'ubuntu-latest' }
    outputs:
      category:      { value: ${{ jobs.collect.outputs.category }} }
      fingerprint:   { value: ${{ jobs.collect.outputs.fingerprint }} }
      is-flaky:      { value: ${{ jobs.collect.outputs.is-flaky }} }
      requires-analysis: { value: ${{ jobs.collect.outputs.requires-analysis }} }

permissions:
  actions: read
  contents: read

jobs:
  collect:
    runs-on: ${{ inputs.runs-on }}
    outputs:
      category: ${{ steps.rca.outputs.category }}
      fingerprint: ${{ steps.rca.outputs.fingerprint }}
      is-flaky: ${{ steps.rca.outputs.is-flaky }}
      requires-analysis: ${{ steps.rca.outputs.requires-analysis }}
    steps:
      - name: Restore Drain3 baseline
        uses: actions/cache@v4
        with:
          path: .drain/
          key: drain3-${{ inputs.workflow-name }}-${{ inputs.run-id }}
          restore-keys: drain3-${{ inputs.workflow-name }}-

      - name: Restore fingerprint history
        uses: actions/cache@v4
        with:
          path: .rca-history/
          key: rca-history-${{ inputs.run-id }}
          restore-keys: rca-history-

      - name: Collect
        id: rca
        uses: acme/ci-rca-collector@v1
        with:
          run-id: ${{ inputs.run-id }}
          mode: ${{ inputs.conclusion == 'success' && 'train' || 'collect' }}

      - name: Upload artifact
        if: inputs.conclusion == 'failure'
        uses: actions/upload-artifact@v4
        with:
          name: rca-${{ inputs.run-id }}
          path: rca/
          retention-days: 30
```

**Caveat:** a reusable workflow can only be nested three levels deep, and its `permissions` cannot
exceed the caller's. Callers must grant `actions: read` in their own stub.

### 5.5 The consumer stub — why it cannot be removed

`workflow_run` is a **repository-scoped trigger**. It fires on runs *in the repository where the
workflow file lives*, and the file must be on that repository's **default branch**. It cannot be
supplied by an action or inherited from a reusable workflow.

So every consuming repo keeps this, and nothing more (full version in §12.1):

```yaml
name: RCA Collect
on:
  workflow_run:
    workflows: ["CI"]
    types: [completed]
permissions: { actions: read, contents: read }
jobs:
  rca:
    uses: acme/ci-rca-collector/.github/workflows/rca.yml@v1
    with:
      run-id: ${{ github.event.workflow_run.id }}
      workflow-name: ${{ github.event.workflow_run.name }}
      conclusion: ${{ github.event.workflow_run.conclusion }}
      head-branch: ${{ github.event.workflow_run.head_branch }}
```

Note the `workflows: ["CI"]` array is a **literal name match** against the consuming repo's workflow
`name:` field, not the filename. Document this — it is a frequent adoption failure.

### 5.6 Versioning and distribution

- Tag releases `v1.0.0`, `v1.1.0`, and maintain a **floating `v1` tag** that consumers pin to. Move
  it on each backward-compatible release. This is the convention every marketplace action follows.
- Add a release workflow that force-updates the major tag: `git tag -f v1 && git push -f origin v1`.
- **Breaking the `summary.json` schema means bumping to `v2` and incrementing `schema_version`.**
  The two must move together; Phase 2 pins against `schema_version`.
- If `acme/ci-rca-collector` is **private**, org admins must enable
  *Settings → Actions → General → Access → "Accessible from repositories in the organization"*, and
  consuming repos need `Allow <org>, and select non-<org>, actions and reusable workflows` under
  their own Actions policy. Without both, `uses:` fails with an unhelpful "not found" error. Flag
  this prominently in the action's README — it is the single most common adoption blocker.
- Pin third-party actions inside the composite action (`actions/setup-python@v5`) by major tag;
  pin by SHA if your org requires it.

### 5.7 Cross-repository cache behaviour

`actions/cache` is **scoped per repository** — a Drain3 baseline trained in `acme/widgets` is not
visible to `acme/gadgets`. This is correct and desirable: baselines are per-job, per-codebase, and
sharing them across repos would be actively harmful.

Within a repository, the branch-scoping rules still apply: caches written on the default branch are
readable from every branch, but branch caches are isolated from each other. Hence training only on
successful default-branch runs.

**Caching is the caller's responsibility, not the action's.** The action reads and writes
`drain-cache-dir` and `history-dir` in the workspace; `actions/cache` steps live in the caller's
workflow (or in the reusable workflow of §5.4). A composite action cannot reliably run the cache
save step at job end, so do not attempt it.

### 5.8 Self-hosted runner considerations

Your runners are ephemeral pods, which usually means a minimal image.

- `actions/setup-python@v5` needs either a preinstalled Python in the tool cache or outbound network
  to download one. On a locked-down pod it may fail. Provide `setup-python: false` and document that
  the image must then ship Python ≥ 3.11 plus the packages in `requirements.txt`.
- Prefer baking `requirements.txt` into the runner image and setting `setup-python: false` — it
  removes a network dependency and several seconds per run.
- Composite actions do not support `runs-on`; the caller controls the runner. Expose `runs-on` as an
  input on the reusable workflow (done in §5.4) so callers can target a runner label.
- If the runner image lacks `pip`, the install step fails with a confusing error. Add a preflight
  check in the install step that reports the Python and pip versions before installing.

---

## 6. Build order

Implement and test in this sequence. Each step is independently verifiable.

1. `models.py` — the schema. Everything else depends on it.
2. `github_api.py` + `cleaner.py` — can you pull and clean a real failed log?
3. `classify.py` — the short-circuit checks. Highest value per line of code.
4. `extract.py` — error windows, stack traces, JUnit, annotations.
5. The non-recurrence half of `history.py` (last success, its age, same-SHA reruns, recent
   outcomes), **then** `changes.py`. That order matters: the last-success SHA is the compare base,
   so `changes.py` cannot resolve a range without it.
6. `drain_index.py` — the only genuinely tricky module. Ship without it if needed; it degrades
   gracefully. Include `masking_config_hash()` here (§10.3) — it is trivial and everything
   downstream depends on it existing from the start.
7. `history.py` recurrence — the `HistoryStore` interface and the `cache` backend (§10). **Must come
   after `drain_index.py`**, because both fingerprints are derived from Drain3 templates.
8. `budget.py` + `redact.py` + `render.py`.
9. `outputs.py` — `$GITHUB_OUTPUT` writer. Small, but everything downstream depends on it.
10. `cli.py` wiring.
11. `action.yml` (§5.3), then `self-test.yml` to dogfood it.
12. The reusable workflow (§5.4) and the consumer stub (§12.1).

Build the `HistoryStore` interface even though Phase 1 ships only the `cache` backend. Retrofitting
an interface after storage calls have leaked through the codebase is the expensive version of this.

Ship a working `action.yml` early — around step 5, wrapping whatever exists. A composite action that
returns a partial summary is testable from a real repo; a perfect library that nobody can call is not.

---

## 7. Collection pipeline

Run stages in this order. Cheap and deterministic first, so the expensive work is skipped when a
verdict is already certain.

### Stage 0 — Resolve the run
Inputs from the `workflow_run` event payload: `run_id`, `head_sha`, `head_branch`, `run_attempt`,
`event`, `actor`, `html_url`. Fetch the run object and its jobs list.

Fetch: `GET /repos/{owner}/{repo}/actions/runs/{run_id}` and
`GET /repos/{owner}/{repo}/actions/runs/{run_id}/jobs?per_page=100` (paginate).

**Capture the full step table, not only the failed step.** A step often fails because an earlier
one half-succeeded — a cache restore that missed, a setup step that warned, a dependency install
that partially completed. For every step in each analysed job record `name`, `conclusion`,
`number`, and `duration_seconds`. Render it as a compact table; it costs roughly 100 tokens and
frequently contains the answer.

Two derived signals from it, both free:

- **Cache miss before a dependency failure.** A step named like a cache restore concluding
  `success` but with a sub-second duration usually means a miss, not a hit. Flag it when the
  failing step is `dependency`-classified.
- **Where the time went.** The longest step, and any step taking more than 3× its duration in the
  last successful run of the same job. A step that normally takes 40 seconds and ran for 6 minutes
  before dying is a hang, not a logic error.

**Record runner identity.** The jobs API returns `runner_name`, `runner_group_name`, and `labels`.
On ephemeral self-hosted pods this is disproportionately valuable: failures concentrated on one
runner group or image tag are an infrastructure problem regardless of what the logs say. Record
`job.runner_name`, `job.runner_group`, `job.runner_labels`, and carry them into the fingerprint
record's metadata (not the hash) so the pattern is visible across runs.

**Record queue time** — `started_at - created_at` per job. A job that waited eleven minutes for a
runner points at pool capacity, and on ARC that is a different team's problem than a broken test.
Set `job.queue_seconds`, and flag it in `collection_notes` above a configurable threshold
(default 300s).

**Detect timeouts explicitly rather than inferring them from `conclusion`.** GitHub's conclusion
value for a job killed by `timeout-minutes` is not reliably distinguishable from other outcomes, so
match on the log signature instead — `exceeded the maximum execution time` — and on the secondary
signal of a job duration landing within a few seconds of the workflow's configured
`timeout-minutes`. Set `classification.category = "timeout"` and treat the job as a genuine failure
even when its conclusion reads `cancelled`. Without this, the `cancelled` exclusion below silently
discards every timeout.

Identify failed jobs: `conclusion == "failure"`. For each, find the first step where
`conclusion == "failure"` — record its `name` and `number`.

**Cap the number of failed jobs analysed at 3.** A matrix build can fail 20 legs at once, and 20
full evidence packages will blow the token budget and produce a report nobody reads. When more than
3 fail:

- Analyse the 3 that failed **earliest** — later legs are often cascade or cancellation.
- Record `run.failed_job_total` and `run.failed_jobs_analysed` so the omission is visible.
- Detect the common case where **all matrix legs failed identically**: fingerprint each leg's
  first-error window separately, and if they match, analyse one leg and state that the rest are
  identical. This is the single most useful matrix behaviour and it is cheap.
- Exclude jobs whose conclusion is `cancelled`; a cancelled leg is a consequence of `fail-fast`,
  not a failure. Only treat `cancelled` as meaningful when it is the *only* non-success outcome.

**Capture pull-request context now, even though Phase 3 consumes it.** Getting the PR number from a
`workflow_run` event is a known trap:

- `github.event.workflow_run.pull_requests` is populated for same-repo branches but is **empty for
  pull requests from forks**. Do not rely on it alone.
- Fallback: `GET /repos/{owner}/{repo}/commits/{head_sha}/pulls` resolves the PR for a SHA in both
  cases.
- Record `run.pr_number`, `run.pr_url`, `run.is_fork`, and `run.head_repo`. If none resolves, set
  them `None` — a push to a branch with no PR is normal, not an error.

`run.is_fork` matters beyond convenience: Phase 3 must not post comments carrying fork-controlled
content back with elevated permissions, and Phase 1 should mark the report so that decision has the
information it needs.

### Stage 1 — Runner health check (short-circuit)
Scan the raw log of each failed job for runner-infrastructure signatures. If matched, set
`verdict.short_circuit` and `classification.category = "infra_runner"`, and **skip stages 5–8**.

Patterns (case-insensitive, `config.py: RUNNER_FAILURE_PATTERNS`):

```
The runner has received a shutdown signal
lost communication with the server
The operation was canceled
The self-hosted runner .* lost communication
Failed to initialize container
no space left on device
Error response from daemon: .*pull access denied
The job was not acquired
Received request to deprovision
```

Also flag as `infra_runner` when **all** of: job conclusion is `failure`, total log lines `< 50`,
and no failed step is identifiable. That combination almost always means the pod died.

### Stage 2 — Blast-radius check (short-circuit)

Before attributing anything to this change, ask whether the rest of the repository is also on fire.
This is the strongest deterministic infrastructure signal available and it costs one API call.

`GET /repos/{owner}/{repo}/actions/runs?created=>{now-30min}&per_page=50`

Count runs that concluded `failure` in the window, grouped by workflow and branch. Then:

| Observation | Conclusion |
|---|---|
| ≥ 3 workflows failed, spanning ≥ 2 unrelated branches, within 30 minutes | Almost certainly infrastructure. Set `verdict.short_circuit = "infra_widespread"` and `is_infra_vs_code = "infra"`. |
| Same workflow failing on ≥ 3 branches including the default branch | Broken shared dependency, registry, or credential expiry. Set `is_infra_vs_code = "infra"`, do not short-circuit. |
| Only this run failing | Normal. Proceed, and record it — "no other failures in the last 30 minutes" is itself useful evidence that the change is implicated. |

Record `run.concurrent_failures`, `run.concurrent_workflows`, and `run.concurrent_branches`
regardless of outcome, so the report can state the negative case as well as the positive.

**A widespread failure still gets a report**, just a short one: the step table, the first-error
window, and the blast-radius finding. Skip Drain3, change context, and the diff — none of them
describe the problem, and gathering them wastes the budget and the reader's time.

Two guards. Exclude the collector's own workflow from the count, or a storm of collect runs
inflates the number. And treat scheduled workflows separately — a nightly job failing at 02:00
alongside a push build is coincidence, not correlation.

### Stage 3 — Rerun / flake check (short-circuit)
- If `run_attempt > 1`, note it.
- `GET /repos/{owner}/{repo}/actions/runs?head_sha={sha}` — if any run on the **same SHA** with the
  same workflow concluded `success`, set `classification.is_flaky = true`,
  `verdict.short_circuit = "flake_same_sha_passed"`. Identical code passed; there is nothing to
  diagnose in the source.

This is a deterministic verdict. Never leave it to a model.

### Stage 4 — Fetch and clean logs
Per failed job: `GET /repos/{owner}/{repo}/actions/jobs/{job_id}/logs`. This returns a **302 redirect
to a short-lived (~1 minute) plaintext URL**. Follow it, do not cache the URL.

Robustness requirements:
- Retry on 5xx with exponential backoff (3 attempts). Very large logs can 500 or time out.
- **Respect rate limits.** `GITHUB_TOKEN` gets 1,000 requests/hour per repository — not the 5,000
  a PAT gets. A matrix build plus history lookups can approach this. Read `x-ratelimit-remaining`
  on every response; below 50, stop optional collection (history, recent outcomes, artifacts),
  note it in `collection_notes`, and finish with what you have. On a 403 carrying
  `x-ratelimit-remaining: 0`, do not retry — honour `x-ratelimit-reset` or abandon that call.
  Secondary rate limits return 403 with a `retry-after` header; sleep for it, once, then give up.
- Prefer one paginated call over many single-item calls. Fetching 20 matrix job logs individually
  is the fastest route to a rate-limit wall.
- If a job's log is unavailable, record `log_unavailable: true` for that job and continue. Never
  abort the whole collection.
- Logs are retained ~90 days by default; a missing log on an old run is expected, not an error.

Cleaning (`cleaner.py`), in order:
1. Strip the leading ISO-8601 timestamp GitHub prefixes to every line (`2026-09-08T12:34:56.7891234Z `).
2. Strip ANSI escape sequences: `\x1b\[[0-9;?]*[ -/]*[@-~]`.
3. Strip carriage-return progress redraws: keep only the text after the final `\r` on a line.
4. Drop known noise lines: `##[group]`, `##[endgroup]`, `##[debug]`, `Post job cleanup`,
   `Cleaning up orphan processes`, `Download action repository`.

   **Two guards on this step.** First, before discarding the `Set up job` group, extract and keep
   the environment facts it contains: runner image name and version, operating system, and
   available disk space at start. Four lines, and they answer "did the runner image change?" —
   which on a rolling ARC image is a real and otherwise invisible cause.
   Second, only drop `Post job cleanup` output **when the failure is not in a post step**. If the
   failed step number corresponds to a post-action, that output is the entire evidence and dropping
   it leaves an empty report.
5. **Join multi-line records into single logical records** before anything else consumes them.
   Continuation heuristics: lines starting with whitespace + `at `, `File "`, `Caused by:`,
   `\tat `, `  ... N more`, or a leading tab. This matters enormously for Drain3 and for stack
   trace extraction — do not skip it.

Preserve `##[error]` markers; they are GitHub's own high-precision error annotations.

Record raw and cleaned line counts and byte size per job as `log_lines_raw`, `log_lines_clean`,
`log_bytes`. Volume is itself a signal: a job emitting 40× its usual output is looping or retrying,
and one emitting almost nothing died before it started.

### Stage 5 — Regex pre-classification
Classify against an ordered rule table in `config.py`. First match wins; record all matches.

| Category | Signals |
|---|---|
| `oom` | `OOMKilled`, `Killed process`, `signal: killed`, `JavaScript heap out of memory`, `MemoryError`, exit code 137 |
| `timeout` | `timed out`, `context deadline exceeded`, `ETIMEDOUT`, job duration at the workflow timeout |
| `disk_space` | `no space left on device`, `ENOSPC` |
| `dependency` | `npm ERR!`, `ERESOLVE`, `Could not resolve dependency`, `ModuleNotFoundError`, `go: .* not found`, `Could not find artifact` |
| `network_dns` | `Could not resolve host`, `Temporary failure in name resolution`, `ECONNREFUSED`, `EAI_AGAIN` |
| `auth` | `401 Unauthorized`, `403 Forbidden`, `authentication required`, `permission denied`, `invalid credentials` |
| `image_pull` | `ImagePullBackOff`, `ErrImagePull`, `pull access denied`, `manifest unknown` |
| `compile` | `error:`, `error TS`, `cannot find symbol`, `SyntaxError`, `undefined reference to` |
| `test_failure` | JUnit failures present, `FAILED`, `AssertionError`, `Test run failed` |
| `crash` | `panic:`, `Segmentation fault`, `core dumped`, `fatal error:` |
| `unknown` | fallback |

Record: `category`, `matched_pattern`, `matched_line_number`, `confidence` (`high` for exact
signatures like `OOMKilled`, `low` for generic `error:`).

### Stage 6 — Error extraction
Produce, per failed job:

- **`first_error_window`** — 30 lines before through 30 lines after the *first* line matching the
  error patterns. Usually the cause.
- **`tail_window`** — last 150 lines of the failed step. Usually the symptom.
- Label both explicitly in output. If the two windows overlap, merge and mark as `merged`.
- **`stack_traces`** — detected traces, each truncated to **top 10 frames + bottom 5**, with an
  `… N frames elided …` marker. Never cut a trace mid-frame.
- **`annotations`** — `GET /repos/{owner}/{repo}/check-runs/{id}/annotations` for the failed job, or
  scrape `##[error]` lines. High precision, near-zero cost.
- **`error_lines`** — grep matches for `ERROR|FAIL|Exception|Traceback|panic:|##\[error\]|FATAL|
  Segmentation fault|npm ERR!`. Record the match plus its line number. These are **not** rendered
  as a standalone section; they are fed into Stage 7, where they set the `has_error_match` flag used
  for tier assignment, and surface as representative lines under their template.
- **`artifacts`** — list every artifact the run produced (`GET /actions/runs/{run_id}/artifacts`):
  name, size, and expiry. **Download only what is parseable** (JUnit XML, and later coverage
  summaries); for everything else record the name so a reader knows a screenshot bundle or core
  dump exists without the collector fetching 300 MB. Skip artifacts over 50 MB entirely, noting the
  skip. For e2e suites the presence of a `screenshots` or `traces` artifact is often the most
  actionable line in the whole report.
- **`junit`** — parse any `**/*junit*.xml`, `**/test-results/**/*.xml`, `**/surefire-reports/*.xml`
  from the run's artifacts (`GET /actions/runs/{run_id}/artifacts`, download and unzip). Extract
  failed tests only: `classname`, `name`, `message`, first 20 lines of the failure body. Cap at 5
  tests, report the total count. If no JUnit XML exists, set `junit: null` — do not fail.

### Stage 7 — Drain3 novelty diff
Wrap Drain3 in `drain_index.py`. Two modes:

- **`train(lines, key)`** — called on **successful runs of the default branch only**. Feeds cleaned
  lines into the miner and persists state.
- **`novelty(lines, key)`** — called on failing runs. Feeds cleaned lines and returns only those
  where `result["change_type"] == "cluster_created"`, i.e. templates never seen in a healthy build.

State persistence uses `FilePersistence` at `.drain/{workflow}_{job}.bin`, restored through
`actions/cache`. Cache key design is in the workflow below.

`drain3.ini` masking — mask before clustering or templates fragment uselessly:

> ⚠ **Every value in the two sections below is part of the fingerprint key.** Editing any masking
> regex, `mask_prefix`/`mask_suffix`, or `sim_th` changes every template string and therefore every
> fingerprint, orphaning the stored history. Read §10.3 before touching this file and bump
> `MASKING_CONFIG_VERSION` in the same commit.

```ini
[MASKING]
masking = [
  {"regex_pattern":"((?<=[^A-Za-z0-9])|^)(([0-9a-f]{2,}:){3,}([0-9a-f]{2,}))((?=[^A-Za-z0-9])|$)","mask_with":"ID"},
  {"regex_pattern":"((?<=[^A-Za-z0-9])|^)(\\d{1,3}\\.\\d{1,3}\\.\\d{1,3}\\.\\d{1,3})((?=[^A-Za-z0-9])|$)","mask_with":"IP"},
  {"regex_pattern":"((?<=[^A-Za-z0-9])|^)([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})((?=[^A-Za-z0-9])|$)","mask_with":"UUID"},
  {"regex_pattern":"(/[\\w.-]+){2,}","mask_with":"PATH"},
  {"regex_pattern":"((?<=[^A-Za-z0-9])|^)(\\d+(\\.\\d+)?(ms|s|m|h))((?=[^A-Za-z0-9])|$)","mask_with":"DUR"},
  {"regex_pattern":"((?<=[^A-Za-z0-9])|^)([\\-\\+]?\\d+)((?=[^A-Za-z0-9])|$)","mask_with":"NUM"}
  ]
mask_prefix = <:
mask_suffix = :>

[DRAIN]
sim_th = 0.4
depth = 4
max_children = 100
max_clusters = 2048
```

**Graceful degradation is mandatory.** If no baseline exists (cache miss, first run, new job), set
`drain.baseline_available = false`, skip novelty and baseline-count comparison, and fall back to
rarity ranking within the single log (rarest clusters first). All templates are then reported with
`is_novel = null` and `baseline_count = null` — **not** `false` and `0`, which would falsely assert
that everything is new. The tool must never fail because Drain3 has no state.

#### 6.1 Per-template data to capture

For every cluster produced from the failing log, record:

| Field | Notes |
|---|---|
| `template_id` | Stable within this report; render as `#1`, `#2`, … |
| `template` | The masked template string, e.g. `Exception: Connection refused to <:HOST:>:<:PORT:>` |
| `count` | Occurrences in this failing log |
| `baseline_count` | Occurrences in the baseline. `null` if no baseline. |
| `is_novel` | `true` if the cluster was created during this parse (absent from baseline). `null` if no baseline. |
| `first_line` | Line number of the **first** occurrence. Required — see below. |
| `has_error_match` | `true` if any occurrence matched a Stage 6 error pattern |
| `variables` | Extracted parameter values per masked position (see 6.2) |
| `tier` | `T1`–`T5`, assigned per 6.3 |
| `representative_line` | The raw text of the first occurrence, redacted |

**`first_line` is not optional.** Clustering destroys sequence, and sequence is what distinguishes
cause from symptom. "Connection refused" at line 412 followed by "retrying" at line 415 is a causal
story; the same two templates unordered is a guess. Every ordering decision within a tier uses it.

#### 6.2 Variable extraction

Drain3 returns the matched parameters for each log line via `TemplateMiner.extract_parameters()`
(or `get_parameter_list()` on the cluster, depending on version — use whichever the pinned version
provides and note it in a comment). Aggregate across all occurrences of the template:

- **Low-cardinality string vars** (≤ 5 distinct values): list them, e.g. `host=db`, `port=5432`.
- **High-cardinality string vars** (> 5 distinct): report `N distinct values` plus the first 3.
- **Numeric vars**: report `[min..max]` and the median. If a baseline exists, report the baseline
  median alongside, e.g. `ms=[8..4312], p50 41 (baseline p50 12)`.
- **Sequence vars** where values look like a progression (retry backoff, attempt counters): render
  the sequence compactly with run-length encoding, e.g. `[1, 2, 4, 8, 16, 30×9]`. This makes an
  exhausted retry budget visible at a glance.

**Extracted variables MUST pass through `redact.py` before being stored or rendered.** A masked
template hides the *shape* of a secret but the extracted variable holds its *value*. Without this,
a line like `Authorization: Bearer sk-live-abc123` becomes template `Authorization: Bearer <:ID:>`
with `id=sk-live-abc123` in the variables block — a live secret written into a downloadable
artifact. This is the single most important rule in this section.

#### 6.3 Tier assignment (deterministic ranking)

Do **not** compute a weighted relevance score. A magic float is undebuggable and its ordering shifts
whenever weights are tuned. Assign discrete tiers:

| Tier | Condition | Rendering |
|---|---|---|
| **T1** | `is_novel` AND `has_error_match` | Full row + variables + representative line |
| **T2** | `is_novel`, no error match | Full row + variables |
| **T3** | Not novel, `has_error_match`, `count <= 5` | Full row + variables |
| **T4** | Not novel, `has_error_match`, `count > 5` | Row only |
| **T5** | Everything else | Row only, or aggregated into the omitted-count line |

Within a tier, sort by `first_line` **ascending**. Earliest is nearest the cause.

When `baseline_available == false`, T1/T2 are unreachable; fall back to: `has_error_match` and
`count <= 5` → T3, `has_error_match` → T4, else T5, so the report still ranks sensibly.

**Demote, do not omit.** A T5 template costs roughly 15 tokens as a table row, and its count carries
information even when its content does not. Drop the line *content*, keep the row. Only aggregate
into "N further templates omitted" once the section cap in §8 is reached — and always state the
omission criteria alongside the count.

#### 6.4 Depletion detection

Absence and depletion are signals, and a rank-by-relevance filter discards both. After tiering,
scan for templates where a baseline exists and:

- `count == 0` and `baseline_count >= 20` → **missing**: a step that normally runs did not run.
- `count < baseline_count * 0.25` and `baseline_count >= 20` → **depleted**: throughput collapsed.
- `count > baseline_count * 10` and `count >= 50` → **flooding**: retry storm or runaway loop.

Emit each as a `⚠` note under the table with the ratio. A template like
`INFO: request handled in <:NUM:>ms` at count 200 looks like pure noise — but at a baseline of 4,812
it means throughput fell to 4%, which may be the most important fact in the log. Promote any
template carrying a depletion note to **T2** regardless of its computed tier.

#### 6.5 Fingerprint

Two fingerprints are computed, both from masked template strings only. The full definition —
coarse vs fine, match kinds, and Jaccard near-miss detection — is in **§10.2**; the rules that
matter at this stage are:

**Counts, line numbers, and variable values must be excluded from the hash input.** Including them
breaks recurrence detection whenever load, log volume, or environment varies — the same failure
would fingerprint differently on every run. Hash template strings only.

If `baseline_available == false`, fall back to hashing the T3 template strings and set
`fingerprint_degraded = true` so downstream consumers know the match is weaker.

### Stage 8 — Change and history context
`changes.py`:

**Collect across the blame range, not just the head commit.** A PR carries several commits and a
branch may have moved several times since the last green run. Analysing only the tip commit misses
the change that actually broke it. Resolve the range first (from `history.py`, below), then use the
**compare endpoint**, which returns commits and cumulative files in one call:

`GET /repos/{owner}/{repo}/compare/{base}...{head}`

- `base` = the SHA of the last successful run of this workflow on this branch.
- `head` = `run.head_sha`.
- If no prior success exists (new branch, first run), fall back to the PR's merge base, then to the
  head commit alone. Record which was used in `changes.range_basis`.
- The comparison is **capped by GitHub at 250 commits and 300 files**. Detect truncation via the
  response's `total_commits` versus the returned array length and set `changes.range_truncated`;
  a range that large means the base resolution went wrong, and saying so is more useful than
  silently analysing a fraction.

**Commits in the range** — the cheapest high-signal evidence in the whole collector:

- For each commit: short SHA, **subject line only** (first line of the message), author login,
  and files-changed count.
- Cap at **10 commits**, most recent first, plus a count of the remainder.
- Never include full commit bodies. They contain issue templates, co-author trailers, and
  generated changelogs, all of which are noise at high token cost.
- **Flag reverts.** A subject matching `^Revert "` or containing `This reverts commit` is a strong
  signal: either the failure is what was being reverted, or the revert itself broke something.
  Set `commit.is_revert`.
- **Flag merges.** `parents.length > 1`. A merge commit's own "changed files" compares against the
  first parent only, which is why the range compare above is used rather than per-commit lookups.

**Changed files** — cumulative across the range, paths and diffstat only, never full diffs by
default. Include a **full diff hunk only** for files whose path appears in an extracted stack trace;
cap at 3 files, 100 lines each.

**Classify changed paths by blast radius.** This is deterministic, nearly free, and often more
informative than the diff itself. A build that breaks the same day the CI config changed is almost
never a coincidence:

| Class | Matches | Why it matters |
|---|---|---|
| `ci_config` | `.github/workflows/**`, `.github/actions/**`, `action.yml` | Changed the pipeline itself |
| `container` | `Dockerfile*`, `*.dockerfile`, `.dockerignore`, runner image refs | Changed the build environment |
| `dependency` | `package.json`, `requirements*.txt`, `pyproject.toml`, `go.mod`, `pom.xml`, `build.gradle*`, `Gemfile`, `Cargo.toml` | Changed what gets installed |
| `lockfile` | the lockfiles listed below | Changed resolved versions |
| `build_config` | `Makefile`, `tsconfig.json`, `webpack.*`, `vite.*`, `babel.*`, `.eslintrc*` | Changed how code is built |
| `infra` | `*.tf`, `helm/**`, `k8s/**`, `*.yaml` under deploy paths | Reserved for Phase 4; record but do not act on |
| `test` | paths matching the repo's test conventions | Failure may be in the test, not the code |
| `source` | everything else | |

Set `changes.classes` to the set present, and surface `ci_config`, `container`, and `lockfile`
prominently in the report — these three account for a large share of "worked yesterday, broken
today" failures, and a human scanning the report should see them without hunting.

**Lockfile deltas.** If any of `package-lock.json`, `yarn.lock`, `pnpm-lock.yaml`, `poetry.lock`,
`Pipfile.lock`, `go.sum`, `Gemfile.lock`, `Cargo.lock` changed, do **not** include the diff — they
run to thousands of lines. Parse into version transitions:

```
esbuild 0.19.2 → 0.21.0 (major)
+ node-gyp 10.1.0 (added)
- left-pad 1.3.0 (removed)
… 47 more changes
```

Cap at 15 entries plus a count, and sort **major bumps first** — they carry the most risk. A version
bump adjacent in time to a build break is very high signal.

**Pull-request metadata**, when `run.pr_number` resolved (Stage 0): title, labels, draft status, and
the **first 500 characters** of the body. The title frequently states the intent that the diff only
implies. Truncate the body hard; PR templates are mostly boilerplate.

`history.py`:
- **Last successful run on this branch:**
  `GET /actions/workflows/{workflow_id}/runs?branch={b}&status=success&per_page=1`. Record its
  `head_sha` — this is the `base` that `changes.py` compares against, so resolve it first.
  Also record how long ago it was: a last-green from four hours ago bounds the problem far more
  tightly than one from three weeks ago, and the age alone tells a reader how much to trust the
  blame range.
- **Narrow the suspect commit where the data allows it.** If the workflow ran on intermediate
  commits in the range, the last commit that *passed* and the first that *failed* bracket the
  culprit exactly. Query the runs list for this workflow and branch, walk back to the last success,
  and set `history.first_failing_sha` when the boundary is unambiguous. When the range collapses
  to one commit, say so plainly — that is the strongest change signal the collector can produce.
  When intermediate runs are missing (commits pushed in a batch), leave it `None` rather than
  guessing.
- **Recent run outcomes** for this workflow+branch, last 10, as a pass/fail string: `✓✓✗✓✓✓✗✗✗✗`.
  A long unbroken green run ending now points at the change; an alternating pattern points at
  flakiness, and the model should be shown the difference rather than asked to infer it.
- Fingerprint history: look up both fingerprints through the `HistoryStore` interface, then
  upsert the current failure. Backends, record shape, concurrency, and the dual coarse/fine
  matching rules are specified in **§10** — do not implement storage inline here.
  If the fingerprint has been seen on **unrelated branches**, set `history.cross_branch` and drive
  `classification.is_flaky` from it: that is a deterministic conclusion, not a judgement call.
  A history-store failure must never abort collection — set `history.backend_degraded`, note it in
  `collection_notes`, and continue.

**Ordering note:** `history.py` must resolve the last-success SHA **before** `changes.py` runs,
because it supplies the compare base. This is the one ordering dependency inside Stage 8; the rest
of the stage is order-independent.

### Stage 9 — Redact, budget, render
- **Redaction runs before writing anything to disk.** The artifact is downloadable by anyone with
  repo read access. Scrub: `ghp_/gho_/ghs_/github_pat_` tokens, AWS keys (`AKIA[0-9A-Z]{16}`),
  `Bearer <token>`, JWTs (`eyJ[A-Za-z0-9_-]{10,}\.`), private key blocks, `password=`/`token=`/
  `secret=`/`api[_-]?key=` assignments, and any value of an env var named like a secret. Replace
  with `***REDACTED***`. GitHub masks its own secrets in logs, but not values your build derives.
- **Budget** (`budget.py`): estimate tokens as `len(text) / 4`. Enforce per-section caps from
  §8 below. Trim from the middle of log blocks, never the ends. Record what was trimmed in
  `budget_report` so it is visible in the output.
- **Render** `summary.md` per §9, write both files to `rca/`.

---

## 8. Token budget

Total evidence target: **6,000 tokens**. Per-section caps enforced by `budget.py`:

| Section | Cap (tokens) |
|---|---|
| Metadata + verdict + classification | 300 |
| Step table + runner identity + blast radius | 250 |
| Annotations | 200 |
| First-error window | 1,000 |
| Tail window | 1,000 |
| Stack traces | 600 |
| **Log templates** (table + variables + representative lines) | **1,200** |
| JUnit failures | 800 |
| Change context (commits, diffstat, lockfile, PR metadata) | 900 |
| History | 200 |

The log-templates cap is spent in tier order: render all T1 rows with variables and representative
lines, then T2, and so on, stopping when the cap is reached. Remaining templates collapse into the
"N further templates omitted" line. T1 must never be truncated — if T1 alone exceeds the cap, raise
it and record the overrun in `budget_report`.

If a section is under its cap the surplus is **not** redistributed; keep the output small.

---

## 9. Log Summary format (`summary.md`)

This is the deliverable a human reviews in Phase 1 and the model consumes in Phase 2. Sections must
appear in this exact order. Sections with no data are **omitted entirely**, not left empty.

````markdown
# CI Failure Report

**Repository:** acme/widgets · **Workflow:** CI · **Run:** [#4821](https://github.com/...) (attempt 1)
**Branch:** `feature/parser-rewrite` · **Commit:** `a3f9c21` · **Trigger:** pull_request by @dsharma
**Collected:** 2026-09-08T14:22:10Z · **Collector:** v0.1.0

## Verdict

| Field | Value |
|---|---|
| Category | `dependency` (confidence: high) |
| Infra or code | `code` |
| Flaky | no |
| Short-circuited | no |
| Fingerprint | `a91f3c7e0b2d4855` |
| Seen before | 3× in 14 days (branches: `main`, `feature/parser-rewrite`) |

## Failed Jobs

| Job | Failed step | Exit code | Duration | Queued | Runner |
|---|---|---|---|---|---|
| `build (node-20)` | `Install dependencies` | 1 | 2m 14s | 8s | `arc-linux-xl / pod-7f9c2` |

Runner image `ghcr.io/acme/runner:2026.08.3` · ubuntu-24.04 · 42 GB free at start

**No other workflows failed in this repository in the last 30 minutes** — the failure is local to
this run.

### Steps — `build (node-20)`

| # | Step | Result | Time |
|---|---|---|---|
| 1 | Set up job | ✓ | 3s |
| 2 | Checkout | ✓ | 4s |
| 3 | Restore node_modules cache | ✓ | 0.4s ⚠ likely miss |
| 4 | Install dependencies | ✗ | 2m 08s |
| 5 | Run tests | – skipped | |

Log volume: 1,204 lines (11,830 raw), 84 KB — within normal range for this job.

## Annotations

- `##[error]` Process completed with exit code 1.
- `##[error]` npm ERR! ERESOLVE could not resolve

## Heuristic Classification

Matched rule `dependency` on pattern `ERESOLVE` at line 412 of `build (node-20)`.
Other rules matched: `compile` (low confidence).

## First Error Window — `build (node-20)` / `Install dependencies`

Lines 382–442 of 1,204. This is the earliest error in the log and is usually closest to the cause.

```text
npm ERR! code ERESOLVE
npm ERR! ERESOLVE could not resolve
npm ERR!
npm ERR! While resolving: @acme/parser@2.1.0
npm ERR! Found: typescript@5.4.2
...
```

## Tail Window — `build (node-20)` / `Install dependencies`

Last 150 lines of the failed step. This is usually the symptom, not the cause.

```text
...
npm ERR! A complete log of this run can be found in: /home/runner/.npm/_logs/...
##[error]Process completed with exit code 1.
```

## Stack Traces

### Trace 1 — `TypeError: Cannot read properties of undefined`

```text
at parseNode (src/parser/node.ts:88:19)
at Object.parse (src/parser/index.ts:23:5)
… 12 frames elided …
at processTicksAndRejections (node:internal/process/task_queues:95:5)
```

## Log Templates

Clustered by Drain3. **New** = template absent from the baseline (last successful `main` build of
this job, run #4790, 11 days old, 1,842 templates). **Base** = occurrences in that baseline.
Ordered by tier, then by first occurrence.

| # | New | Count | Base | Line | Template |
|---|-----|-------|------|------|----------|
| 1 | ● | 1 | 0 | 412 | `Exception: Connection refused to <:HOST:>:<:PORT:>` |
| 2 | ● | 14 | 0 | 415 | `Retrying connection in <:NUM:>s` |
| 3 | ● | 3 | 0 | 502 | `ERROR: <:NAME:> failed: AssertionError` |
| 4 | | 200 | 4,812 | 12 | `INFO: request handled in <:NUM:>ms` |
| 5 | | 38 | 41 | 455 | `npm WARN deprecated <:PATH:>` |

**Variables**

- **#1** — host=`db`, port=`5432`
- **#2** — delay=`[1, 2, 4, 8, 16, 30×9]` (retry budget exhausted)
- **#3** — name=`test_user_login`, `test_session_expiry`, `test_token_refresh`
- **#4** — ms=`[8..4312]`, p50 `41` (baseline p50 `12`)

⚠ **Depleted** — template #4 at 200 vs baseline 4,812 (4%). Request throughput collapsed.

142 further templates omitted (present in baseline, count > 5, no error-pattern match).

### Representative lines

Raw first occurrence for each ranked template. Excludes lines already shown in the error windows
above.

- **#1** (L412) `Exception: Connection refused to db:5432`
- **#3** (L502) `ERROR: test_user_login failed: AssertionError: expected 200, got 503`

## Artifacts

| Name | Size | Parsed |
|---|---|---|
| `test-results` | 84 KB | ✓ JUnit |
| `playwright-traces` | 22 MB | — download to inspect |

## Failed Tests

3 of 412 tests failed. Showing 3.

### `parser.spec.ts › ParserSuite › handles nested arrays`
```text
AssertionError: expected [] to deeply equal [ 1, 2, 3 ]
    at Context.<anonymous> (test/parser.spec.ts:44:18)
```

## Changes Since Last Green

Comparing `7d2e881..a3f9c21` — 3 commits, 6 files (+142 / −38). Base is the last successful run of
this job on this branch, 4 hours ago.

⚠ **Pipeline configuration changed in this range** (`ci_config`, `lockfile`).

| Commit | Author | Subject | Files |
|---|---|---|---|
| `a3f9c21` | @dsharma | Handle nested arrays in parser | 3 |
| `c81b40e` | @dsharma | Bump typescript to 5.4.2 | 2 |
| `7f2a559` | @rmehta | Run tests on node 22 in CI | 1 |

**PR #418** — *Parser rewrite for nested structures* · labels: `area/parser`, `needs-review`

```text
src/parser/node.ts        | 88 ++++++++++---
src/parser/index.ts       | 12 +-
.github/workflows/ci.yml  |  6 +-
package.json              |  4 +-
package-lock.json         | 76 ++++++-----
```

### Lockfile changes — `package-lock.json`

```text
typescript 5.3.3 → 5.4.2 (minor)
+ @types/node 20.11.0 (added)
… 12 more changes
```

### Diff for files named in stack traces

<details><summary><code>src/parser/node.ts</code> (88 lines changed)</summary>

```diff
@@ -84,7 +84,7 @@
-  const children = node.children ?? [];
+  const children = node.children;
```
</details>

## History

- Last success on this branch: run [#4805](...), commit `7d2e881`, 4 hours ago.
- **First failing commit:** `c81b40e` — run #4812 passed on `7f2a559`, run #4818 failed on `c81b40e`.
- Recent outcomes for `build (node-20)` on this branch: `✓✓✓✗`
- Same SHA has not been run before.

## Collection Notes

- Drain3 baseline: loaded from cache (`drain3-CI-build-4790`), 1,842 templates.
- Templates: 147 clustered — T1:1, T2:2, T3:2, T4:0, T5:142.
- Trimmed: tail window from 2,140 → 1,000 tokens (middle elided).
- JUnit: parsed from artifact `test-results`.
- Secrets redacted: 0 matches in content, 0 in extracted variables.
- Fingerprint computed from 3 T1/T2 templates (not degraded).
````

---

## 10. Failure memory (recurrence detection)

Nothing here trains a model. The model's weights never change and no fine-tuning occurs at any
point. "Memory" is a lookup table the collector owns: **fingerprint → what we know about that
failure**. When a match is found, the record is injected into the report (and, from Phase 2, into
the prompt) as evidence — the same way you would brief a new engineer on a problem they had not
seen before.

State this explicitly in the README. "The system learns" invites a question nobody can answer;
"the system remembers what it has seen" is fully auditable.

### 10.1 Two distinct memories — do not conflate them

| | Drain3 baseline | Fingerprint history |
|---|---|---|
| **Holds** | What a *healthy* build looks like | What *failures* we have seen before |
| **Keyed by** | workflow + job | fingerprint hash |
| **Written on** | Successful default-branch runs | Every collected failure |
| **Lifetime** | Refreshed continuously; disposable | Long-lived; the valuable one |
| **If lost** | Novelty detection degrades to rarity ranking | Recurrence detection silently stops |
| **Store** | `actions/cache` is fine | `actions/cache` is **not** sufficient past Phase 1 |

They have different durability requirements and must not share a storage backend just because both
are "state."

### 10.2 Dual fingerprint — exact matching alone is too brittle

A single reworded error message changes the hash and the recurrence is missed. Compute two:

- **`fingerprint_fine`** — sha256 of the sorted template strings of **all** T1/T2 templates,
  truncated to 16 hex. Answers "this exact failure again."
- **`fingerprint_coarse`** — sha256 of the **single highest-ranked** T1 template only. Answers
  "this class of failure again."

Match coarse first, then check whether fine also matches, and report which:

| Match | Meaning | Report as |
|---|---|---|
| Fine matches | Same failure, same shape | `exact` |
| Coarse only | Same class, different detail | `similar` |
| Neither | Novel | `new` |

For near-miss detection beyond this, use **Jaccard similarity over the sets of masked template
strings** (`|A ∩ B| / |A ∪ B|`, report a match above ~0.6).

> **The set elements must be the template strings themselves, never template IDs.** Drain3 cluster
> IDs are assigned by insertion order into a particular state file, so the same template is cluster
> 47 in one run's state and cluster 112 in another's. `LogTemplate.template_id` is worse — it is a
> per-report ordinal (`#1`, `#2`) with no meaning outside that one report. Neither survives crossing
> a run boundary, and using either produces similarity scores that look plausible and are noise.

For fixed-width set members, hash each template string individually (`sha256(template)[:16]`) and
compare the sets of those hashes. That is equivalent and cheaper to store, but note the two hash
levels: a **per-template** hash is a set element for similarity, while the **fingerprint** is a hash
over the sorted collection. Do not reuse one for the other.

Jaccard is cheap, deterministic, and you can show someone exactly which templates overlapped.

**Do not use vector embeddings here.** Log templates are already normalised structured strings —
embeddings add infrastructure, add a second thing that can be unavailable, and destroy the property
that a match is explainable in one sentence.

Both fingerprints exclude counts, line numbers, and variable values, per §7.6.5.

### 10.3 Masking config is schema — guard it

Every fingerprint is a hash over masked template strings, so the masking configuration is part of
the key. **Change `drain3.ini` and every template string changes, so every fingerprint changes, and
the entire history is orphaned in one commit.** Records are not corrupted — they become unreachable,
and every failure reports as `new`. Nothing errors, which is what makes this dangerous.

This is not a hypothetical. Adding one masking regex to quiet a noisy template is exactly the kind
of small Friday change someone makes without realising it resets months of accumulated recurrence
data.

**Guard 1 — stamp the config on every record.**

```python
def masking_config_hash(ini_path: str) -> str:
    """sha256 over the normalised [MASKING] and [DRAIN] sections, first 12 hex.

    Include: every masking regex and its mask_with, mask_prefix, mask_suffix,
             sim_th, depth, max_children.
    Exclude: comments, whitespace, key order, and anything under [SNAPSHOT]
             or [PROFILING] — those do not affect the templates produced.
    """
```

Store it as `FailureRecord.masking_config_hash`, and carry the current run's value on
`DrainReport.masking_config_hash`.

**Guard 2 — degrade instead of silently reporting `new`.** On lookup, compare the current hash
against the record's:

| Condition | Behaviour |
|---|---|
| Hashes match | Normal exact/coarse matching |
| Hashes differ | **Skip exact matching entirely** — the hashes are not comparable. Fall to similarity over template strings, which survives most masking changes because the unmasked words are unchanged. Set `history.config_drift = true`. |
| No hash on record (pre-guard data) | Treat as differing |

Surface drift in `collection_notes` and in the report's Collection Notes section:
`Masking config changed since this record was written (a91f… → 3c72…); matched by similarity only.`

**Guard 3 — treat a config change as a migration.** Bump a `MASKING_CONFIG_VERSION` constant in
`config.py` alongside any `drain3.ini` edit, and require the PR to state which path was chosen:

- **Accept the reset.** Fine early on when there is little history. Say so in the PR.
- **Re-fingerprint.** Replay each record's stored `templates` through the new masking config,
  recompute both fingerprints, and rewrite the records. Only possible because §10.4 stores the
  template strings on the record — which is one of the reasons it does.

A `cli.py refingerprint --dry-run` subcommand that reports how many records would change is worth
the hour it takes to write, and makes the choice above an informed one.

The Drain3 baseline itself needs no guard: it is rebuilt from successful runs continuously, so a
config change simply retrains it. Only the fingerprint history is at risk.

### 10.4 The record

```python
class FailureRecord(BaseModel):
    fingerprint: str                       # fine
    fingerprint_coarse: str
    first_seen: datetime
    last_seen: datetime
    count: int = 1
    branches: list[str] = []               # deduped
    run_ids: list[int] = []                # capped at the 20 most recent
    category: str                          # from Stage 5
    templates: list[str] = []              # the masked strings that produced the hash.
                                           # Required for Jaccard similarity (§10.2) and for
                                           # re-fingerprinting after a masking change (§10.3).
    template_hashes: list[str] = []        # sha256(template)[:16], one per entry in templates.
                                           # Set elements for similarity — NOT cluster or report IDs.
    masking_config_hash: str               # see §10.3. Guards against orphaned history.
    last_summary: str | None = None        # one-line description of the failure
    resolution: str | None = None          # what actually fixed it — human-supplied
    resolution_author: str | None = None
    resolution_run_id: int | None = None
    human_verified: bool = False
    schema_version: Literal["1.0"] = "1.0"
```

Cap `run_ids` and `branches`, or long-lived records grow without bound. Never store raw log lines
in a record — only masked templates, which are post-redaction by construction.

**`resolution` is the field carrying most of the value, and it is the only one that cannot be
populated automatically.** Everything else fills itself. Design for it being empty most of the
time; treat any resolution text as a bonus rather than assuming the field is reliable. Capture
options, ordered by how likely people are to actually use them:

1. A bot comment on the recurrence issue that engineers reply to (Phase 3).
2. A `/resolved <text>` command in a PR comment.
3. Inference from the diff of the commit that first turned the pipeline green after that fingerprint
   last appeared. Automatic but unreliable — mark these `human_verified: false`.

### 10.5 Storage backends — pluggable, `cache` for Phase 1

Define a small interface in `history.py` and implement backends behind it. The collector must not
know which is in use.

```python
class HistoryStore(Protocol):
    def get(self, fingerprint: str) -> FailureRecord | None: ...
    def find_by_coarse(self, coarse: str) -> list[FailureRecord]: ...
    def upsert(self, record: FailureRecord) -> None: ...
    def close(self) -> None: ...          # flush; must be idempotent
```

Selected by a `history-backend` action input: `cache` (default) | `issues` | `redis` | `none`.

| Backend | Durability | Setup | When to use |
|---|---|---|---|
| **`cache`** | **Weak** — evicted after ~7 days unused; 10 GB per-repo cap with LRU eviction | None | **Phase 1 default.** Losing history occasionally does not matter while proving the concept. |
| **`issues`** | Strong | None beyond `issues: write` | **Phase 3 default.** Free, searchable, and engineers can add resolutions as ordinary comments. |
| **`redis`** | Strong | An in-cluster Redis | Once this spans many repos and shared history is wanted. |
| **`none`** | — | — | Local development and fixture replay. |

**`cache` is a Phase 1 expedient, not the destination.** Say so in the README so nobody is surprised
when a record vanishes. Concretely: cache entries are immutable once written, so the run-id-suffixed
key with a `restore-keys` prefix match (already in §5.4) is required; and because caches are scoped
per repository, history never crosses repo boundaries on this backend.

#### The `issues` backend

One GitHub issue per fingerprint, in the consuming repo:

- **Title:** `[RCA] <category>: <one-line summary>`
- **Labels:** `rca-fingerprint`, `rca:<category>`
- **Body:** a fenced ` ```json ` block holding the serialised `FailureRecord`, under a
  human-readable summary. Parse the fenced block on read; ignore everything outside it.
- **Lookup:** `GET /search/issues?q=repo:{r}+label:rca-fingerprint+"{fingerprint}"+in:body`, or
  keep a single index issue mapping fingerprint → issue number to avoid the search API's rate limit
  (30 req/min authenticated) and its eventual-consistency lag.
- **On recurrence:** post a comment with the new run link and bump `count` in the body.
- **Resolution capture:** a comment beginning `/resolved ` sets `resolution` and
  `human_verified: true`. Close the issue when a human closes it; reopen on recurrence.

Requires `issues: write`, which the Phase 1 stub does **not** grant. Adding this backend means
widening the caller's `permissions:` block — call that out at the point of adoption.

Two cautions: `GITHUB_TOKEN` cannot trigger further workflow runs, so issue automation will not
cascade (usually desirable here); and issue bodies have a 65,536-character limit, which capped
`run_ids` and `templates` keep you well clear of.

### 10.6 Concurrency

Parallel failing jobs will collect concurrently and race on the store.

- **`cache`:** last-write-wins, and a losing writer silently drops records. Accept this in Phase 1;
  note it in `collection_notes` rather than pretending it cannot happen.
- **`issues`:** read the issue, merge, then update — and retry once on a `409`. Two runs appending
  a comment concurrently is harmless; two runs overwriting the JSON body is not.
- **`redis`:** use `WATCH`/`MULTI` or a Lua script for the read-modify-write.

Upserts must be **idempotent by run ID**: re-running collection on the same run must not increment
`count` or duplicate a `run_ids` entry. This is checked in §14.

### 10.7 What lands in the report

Under Verdict in `summary.md` (§9), the "Seen before" row is driven by this section:

| Match | Row renders as |
|---|---|
| `new` | `First occurrence` |
| `similar` | `Similar failure seen 2× in 9 days (class match)` |
| `exact` | `Seen 3× in 14 days (branches: main, feature/parser-rewrite)` |
| `exact` + resolution | as above, plus a **Previously resolved by** line quoting `resolution` |

**Cross-branch recurrence is the highest-value signal in the whole system.** The same fingerprint
appearing on unrelated branches, from different authors, is strong evidence of infrastructure or
flakiness rather than the change under test — and it is a deterministic conclusion, so set
`classification.is_flaky` from it directly rather than leaving it to a model.

---

## 11. `summary.json` schema

Define with Pydantic in `models.py`. `summary.json` is `model_dump_json(indent=2)` of `Summary`.

```python
class RunMeta(BaseModel):
    run_id: int
    run_attempt: int
    workflow_name: str
    html_url: str
    event: str                             # push | pull_request | schedule | ...
    actor: str
    head_sha: str
    head_branch: str
    # pull-request context — see Stage 0. All None for a branch push with no PR.
    pr_number: int | None = None
    pr_url: str | None = None
    is_fork: bool = False
    head_repo: str | None = None
    # blast radius — see Stage 2
    concurrent_failures: int = 0           # repo-wide failed runs in the last 30 min
    concurrent_workflows: list[str] = []
    concurrent_branches: list[str] = []
    # matrix / fan-out
    failed_job_total: int
    failed_jobs_analysed: int
    matrix_legs_identical: bool = False    # all failed legs share a first-error fingerprint

class Verdict(BaseModel):
    short_circuit: Literal[
        "infra_runner", "infra_widespread", "flake_same_sha_passed", "no_failed_jobs"
    ] | None = None
    requires_analysis: bool = True
    reason: str | None = None

class Classification(BaseModel):
    category: str                      # see Stage 5 table
    confidence: Literal["high", "medium", "low"]
    matched_pattern: str | None
    matched_line: int | None
    other_matches: list[str] = []
    is_infra_vs_code: Literal["infra", "code", "unknown"]
    is_flaky: bool = False

class StepInfo(BaseModel):
    number: int
    name: str
    conclusion: str                        # success | failure | skipped | cancelled
    duration_seconds: int | None = None
    duration_ratio_vs_last_green: float | None = None   # >3.0 suggests a hang
    suspected_cache_miss: bool = False

class ArtifactInfo(BaseModel):
    name: str
    size_bytes: int
    expired: bool = False
    parsed: bool = False                   # True only for artifacts actually downloaded

class RunnerInfo(BaseModel):
    name: str | None = None
    group: str | None = None
    labels: list[str] = []
    image: str | None = None               # from the Set up job group
    os: str | None = None
    disk_free_at_start: str | None = None

class LogWindow(BaseModel):
    label: Literal["first_error", "tail", "merged"]
    start_line: int
    end_line: int
    total_lines: int
    content: str
    truncated: bool = False

class TemplateVariable(BaseModel):
    position: int                            # index of the masked slot in the template
    mask: str                                # e.g. "HOST", "NUM"
    kind: Literal["string_low", "string_high", "numeric", "sequence"]
    values: list[str] = []                   # low-cardinality strings, or first 3 of high
    distinct_count: int | None = None        # set when kind == "string_high"
    minimum: float | None = None             # numeric only
    maximum: float | None = None
    median: float | None = None
    baseline_median: float | None = None
    sequence_rle: str | None = None          # e.g. "[1, 2, 4, 8, 16, 30x9]"
    # NOTE: all values here are post-redaction. See redact.py.

class LogTemplate(BaseModel):
    template_id: int                         # 1-based, stable within this report
    template: str                            # masked template string
    count: int
    baseline_count: int | None = None        # None when no baseline available
    is_novel: bool | None = None             # None when no baseline available
    first_line: int
    has_error_match: bool = False
    tier: Literal["T1", "T2", "T3", "T4", "T5"]
    variables: list[TemplateVariable] = []
    representative_line: str | None = None   # redacted raw text of first occurrence
    anomaly: Literal["missing", "depleted", "flooding"] | None = None
    anomaly_ratio: float | None = None

class DrainReport(BaseModel):
    baseline_available: bool
    baseline_run_id: int | None = None
    baseline_age_days: int | None = None
    baseline_template_count: int | None = None
    total_clusters: int
    tier_counts: dict[str, int]              # {"T1": 1, "T2": 2, ...}
    templates: list[LogTemplate]             # ranked; T5 may be truncated
    omitted_count: int = 0
    omitted_criteria: str | None = None
    fingerprint_degraded: bool = False
    masking_config_hash: str | None = None   # §10.3

class FailedJob(BaseModel):
    job_id: int
    name: str
    failed_step_name: str | None
    failed_step_number: int | None
    exit_code: int | None
    duration_seconds: int | None
    log_unavailable: bool = False
    queue_seconds: int | None = None       # started_at - created_at; ARC capacity signal
    runner: RunnerInfo | None = None
    steps: list[StepInfo] = []             # the full table, not just the failed step
    log_lines_raw: int = 0
    log_lines_clean: int = 0
    log_bytes: int = 0
    windows: list[LogWindow] = []
    stack_traces: list[StackTrace] = []
    error_lines: list[ErrorLine] = []        # feeds tiering; not rendered standalone
    annotations: list[str] = []

class CommitInfo(BaseModel):
    sha: str                               # short, 7-8 chars
    subject: str                           # first line only — never the full body
    author: str | None = None              # login where available, else name
    authored_at: datetime
    files_changed: int
    is_revert: bool = False
    is_merge: bool = False

class ChangeContext(BaseModel):
    base_sha: str | None = None
    head_sha: str
    range_basis: Literal["last_success", "merge_base", "head_only"] = "head_only"
    range_truncated: bool = False          # GitHub caps compare at 250 commits / 300 files
    total_commits: int = 0
    commits: list[CommitInfo] = []         # capped at 10, most recent first
    files_changed: int = 0
    additions: int = 0
    deletions: int = 0
    diffstat: str | None = None
    classes: list[str] = []                # ci_config | container | dependency | lockfile |
                                           # build_config | infra | test | source
    lockfile_deltas: dict[str, list[str]] = {}   # filename -> rendered transitions
    stack_trace_diffs: dict[str, str] = {}       # path -> hunk, max 3 files
    pr_title: str | None = None
    pr_labels: list[str] = []
    pr_is_draft: bool = False
    pr_body_excerpt: str | None = None     # first 500 chars

class HistoryContext(BaseModel):
    last_success_run_id: int | None = None
    last_success_sha: str | None = None
    last_success_at: datetime | None = None
    last_success_age_hours: float | None = None    # how far back the blame range reaches
    blame_range: str | None = None                 # "7d2e881..a3f9c21"
    first_failing_sha: str | None = None           # None when intermediate runs are missing
    recent_outcomes: str | None = None             # "✓✓✓✗"
    same_sha_runs: list[int] = []
    # recurrence — see §10
    match: Literal["new", "similar", "exact"] = "new"
    match_similarity: float | None = None          # Jaccard over template strings (§10.2)
    config_drift: bool = False                     # record predates current masking config (§10.3)
    seen_count: int = 0
    first_seen: datetime | None = None
    branches_seen: list[str] = []
    cross_branch: bool = False                     # drives classification.is_flaky
    previous_summary: str | None = None
    previous_resolution: str | None = None
    resolution_verified: bool = False
    backend: Literal["cache", "issues", "redis", "none"] = "cache"
    backend_degraded: bool = False                 # store unreachable; collection continued

class Summary(BaseModel):
    schema_version: Literal["1.0"] = "1.0"
    collector_version: str
    collected_at: datetime
    run: RunMeta
    verdict: Verdict
    classification: Classification
    failed_jobs: list[FailedJob]
    junit: JUnitReport | None = None
    artifacts: list[ArtifactInfo] = []
    drain: DrainReport | None = None
    changes: ChangeContext | None = None
    history: HistoryContext | None = None
    fingerprint: str                   # fine — see §10.2
    fingerprint_coarse: str
    kubernetes: None = None            # reserved for Phase 4 (CD)
    budget_report: BudgetReport
    collection_notes: list[str] = []
```

Rules:
- Keep `schema_version`. Phase 2 prompt templates will pin against it.
- The `kubernetes` key exists now and is always `null`. Do not remove it.
- Every optional section is `None` when unavailable, never an empty stub.
- `is_novel` and `baseline_count` are **tri-state**. `None` means "unknown, no baseline"; `False`
  and `0` are positive assertions that the template *was* seen in a healthy build. Conflating them
  makes every first run look like a catastrophe. Enforce this with a validator: if
  `DrainReport.baseline_available is False`, every template must have `is_novel is None` and
  `baseline_count is None`.
- `LogTemplate.representative_line` and all `TemplateVariable.values` are post-redaction. Add a
  unit test asserting that a synthetic log containing `Bearer sk-live-abc123` produces no template,
  variable, or representative line containing `sk-live-abc123`.

---

## 12. Sample workflows

### 12.1 Consumer stub — `.github/workflows/rca-collect.yml`

Two variants. Pick one. Both go in the **consuming** repo, on its **default branch**.

**Variant A — via the reusable workflow (recommended).** Least YAML, caching included.

```yaml
name: RCA Collect

on:
  workflow_run:
    workflows: ["CI", "Test Failure Scenarios"]
    types: [completed]

permissions:
  actions: read
  contents: read

jobs:
  rca:
    # Analyse failures; also train the Drain3 baseline from successful default-branch runs.
    if: >
      github.event.workflow_run.conclusion == 'failure' ||
      (github.event.workflow_run.conclusion == 'success' &&
       github.event.workflow_run.head_branch == 'main')
    uses: acme/ci-rca-collector/.github/workflows/rca.yml@v1
    with:
      run-id: ${{ github.event.workflow_run.id }}
      workflow-name: ${{ github.event.workflow_run.name }}
      conclusion: ${{ github.event.workflow_run.conclusion }}
      head-branch: ${{ github.event.workflow_run.head_branch }}
      runs-on: ubuntu-latest
```

**Variant B — calling the action directly.** More YAML, but full control over caching, runner, and
what happens with the outputs.

```yaml
name: RCA Collect

on:
  workflow_run:
    workflows: ["CI", "Test Failure Scenarios"]
    types: [completed]

permissions:
  actions: read
  contents: read

concurrency:
  group: rca-${{ github.event.workflow_run.head_branch }}
  cancel-in-progress: false

jobs:
  collect:
    if: >
      github.event.workflow_run.conclusion == 'failure' ||
      (github.event.workflow_run.conclusion == 'success' &&
       github.event.workflow_run.head_branch == 'main')
    runs-on: ubuntu-latest
    timeout-minutes: 5
    steps:
      - name: Restore Drain3 baseline
        uses: actions/cache@v4
        with:
          path: .drain/
          key: drain3-${{ github.event.workflow_run.name }}-${{ github.event.workflow_run.id }}
          restore-keys: |
            drain3-${{ github.event.workflow_run.name }}-

      - name: Restore fingerprint history
        uses: actions/cache@v4
        with:
          path: .rca-history/
          key: rca-history-${{ github.event.workflow_run.id }}
          restore-keys: |
            rca-history-

      - name: Collect failure context
        id: rca
        uses: acme/ci-rca-collector@v1
        with:
          run-id: ${{ github.event.workflow_run.id }}
          mode: ${{ github.event.workflow_run.conclusion == 'success' && 'train' || 'collect' }}

      - name: Upload artifact
        if: github.event.workflow_run.conclusion == 'failure'
        uses: actions/upload-artifact@v4
        with:
          name: rca-${{ github.event.workflow_run.id }}
          path: rca/
          retention-days: 30

      # Demonstrates the outputs contract. Phase 2 replaces this with the LLM call.
      - name: Report verdict
        if: steps.rca.outputs.requires-analysis == 'false'
        run: |
          echo "Deterministic verdict: ${{ steps.rca.outputs.short-circuit }}"
          echo "Category: ${{ steps.rca.outputs.category }} (${{ steps.rca.outputs.confidence }})"
          echo "Flaky: ${{ steps.rca.outputs.is-flaky }}"
          echo "Fingerprint ${{ steps.rca.outputs.fingerprint }} seen ${{ steps.rca.outputs.seen-count }}x before"
```

**Adoption checklist for a new consuming repo:**

1. Copy the stub to `.github/workflows/rca-collect.yml`.
2. Change `workflows: ["CI"]` to match the **`name:` field** of the workflows to watch — not filenames.
3. **Merge to the default branch.** `workflow_run` will not fire from a branch or a PR.
4. If the action repo is private, confirm the org and repo Actions access settings per §5.6.
5. Trigger a failure and check the run's Summary tab.

### 12.2 `.github/workflows/test-failures.yml`

You need real, reproducible failures to develop against. This workflow generates one of each
category on demand.

```yaml
name: Test Failure Scenarios

on:
  workflow_dispatch:
    inputs:
      scenario:
        type: choice
        options: [compile, test, dependency, oom, timeout, disk, network, crash, all]
        default: all

jobs:
  compile:
    if: contains(fromJSON('["compile","all"]'), inputs.scenario)
    runs-on: ubuntu-latest
    steps:
      - run: |
          cat > bad.py <<'EOF'
          def broken(
          EOF
          python bad.py

  test:
    if: contains(fromJSON('["test","all"]'), inputs.scenario)
    runs-on: ubuntu-latest
    steps:
      - run: pip install pytest
      - run: |
          mkdir -p t && cat > t/test_x.py <<'EOF'
          def test_pass(): assert True
          def test_fail(): assert [1,2] == [1,2,3], "list mismatch"
          def test_raise(): raise ValueError("boom in nested call")
          EOF
      - run: pytest t/ --junitxml=junit.xml
      - if: always()
        uses: actions/upload-artifact@v4
        with: { name: test-results, path: junit.xml }

  dependency:
    if: contains(fromJSON('["dependency","all"]'), inputs.scenario)
    runs-on: ubuntu-latest
    steps:
      - run: pip install this-package-does-not-exist-9f3a

  oom:
    if: contains(fromJSON('["oom","all"]'), inputs.scenario)
    runs-on: ubuntu-latest
    steps:
      - run: |
          python -c "x=[]
          while True: x.append(' ' * 10_000_000)"

  timeout:
    if: contains(fromJSON('["timeout","all"]'), inputs.scenario)
    runs-on: ubuntu-latest
    timeout-minutes: 1
    steps:
      - run: sleep 300

  disk:
    if: contains(fromJSON('["disk","all"]'), inputs.scenario)
    runs-on: ubuntu-latest
    steps:
      - run: fallocate -l 200G /tmp/big || dd if=/dev/zero of=/tmp/big bs=1M

  network:
    if: contains(fromJSON('["network","all"]'), inputs.scenario)
    runs-on: ubuntu-latest
    steps:
      - run: curl --fail --max-time 10 https://this-host-does-not-exist-9f3a.invalid/

  crash:
    if: contains(fromJSON('["crash","all"]'), inputs.scenario)
    runs-on: ubuntu-latest
    steps:
      - run: |
          cat > c.go <<'EOF'
          package main
          func main() { var p *int; _ = *p }
          EOF
          go run c.go

  noisy:
    # Large, repetitive log with one real error buried in it — the Drain3 tiering test case.
    if: contains(fromJSON('["noisy","all"]'), inputs.scenario)
    runs-on: ubuntu-latest
    steps:
      - run: |
          for i in $(seq 1 20000); do echo "INFO processing record $i of 20000 [ok]"; done
          echo "Exception: Connection refused to db:5432"
          for d in 1 2 4 8 16 30 30 30 30 30 30 30 30 30; do echo "Retrying connection in ${d}s"; done
          echo "ERROR failed to flush buffer: connection reset by peer"
          for i in $(seq 1 5000); do echo "INFO cleanup task $i complete"; done
          exit 1

  depleted:
    # Same templates as `noisy` but at ~5% volume. Run AFTER a baseline is trained from
    # `noisy` on main. Tests depletion detection and T2 promotion.
    if: contains(fromJSON('["depleted","all"]'), inputs.scenario)
    runs-on: ubuntu-latest
    steps:
      - run: |
          for i in $(seq 1 1000); do echo "INFO processing record $i of 20000 [ok]"; done
          echo "ERROR failed to flush buffer: connection reset by peer"
          exit 1

  secrets:
    # Verifies redaction of BOTH template content and extracted Drain3 variables.
    # These are fake values, not real credentials.
    if: contains(fromJSON('["secrets","all"]'), inputs.scenario)
    runs-on: ubuntu-latest
    steps:
      - run: |
          echo "Calling API with Authorization: Bearer sk-live-abc123def456"
          echo "connecting user=admin password=hunter2 host=db.internal"
          echo "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE"
          exit 1
```

Add `noisy`, `depleted`, and `secrets` to the `workflow_dispatch` choice list above.

**Ordering note for the `depleted` test:** the baseline is trained only from successful runs on
`main`. To test depletion you need a *successful* high-volume run first. Either temporarily add
`exit 0` to a `main` run of `noisy`, or use the `train --from-fixture` path to seed
`.drain/` directly from a captured high-volume log. The second is faster and is what the unit test
should do.

---

## 13. Non-functional requirements

- **The collector must never fail the workflow.** Wrap `main()` so any unexpected exception is
  caught, logged, appended to `collection_notes`, and still produces a partial `summary.md` with
  whatever was gathered. Exit 0 always in CI mode; the `--strict` flag (surfaced as the
  `fail-on-error` action input) may exit non-zero for local dev. **Outputs must still be written on
  the failure path** — emit `category=unknown`, `requires-analysis=true`, and an empty
  `short-circuit` rather than nothing, or callers' `if:` conditions silently misbehave.
- **The action must be self-contained.** No `actions/checkout` inside the composite action, no
  assumption that the caller's workspace contains anything. Everything the collector needs comes
  from `$GITHUB_ACTION_PATH`.
- **No writes outside the workspace and the two cache dirs.** The caller's checkout, if any, must be
  left untouched.
- **Deterministic.** Same run ID + same cache state → byte-identical output. No timestamps inside
  content blocks, no unordered set iteration.
- **Offline-testable.** A `--from-fixture <dir>` flag replays captured raw logs and API responses
  from `tests/rca/fixtures/`, with zero network access. Debugging this in CI is slow; make local
  iteration possible from day one. Add a `capture` subcommand that saves a real run's raw responses
  into a fixture dir.
- **Idempotent.** Re-running against the same run ID must not double-count fingerprint history.
- **Runtime budget: 90 seconds of collection, 3 minutes wall clock for the job.** Set a
  `timeout-minutes: 5` on the collect job. A report that arrives after the engineer has already
  read the logs has no value, so treat slowness as a correctness bug. Enforce per-call HTTP
  timeouts (30s) and an overall deadline; on deadline, emit what has been gathered and note the
  truncation rather than failing.
- **Guard against collect-storms.** Several CI workflows failing together will each trigger a
  collect run. Add a concurrency group to the consumer stub so repeated failures on one branch
  supersede rather than queue:
  ```yaml
  concurrency:
    group: rca-${{ github.event.workflow_run.head_branch }}
    cancel-in-progress: false
  ```
  Use `cancel-in-progress: false` — cancelling a collect run mid-write can leave the history store
  in a partial state, and the runs are short anyway.
- **`refingerprint` subcommand.** `cli.py refingerprint [--dry-run]` replays each stored record's
  `templates` through the current masking config, recomputes both fingerprints and
  `masking_config_hash`, and rewrites the records. `--dry-run` reports how many would change and
  writes nothing. This is the migration path in §10.3 and must exist before the first `drain3.ini`
  edit, not after it.
- **Logging.** Structured logs to stderr at `--verbose`; `summary.md` to `rca/` only.
- **Dependencies** — keep minimal: `pydantic>=2`, `httpx`, `drain3`, `python-dateutil`. Avoid
  `PyGithub` unless it earns itself; the raw REST calls here are few and the redirect handling for
  logs is easier with `httpx` directly (`follow_redirects=True`).

---

## 14. Acceptance criteria for Phase 1

1. Trigger `test-failures.yml` with `scenario: all`. `rca-collect.yml` fires for each failed job.
2. Each produces a `summary.md` in the job summary and an uploaded artifact.
3. `classification.category` is correct for at least 7 of the 9 scenarios.
4. The `noisy` scenario's summary surfaces `ERROR failed to flush buffer` in the first-error window,
   and the `INFO processing record` lines collapse to a single **T5** row with a count of ~20,000.
5. In the `noisy` scenario, `ERROR failed to flush buffer` is assigned **T1** and appears first in
   the templates table.
6. **Depletion detection:** run `depleted` (below) after a baseline has been trained from `noisy`
   run on `main`. The `INFO processing record` template must carry `anomaly == "depleted"` with a
   ratio near 0.05, and be promoted to T2.
7. **Tri-state:** with the Drain3 cache deleted, every template has `is_novel is None`, and no
   template is assigned T1 or T2.
8. **Redaction:** a fixture log containing `Authorization: Bearer sk-live-abc123` produces a
   `summary.json` in which that string appears nowhere — not in templates, variables, representative
   lines, or windows.
9. **Fingerprint stability:** two runs of the same failure with different line counts, timestamps,
   container IDs, and durations produce the **same** fingerprint.
10. `summary.md` for every scenario fits under 8,000 estimated tokens.
11. Killing a runner pod mid-job yields `verdict.short_circuit == "infra_runner"`.
12. Re-running a failed job that then passes yields `classification.is_flaky == true` on the
    subsequent collection for that SHA.
13. `python -m tools.rca.cli collect --from-fixture tests/rca/fixtures/npm-eresolve` works with the
    network disabled.
14. **Action consumability:** a second repository containing only the §12.1 stub, with no Python and
    no checkout of the collector, produces a correct `summary.md` in its job summary.
15. **Outputs contract:** `steps.rca.outputs.category`, `fingerprint`, `is-flaky`, `short-circuit`,
    and `requires-analysis` are all readable by a following step in the caller's workflow, and
    `requires-analysis == 'false'` on the `infra_runner` scenario.
16. **Outputs on the failure path:** injecting an exception into the collector still yields a step
    that exits 0 and emits `category=unknown` and `requires-analysis=true`.
17. **Pinned-ref resolution:** `uses: acme/ci-rca-collector@v1` resolves and runs after the floating
    `v1` tag is moved to a new commit.
18. **No-setup-python path:** with `setup-python: false` on a runner image that ships Python and the
    requirements preinstalled, the action completes without network access to PyPI.
19. **Recurrence, exact:** running the same fixture failure twice yields `match == "exact"` and
    `seen_count == 2` on the second run, with an identical `fingerprint`.
20. **Recurrence, similar:** a fixture whose top error message is reworded but whose remaining
    templates are unchanged yields `match == "similar"`, not `"new"`.
21. **Idempotency:** re-running collection against the same run ID leaves `count` and `run_ids`
    unchanged — no double-counting.
22. **Cross-branch flake:** the same fingerprint recorded from two unrelated branches sets
    `history.cross_branch` and `classification.is_flaky` without any model involvement.
23. **Backend parity:** the same fixture produces identical `history.match` and `seen_count` under
    the `cache` and `issues` backends.
24. **Store outage:** with the history store unreachable, collection still completes, emits a full
    `summary.md`, and sets `history.backend_degraded == true`.
25. **Similarity uses strings, not IDs:** two fixtures producing the same templates in a different
    order — so Drain3 assigns different cluster IDs — yield a Jaccard score of 1.0, not a lower one.
26. **Config drift is detected:** after adding one masking regex to `drain3.ini`, a previously
    recorded failure yields `history.config_drift == true` and `match == "similar"` — never
    `"exact"`, and never `"new"` when the unmasked words are unchanged.
27. **Config hash is stable and scoped:** reordering keys, reformatting whitespace, or editing a
    comment in `drain3.ini` leaves `masking_config_hash` unchanged; changing any masking regex or
    `sim_th` changes it.
28. **Re-fingerprint dry run:** `cli.py refingerprint --dry-run` against a fixture store reports the
    correct count of records whose fingerprints would change, and writes nothing.
29. **Matrix cap:** a run with 8 failing matrix legs analyses 3, sets `failed_job_total == 8` and
    `failed_jobs_analysed == 3`, and stays under the token budget.
30. **Identical legs:** when every failing leg shares a first-error fingerprint,
    `matrix_legs_identical == true` and the report analyses one leg while stating the rest match.
31. **Fail-fast cancellations:** legs with conclusion `cancelled` alongside a genuine `failure` are
    excluded from analysis and from `failed_job_total`.
32. **Fork PR context:** a failure on a fork pull request — where
    `workflow_run.pull_requests` is empty — still resolves `pr_number` via the commit fallback and
    sets `is_fork == true`.
33. **Rate-limit degradation:** with `x-ratelimit-remaining` forced below 50, collection skips
    optional history and artifact calls, completes, and records the reason in `collection_notes`.
34. **Runtime:** collection against the `noisy` fixture completes within 90 seconds.
35. **Source-agnostic core:** `cleaner.py`, `drain_index.py`, `budget.py`, `redact.py` and
    `history.py` contain no import of `github_api` and no reference to `job_id` or `run_id`.
    Enforce with a test that greps the modules — it is the cheapest way to keep §1.3 true.
36. **Multi-commit range:** a failure whose branch advanced 3 commits since the last green run
    lists all 3 subjects, not just the tip commit's.
37. **Base fallback:** on a branch with no prior successful run, collection completes with
    `range_basis == "merge_base"` or `"head_only"` and does not error.
38. **Blast radius:** a range touching `.github/workflows/ci.yml` sets `ci_config` in
    `changes.classes` and surfaces it prominently in `summary.md`.
39. **Revert detection:** a commit whose subject begins `Revert "` sets `is_revert`.
40. **First failing commit:** where intermediate runs exist for every commit in the range,
    `first_failing_sha` is set to the correct commit; where they are missing, it is `None` rather
    than a guess.
41. **Lockfile ordering:** major version bumps sort above minor and patch bumps in the rendered
    delta.
42. **Blast radius, positive:** with three unrelated workflows failing on two branches inside the
    window, `short_circuit == "infra_widespread"` and Drain3 and change context are skipped.
43. **Blast radius, negative:** an isolated failure records `concurrent_failures == 0` and states
    it in the report rather than omitting the section.
44. **Collector storms excluded:** repeated collect-workflow runs do not inflate
    `concurrent_failures`.
45. **Timeout not discarded:** a job killed by `timeout-minutes` is classified `timeout` and
    analysed, even when its conclusion reads `cancelled`.
46. **Post-step failure:** a job whose only failure is in a post-action produces a report
    containing that step's output, not an empty one.
47. **Step table:** the report lists all steps with conclusions, and flags a sub-second successful
    cache-restore step preceding a dependency failure.
48. **Runner identity:** `runner_name`, `runner_group` and the image tag from `Set up job` appear
    in `summary.json` for a self-hosted run.
49. **Artifact handling:** an artifact over 50 MB is listed by name and size but not downloaded.

---

## 15. Deferred to later phases

Per-phase detail behind the table in §1.2. Each phase extends the **same action**, adding inputs and
outputs rather than replacing anything. `summary.json` stays the interface between phases.

- **Phase 2:** analysis via the ST ChatGPT bridge. Contract is §16. `collect` stays collection-only;
  `analyze` is a separate CLI. Gate on `requires_analysis`, cache by fingerprint, validate citations,
  call `trinity_for_api` then fall back to `alfred_for_api`. New outputs (wired later, not in Slice H):
  `root-cause`, `suggested-fix`, `rca-confidence`.
- **Phase 3:** delivery, and the point at which `history-backend` moves from `cache` to `issues`
  (§10.4) so failure memory becomes durable and engineers can supply resolutions as comments.
  Sticky PR comment, auto-created issue with labels, SMTP email. Likely a
  **separate action** (`acme/ci-rca-reporter@v1`) consuming this action's `json-path` output, so
  collection and delivery version independently. Note that `GITHUB_TOKEN` cannot trigger further
  workflow runs, which matters once comments start firing automation.
- **Phase 4:** CD / Kubernetes — populate the reserved `kubernetes` key from `kubectl describe`,
  Warning events, `kubectl logs --previous`, rollout status, container exit reasons. Adds a
  `kubeconfig` input and a `mode: collect-k8s`. Consider `k8sgpt` as the deterministic analyzer
  front-end. This bumps `schema_version`, and therefore the action's major version.

---

## 16. Phase 2 — Analysis (ST ChatGPT)

Phase 1 is unchanged: `collect` still emits `summary.json` / `summary.md` and never calls a model.
Phase 2 adds a **separate** `analyze` CLI that reads those artifacts and, when gated in, calls the
ST ChatGPT client-apps bridge.

Do **not** fold analyze into `collect`. Do **not** change `action.yml` until a later slice wires it.

### 16.1 Gate

Call the bridge only when `summary.verdict.requires_analysis is True`.

Otherwise write an `AnalysisRecord` with `status="gated"`, make no HTTP request, and leave
`summary.md` as the published artifact.

### 16.2 Cache

Cache key: `sha256(f"{fine}|{category}|{masking_hash}|{prompt_version}")[:16]`.

`fine` is `summary.fingerprint`, `category` is `classification.category`, `masking_hash` is
`drain.masking_config_hash` (empty string when Drain3 did not run).

On hit: `status="cached"`, `cache_hit=True`, reuse the stored `AnalysisResult`. No HTTP.

`PROMPT_VERSION` is `"p2.1"`. Bump it in the same commit as any prompt-template change.

### 16.3 ST ChatGPT bridge (`stgpt_client.py`)

Not OpenAI, not PyGithub. One module, httpx, same TLS helpers as the GitHub client
(`RCA_SSL_CERT_FILE` / `RCA_SSL_VERIFY` via `resolve_ssl_verify()`).

Config (`config.py`):

| Name | Default / source |
|---|---|
| `STGPT_API_URL` | `https://api-ai-bridge-dev.st.com/chatgpt/api/client-apps` |
| `STGPT_CLIENT_APP_NAME` | `gtrd_srmtdpplm` |
| `STGPT_API` | env `STGPT_API` (secret name on GHES). Never log it. |
| `PERSONAS` | `("trinity_for_api", "alfred_for_api")` |
| `PROMPT_VERSION` | `"p2.1"` |
| `STGPT_SERVICE` | `"chatgpt"` (the `service` segment of the auth hash) |

Auth token:

```
generate_auth_token(client, service, key, ts, nonce)
  = SHA1 hex of f"{client}_{service}_{key}_{ts}_{nonce}"
```

`post_chat(url, api_key, client_app_name, persona, messages, ...)`:

- `POST {url.rstrip("/")}/{client_app_name}`
- JSON body includes `persona` and `messages` (OpenAI-shaped `{role, content}`)
- Headers:
  - `stchatgpt-auth-token` — the SHA1 hex
  - `stchatgpt-auth-nonce` — the nonce used in the hash
  - `stchatgpt-auth-timestamp` — Unix-epoch seconds, the `ts` used in the hash
- `verify=` from `resolve_ssl_verify()` (same order as GitHub: `RCA_SSL_CERT_FILE`,
  `SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`, then `RCA_SSL_VERIFY=false`)
- Return `(status_code, body dict, completion str | None, response_id)`
- `completion` is `body["completion"]` when that value is a string; otherwise `None`
- `response_id` is `body["id"]` or `body["response_id"]` when present
- Never print or log `api_key` or the auth token. HTTP error statuses are returned, not raised.
  Transport failures raise `StgptError` with secrets stripped.

### 16.4 trinity → alfred

1. Call `trinity_for_api`.
2. Parse `completion` as JSON into `AnalysisResult`. One repair retry on the **same** persona if
   parse fails.
3. Run citation validation.
4. If the result is still unusable (`parse_error`, `citation_invalid`, empty completion, or
   bridge error), call `alfred_for_api` with the same prompt and the same one-repair-retry rule.
5. `fallback_used=True` when alfred is invoked. `persona` records which persona produced the
   accepted result (or the last attempt).
6. If both personas fail: `status="unusable"` and publish `summary.md` alone.

### 16.5 Citation validation

Every `AnalysisCitation.quote` must be a substring of the **`<EVIDENCE>` block actually sent**
to the model (budget 6000 tokens, built from `summary.json` only). A miss triggers one repair
retry on the same persona. After repair (and alfred, if invoked) still invented: `status="unvalidated"`.
Do not publish an `ok` root cause that cites text the collector did not send.

Allowed `source` values: `first_error_window`, `tail_window`, `stack_traces`, `log_templates`,
`junit`, `change_context`, `annotations`, `history`, `step_table`.

### 16.6 `analyze` CLI

```
python -m tools.rca.cli analyze --summary rca/summary.json --out rca/
```

Separate subcommand from `collect`. Reads `summary.json`, applies gate → cache → bridge →
validate, writes `analysis.json` (`AnalysisRecord`) next to the summary. Does not re-collect
logs. Does not post PR comments or open issues.

### 16.7 Schema (`models.py`)

Not attached to `Summary` in Slice H (that would change collect output). Defined now so later
slices import one schema.

```python
class AnalysisCitation(BaseModel):
    quote: str
    source: Literal[
        "first_error_window", "tail_window", "stack_traces", "log_templates",
        "junit", "change_context", "annotations", "history", "step_table",
    ]
    line: int | None = None

class AnalysisResult(BaseModel):
    root_cause: str
    suggested_fix: str
    confidence: Literal["high", "medium", "low"]
    citations: list[AnalysisCitation] = []

class AnalysisRecord(BaseModel):
    status: Literal[
        "ok",                 # valid result from trinity or alfred
        "cached",             # replayed from the analysis cache
        "gated",              # requires_analysis false, short_circuit, or missing STGPT_API
        "unvalidated",        # structured result whose citations were not in evidence
        "failed",             # analyze error; exit 0 unless --strict
        "parse_error",        # completion was not a valid AnalysisResult
        "citation_invalid",   # a quote was not in the cited evidence
        "bridge_error",       # HTTP / auth / transport failure
        "unusable",           # both personas failed; publish summary.md only
    ]
    prompt_version: str = "p2.1"
    persona: str | None = None
    fingerprint: str | None = None
    schema_version: str | None = None
    result: AnalysisResult | None = None
    cache_hit: bool = False
    fallback_used: bool = False
    response_id: str | None = None
    notes: list[str] = []
    analyzed_at: datetime | None = None
```

### 16.8 Phase 2 build order

| Slice | Ships | Must not |
|---|---|---|
| **H** | `stgpt_client.py`, Analysis* models, STGPT config | collect, `action.yml`, real network |
| **I** | `prompt.py` + `analyze.py` + `analyze` CLI: gate, cache, trinity→alfred, citations | `action.yml` |
| J | action.yml `analyze` input / outputs | changing collect's default path |
