# Phase 1 execution playbook

How to turn `ci-rca-collector-spec-v8.md` into a working GitHub Action using Grok CLI, first on github.com, later on `github.st.com`.

## 0. Recommended strategy (do this, not "generate the whole repo")

The spec is large and internally consistent. The failure mode with coding agents is asking for all 15 modules in one shot: contracts drift, GitHub calls leak into source-agnostic modules, and you cannot test anything until the end.

**Work in thin vertical slices that each produce a runnable artifact.**

| Slice | What ships | How you know it works |
|---|---|---|
| A | Repo skeleton + host/token config + `models.py` | `python -c "from tools.rca.models import Summary"` |
| B | `github_api.py` + `cleaner.py` + CLI `collect --from-fixture` | Clean a captured log offline |
| C | Thin `action.yml` that runs the CLI | Action runs on a real failed workflow on github.com |
| D | Classify + extract + render a short report | `summary.md` exists and is readable |
| E | History last-success + `changes.py` | Commits/range appear in the report |
| F | Drain3 + fingerprints + cache backend | Recurrence + novelty work |
| G | Budget, redact, outputs, reusable workflow, consumer stub | Acceptance items 1–16, 35 |

Ship slice C as early as the spec says (~build-order step 5). A partial action you can invoke from a second repo is more valuable than a perfect library nobody can call.

## 1. Repo layout you should create first

Use **two repositories**.

### Action repo (shared logic)

Suggested name on github.com: `your-org/ci-rca-collector`  
Later the same tree moves to `github.st.com/<org>/ci-rca-collector`.

```
action.yml
requirements.txt
drain3.ini
AGENTS.md
README.md
docs/ci-rca-collector-spec-v8.md
tools/rca/
  __init__.py
  cli.py
  config.py
  models.py
  github_api.py
  ...
.github/workflows/
  rca.yml
  self-test.yml
tests/rca/
  fixtures/
  test_*.py
```

### Consumer / dogfood repo

A tiny repo that only contains CI + the stub in spec §12.1. This is where you generate real failures with `test-failures.yml`. Do **not** put Python here.

Why two repos: acceptance criterion 14 is "a second repository containing only the stub produces a correct summary." If you dogfood only inside the action repo you will miss the `${{ github.action_path }}` packaging bugs.

## 2. GitHub host + token — design this before any API call

Actions already injects the right URLs. Your code must **read them**, not invent them.

| Env var | github.com | github.st.com (GHES) | Who sets it |
|---|---|---|---|
| `GITHUB_SERVER_URL` | `https://github.com` | `https://github.st.com` | Actions runtime |
| `GITHUB_API_URL` | `https://api.github.com` | `https://github.st.com/api/v3` | Actions runtime |
| `GH_HOST` | unset / `github.com` | `github.st.com` | you, for `gh` CLI / laptop |
| `RCA_GITHUB_HOST` | optional override | `github.st.com` | you |
| `RCA_GITHUB_API_URL` | optional override | `https://github.st.com/api/v3` | you |

Token order (implement exactly this, document it in README):

1. CLI `--token`
2. `RCA_GITHUB_TOKEN`
3. **`COMMON_ACTIONS_PAT`**  ← org/repo secret you will use
4. `GITHUB_TOKEN`            ← default Actions token
5. `GH_TOKEN`                ← `gh` CLI convention

On github.com during Phase 1 testing:

- Prefer `secrets.COMMON_ACTIONS_PAT` if you create a classic/fine-grained PAT with `actions:read` + `contents:read` (and `repo` if private).
- `github.token` is enough for same-repo collection if the workflow declares `permissions: { actions: read, contents: read }`.

On `github.st.com` later:

- Keep the **same secret name** `COMMON_ACTIONS_PAT` as an org secret.
- You usually still pass it into the action `github-token` input because GHES `GITHUB_TOKEN` scopes are often tighter.
- You should **not** need to change Python. Actions will set `GITHUB_API_URL` to `https://github.st.com/api/v3`.

Laptop / Grok CLI local runs against real runs:

