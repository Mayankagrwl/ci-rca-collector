# CI RCA Collector — recommendations

*Single, current reference. Supersedes the earlier two drafts. Two goals, solved together: (1) identify the failure **precisely**, and (2) send **less** to the AI model — without losing quality — while (3) always emitting one consistent "AI Diagnosis" section whatever the source. Every rule below is pattern/config-driven; nothing is hardcoded to any one failure (the Artifactory case is only an illustration).*

---

## 1. The problem, from three real runs of one gate failure

Three views of the same failure — release "already exists" → exit 1 at step **Check Version in Artifactory**:

| View | Path taken | Verdict | Correct? | What it cited |
|---|---|---|---|---|
| A | AI, focused evidence | "release already exists → bump package.json" | ✅ | the **failed step's own line** (`first_error_window:32`) |
| B | Deterministic gate (R14) | "CI workflow changed → revert pipeline edit" | ❌ generic | curl line + `pipeline_logs` — not the failed step |
| C | AI, full/large log | "Artifactory permission problem" | ❌ symptom | `log_templates:3` (a later permission error) — not the failed step |

**The signal that separates right from wrong is *what got cited*, not how much log was sent.** The correct run quoted the failed step's own error line. Both wrong runs — the deterministic one *and* the "sent everything" AI one — quoted something else (a config-diff guess, a Drain3 template, a downstream permission error).

Consequences that reframe the whole effort:

- Sending **more did not help** — run C sent more and latched onto a *later symptom* (`Not enough permissions to delete/overwrite … test-report-3.1.21.tar`), which is a follow-on, not the gate that stopped the job.
- Gating to **deterministic-only did not help either** — R14 ("config changed") is a *low-evidence* verdict that fired confidently and buried the real cause.
- The correct run did exactly one thing differently: it **anchored on the GitHub-designated failed step** and treated everything else as secondary.

So the fix for cost (send less) and the fix for precision (be right) are **the same fix**: anchor on the failed step, quote it, and withhold everything that comes after it. That is why both problems can be solved at once.

---

## 2. Why each miss happened, in your code

**Run C — AI chased a symptom.** `prompt.py :: build_evidence()` packs `log_templates` (Drain3 T1/T2, ≤5). The permission error had become a high-tier template, so it was packed, and the model treated a *templated aggregate* as the cause, over `failed_step_excerpt`. Your system prompt says "follow-on 403/permission are symptoms," but that's advice, not enforcement — nothing stops the model citing a template as the root cause.

**Run B — deterministic R14 guessed.** `_rule_config_only` / `_rule_r18` produce R14, set `requires_analysis=False`, and `refine_blast_radius` rewrites the one-liner to the generic "revert the pipeline edit." `user_facing()` is *supposed* to prefer `specific_log_cause()` for R14 — but it returned nothing, because the failed step's line ("This release already exists…") never reached `error_lines` / the `first_error` window. The window was centered on the wrong place (the curl/manifest check), so R14's blast-radius text won by default. **R14 is a guess that masqueraded as "deterministic_sufficient."**

**Run A — it worked** only because the failed-step line landed in the first-error window and got cited. Nothing more sophisticated than that.

Separately, an **evidence mismatch** compounds this: the system prompt tells the model it may cite `stack_traces`, `junit`, `code_context`, `history`, `last_green_compare` — but `build_evidence()` never packs those. Since citations must be verbatim substrings of the evidence, any citation to those sources is auto-rejected, pushing results toward `cannot_determine` and starving the model of exactly the high-signal evidence (stack frame, failing test, source hunk) that would resolve ambiguity. You collect it and then drop it before the call.

---

## 3. Core principle — anchor on the failed step; everything after is a symptom

GitHub already names the failed step (`failed_step_name` / `failed_step_number`, the red-X step). Make it the spine of both collection and diagnosis.

1. **Center the excerpt on the authoritative failed step, not a whole-log regex sweep.** You have `_pick_failed_group(step_name)` in `extract.py`; ensure `failed_step_name` is reliably populated from the job step list and *always* prefer the group match over `_first_error_index` scanning the whole log. The excerpt for "Check Version in Artifactory" must contain its lines 31-33.

