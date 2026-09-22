# CI RCA Collector — precision + consistent output (updated recommendations)

*Builds on `ci-rca-collector-recommendations.md`. Focus: (1) always emit one "AI Diagnosis" section whatever the source, and (2) get the **precise** root cause while sending **less** to the model — solving both together, not trading one for the other. The Artifactory case in the screenshots is used only to illustrate; every rule below is pattern/config-driven, nothing hardcoded to Artifactory.*

---

## 1. What the three screenshots actually prove

Three views of the same gate failure (release "already exists" → exit 1 at step **Check Version in Artifactory**, per the run's step list):

| Run/view | Path taken | Verdict | Correct? | What it cited |
|---|---|---|---|---|
| Image 1 | AI, focused evidence | "release already exists → bump package.json" | ✅ correct | the failed step's own line (`first_error_window:32`) |
| Image 2 | Deterministic gate (R14) | "CI workflow changed → revert pipeline edit" | ❌ generic miss | curl line + `pipeline_logs` — **not** the failed step |
| Image 3 | AI, full/large log | "Artifactory permission problem" | ❌ wrong symptom | `log_templates:3` (a later permission error) — **not** the failed step |

**The signal that separates right from wrong is not how much log was sent. It is *what was cited*.** Every correct answer quoted the failed step's own error line. Both wrong answers — the deterministic one *and* the "sent everything" AI one — quoted something else (a config-diff guess, a Drain3 template, a downstream permission error).

This reframes the whole problem:

- Sending **more** did not help precision — image 3 sent more and latched onto a *later symptom* (`Not enough permissions to delete/overwrite … test-report-3.1.21.tar`), which is a follow-on, not the gate that actually stopped the job.
- Gating to **deterministic-only** did not help either — R14 ("config changed") is a *low-evidence* verdict that fired confidently and buried the real cause.
- The one thing the correct run did was **anchor on the GitHub-designated failed step** ("Check Version in Artifactory", line 32) and treat everything else as secondary.

So the fix for cost (send less) and the fix for precision (be right) are **the same fix**: anchor on the failed step, quote it, and demote/withhold everything that comes after it.

---

## 2. Root-cause of each miss, in your code

**Image 3 (AI latched onto a symptom).** Your evidence pack (`prompt.py :: build_evidence`) includes `log_templates` (Drain3 T1/T2, up to 5). The permission error had become a high-tier template, so it was packed — and the model treated a *templated aggregate* as the root cause, over `failed_step_excerpt`. Templates are frequency signals, not causal evidence; right now nothing stops the model from citing one as the cause. Your system prompt already says "follow-on permission/403 are symptoms," but it's advice, not enforcement.

**Image 2 (deterministic R14 miss).** `_rule_config_only` / `_rule_r18` produce R14 and set `requires_analysis=False`, and `refine_blast_radius` rewrites the one-liner to the generic "revert the pipeline edit." `user_facing()` is *supposed* to prefer `specific_log_cause()` for R14 — but in that run `specific_log_cause()` returned nothing, because the failed step's line ("This release already exists…") never made it into `error_lines` / `first_error` window. So the window was centered on the wrong place (the curl/manifest check), and R14's blast-radius text won by default. **R14 is a guess that masqueraded as "deterministic_sufficient."**

**Image 1 (why it worked).** The failed-step line landed in the first-error window, `specific_log_cause()` / the model saw it, and it was cited. Nothing more sophisticated than that.

---

## 3. The core principle: anchor on the failed step, everything after is a symptom

GitHub already tells you which step failed (`failed_step_name` / `failed_step_number`, the red-X step). Make that the spine of both collection and diagnosis:

1. **Center the excerpt on the authoritative failed step, not on a whole-log regex sweep.** You already have `_pick_failed_group(step_name)` in `extract.py`; make `failed_step_name` reliably populated from the GitHub job step list and *always* prefer the group match over `_first_error_index` scanning across the whole log. The excerpt for "Check Version in Artifactory" must contain its lines 31-33.

2. **Guarantee the failed step's specific-cause line is captured.** When the failed-step group contains a `_SEMANTIC_CAUSE` line ("already exists", "must update", "ERESOLVE", "No matching distribution", "error TS…", "FAILED …", quality/coverage-gate messages), pin it as `primary_failure_line` on the job and put it **first** in evidence — ahead of the deterministic hint, ahead of templates, ahead of pipeline_logs.

3. **Symptom demotion (enforce, don't just suggest).** Define a small, config-driven notion of a *terminal cause*: a line in the failed step that by itself explains the exit (version/tag already exists, gate/threshold failed, compile error, dependency-not-found, assertion). When a terminal cause is present in the failed step, automatically **exclude or hard-demote** later-in-time evidence — pipeline_logs windows, permission/403/network lines, `log_templates` — from both the deterministic answer and the AI evidence pack. This is exactly the difference between image 1 and image 3, made mechanical. Keep it as a rule table (`TERMINAL_CAUSE_PATTERNS`), never an if-Artifactory.

4. **Citation-grounding gate — the single highest-value check.** Require that the final root cause quote at least one line from `failed_step_excerpt` or `first_error_window`. You already verify citations are verbatim substrings of evidence; add the converse rule:

   > If no citation comes from the failed step's own excerpt/first-error window, the answer is not grounded in the failure. Downgrade confidence, and — if the model was used — re-ask once with `log_templates` + `pipeline_logs` removed from the evidence.

   This one rule would have caught **both** wrong screenshots: image 2 (cited pipeline_logs/curl only) and image 3 (cited log_templates only) both fail it; image 1 passes it. It turns "the model wandered" and "the deterministic rule guessed" into a detectable, recoverable state.

---

## 4. Revised conditional-AI decision (fixes the gating that produced image 2)

My earlier note said "skip the model when `requires_analysis=False` and confidence is high." Image 2 shows the danger: **some deterministic verdicts are confidently wrong.** Refine the gate so it keys on *evidence quality at the failed step*, not on a rule's self-reported sufficiency.

**Treat a verdict as truly sufficient (skip the model) only when it is anchored:**

- A **signature** category (`dependency`, `compile`, `oom`, `timeout`, `image_pull`, `auth`, `test_failure`, `disk_space`) fired **high-confidence** *and* a terminal-cause line from the failed step is present and cited. → deterministic answer is trustworthy, skip AI. *(This is image 1's answer, reachable with no model call at all.)*
- A hard short-circuit: `infra_runner`, `infra_widespread`, `flake_same_sha_passed`, `no_failed_jobs`. → skip AI.

**Do NOT treat as sufficient (these must NOT suppress the model):**

- **R14 (config/blast-radius) and R18 (residual) without a cited failed-step cause.** These are low-evidence guesses. If there's no terminal cause at the failed step, either call the model (with the focused pack) or, if the model is off, render the answer but mark **Confidence: low** and say "needs review" — never present a config-diff guess as a confident root cause. Concretely: **stop letting R14 set `requires_analysis=False` unless `specific_log_cause()` is non-empty.**

**When you do call the model, send the focused pack, not the firehose:**

- Order: `primary_failure_line` → `failed_step_excerpt` (untrimmed) → `first_error_window` → `stack_traces`/`junit` (category-appropriate) → deterministic hint. **Withhold `log_templates` and `pipeline_logs` when a terminal cause exists at the failed step.** Only add pipeline_logs when the job log is genuinely uninformative (you already detect this: `_job_logs_are_exit_only` + `_pipeline_has_first_error`).
- One persona by default; fall back to the second only on a failed citation-grounding check or bad JSON.

Net effect on your two goals:
- **Precision:** image-1-style answers become the *norm*, because the failed step is pinned and symptoms are withheld; the grounding gate rejects image-2/image-3-style drift.
- **Cost:** most anchored signature failures resolve deterministically with **no** model call; the calls that remain carry a small, focused pack instead of the firehose.

They're the same change, which is why you can "solve both at the same time."

---

## 5. Always emit one "AI Diagnosis" section — whatever the source

You want the attached layout every time, whether the answer came from the model or from code. You are close (`analyze.py :: finalize_user_card` + `_diagnosis_markdown` already fill deterministic fields on skip). Lock it in as a contract:

**The section always renders, always with the same fields, never blank:**

```
## AI Diagnosis
Status:        ok | cached | deterministic | needs-review        # never "skipped/failed" as a dead end
Source:        ai | deterministic | hybrid                        # hybrid = model text kept but grounded on det. cause
Model called:  yes | no
Confidence:    high | medium | low
Root cause:    <always populated>
Suggested fix: <always populated>
Citations:     <>=1, and >=1 from the failed step's excerpt/first_error_window>
Notes:         <reason code, persona, cache, grounding result>
```

Guarantees to add:

- **Never empty.** If the model is skipped, off, errored, gated, or fails the grounding check, fall back to the deterministic card — which you already build — so `Root cause` / `Suggested fix` are always filled. (Present today via `finalize_user_card`; make it an invariant with a test: "for any status, root_cause and suggested_fix are non-empty.")
- **Rename Status "skipped" → a positive state.** "skipped/Model called: no" reads like a gap to users (image 2). Show `Status: deterministic` or `Status: needs-review` instead, so the section always looks like a real diagnosis, not an omission. Keep `Model called: no` and `Source: deterministic` for transparency.
- **Source: hybrid.** When the model produced usable prose but you re-anchored it on the failed step (grounding gate), label it `hybrid` — honest, and it's the state that gives image-1 quality even when the model drifted.
- **Confidence reflects grounding, not the model's self-report.** Image 3 said "high" while being wrong. Cap confidence at `medium` whenever the answer isn't cited to the failed step; reserve `high` for anchored + cited answers.

---

## 6. Suggested order of work

1. **Failed-step anchoring (collection).** Ensure `failed_step_name` is populated from the job step list and the excerpt is always centered on that group; pin `primary_failure_line`. *Root fix for image 2's missing cause line.*
2. **Citation-grounding gate (analyze + deterministic).** Require ≥1 citation from the failed step; on failure, downgrade confidence and, for the model path, re-ask once with templates/pipeline_logs removed. *Catches both wrong screenshots.*
3. **Terminal-cause table + symptom demotion.** Config-driven `TERMINAL_CAUSE_PATTERNS`; when present at the failed step, withhold later symptoms/templates/pipeline_logs from evidence and from the deterministic one-liner. *Prevents image 3.*
4. **Refine the AI gate.** R14/R18 without a cited failed-step cause no longer count as "sufficient"; anchored high-confidence signatures skip the model. *Prevents image 2 from silently shipping, and cuts model calls on the clear cases.*
5. **Diagnosis-section invariant.** Always render the section, never blank, `skipped`→`deterministic`/`needs-review`, add `hybrid`, confidence capped by grounding. *Your output-format requirement.*
6. **Emit a grounding/decision output** (`diagnosis-grounded: true|false`, `analyze-decision`) so you can measure how often answers are anchored and tune the tables.

Items 1-2 alone would have flipped both wrong screenshots to correct. Items 3-4 are what let you send far less to the model without losing the image-1 result. Item 5 is your formatting guarantee. None of it touches the source-agnostic modules your AGENTS.md protects, and none of it hardcodes the Artifactory case — the patterns live in config tables you extend as new terminal-cause shapes appear.
