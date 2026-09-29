"""Pydantic schema for rca/summary.json. Single source of truth (§11)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, model_validator


# --- types named by the spec but not fully expanded ---


class StackTrace(BaseModel):
    """Detected stack, truncated to top 10 frames + bottom 5 (Stage 6)."""

    headline: str | None = None
    content: str
    frame_count: int = 0
    elided_count: int = 0


class ErrorLine(BaseModel):
    """Grep hit feeding Drain3 tiering; not rendered standalone (Stage 6)."""

    line_number: int
    text: str


class JUnitFailure(BaseModel):
    classname: str
    name: str
    message: str | None = None
    body: str | None = None  # first 20 lines of the failure body
    count: int = 1  # collapsed near-duplicate failures sharing this message


class JUnitReport(BaseModel):
    total_failures: int
    total_tests: int | None = None
    failures: list[JUnitFailure] = []  # capped at 5
    source_artifact: str | None = None


class BudgetReport(BaseModel):
    total_cap_tokens: int = 6000
    total_used_tokens: int = 0
    section_used: dict[str, int] = {}
    trimmed: list[str] = []
    t1_overrun: bool = False
    t1_raised_to: int | None = None


# --- §10.4 ---


class FailureRecord(BaseModel):
    fingerprint: str  # fine
    fingerprint_coarse: str
    first_seen: datetime
    last_seen: datetime
    count: int = 1
    branches: list[str] = []  # deduped
    run_ids: list[int] = []  # capped at the 20 most recent
    category: str  # from Stage 5
    templates: list[str] = []
    template_hashes: list[str] = []
    masking_config_hash: str
    last_summary: str | None = None
    resolution: str | None = None
    resolution_author: str | None = None
    resolution_run_id: int | None = None
    human_verified: bool = False
    schema_version: Literal["1.0"] = "1.0"

    def absorb(self, other: FailureRecord) -> FailureRecord:
        """Idempotent merge: same occurrence identity does not bump count."""
        incoming = list(other.run_ids)
        if incoming and any(item in self.run_ids for item in incoming):
            return self
        merged_ids = list(self.run_ids)
        for item in incoming:
            if item not in merged_ids:
                merged_ids.append(item)
        merged_ids = merged_ids[-20:]
        branches: list[str] = []
        for name in [*self.branches, *other.branches]:
            if name and name not in branches:
                branches.append(name)
        later = other.last_seen if other.last_seen > self.last_seen else self.last_seen
        earlier = other.first_seen if other.first_seen < self.first_seen else self.first_seen
        return self.model_copy(
            update={
                "last_seen": later,
                "first_seen": earlier,
                "count": self.count + 1,
                "branches": branches,
                "run_ids": merged_ids,
                "category": other.category or self.category,
                "templates": other.templates or self.templates,
                "template_hashes": other.template_hashes or self.template_hashes,
                "masking_config_hash": other.masking_config_hash or self.masking_config_hash,
                "last_summary": other.last_summary or self.last_summary,
                "resolution": self.resolution or other.resolution,
                "resolution_author": self.resolution_author or other.resolution_author,
                "resolution_run_id": self.resolution_run_id or other.resolution_run_id,
                "human_verified": self.human_verified or other.human_verified,
            }
        )


# --- §11 ---


class RunMeta(BaseModel):
    run_id: int
    run_attempt: int
    workflow_name: str
    html_url: str
    event: str  # push | pull_request | schedule | ...
    actor: str
    head_sha: str
    head_branch: str
    pr_number: int | None = None
    pr_url: str | None = None
    is_fork: bool = False
    head_repo: str | None = None
    concurrent_failures: int = 0
    concurrent_workflows: list[str] = []
    concurrent_branches: list[str] = []
    failed_job_total: int
    failed_jobs_analysed: int
    matrix_legs_identical: bool = False


class Verdict(BaseModel):
    short_circuit: (
        Literal["infra_runner", "infra_widespread", "flake_same_sha_passed", "no_failed_jobs"]
        | None
    ) = None
    requires_analysis: bool = True
    reason: str | None = None


class Classification(BaseModel):
    category: str
    confidence: Literal["high", "medium", "low"]
    matched_pattern: str | None
    matched_line: int | None
    other_matches: list[str] = []
    is_infra_vs_code: Literal["infra", "code", "unknown"]
    is_flaky: bool = False
    matched_stream: str | None = None


class StepInfo(BaseModel):
    number: int
    name: str
    conclusion: str  # success | failure | skipped | cancelled
    duration_seconds: int | None = None
    duration_ratio_vs_last_green: float | None = None
    suspected_cache_miss: bool = False


class ArtifactInfo(BaseModel):
    name: str
    size_bytes: int
    expired: bool = False
    parsed: bool = False
    kind: Literal["pipeline_logs", "junit", "other"] = "other"
    stage: str | None = None
    skipped_reason: str | None = None


class RunnerInfo(BaseModel):
    name: str | None = None
    group: str | None = None
    labels: list[str] = []
    image: str | None = None
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
    position: int
    mask: str
    kind: Literal["string_low", "string_high", "numeric", "sequence"]
    values: list[str] = []
    distinct_count: int | None = None
    minimum: float | None = None
    maximum: float | None = None
    median: float | None = None
    baseline_median: float | None = None
    sequence_rle: str | None = None


class LogTemplate(BaseModel):
    template_id: int
    template: str
    count: int
    baseline_count: int | None = None
    is_novel: bool | None = None
    first_line: int
    has_error_match: bool = False
    tier: Literal["T1", "T2", "T3", "T4", "T5"]
    variables: list[TemplateVariable] = []
    representative_line: str | None = None
    anomaly: Literal["missing", "depleted", "flooding"] | None = None
    anomaly_ratio: float | None = None


class DrainReport(BaseModel):
    baseline_available: bool
    baseline_run_id: int | None = None
    baseline_age_days: int | None = None
    baseline_template_count: int | None = None
    total_clusters: int
    tier_counts: dict[str, int]
    templates: list[LogTemplate]
    omitted_count: int = 0
    omitted_criteria: str | None = None
    fingerprint_degraded: bool = False
    masking_config_hash: str | None = None

    @model_validator(mode="after")
    def _baseline_tri_state(self) -> DrainReport:
        if not self.baseline_available:
            for template in self.templates:
                if template.is_novel is not None or template.baseline_count is not None:
                    raise ValueError(
                        "is_novel and baseline_count must be None when "
                        "baseline_available is False"
                    )
        return self


class FailedStepExcerpt(BaseModel):
    """Last lines of the first failed step. Never middle-trimmed for STGPT."""

    name: str
    lines: list[str] = []


class FailedJob(BaseModel):
    job_id: int
    name: str
    failed_step_name: str | None
    failed_step_number: int | None
    exit_code: int | None
    duration_seconds: int | None
    log_unavailable: bool = False
    queue_seconds: int | None = None
    runner: RunnerInfo | None = None
    steps: list[StepInfo] = []
    log_lines_raw: int = 0
    log_lines_clean: int = 0
    log_bytes: int = 0
    windows: list[LogWindow] = []
    stack_traces: list[StackTrace] = []
    error_lines: list[ErrorLine] = []
    annotations: list[str] = []
    timeout_minutes: int | None = None
    failed_step_excerpt: FailedStepExcerpt | None = None
    primary_failure_line: str | None = None


class CommitInfo(BaseModel):
    sha: str
    subject: str
    author: str | None = None
    authored_at: datetime
    files_changed: int
    files: list[str] = []
    is_revert: bool = False
    is_merge: bool = False


class ChangeContext(BaseModel):
    base_sha: str | None = None
    head_sha: str
    range_basis: Literal["last_success", "merge_base", "head_only"] = "head_only"
    range_truncated: bool = False
    total_commits: int = 0
    commits: list[CommitInfo] = []
    files_changed: int = 0
    additions: int = 0
    deletions: int = 0
    diffstat: str | None = None
    classes: list[str] = []
    files: list[str] = []
    lockfile_deltas: dict[str, list[str]] = {}
    stack_trace_diffs: dict[str, str] = {}
    pr_title: str | None = None
    pr_labels: list[str] = []
    pr_is_draft: bool = False
    pr_body_excerpt: str | None = None


class HistoryContext(BaseModel):
    last_success_run_id: int | None = None
    last_success_sha: str | None = None
    last_success_at: datetime | None = None
    last_success_age_hours: float | None = None
    blame_range: str | None = None
    first_failing_sha: str | None = None
    recent_outcomes: str | None = None
    same_sha_runs: list[int] = []
    match: Literal["new", "similar", "exact"] = "new"
    match_similarity: float | None = None
    config_drift: bool = False
    seen_count: int = 0
    first_seen: datetime | None = None
    branches_seen: list[str] = []
    cross_branch: bool = False
    previous_summary: str | None = None
    previous_resolution: str | None = None
    resolution_verified: bool = False
    backend: Literal["cache", "issues", "redis", "none"] = "cache"
    backend_degraded: bool = False


class PipelineLogStream(BaseModel):
    artifact_name: str
    stage: str | None = None
    file: str
    phase: Literal["setup", "run", "teardown"] | None = None
    step_name: str | None = None
    windows: list[LogWindow] = []
    error_lines: list[ErrorLine] = []
    stack_traces: list[StackTrace] = []
    templates: list[LogTemplate] = []


class LastGreenCompare(BaseModel):
    artifact_name: str | None = None
    novel_templates: list[str] = []
    missing_on_fail: list[str] = []
    present_on_green: list[str] = []
    available: bool = False
    skipped_reason: str | None = None


class CodeHunk(BaseModel):
    path: str
    ref: str | None = None
    start_line: int = 1
    end_line: int = 1
    content: str = ""
    note: str | None = None


class CodeContext(BaseModel):
    hunks: list[CodeHunk] = []
    notes: list[str] = []
    skipped_reason: str | None = None
    ref_sha: str | None = None
    basis: Literal["first_failing", "head"] | None = None


class DeterministicDiagnosis(BaseModel):
    rule_id: str
    one_liner: str
    suspected_stage: str | None = None
    suspected_files: list[str] = []
    citations: list[str] = []
    winning_stream_id: str | None = None
    fix_one_liner: str | None = None


class DeliveryReport(BaseModel):
    """Phase 3 delivery outcome (delivery spec v1.3 §15). Written back by ``deliver``."""

    trigger: str
    severity: str
    suppressed_by: str | None = None
    delivered_to: list[str] = []
    comment_url: str | None = None
    issue_url: str | None = None
    owner_resolved_by: str | None = None
    errors: list[str] = []
    dry_run: bool = False
    notes: list[str] = []


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
    pipeline_logs: list[PipelineLogStream] = []
    last_green_compare: LastGreenCompare | None = None
    code_context: CodeContext | None = None
    diagnosis: DeterministicDiagnosis | None = None
    drain: DrainReport | None = None
    changes: ChangeContext | None = None
    history: HistoryContext | None = None
    fingerprint: str
    fingerprint_coarse: str
    kubernetes: None = None
    budget_report: BudgetReport
    collection_notes: list[str] = []
    delivery: DeliveryReport | None = None


# --- Phase 2 analysis (§16). Not attached to Summary in Slice H. ---

AnalysisStatus = Literal[
    "ok",
    "cached",
    "gated",
    "skipped",
    "unvalidated",
    "failed",
    "parse_error",
    "citation_invalid",
    "bridge_error",
    "unusable",
]

AnalysisCitationSource = Literal[
    "first_error_window",
    "tail_window",
    "stack_traces",
    "log_templates",
    "junit",
    "change_context",
    "annotations",
    "history",
    "step_table",
    "pipeline_logs",
    "code_context",
    "last_green_compare",
    "deterministic_rule",
    "failed_step_excerpt",
]


class AnalysisCitation(BaseModel):
    quote: str
    source: AnalysisCitationSource
    line: int | None = None


class AnalysisResult(BaseModel):
    root_cause: str
    suggested_fix: str
    confidence: Literal["high", "medium", "low"]
    citations: list[AnalysisCitation] = []
    cannot_determine: bool = False
    suspected_files: list[str] = []
    suspected_stage: str | None = None
    infra_or_code: str | None = None
    used_deterministic_rule: str | None = None
    source: str | None = None


class GroundingResult(BaseModel):
    """Prompt-evidence grounding score (eval-spec §4.1, mapped to real fields).

    Distinct from ``AnalysisRecord.grounded`` (the failed-step anchor check that
    drives the Step 5 confidence cap): this is the anti-fabrication rate — every
    citation quote must appear in the sent evidence after §4.2 normalisation.
    """

    grounded: bool = False
    citation_count: int = 0
    ungrounded_citations: list[str] = []
    grounding_rate: float = 0.0
    component_grounded: bool | None = None
    shas_grounded: bool | None = None
    checks_run: list[str] = []


class ValidationTelemetry(BaseModel):
    schema_valid_first_try: bool | None = None
    repair_attempted: bool = False
    repair_succeeded: bool = False
    parse_error: str | None = None  # capped at 200 chars
    fallback_used: bool = False


class CallTelemetry(BaseModel):
    prompt_tokens_est: int = 0
    completion_tokens_est: int = 0
    latency_ms: int | None = None
    total_pipeline_ms: int | None = None
    short_circuited: bool = False
    cache_hit: bool = False


class AnalysisRecord(BaseModel):
    status: AnalysisStatus
    prompt_version: str = "p2.4"
    persona: str | None = None
    fingerprint: str | None = None
    schema_version: str | None = None
    result: AnalysisResult | None = None
    cache_hit: bool = False
    fallback_used: bool = False
    response_id: str | None = None
    notes: list[str] = []
    analyzed_at: datetime | None = None
    raw_completion: str | None = None
    stgpt_responses: list[dict[str, Any]] = []
    reason_code: str | None = None
    model_called: bool = False
    grounded: bool | None = None
    grounding: GroundingResult | None = None
    validation_telemetry: ValidationTelemetry | None = None
    call_telemetry: CallTelemetry | None = None