```bash
# github.com
export GITHUB_API_URL=https://api.github.com
export COMMON_ACTIONS_PAT=ghp_...   # or GITHUB_TOKEN

# later, GHES
export GITHUB_SERVER_URL=https://github.st.com
export GITHUB_API_URL=https://github.st.com/api/v3
export GH_HOST=github.st.com
export COMMON_ACTIONS_PAT=...       # GHES PAT
```

`github_api.py` must use `httpx.Client(base_url=api_url, headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})`.

Never concatenate `https://api.{host}` for Enterprise. That is the classic GHES bug. Host `github.st.com` → API `https://github.st.com/api/v3`.

## 3. Local bootstrap (15 minutes, before Grok writes code)

```bash
mkdir ci-rca-collector && cd ci-rca-collector
git init
mkdir -p tools/rca tests/rca/fixtures docs .github/workflows

# put the spec where the agent can see it
cp /path/to/ci-rca-collector-spec-v8.md docs/
cp /path/to/AGENTS.md .

python3 -m venv .venv
source .venv/bin/activate
# leave requirements.txt to slice A
```

Pin a floating tag plan now, even if you have no code:

- develop on `main`
- first working action tag `v0.1.0`
- floating `v1` only after the action is consumable

## 4. How to drive Grok CLI (the part that actually determines quality)

Grok Build (`grok`) is already on your machine. Use **one session per slice**, not one session for the whole spec.

```bash
cd ci-rca-collector
grok
```

At the start of every session, pin context:

```text
Read AGENTS.md and docs/ci-rca-collector-spec-v8.md.
We are implementing Phase 1 only.
Current slice: <A–G>.
Do not implement modules outside this slice.
GitHub host/token resolution is defined in AGENTS.md — never hardcode api.github.com.
```

Rules that keep the agent on the rails:

1. **Reference the spec file** with `@docs/ci-rca-collector-spec-v8.md` and the target module section.
2. **Ask for one module + its tests**, then run the tests yourself before the next prompt.
3. After `models.py` exists, say: "Do not invent fields. If the spec model is missing a type (`StackTrace`, `ErrorLine`, `JUnitReport`, `BudgetReport`), add the minimal spec-faithful definition and stop."
4. After each slice, run: `rg -n "job_id|run_id|github_api" tools/rca/cleaner.py tools/rca/drain_index.py tools/rca/budget.py tools/rca/redact.py tools/rca/history.py` — must be empty (acceptance 35).
5. Prefer plan mode for slices C, F, G.

Headless example if you want a recorded prompt:

```bash
grok -p "Implement tools/rca/models.py exactly from spec §11. Add the missing referenced models with the smallest faithful schema. No other files."
```

Do **not** paste the entire spec into every prompt. Point at the file.

## 5. Slice-by-slice Grok prompts

Copy these. Change nothing essential.

### Slice A — schema + config

```text
@docs/ci-rca-collector-spec-v8.md @AGENTS.md

Create only:
- tools/rca/__init__.py
- tools/rca/config.py   (budgets, RUNNER_FAILURE_PATTERNS, CLASSIFY_RULES stubs, MASKING_CONFIG_VERSION, host/token resolution helpers)
- tools/rca/models.py   (spec §11, including referenced types the spec names but does not fully expand)
- requirements.txt
- drain3.ini            (exact contents from spec §7 Stage 7)

config.py must expose:
  resolve_github_api_url()
  resolve_github_server_url()
  resolve_github_token()

Resolution order is in AGENTS.md. Include COMMON_ACTIONS_PAT.
Add a unit test tests/rca/test_config_host.py covering:
- default github.com API
- GITHUB_API_URL passthrough
- host github.st.com → https://github.st.com/api/v3
- token order COMMON_ACTIONS_PAT over GITHUB_TOKEN
```

### Slice B — fetch + clean + fixture replay

```text
Implement tools/rca/github_api.py and tools/rca/cleaner.py per spec §7 Stages 0 and 4 and §3.
httpx only. Follow redirects for job logs. Honour x-ratelimit-remaining.
Then implement a minimal tools/rca/cli.py with:
  collect --run-id --repo --out --from-fixture
  capture --run-id --repo --out-fixture
No Drain3, no classify yet.
Add tests/rca/test_cleaner.py with a raw GitHub-timestamped ANSI log.
```

Capture one real failure as soon as this works:

```bash
export COMMON_ACTIONS_PAT=...
python -m tools.rca.cli capture --run-id <ID> --repo <owner/repo> --out-fixture tests/rca/fixtures/first-failure
python -m tools.rca.cli collect --from-fixture tests/rca/fixtures/first-failure --out /tmp/rca
```

### Slice C — thin action (do this earlier than it feels ready)

```text
Write root action.yml exactly from spec §5.3 with these additions:
- input github-api-url (optional, default empty; we pass GITHUB_API_URL)
- env GITHUB_API_URL, GITHUB_SERVER_URL, COMMON_ACTIONS_PAT forwarded
- preflight in the pip install step that prints python and pip versions
Wire cli.py so it writes rca/summary.json and rca/summary.md even if later stages are stubs.
```

Consumer stub on github.com (variant B first — easier to debug than the reusable workflow):

```yaml
# in the dogfood repo: .github/workflows/rca-collect.yml
name: RCA Collect
on:
  workflow_run:
    workflows: ["CI", "Test Failure Scenarios"]
    types: [completed]
permissions:
  actions: read
  contents: read
jobs:
  collect:
    if: github.event.workflow_run.conclusion == 'failure'
    runs-on: ubuntu-latest
    timeout-minutes: 5
    steps:
      - uses: your-org/ci-rca-collector@main
        with:
          run-id: ${{ github.event.workflow_run.id }}
          github-token: ${{ secrets.COMMON_ACTIONS_PAT || github.token }}
```

Until you publish a tag, pin `@main` or `@<sha>`. Move to `@v1` after slice G.

If the action repo is private: org Settings → Actions → accessible from org repos. This is the #1 adoption failure on both github.com and GHES.

### Slice D — classify, extract, render

```text
Implement classify.py (Stages 1–3, 5), extract.py (Stage 6), render.py + budget.py + redact.py enough to emit the spec §9 markdown with omitted-empty-sections.
Short-circuit infra_runner and flake_same_sha_passed must set requires_analysis=false.
Skip Drain3 and changes if not present; render must tolerate missing sections.
```

Then add spec §12.2 `test-failures.yml` to the dogfood repo and run `scenario: compile`, then `dependency`, then `timeout`. Check category accuracy before touching Drain3.

### Slice E — history (non-recurrence) then changes

Order is mandatory: last-success SHA first, then compare API.

```text
Implement the non-recurrence half of history.py, then changes.py, per spec Stage 8 and §6 item 5.
range_basis must be recorded. Cap commits at 10. Classify paths. Parse lockfile deltas without embedding the lockfile.
```

### Slice F — Drain3 + fingerprint store

```text
Implement drain_index.py including masking_config_hash().
Then HistoryStore protocol + cache backend + none backend.
Wire fingerprints fine/coarse, Jaccard on template strings (not IDs), config drift.
Add refingerprint --dry-run subcommand.
```

Do **not** implement the `issues` backend in the first working workflow. Spec allows `cache` for Phase 1. Building `issues` early forces `issues: write` and complicates GHES permissions.

### Slice G — outputs, reusable workflow, hardening

```text
Implement outputs.py writing GITHUB_OUTPUT.
Add .github/workflows/rca.yml from spec §5.4.
Add self-test.yml against fixtures.
Guard collect() so unexpected exceptions still write outputs (category=unknown, requires-analysis=true) and exit 0.
```

## 6. Testing plan on github.com (before any GHES move)

1. Create public or private repos on github.com: `ci-rca-collector` + `ci-rca-dogfood`.
2. Put `COMMON_ACTIONS_PAT` in both repos (or org). Token scopes: `repo`, `actions:read`. Fine-grained: Actions Read + Contents Read on both repos.
3. Merge consumer stub to **default branch**. `workflow_run` does not fire from a feature branch.
4. `workflows:` in the stub must match the **`name:`** field, not the filename.
5. Run `Test Failure Scenarios` with `compile`, then `all`.
6. Confirm:
   - collect workflow appears
   - job summary contains `CI Failure Report`
   - artifact `rca-<run-id>` downloads
   - `steps.rca.outputs.category` is readable if you added the echo step
7. Delete the Drain3 cache and rerun: templates must be tri-state `is_novel=null` (acceptance 7).
8. Run the `secrets` scenario and grep the artifact for `sk-live-abc123` — must be absent.

