# Claude Code task — Step 14b: sharper headlines, cleaner evidence, Maven file locations

## Context

`ci-rca-collector`. Read `AGENTS.md` and `docs/rca-delivery-spec-v1.3.md` (§7, §7.1, §7.2) first. Steps 12–14 are merged. A dry-run of every golden through `deliver --dry-run --offline` showed three **general** quality problems in what Phase 3 would post. Fix them before Step 15 starts posting.

Do **only** what this document scopes. Nothing is hardcoded to a project, file, or message.

## Findings (reproduced on the committed goldens)

**F1 — Maven/javac file locations are not parsed.** `extract.extract_source_paths("[ERROR] /work/src/test/TestConfig.java:[19,40] cannot find symbol")` returns `[]`. `_SOURCE_PATHS` handles `File "x", line N`, `path(12,3):`, `path:12[:3]` and `(path:12)`, but not the Maven form `path:[line,col]`. It also misses Kotlin/Gradle's `file:///path/Foo.kt:12:5` form. Java is a primary stack for this project, so for every Maven compile failure: `suspected_files` is empty, no code context is fetched, and CODEOWNERS routing (Step 17) will have nothing to route on.

**F2 — The exit-code line leads the evidence.** For `docker-already-exists-benign` the evidence block starts with `##[error]Process completed with exit code 1.`, ahead of the actual `AssertionError`. Exit-code lines never explain anything. `extract._primary_failure_line` already skips them, but `render_comment`'s evidence builder only skips benign lines.

**F3 — Generic headlines when a specific cause line exists.** `diagnose.user_facing()` falls back to a bare template when its category branch finds no specific detail. Examples: "Compilation failed." (no suspected file), "A test assertion failed." (no JUnit/pytest test name), "Package install failed: a package was not found …" (no package name). This happens even when `primary_failure_line` holds the precise cause, such as `AssertionError: response body mismatch`. The headline is the most valuable line of the comment.

## Goal

1. **F1, in `extract.py`** (source-agnostic, keep it that way):
   - Add a `_SOURCE_PATHS` form for `path:[line,col]` (Maven/javac), capturing path and line.
   - Tolerate a `file://` prefix before a path (Kotlin/Gradle `e: file:///…/Foo.kt:12:5`), capturing the path without the scheme.
   - Existing forms and their results must be unchanged.
2. **F2, evidence:** add a small public helper `extract.is_exit_code_line(line) -> bool`, reusing the existing exit-code regex. It covers `Process completed with exit code N` with or without `##[error]` / `Error:` prefixes. Use it in `render_comment`'s evidence builder so such lines are never included. It is the same rule `_primary_failure_line` already applies.
3. **F3, `diagnose.user_facing()`:** when a category branch has **no** specific detail, append a cleaned cause line: `"<generic sentence> — <cause line>"`. The branches are compile without a suspected file, test_failure without a test name, and dependency without a package name.
   - The cause line is `primary_failure_line`, only if it is present, not benign, not an exit-code line, and not already contained in the sentence.
   - Clean it for headline use with a small configurable prefix-strip table in `config.py`, for example `##[error]`, `[ERROR]`, `ERROR:`, `Error:`, and pytest's leading `E   `.
   - Cap the headline at about 300 characters.
   - Never include a rule id, never use a benign line.
   - Branches that already have a specific detail are unchanged. The `release` branch (R19) is unchanged, since its root cause *is* the line.

## Constraints

- Categories, confidence, `requires_analysis`, short-circuits and gate decisions must not change for any golden; this is text quality only. `run_eval --no-llm` must stay at 1.0.
- `extract.py` stays GitHub-agnostic. No new dependencies.
- If a Step 14 render test pins an old generic headline string, update it with a one-line justification.

## Acceptance criteria

1. `extract_source_paths` returns `('/work/src/test/TestConfig.java', 19)` for the Maven line and `('/…/Foo.kt', 12)` for a `file://` Kotlin line. All existing extract tests stay green.
2. `java-compile-in-pipeline` golden: `diagnose` now carries the Java file in `suspected_files`, and the headline names it. The category is still `compile`.
3. `docker-already-exists-benign` golden: the evidence block does not contain an exit-code line, and its first line is the `AssertionError` line. The headline becomes `A test assertion failed — AssertionError: response body mismatch` or equivalent cleaned text.
4. `artifactory-version-exists` headline is byte-identical to before.
5. No rendered golden contains `\bR\d+\b` outside the marker, and all stay ≤ 4000 characters.
6. `pytest tests/rca -q` and `pytest tests/eval -q` are green, the acceptance-35 grep is empty, and `run_eval --no-llm` category accuracy is 1.0.

## Out of scope

Posting anything (Step 15). New categories. Changes to the STGPT prompt or to model-sourced headlines.

## Deliverable

A focused diff over `extract.py`, `diagnose.py`, `config.py` and `deliver/render_comment.py`, plus tests. Show the before/after comment body for `java-compile-in-pipeline` and `docker-already-exists-benign`.