2. **Guarantee the specific-cause line is captured and comes first.** When the failed-step group contains a `_SEMANTIC_CAUSE` line ("already exists", "must update", "ERESOLVE", "No matching distribution", "error TS…", "FAILED …", quality/coverage-gate messages), pin it as `primary_failure_line` and put it **first** in evidence — ahead of the deterministic hint, templates, and pipeline_logs.

3. **Symptom demotion — enforce it, don't just prompt it.** Define a config-driven `TERMINAL_CAUSE_PATTERNS`: a line in the failed step that by itself explains the exit (version/tag already exists, gate/threshold failed, compile error, dependency-not-found, assertion). When a terminal cause is present, **exclude or hard-demote** later-in-time evidence — `pipeline_logs` windows, permission/403/network lines, `log_templates` — from both the deterministic one-liner and the AI evidence pack. This is the difference between run A and run C, made mechanical. It is a rule table you extend, never an if-Artifactory.

4. **Citation-grounding gate — the single highest-value check.** You already verify citations are verbatim substrings of evidence; add the converse:

   > The final root cause must quote at least one line from `failed_step_excerpt` or `first_error_window`. If none does, the answer is not grounded in the failure: downgrade confidence, and — if the model was used — re-ask **once** with `log_templates` + `pipeline_logs` removed.

   This one rule catches **both** wrong runs: B (cited pipeline_logs/curl only) and C (cited a template only) both fail it; A passes. It turns "the model wandered" and "the deterministic rule guessed" into a detectable, recoverable state.

---

## 4. When to call the model (send less, safely)

Gate on **evidence quality at the failed step**, not on a rule's self-reported sufficiency — because Run B proves some deterministic verdicts are confidently wrong. Make the policy a workflow input (`analyze-policy: auto | always | never`) so consumers can dial cost vs depth; default `auto`, keep `always` for A/B comparison.

**Skip the model — the deterministic answer is trustworthy — only when anchored:**