Local gate before each push:

```bash
pytest tests/rca -q
python -m tools.rca.cli collect --from-fixture tests/rca/fixtures/first-failure --out /tmp/rca
```

## 7. Moving to github.st.com

You should not fork the code. You migrate the same git history.

1. Add GHES remote: `git remote add st https://github.st.com/<org>/ci-rca-collector.git`
2. Create org secret `COMMON_ACTIONS_PAT` on GHES (new token issued by that appliance).
3. Confirm Actions can resolve `uses: <org>/ci-rca-collector@v1` (GHES actions access policy is separate from github.com).
4. In the consumer stub on GHES you usually do **not** set API URL inputs. The runner sets `GITHUB_API_URL=https://github.st.com/api/v3`.
5. If runners are locked-down ARC pods: set `setup-python: false` and bake `requirements.txt` into the image (spec §5.8).
6. Self-hosted caveats that bite on GHES more than on github.com:
   - no outbound PyPI → bake deps
   - `actions/cache` may talk to a different blob service; first run always cache-misses
   - rate limits and search API behaviour differ; keep history backend on `cache` until Phase 3
7. Laptop test against a GHES run:

```bash
export GH_HOST=github.st.com
export GITHUB_API_URL=https://github.st.com/api/v3
export COMMON_ACTIONS_PAT=...
python -m tools.rca.cli collect --run-id <id> --repo <org/repo> --out /tmp/rca
```

If collection works locally against GHES with only env vars changed, the action will work there.

## 8. action.yml additions the spec does not spell out (you need them)

Keep spec inputs. Add these so the host switch is a workflow edit, not a code edit:

```yaml
inputs:
  github-api-url:
    description: 'Override GitHub REST API base. Leave empty to use GITHUB_API_URL / autodetect.'
    required: false
    default: ''
  github-server-url:
    description: 'Override GitHub server URL (https://github.com or https://github.st.com).'
    required: false
    default: ''
```

In the collect step `env:`:

```yaml
GITHUB_TOKEN: ${{ inputs.github-token }}
COMMON_ACTIONS_PAT: ${{ env.COMMON_ACTIONS_PAT }}
GITHUB_API_URL: ${{ inputs.github-api-url != '' && inputs.github-api-url || env.GITHUB_API_URL }}
GITHUB_SERVER_URL: ${{ inputs.github-server-url != '' && inputs.github-server-url || env.GITHUB_SERVER_URL }}
RCA_GITHUB_API_URL: ${{ inputs.github-api-url }}
RCA_GITHUB_HOST: ${{ inputs.github-server-url }}
```

Consumer on GHES, if autodetection ever fails:

```yaml
with:
  github-token: ${{ secrets.COMMON_ACTIONS_PAT }}
  github-server-url: https://github.st.com
  github-api-url: https://github.st.com/api/v3
```

That is the "change on the fly" knob. Prefer leaving it empty and trusting Actions env.

## 9. What not to do

- Do not implement Phase 2/3/4 now.
- Do not put `api.github.com` in classify/extract/render.
- Do not start with the `issues` history backend.
- Do not run the collector as `if: failure()` inside the failing job.
- Do not ask Grok to "implement the whole spec".
- Do not train Drain3 on failing runs or on non-default branches.
- Do not edit `drain3.ini` after fingerprints exist without `MASKING_CONFIG_VERSION` + `refingerprint --dry-run`.

## 10. Definition of "working workflow"

You are done with the first working cut when all of these are true on github.com:

1. A failing job in the dogfood repo triggers `RCA Collect`.
2. The collect job exits 0.
3. Job summary shows `CI Failure Report` with Verdict + Failed Jobs + at least one error window.
4. Artifact `rca/` contains `summary.md` and `summary.json` with `schema_version: "1.0"` and `kubernetes: null`.
5. `category` / `requires-analysis` / `fingerprint` outputs are set.
6. Switching only `COMMON_ACTIONS_PAT` + `GITHUB_API_URL` lets the same CLI collect a run from `github.st.com`.

Drain3 recurrence, blast-radius short-circuit, and matrix caps can land in the next one or two slices. They are not required to prove the packaging works.
