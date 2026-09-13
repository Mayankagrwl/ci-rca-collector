"""Stages 1, 3 and 5 — runner health, same-SHA flake, regex pre-classification."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from .config import CLASSIFY_RULES, RUNNER_FAILURE_PATTERNS

_INFRA_CATEGORIES = frozenset(
    {
        "infra_runner",
        "oom",
        "timeout",
        "disk_space",
        "network_dns",
        "auth",
        "image_pull",
    }
)
_CODE_CATEGORIES = frozenset({"dependency", "compile", "test_failure", "crash"})

_RUNNER_RES = [re.compile(p, re.IGNORECASE) for p in RUNNER_FAILURE_PATTERNS]
_RULE_RES = [
    (rule["category"], re.compile(rule["pattern"]), rule["confidence"])
    for rule in CLASSIFY_RULES
]


@dataclass
class ClassificationHit:
    category: str
    confidence: str
    matched_pattern: str | None = None
    matched_line: int | None = None
    other_matches: list[str] = field(default_factory=list)
    is_infra_vs_code: str = "unknown"
    is_flaky: bool = False
    short_circuit: str | None = None
    reason: str | None = None
    requires_analysis: bool = True


def _side(category: str) -> str:
    if category in _INFRA_CATEGORIES:
        return "infra"
    if category in _CODE_CATEGORIES:
        return "code"
    return "unknown"


def check_runner_health(
    raw_log: str,
    *,
    conclusion: str | None,
    failed_step_name: str | None,
    log_lines: int,
) -> ClassificationHit | None:
    """Stage 1. Returns a hit when the job looks like runner infrastructure."""
    for compiled in _RUNNER_RES:
        match = compiled.search(raw_log)
        if match:
            return ClassificationHit(
                category="infra_runner",
                confidence="high",
                matched_pattern=compiled.pattern,
                is_infra_vs_code="infra",
                short_circuit="infra_runner",
                reason=f"runner health matched {match.group(0)!r}",
                requires_analysis=False,
            )
    if conclusion == "failure" and log_lines < 50 and not failed_step_name:
        return ClassificationHit(
            category="infra_runner",
            confidence="medium",
            matched_pattern="short_log_no_failed_step",
            is_infra_vs_code="infra",
            short_circuit="infra_runner",
            reason="failure with <50 log lines and no failed step",
            requires_analysis=False,
        )
    return None


def classify_lines(lines: Sequence[str]) -> ClassificationHit:
    """Stage 5. First rule match wins; every matching category is recorded."""
    winner: ClassificationHit | None = None
    others: list[str] = []
    seen_categories: set[str] = set()
    for index, line in enumerate(lines, start=1):
        for category, compiled, confidence in _RULE_RES:
            if not compiled.search(line):
                continue
            if winner is None:
                winner = ClassificationHit(
                    category=category,
                    confidence=confidence,
                    matched_pattern=compiled.pattern,
                    matched_line=index,
                    is_infra_vs_code=_side(category),
                )
                seen_categories.add(category)
            elif category not in seen_categories:
                others.append(category)
                seen_categories.add(category)
            break
    if winner is None:
        return ClassificationHit(
            category="unknown",
            confidence="low",
            is_infra_vs_code="unknown",
        )
    winner.other_matches = others
    return winner


def check_same_sha_flake(
    prior_runs: Sequence[Mapping[str, Any]],
    workflow_name: str,
) -> ClassificationHit | None:
    """Stage 3. `prior_runs` must already exclude the current run."""
    if not workflow_name:
        return None
    for run in prior_runs:
        if run.get("conclusion") != "success":
            continue
        if _workflow_name(run) == workflow_name:
            return ClassificationHit(
                category="unknown",
                confidence="high",
                is_infra_vs_code="unknown",
                is_flaky=True,
                short_circuit="flake_same_sha_passed",
                reason="same SHA previously succeeded for this workflow",
                requires_analysis=False,
            )
    return None


def _workflow_name(run: Mapping[str, Any]) -> str:
    name = run.get("name")
    if name:
        return str(name)
    workflow = run.get("workflow")
    if isinstance(workflow, Mapping) and workflow.get("name"):
        return str(workflow["name"])
    return ""


def classify_failure(
    *,
    raw_logs: Sequence[str],
    cleaned_lines: Sequence[Sequence[str]],
    conclusions: Sequence[str | None],
    failed_step_names: Sequence[str | None],
    log_line_counts: Sequence[int],
    workflow_name: str = "",
    run_attempt: int = 1,
    prior_same_sha_runs: Sequence[Mapping[str, Any]] | None = None,
    no_failed_jobs: bool = False,
) -> ClassificationHit:
    """Compose Stages 1, 3 and 5. Short-circuits win and skip Stage 5."""
    if no_failed_jobs:
        return ClassificationHit(
            category="unknown",
            confidence="low",
            short_circuit="no_failed_jobs",
            reason="no failed jobs",
            requires_analysis=False,
        )

    for raw, conclusion, step, count in zip(
        raw_logs, conclusions, failed_step_names, log_line_counts
    ):
        infra = check_runner_health(
            raw,
            conclusion=conclusion,
            failed_step_name=step,
            log_lines=count,
        )
        if infra is not None:
            return infra

    flake = check_same_sha_flake(prior_same_sha_runs or [], workflow_name)
    notes: list[str] = []
    if run_attempt > 1:
        notes.append(f"run_attempt={run_attempt}")

    primary_lines: Sequence[str] = cleaned_lines[0] if cleaned_lines else []
    regex = classify_lines(primary_lines)

    if flake is not None:
        regex.is_flaky = True
        regex.short_circuit = "flake_same_sha_passed"
        regex.requires_analysis = False
        regex.reason = flake.reason
        return regex

    if notes and regex.reason is None:
        regex.reason = "; ".join(notes)
    return regex