- A **signature** category (`dependency`, `compile`, `oom`, `timeout`, `image_pull`, `auth`, `test_failure`, `disk_space`) fired **high-confidence** *and* a terminal-cause line from the failed step is present and cited. *(This is Run A's answer, reachable with no model call at all.)*
- A hard short-circuit: `infra_runner`, `infra_widespread`, `flake_same_sha_passed`, `no_failed_jobs`.
- History reuse: `history.match == "exact"` with a stored `previous_resolution` (R15).

**Do NOT let these suppress the model:**

- **R14 (config/blast-radius) and R18 (residual) without a cited failed-step cause.** These are low-evidence guesses. Stop letting R14 set `requires_analysis=False` unless `specific_log_cause()` is non-empty. If there's no terminal cause at the failed step, either call the model or, when the model is off, render the answer but mark **Confidence: low / needs-review** — never present a config-diff guess as a confident root cause.

**When you do call, send a focused pack — not the firehose:**

- Order: `primary_failure_line` → `failed_step_excerpt` (untrimmed) → `first_error_window` → `stack_traces` / `junit` / `code_context` (category-appropriate — the sources the prompt already promises but doesn't pack) → deterministic hint.
- **Withhold `log_templates` and `pipeline_logs` when a terminal cause exists at the failed step.** Add pipeline_logs only when the job log is genuinely uninformative (you already detect this: `_job_logs_are_exit_only` + `_pipeline_has_first_error`).
- Make the pack **category-aware**: compile/crash/test → stack traces + junit + code hunks first; dependency → resolver line + lockfile delta; infra/oom/timeout → you shouldn't be calling the model at all.
- **One persona by default**; fall back to the second only on a failed grounding check or bad JSON (halves call cost on the ambiguous set).

Net effect: anchored signature failures resolve deterministically with **no** model call; the calls that remain carry a small, focused pack. Precision goes up because symptoms are withheld and the grounding gate rejects drift; cost goes down because you both call less and send less. Same change, both wins.

---

## 5. Always emit one "AI Diagnosis" section — whatever the source

You want the attached layout every time, model or not. `analyze.py :: finalize_user_card` + `_diagnosis_markdown` already fill deterministic fields on skip — lock it in as a contract:

```
## AI Diagnosis
Status:        ok | cached | deterministic | needs-review     # never a dead-end "skipped/failed"
Source:        ai | deterministic | hybrid                     # hybrid = model prose re-anchored on the failed step
Model called:  yes | no
Confidence:    high | medium | low
Root cause:    <always populated>
Suggested fix: <always populated>
Citations:     >=1, and >=1 from the failed step's excerpt / first_error_window
Notes:         reason code, persona, cache, grounding result
```

Guarantees to add:

- **Never empty.** On skip / off / error / gated / failed-grounding, fall back to the deterministic card so `Root cause` and `Suggested fix` are always filled. Make it an invariant with a test: *for any status, both fields are non-empty.*
- **Rename `Status: skipped` → a positive state** (`deterministic` or `needs-review`). "skipped / Model called: no" reads like a gap (Run B); keep `Model called: no` and `Source: deterministic` for transparency, but the section should always look like a real diagnosis.
- **`Source: hybrid`** when the model produced usable prose but you re-anchored it on the failed step via the grounding gate — that's the state that gives Run-A quality even when the model drifted.
- **Confidence reflects grounding, not the model's self-report.** Run C said "high" while wrong. Cap confidence at `medium` whenever the answer isn't cited to the failed step; reserve `high` for anchored + cited answers.

---

## 6. Collection-side improvements (raise the ceiling for both paths)

- **Error-centered trimming.** `budget.trim_middle` keeps head+tail and elides the middle, which can cut the causal line out of a long first-error window. Keep N lines around `_first_error_index` for `first_error` / `merged` windows instead of pure head/tail.
- **Gate the pipeline-artifact *download* on the verdict.** `analyze-pipeline-logs` fetches up to 3 zips before you know whether you'll even use them. If the verdict is anchored/terminal (Tier "skip"), skip the download — a real latency/bandwidth win on the common path, and it removes the very evidence that misled Run C.
- **JUnit 5-cap collapse.** When 5+ tests fail with a shared message (one root cause, many assertions), collapse by message so the cap doesn't drop the signal.
- **Stack-frame relevance.** When packing stack traces, prioritize frames whose paths intersect `changes.files` (you already compute `application_stack_in_diff`).
- **Redaction backstop.** The allow-list regex runs before write (good — the bridge only sees redacted text), but a bare high-entropy token with no keyword can pass. Add an entropy-based scrubber for long unbroken tokens, since evidence leaves your infra to an external bridge.
- **Token estimate.** `token_count = len//4` is a coarse guardrail; if the bridge exposes a tokenizer, use a closer estimate for the analyze cap so you neither overflow nor over-trim.

---

## 7. Measurement (so you can tune, not guess)

- Emit `diagnosis-grounded: true|false` and `analyze-decision: called | skipped:<reason> | cached`, plus `evidence-tokens`, as action outputs.
- Persist `(category, rule_id, terminal_cause_present, model_called, grounded, status, confidence)` per run to the history backend. After a few weeks this tells you which rules are safe to skip the model on and which categories the model actually improves — turning the tables in §3/§4 from a guess into data.

---

## 8. Order of work (biggest impact first)

1. **Failed-step anchoring (collection).** Populate `failed_step_name` from the job step list; always center the excerpt on that group; pin `primary_failure_line`. *Root fix for Run B's missing cause line.*
2. **Citation-grounding gate.** Require ≥1 citation from the failed step; on failure, downgrade confidence and re-ask once with templates/pipeline_logs removed. *Catches both wrong runs.*
3. **Terminal-cause table + symptom demotion.** Config-driven `TERMINAL_CAUSE_PATTERNS`; withhold later symptoms/templates/pipeline_logs when a terminal cause is at the failed step. *Prevents Run C.*
4. **Refine the AI gate.** R14/R18 without a cited failed-step cause no longer count as sufficient; anchored high-confidence signatures skip the model; focused, category-aware pack when calling. *Prevents Run B shipping silently, and cuts model calls + payload.*
5. **Diagnosis-section invariant.** Always render, never blank; `skipped`→`deterministic`/`needs-review`; add `hybrid`; confidence capped by grounding. *Your output-format requirement.*
6. **Collection improvements + measurement** (§6, §7).

Steps 1-2 alone would have flipped both wrong screenshots to correct. Steps 3-4 are what let you send far less to the model without losing the Run-A result. Step 5 is the formatting guarantee. None of it touches the source-agnostic modules your AGENTS.md protects, and none of it hardcodes any specific failure — the patterns live in config tables you extend as new terminal-cause shapes appear.
