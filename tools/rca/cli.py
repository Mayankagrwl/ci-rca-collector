"""CLI: collect (live or --from-fixture) and capture a run into a fixture dir."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from dateutil.parser import isoparse

from .budget import apply_budget
from .cleaner import CleanResult, clean_log
from .classify import ClassificationHit, classify_failure
from .config import (
    ARTIFACT_DOWNLOAD_MAX_BYTES,
    COLLECTOR_VERSION,
    MAX_FAILED_JOBS_ANALYSED,
    MAX_PIPELINE_LOG_ARTIFACTS,
    QUEUE_SECONDS_THRESHOLD,
)
from .changes import change_context_from_compare, resolve_compare_base
from .drain_index import (
    default_config_path,
    fingerprint_coarse,
    fingerprint_fine,
    masking_config_hash,
    merge_fingerprint_inputs,
    novelty,
    remask_templates,
    template_hash,
    train as drain_train,
)
from .extract import extract_from_lines, failed_step_excerpt_lines
from .github_api import GitHubAPIError, GitHubClient
from .junit import (
    artifact_looks_like_junit,
    merge_junit_reports,
    parse_junit_from_zip,
    parse_junit_xml,
)
from .history import (
    lookup_recurrence,
    open_store,
    summarize as summarize_history,
)
from .models import (
    ArtifactInfo,
    BudgetReport,
    Classification,
    FailedJob,
    FailedStepExcerpt,
    FailureRecord,
    HistoryContext,
    JUnitReport,
    LastGreenCompare,
    RunnerInfo,
    RunMeta,
    StepInfo,
    Summary,
    Verdict,
)
from .outputs import write_failure_outputs, write_github_output
from .redact import redact_summary
from .render import render_markdown

_LOG = logging.getLogger("rca")
_TIMEOUT_SIGNATURE = "exceeded the maximum execution time"
_NON_SUCCESS_CONCLUSIONS = frozenset({"failure", "cancelled", "timed_out"})
_ANALYSE_CONCLUSIONS = frozenset({"failure", "timed_out"})


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
        stream=sys.stderr,
    )
    try:
        if args.cmd == "collect":
            return _cmd_collect(args)
        if args.cmd == "capture":
            return _cmd_capture(args)
        if args.cmd == "train":
            return _cmd_train(args)
        if args.cmd == "refingerprint":
            return _cmd_refingerprint(args)
        if args.cmd == "analyze":
            return _cmd_analyze(args)
        if args.cmd == "telemetry":
            return _cmd_telemetry(args)
        if args.cmd == "cache-keys":
            return _cmd_cache_keys(args)
        if args.cmd == "feedback":
            return _cmd_feedback(args)
        if args.cmd == "deliver":
            if getattr(args, "migrate_history", False):
                return _cmd_migrate_history(args)
            if not args.summary:
                parser.error("deliver requires --summary (or --migrate-history)")
            return _cmd_deliver(args)
        parser.error(f"unknown command {args.cmd}")
    except Exception as exc:  # noqa: BLE001 — collector must not fail the workflow
        if getattr(args, "cmd", None) == "feedback":
            _LOG.error("feedback error: %s", type(exc).__name__)
            return 1 if getattr(args, "strict", False) else 0
        if getattr(args, "cmd", None) == "deliver":
            # Never regenerate summary.json from a stub: it holds collect + analysis.
            _LOG.exception("deliver error")
            return 1 if getattr(args, "strict", False) else 0
        if getattr(args, "cmd", None) == "analyze":
            _LOG.exception("analyze error")
            _emit_failed_analysis(args, f"analyze error: {exc}")
            return 1 if getattr(args, "strict", False) else 0
        _LOG.exception("collector error")
        out = getattr(args, "out", None) or "rca"
        _safe_emit(
            _stub_summary(
                note=f"collector error: {exc}",
                run_id=int(getattr(args, "run_id", None) or 0),
            ),
            Path(out),
            token_budget=getattr(args, "token_budget", None),
        )
        if getattr(args, "strict", False):
            return 1
        return 0
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument("--token", default=None, help="GitHub token (never logged)")
    parent.add_argument("--api-url", default=None, help="GitHub REST API base URL")
    parent.add_argument(
        "--strict",
        action="store_true",
        help="exit non-zero on collection errors (local dev)",
    )
    parent.add_argument("--verbose", action="store_true")

    parser = argparse.ArgumentParser(
        prog="python -m tools.rca.cli",
        description="CI failure context collector (Phase 1)",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    collect = sub.add_parser("collect", parents=[parent])
    _add_run_flags(collect)
    collect.add_argument(
        "--from-fixture",
        default=None,
        help="replay captured API responses; no network",
    )

    train = sub.add_parser("train", parents=[parent])
    _add_run_flags(train)
    train.add_argument(
        "--from-fixture",
        default=None,
        help="replay captured API responses; no network",
    )

    capture = sub.add_parser("capture", parents=[parent])
    capture.add_argument("--run-id", type=int, required=True)
    capture.add_argument("--repo", required=True, help="owner/repo")
    capture.add_argument("--out-fixture", required=True, help="directory to write raw responses")

    refp = sub.add_parser("refingerprint", parents=[parent])
    refp.add_argument("--dry-run", action="store_true")
    refp.add_argument("--history-dir", default=".rca-history")
    refp.add_argument("--drain-config", default=None)

    analyze = sub.add_parser("analyze", parents=[parent])
    analyze.add_argument("--summary", required=True, help="path to summary.json")
    analyze.add_argument("--out", required=True, help="directory for analysis.json / summary.*")
    analyze.add_argument(
        "--from-completion",
        default=None,
        help="offline completion JSON; skips HTTP",
    )
    analyze.add_argument(
        "--cache-dir",
        default=None,
        help="analysis cache directory (default: <out>/analysis-cache)",
    )
    analyze.add_argument(
        "--mode",
        default="collect",
        help="collect | train. STGPT is only considered in collect.",
    )
    analyze.add_argument(
        "--analyze-enabled",
        default="true",
        help="false when the action analyze input is off.",
    )
    analyze.add_argument(
        "--analyze-policy",
        default="auto",
        help="auto (skip when deterministic is trustworthy) | always | never.",
    )
    analyze.add_argument(
        "--requires-analysis",
        default=None,
        help="true|false from collect output; default is summary.verdict.",
    )
    analyze.add_argument(
        "--stgpt-key-present",
        default=None,
        help="true|false; default infers from STGPT_API / API_KEY.",
    )
    analyze.add_argument(
        "--history-dir",
        default=".rca-history",
        help="history directory; per-run telemetry is appended here (cached).",
    )
    analyze.add_argument(
        "--write-telemetry",
        default="true",
        help="false to skip appending the per-run telemetry line.",
    )

    telemetry = sub.add_parser("telemetry", parents=[parent])
    telemetry.add_argument(
        "--history-dir",
        default=".rca-history",
        help="directory holding telemetry.jsonl.",
    )
    telemetry.add_argument("--out", default=None, help="optional path to write the summary")
    telemetry.add_argument("--format", default="json", help="json | md")

    keys = sub.add_parser("cache-keys", parents=[parent])
    keys.add_argument("--workflow-name", default=None)
    keys.add_argument("--job-name", default=None)
    keys.add_argument("--repository", default=None, help="owner/repo")

    deliver = sub.add_parser("deliver", parents=[parent])
    deliver.add_argument("--summary", default=None, help="path to summary.json")
    deliver.add_argument("--analysis", default=None, help="path to analysis.json")
    deliver.add_argument("--out", default="rca", help="directory for delivery-preview.md")
    deliver.add_argument(
        "--dry-run", action="store_true", help="render only; never post (the default)"
    )
    deliver.add_argument(
        "--live",
        action="store_true",
        help="post / edit comments and labels on GitHub (never with --offline or --dry-run)",
    )
    deliver.add_argument(
        "--offline", action="store_true", help="skip every GitHub API call (read-only lookups)"
    )
    deliver.add_argument("--repo", default=None, help="owner/repo (default: from the run URL)")
    for flag in (
        "--comment-on-pr",
        "--comment-on-commit",
        "--comment-on-branch-push",
        "--create-issues",
        "--allow-fork-issues",
    ):
        deliver.add_argument(flag, default=None, help="true | false")
    deliver.add_argument("--issue-threshold", type=int, default=None)
    deliver.add_argument("--confidence-threshold", default=None, help="low | medium | high")
    deliver.add_argument("--quiet-window-minutes", type=int, default=None)
    deliver.add_argument("--platform-team", default=None)
    deliver.add_argument("--default-notify", default=None)
    deliver.add_argument(
        "--migrate-history",
        action="store_true",
        help="one-shot: create issues for cache-history fingerprints not yet tracked",
    )
    deliver.add_argument("--history-dir", default=".rca-history", help="cache history directory")

    feedback = sub.add_parser("feedback", parents=[parent])
    feedback.add_argument(
        "--event", required=True, help="issue_comment event JSON file ($GITHUB_EVENT_PATH)"
    )
    feedback.add_argument("--repo", default=None, help="owner/repo (default: from the event)")
    feedback.add_argument("--out", default="rca", help="directory for feedback-report.md")
    feedback.add_argument("--live", action="store_true", help="apply the resolution on GitHub")
    feedback.add_argument("--dry-run", action="store_true", help="read only (the default)")
    feedback.add_argument("--offline", action="store_true", help="no GitHub API calls at all")
    deliver.add_argument("--migrate-limit", type=int, default=50, help="max issues created per run")
    return parser


def _add_run_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--run-id", type=int, default=None)
    parser.add_argument("--repo", default=None, help="owner/repo")
    parser.add_argument("--out", required=True, help="directory for summary.json / summary.md")
    parser.add_argument("--token-budget", default=None, help="total evidence token budget")
    parser.add_argument("--drain-dir", default=".drain", help="Drain3 baseline directory")
    parser.add_argument("--history-dir", default=".rca-history", help="fingerprint history directory")
    parser.add_argument(
        "--history-backend",
        default=None,
        help="cache | none. Default none for fixtures, cache for live collect.",
    )
    parser.add_argument(
        "--analyze-pipeline-logs",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Download and parse pipeline/docker log artifacts (default on).",
    )
    parser.add_argument(
        "--max-pipeline-log-artifacts",
        type=int,
        default=MAX_PIPELINE_LOG_ARTIFACTS,
        help="Max pipeline/docker log zips to download (fail-side first).",
    )


def _resolve_drain_dir(args: argparse.Namespace) -> Path:
    """Persist Drain3 under the workspace `.drain/`, never github.action_path."""
    raw = getattr(args, "drain_dir", None) or os.environ.get("RCA_DRAIN_DIR") or ".drain"
    path = Path(raw)
    workspace = os.environ.get("GITHUB_WORKSPACE")
    action_path = os.environ.get("GITHUB_ACTION_PATH")
    if not path.is_absolute():
        root = Path(workspace) if workspace else Path.cwd()
        path = root / path
    path = path.resolve()
    if action_path:
        try:
            under_action = path.is_relative_to(Path(action_path).resolve())
        except (OSError, ValueError):
            under_action = False
        if under_action:
            fallback = Path(workspace) if workspace else Path.cwd()
            path = (fallback / ".drain").resolve()
            _LOG.warning("drain dir was under github.action_path; writing to %s", path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _cmd_capture(args: argparse.Namespace) -> int:
    out = Path(args.out_fixture)
    out.mkdir(parents=True, exist_ok=True)
    try:
        with GitHubClient(api_url=args.api_url, token=args.token) as client:
            run = client.get_run(args.repo, args.run_id)
            jobs = client.list_jobs(args.repo, args.run_id)
            pulls: list[dict[str, Any]] = []
            sha = run.get("head_sha")
            if sha and not run.get("pull_requests"):
                try:
                    pulls = client.list_commit_pulls(args.repo, sha)
                except GitHubAPIError as exc:
                    _LOG.warning("commit pulls unavailable: %s", exc)
            _write_json(out / "run.json", run)
            _write_json(out / "jobs.json", {"jobs": jobs})
            _write_json(out / "pulls.json", pulls)
            same_sha: list[dict[str, Any]] = []
            if sha:
                try:
                    same_sha = client.list_runs(args.repo, head_sha=str(sha))
                except GitHubAPIError as exc:
                    _LOG.warning("same-SHA runs unavailable: %s", exc)
            _enrich_same_sha_jobs(client, args.repo, current=run, same_sha=same_sha, notes=[])
            _write_json(out / "same_sha_runs.json", same_sha)
            branch_runs: list[dict[str, Any]] = []
            last_success: dict[str, Any] | None = None
            pull_obj: dict[str, Any] | None = None
            compare_obj: dict[str, Any] | None = None
            workflow_id = run.get("workflow_id")
            branch = run.get("head_branch")
            if workflow_id and branch:
                try:
                    successes = client.list_runs(
                        args.repo,
                        workflow_id=workflow_id,
                        branch=str(branch),
                        status="success",
                        per_page=1,
                    )
                    last_success = successes[0] if successes else None
                except GitHubAPIError as exc:
                    _LOG.warning("last success unavailable: %s", exc)
                try:
                    branch_runs = client.list_runs(
                        args.repo,
                        workflow_id=workflow_id,
                        branch=str(branch),
                        per_page=10,
                    )
                except GitHubAPIError as exc:
                    _LOG.warning("branch runs unavailable: %s", exc)
            prs = run.get("pull_requests") or pulls
            pr_num = prs[0].get("number") if prs else None
            if pr_num:
                try:
                    pull_obj = client.get_pull(args.repo, int(pr_num))
                except GitHubAPIError as exc:
                    _LOG.warning("pull unavailable: %s", exc)
            merge_base = None
            if pull_obj and isinstance(pull_obj.get("base"), dict):
                merge_base = pull_obj["base"].get("sha")
            base_sha = (last_success or {}).get("head_sha") or merge_base
            if base_sha and sha:
                try:
                    compare_obj = client.compare(args.repo, str(base_sha), str(sha))
                except GitHubAPIError as exc:
                    _LOG.warning("compare unavailable: %s", exc)
            _write_json(out / "branch_runs.json", {"workflow_runs": branch_runs})
            if last_success is not None:
                _write_json(out / "last_success.json", last_success)
            if pull_obj is not None:
                _write_json(out / "pull.json", pull_obj)
            if compare_obj is not None:
                _write_json(out / "compare.json", compare_obj)
            _write_json(
                out / "meta.json",
                {"repo": args.repo, "run_id": args.run_id, "api_url": client.api_url},
            )
            logs_dir = out / "logs"
            logs_dir.mkdir(exist_ok=True)
            for job in jobs:
                if job.get("conclusion") not in _NON_SUCCESS_CONCLUSIONS:
                    continue
                job_id = job.get("id")
                if job_id is None:
                    continue
                text = client.get_job_log(args.repo, int(job_id))
                if text is None:
                    (logs_dir / f"{job_id}.unavailable").write_text("", encoding="utf-8")
                else:
                    (logs_dir / f"{job_id}.log").write_text(text, encoding="utf-8")
            try:
                artifacts = client.list_artifacts(args.repo, args.run_id)
            except GitHubAPIError as exc:
                _LOG.warning("artifacts unavailable: %s", exc)
                artifacts = []
            _write_json(out / "artifacts.json", {"artifacts": artifacts})
            art_dir = out / "artifacts"
            art_dir.mkdir(exist_ok=True)
            from .junit import artifact_looks_like_junit
            from .pipeline_logs import match_pipeline_artifact_name

            downloaded = 0
            for raw in artifacts:
                name = str(raw.get("name") or "artifact")
                size = int(raw.get("size_in_bytes") or raw.get("size_bytes") or 0)
                if size > ARTIFACT_DOWNLOAD_MAX_BYTES or raw.get("expired"):
                    continue
                pipeline = match_pipeline_artifact_name(name)
                junit = artifact_looks_like_junit(name)
                if not pipeline and not junit:
                    continue
                if pipeline and downloaded >= MAX_PIPELINE_LOG_ARTIFACTS:
                    continue
                if raw.get("id") is None:
                    continue
                blob = client.download_artifact_zip(args.repo, int(raw["id"]))
                if not blob:
                    continue
                (art_dir / f"{name}.zip").write_bytes(blob)
                if pipeline:
                    downloaded += 1
            if last_success is not None and last_success.get("id") is not None:
                try:
                    green_arts = client.list_artifacts(args.repo, int(last_success["id"]))
                except GitHubAPIError as exc:
                    _LOG.warning("last-green artifacts unavailable: %s", exc)
                    green_arts = []
                green_dir = out / "last_green_artifacts"
                wanted = {
                    str(raw.get("name") or "")
                    for raw in artifacts
                    if match_pipeline_artifact_name(str(raw.get("name") or ""))
                }
                remaining = max(0, MAX_PIPELINE_LOG_ARTIFACTS - downloaded)
                for raw in green_arts:
                    if remaining <= 0:
                        break
                    name = str(raw.get("name") or "")
                    if name not in wanted or raw.get("id") is None:
                        continue
                    blob = client.download_artifact_zip(args.repo, int(raw["id"]))
                    if not blob:
                        continue
                    green_dir.mkdir(exist_ok=True)
                    (green_dir / f"{name}.zip").write_bytes(blob)
                    remaining -= 1
    except Exception as exc:  # noqa: BLE001
        _LOG.exception("capture failed")
        if args.strict:
            raise
        print(f"capture failed: {exc}", file=sys.stderr)
        return 0
    _LOG.info("captured run %s to %s", args.run_id, out)
    return 0


def _cmd_collect(args: argparse.Namespace) -> int:
    out = Path(args.out)
    try:
        notes: list[str] = []
        if args.from_fixture:
            bundle = _load_fixture(Path(args.from_fixture))
        else:
            if not args.run_id or not args.repo:
                raise ValueError("collect requires --from-fixture or both --run-id and --repo")
            bundle = _fetch_live(
                args.repo,
                args.run_id,
                api_url=args.api_url,
                token=args.token,
                analyze_pipeline=bool(getattr(args, "analyze_pipeline_logs", True)),
                max_pipeline=int(
                    getattr(args, "max_pipeline_log_artifacts", None)
                    or MAX_PIPELINE_LOG_ARTIFACTS
                ),
            )
            notes.extend(bundle.pop("notes", []))
        summary = _build_summary(bundle, extra_notes=notes, args=args)
        summary = _maybe_fetch_code_context(summary, bundle, args)
        _annotate_cache_keys(summary, args)
    except Exception as exc:  # noqa: BLE001 — always emit a partial summary
        _LOG.exception("collect failed")
        _safe_emit(
            _stub_summary(note=f"collector error: {exc}", run_id=int(args.run_id or 0)),
            out,
            token_budget=getattr(args, "token_budget", None),
        )
        return 1 if args.strict else 0
    _safe_emit(summary, out, token_budget=getattr(args, "token_budget", None))
    return 0


def _cmd_train(args: argparse.Namespace) -> int:
    drain_dir = _resolve_drain_dir(args)
    notes: list[str] = []
    note = "Drain3 trained 0 jobs"
    try:
        if args.from_fixture:
            bundle = _load_fixture(Path(args.from_fixture))
        else:
            if not args.run_id or not args.repo:
                raise ValueError("train requires --from-fixture or both --run-id and --repo")
            bundle = _fetch_train(
                args.repo, args.run_id, api_url=args.api_url, token=args.token
            )
        notes.extend(bundle.get("notes") or [])
        workflow = str(bundle["run"].get("name") or "workflow")
        jobs = list(bundle.get("jobs") or [])
        logs = bundle.get("logs") or {}
        success_jobs = [job for job in jobs if job.get("conclusion") == "success"]
        trained = 0
        line_total = 0
        for job in success_jobs:
            job_id = job.get("id")
            if job_id is None:
                continue
            raw = logs.get(int(job_id))
            if not raw:
                continue
            cleaned = clean_log(raw).lines
            key = f"{workflow}_{job.get('name') or 'job'}"
            written = drain_train(cleaned, key, drain_dir=drain_dir)
            _LOG.info("Drain3 wrote %s", written)
            trained += 1
            line_total += sum(1 for line in cleaned if str(line).strip())
        if trained == 0:
            why = "no success jobs" if not success_jobs else "no logs"
            note = f"Drain3 trained 0 jobs: {why}"
        else:
            note = (
                f"Drain3 trained {trained} jobs, {line_total} lines, "
                f"output files under {drain_dir}/"
            )
        notes.append(note)
        _LOG.info(note)
    except Exception as exc:  # noqa: BLE001
        _LOG.exception("train failed")
        note = f"train error: {exc}"
        notes.append(note)
        if args.strict:
            raise
    _safe_emit(
        _stub_summary(
            note="; ".join(notes) if notes else note,
            run_id=int(args.run_id or 0),
            requires_analysis=False,
        ),
        Path(args.out),
        token_budget=getattr(args, "token_budget", None),
    )
    return 0


def _summary_if_readable(path: Path) -> Summary | None:
    try:
        if path.is_file():
            return Summary.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return None


def _cmd_cache_keys(args: argparse.Namespace) -> int:
    """Print sanitized actions/cache keys to stdout and $GITHUB_OUTPUT."""
    from .cache_keys import (
        cache_key_part,
        drain_cache_key,
        drain_restore_key,
        history_cache_key,
        history_restore_keys,
    )

    wf = (
        args.workflow_name
        or os.environ.get("RCA_WORKFLOW_NAME")
        or os.environ.get("GITHUB_WORKFLOW")
        or "unknown"
    )
    job = (
        args.job_name
        or os.environ.get("RCA_JOB_NAME")
        or os.environ.get("GITHUB_JOB")
        or "job"
    )
    repo = (
        args.repository
        or getattr(args, "repo", None)
        or os.environ.get("RCA_REPO")
        or os.environ.get("GITHUB_REPOSITORY")
        or "unknown"
    )
    drain = drain_cache_key(wf, job)
    drain_restore = drain_restore_key(wf)
    hist = history_cache_key(repo, wf)
    hist_restore = history_restore_keys(repo)[0]
    values = {
        "drain-cache-key": drain,
        "drain-cache-restore-key": drain_restore,
        "history-cache-key": hist,
        "history-cache-restore-key": hist_restore,
        "workflow-key-part": cache_key_part(wf),
        "job-key-part": cache_key_part(job),
        "repo-key-part": cache_key_part(repo),
    }
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        dest = Path(path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("a", encoding="utf-8") as handle:
            for key, value in values.items():
                handle.write(f"{key}={value}\n")
    for key, value in values.items():
        print(f"{key}={value}")
    return 0


def _annotate_cache_keys(summary: Summary, args: argparse.Namespace) -> None:
    """Log the exact GitHub Actions cache keys in collection_notes."""
    from .cache_keys import drain_cache_key, history_cache_key

    job = os.environ.get("GITHUB_JOB") or (
        summary.failed_jobs[0].name if summary.failed_jobs else "job"
    )
    repo = (
        getattr(args, "repo", None)
        or os.environ.get("RCA_REPO")
        or os.environ.get("GITHUB_REPOSITORY")
        or "unknown"
    )
    drain = os.environ.get("RCA_DRAIN_CACHE_KEY") or drain_cache_key(
        summary.run.workflow_name, job
    )
    hist = os.environ.get("RCA_HISTORY_CACHE_KEY") or history_cache_key(
        repo, summary.run.workflow_name
    )
    for note in (f"drain cache key: {drain}", f"history cache key: {hist}"):
        if note not in summary.collection_notes:
            summary.collection_notes.append(note)


def _cmd_analyze(args: argparse.Namespace) -> int:
    from .analyze import analyze_summary, decide_stgpt_call, skipped_record, write_analysis
    from .models import AnalysisRecord
    from .outputs import write_analysis_github_output

    out = Path(args.out)
    summary_path = Path(args.summary)
    cache_dir = Path(args.cache_dir) if args.cache_dir else out / "analysis-cache"
    try:
        summary = Summary.model_validate_json(summary_path.read_text(encoding="utf-8"))
        call, reason = decide_stgpt_call(
            summary,
            analyze_enabled=getattr(args, "analyze_enabled", "true"),
            requires_analysis=getattr(args, "requires_analysis", None),
            stgpt_key_present=getattr(args, "stgpt_key_present", None),
            mode=getattr(args, "mode", "collect"),
            from_completion=args.from_completion,
            policy=getattr(args, "analyze_policy", "auto"),
        )
        if call:
            record = analyze_summary(
                summary,
                from_completion=args.from_completion,
                cache_dir=cache_dir,
            )
        else:
            record = skipped_record(summary, reason or "analyze_disabled")
        write_analysis(record, summary_path=summary_path, out_dir=out)
        write_analysis_github_output(record, summary=summary)
        if _as_flag(getattr(args, "write_telemetry", "true"), default=True):
            _append_run_telemetry(args, summary, record)
    except Exception as exc:  # noqa: BLE001
        _LOG.exception("analyze error")
        record = AnalysisRecord(
            status="failed",
            notes=[f"analyze error: {exc}"],
            analyzed_at=datetime.now(timezone.utc),
        )
        try:
            write_analysis(record, summary_path=summary_path, out_dir=out)
            loaded = _summary_if_readable(summary_path)
            write_analysis_github_output(record, summary=loaded)
        except Exception:
            _LOG.exception("failed to write analysis.json")
            _emit_failed_analysis(args, f"analyze error: {exc}")
        if args.strict:
            return 1
        return 0
    if record.status == "failed" and args.strict:
        return 1
    return 0


# ---- deliver (Phase 3: dry-run by default; sticky comments only with --live) -------

DELIVERY_PREVIEW_FILE = "delivery-preview.md"
COMMENT_BEGIN = "<!-- BEGIN RCA COMMENT BODY (posted verbatim) -->"
COMMENT_END = "<!-- END RCA COMMENT BODY -->"
_DRY_RUN_NOTE = "dry run: nothing posted (pass --live to post)"
_FLAKY_LABEL = "ci:flaky"
_MIGRATE_PACE_S = 1.0  # GitHub secondary rate limit on content creation
_MIGRATION_REPORT_FILE = "history-migration.md"
_DELIVERY_TIMEOUT_S = 15
_DELIVERY_INPUT_FLAGS = (
    "comment_on_pr",
    "comment_on_commit",
    "comment_on_branch_push",
    "create_issues",
    "allow_fork_issues",
)
_DELIVERY_INPUT_VALUES = (
    "issue_threshold",
    "confidence_threshold",
    "quiet_window_minutes",
    "platform_team",
    "default_notify",
)
_RUN_URL_REPO_RE = re.compile(r"^https?://[^/]+/([^/]+/[^/]+)/actions/runs/\d+")


class OfflineMode(Exception):
    """Raised by the offline client: every lookup falls back (Step 13 fallbacks)."""


class _OfflineClient:
    def get_repo(self, repo: str) -> dict[str, Any]:
        raise OfflineMode()

    def get_run(self, repo: str, run_id: int) -> dict[str, Any]:
        raise OfflineMode()

    def list_runs(self, repo: str, **_kwargs: Any) -> list[dict[str, Any]]:
        raise OfflineMode()

    def ref_is_tag(self, repo: str, name: str) -> bool:
        raise OfflineMode()


def _delivery_clock() -> float:
    """Wall-clock for the 30s delivery budget. Test hook: monkeypatch to a fake clock."""
    return time.monotonic()


# Test hooks for the notification senders (never the GitHub client / token).
_notify_chat_transport: Any = None  # httpx transport for the chat webhook
_notify_smtp_factory: Any = None  # (host, port, timeout, implicit_tls) -> SMTP-like
CHAT_BEGIN = "<!-- BEGIN RCA CHAT TEXT (posted verbatim) -->"
CHAT_END = "<!-- END RCA CHAT TEXT -->"


def _migrate_sleep(seconds: float) -> None:
    """Pacing between migration creates. Test hook: monkeypatch to record instead of sleep."""
    time.sleep(seconds)


def _delivery_client(args: argparse.Namespace) -> GitHubClient:
    """The delivery job's client (15s per call). Test hook: monkeypatch to inject a transport."""
    return GitHubClient(api_url=args.api_url, token=args.token, timeout=_DELIVERY_TIMEOUT_S)


def _delivery_inputs(args: argparse.Namespace, errors: list[str]) -> Any:
    from .deliver import DeliveryInputs

    values: dict[str, Any] = {}
    for name in _DELIVERY_INPUT_FLAGS:
        raw = getattr(args, name, None)
        if raw is not None:
            values[name] = _as_flag(raw, default=False)
    for name in _DELIVERY_INPUT_VALUES:
        raw = getattr(args, name, None)
        if raw is not None:
            values[name] = raw
    try:
        return DeliveryInputs(**values)
    except Exception as exc:  # noqa: BLE001 — bad input → defaults, recorded
        errors.append(f"invalid delivery inputs; using defaults: {_first_line(exc)}")
        return DeliveryInputs()


def _load_delivery_analysis(
    args: argparse.Namespace, raw_summary: dict[str, Any] | None, notes: list[str]
) -> Any:
    from .models import AnalysisRecord

    if getattr(args, "analysis", None):
        path = Path(args.analysis)
        try:
            return AnalysisRecord.model_validate_json(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            notes.append(f"analysis file not found: {path.name}")
        except Exception as exc:  # noqa: BLE001 — corrupt analysis → deterministic card
            notes.append(f"analysis file unreadable ({type(exc).__name__}); ignored")
    embedded = (raw_summary or {}).get("analysis")
    if isinstance(embedded, dict):
        try:
            return AnalysisRecord.model_validate(embedded)
        except Exception as exc:  # noqa: BLE001
            notes.append(f"embedded analysis unreadable ({type(exc).__name__}); ignored")
    return None


def _delivery_repo(args: argparse.Namespace, summary: Summary) -> str | None:
    if getattr(args, "repo", None):
        return str(args.repo)
    env_repo = (os.environ.get("GITHUB_REPOSITORY") or "").strip()
    if env_repo:
        return env_repo
    match = _RUN_URL_REPO_RE.match(summary.run.html_url or "")
    return match.group(1) if match else None


def _first_line(exc: BaseException) -> str:
    text = str(exc).strip().splitlines()
    return f"{type(exc).__name__}: {text[0]}" if text else type(exc).__name__


def _cmd_deliver(args: argparse.Namespace) -> int:
    """Decide, render and (only with --live) post sticky comments. Never fails the workflow."""
    from .deliver.sticky import DeliveryBudget
    from .models import DeliveryReport

    budget = DeliveryBudget(started_at=_delivery_clock())
    out = Path(args.out)
    summary_path = Path(args.summary)
    notes: list[str] = []
    errors: list[str] = []
    live = bool(args.live) and not args.offline and not args.dry_run
    if args.live and args.offline:
        notes.append("--live ignored: --offline makes no GitHub API calls")
    elif args.live and args.dry_run:
        notes.append("--live ignored: --dry-run was also given")
    if not live:
        notes.append(_DRY_RUN_NOTE)

    raw_summary: dict[str, Any] | None = None
    summary: Summary | None = None
    try:
        loaded = json.loads(summary_path.read_text(encoding="utf-8"))
        raw_summary = loaded if isinstance(loaded, dict) else None
        summary = Summary.model_validate(raw_summary)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"summary unreadable: {_first_line(exc)}")

    outcome: dict[str, Any] = {"trigger": "unknown", "severity": "low", "live": live}
    if summary is not None:
        try:
            outcome = _plan_delivery(
                args, summary, raw_summary, notes, errors, live=live, budget=budget
            )
        except Exception as exc:  # noqa: BLE001 — still write whatever we can
            _LOG.exception("deliver planning error")
            errors.append(f"delivery planning failed: {_first_line(exc)}")

    report = DeliveryReport(
        trigger=outcome.get("trigger", "unknown"),
        severity=outcome.get("severity", "low"),
        suppressed_by=outcome.get("suppressed_by"),
        delivered_to=list(outcome.get("delivered_to", [])),
        comment_url=outcome.get("comment_url"),
        issue_url=outcome.get("issue_url"),
        owner_resolved_by=outcome.get("owner_resolved_by"),
        owners=list(outcome.get("owners", [])),
        feedback=dict(outcome.get("feedback") or {}),
        dry_run=not live,
        errors=errors,
        notes=notes,
    )
    try:
        out.mkdir(parents=True, exist_ok=True)
        (out / DELIVERY_PREVIEW_FILE).write_text(
            _delivery_preview(report, outcome), encoding="utf-8"
        )
    except Exception:  # noqa: BLE001
        _LOG.exception("failed to write delivery preview")
    if raw_summary is not None:
        try:
            _write_back_delivery(summary_path, out, report)
        except Exception as exc:  # noqa: BLE001
            _LOG.exception("failed to write delivery into summary.json")
            report.errors.append(f"summary.json write-back failed: {_first_line(exc)}")
    try:
        from .outputs import write_delivery_github_output

        write_delivery_github_output(report)
    except Exception:  # noqa: BLE001
        _LOG.exception("failed to write delivery outputs")
    return 1 if (args.strict and report.errors) else 0


def _plan_delivery(
    args: argparse.Namespace,
    summary: Summary,
    raw_summary: dict[str, Any] | None,
    notes: list[str],
    errors: list[str],
    *,
    live: bool,
    budget: Any,
) -> dict[str, Any]:
    from .deliver.severity import severity
    from .deliver.suppress import evaluate
    from .deliver.targets import (
        default_branch_state,
        resolve_context,
        route,
        should_open_issue,
    )

    now = datetime.now(timezone.utc)
    inputs = _delivery_inputs(args, errors)
    record = _load_delivery_analysis(args, raw_summary, notes)
    if summary.diagnosis is None:
        # Older / captured summaries carry no deterministic card; re-derive it in
        # memory (never written back) so the preview matches what collect produces.
        from .diagnose import apply_verdict, diagnose

        summary = apply_verdict(summary, diagnose(summary.model_copy(deep=True)))
        notes.append("summary had no diagnosis; deterministic verdict re-derived in memory")

    repo = _delivery_repo(args, summary)
    client: Any
    closer: Any = None
    if args.offline:
        client = _OfflineClient()
        notes.append("offline: no GitHub API calls; lookups use fallbacks")
    elif not repo:
        client = _OfflineClient()
        notes.append("no repository known (--repo / GITHUB_REPOSITORY); lookups skipped")
    else:
        client = closer = _delivery_client(args)
    try:
        context, ctx_notes = resolve_context(summary, client=client, repo=repo or "", now=now)
        notes.extend(ctx_notes)
        plan = route(context, inputs)
        notes.extend(plan.notes)
        open_issue = should_open_issue(context, summary, inputs)
        state = None
        if context.trigger in {"push_default", "tag"}:
            state = default_branch_state(
                summary,
                client=client,
                repo=repo or "",
                default_branch=context.default_branch,
                now=now,
            )
            if state.source != "run_list":
                notes.append(f"default-branch state from {state.source}")
        sev = severity(context, state, summary)
        outcome: dict[str, Any] = {
            "trigger": context.trigger,
            "severity": sev,
            "context": context,
            "plan": plan,
            "open_issue": open_issue,
            "state": state,
            "live": live,
            "delivered_to": [],
        }
        _resolve_ownership(
            outcome,
            summary=summary,
            record=record,
            inputs=inputs,
            client=client,
            repo=repo or "",
            online=not isinstance(client, _OfflineClient),
            budget=budget,
            notes=notes,
        )
        # The issue goes first so the comment footer can say "tracked in #N".
        _deliver_issue(
            outcome,
            summary=summary,
            record=record,
            inputs=inputs,
            client=client,
            repo=repo or "",
            online=not isinstance(client, _OfflineClient),
            now=now,
            budget=budget,
            notes=notes,
            errors=errors,
        )
        try:
            _deliver_comment(
                outcome,
                summary=summary,
                record=record,
                inputs=inputs,
                client=client,
                repo=repo or "",
                online=not isinstance(client, _OfflineClient),
                now=now,
                budget=budget,
                notes=notes,
                errors=errors,
            )
            # Lowest-priority write: after the issue and the comment, before housekeeping.
            _deliver_notifications(
                outcome,
                summary=summary,
                record=record,
                inputs=inputs,
                client=client,
                repo=repo or "",
                online=not isinstance(client, _OfflineClient),
                now=now,
                budget=budget,
                notes=notes,
                errors=errors,
            )
        finally:
            # Housekeeping only after the comment, so it can never starve it (F1).
            _finish_issue(outcome, now=now, notes=notes, errors=errors)
    finally:
        if closer is not None:
            closer.close()
    return outcome


def _resolve_ownership(
    outcome: dict[str, Any],
    *,
    summary: Summary,
    record: Any,
    inputs: Any,
    client: Any,
    repo: str,
    online: bool,
    budget: Any,
    notes: list[str],
) -> None:
    """Who owns this failure (v1.3 §9). CODEOWNERS is read at the default branch."""
    from .deliver.owners import parse_codeowners, resolve_owner
    from .deliver.owners_io import load_codeowners, resolve_workspace

    context = outcome["context"]
    rules = None
    try:
        if online:
            text, load_notes = load_codeowners(
                client, repo, context.default_branch, budget=budget, clock=_delivery_clock
            )
            notes.extend(load_notes)
            if text is not None:
                rules, parse_notes = parse_codeowners(text)
                notes.extend(parse_notes)
        else:
            notes.append("CODEOWNERS not read (no API access)")
        resolution = resolve_owner(
            summary, record, context, inputs, rules, workspace=resolve_workspace(os.environ)
        )
    except Exception as exc:  # noqa: BLE001 — ownership never blocks delivery
        _LOG.exception("owner resolution failed")
        notes.append(f"owner resolution failed: {_first_line(exc)}")
        outcome.update(owners=[], owner_resolved_by="none")
        return
    notes.extend(resolution.notes)
    outcome.update(owners=resolution.owners, owner_resolved_by=resolution.resolved_by)


def _deliver_issue(
    outcome: dict[str, Any],
    *,
    summary: Summary,
    record: Any,
    inputs: Any,
    client: Any,
    repo: str,
    online: bool,
    now: datetime,
    budget: Any,
    notes: list[str],
    errors: list[str],
) -> None:
    """Open / maintain the fingerprint issue (v1.3 §8.2). Never blocks the comment."""
    from .deliver.issues import (
        IssuesHistoryStore,
        default_labels,
        record_from_summary,
        recurrence_comment,
    )
    from .deliver.render_comment import card_headline
    from .deliver.suppress import evaluate

    context = outcome["context"]
    live = outcome["live"]
    decision = evaluate(summary, record, context, inputs, existing=None, now=now)
    if not outcome.get("open_issue"):
        outcome["issue_action"] = "none (policy)"
        return
    if decision.suppressed_by is not None:
        outcome["issue_action"] = f"none (suppressed by {decision.suppressed_by})"
        return
    if not online:
        outcome["issue_action"] = "would create or update (not looked up)"
        return
    store = None
    try:
        headline, fix = card_headline(summary, record, decision)
        failure = record_from_summary(summary, headline, now)
        store = IssuesHistoryStore(
            client, repo, budget=budget, clock=_delivery_clock, dry_run=not live
        )
        cls = summary.classification
        result = store.sync(
            failure,
            labels=default_labels(
                cls.category, infra=cls.is_infra_vs_code == "infra", flaky=bool(cls.is_flaky)
            ),
            fix=fix,
            run_url=summary.run.html_url,
            recurrence=recurrence_comment(summary, fork=context.is_fork),
            fork=context.is_fork,
            owners=list(outcome.get("owners", [])),
        )
        outcome["issue_action"] = result.action
        outcome["issue_previous_updated_at"] = result.previous_updated_at
        if result.number is not None and result.action not in {"failed", "skipped"}:
            outcome["issue_ref"] = result.number
            outcome["issue_url"] = result.url
        if live:
            outcome["delivered_to"].extend(result.delivered)
    except Exception as exc:  # noqa: BLE001 — an issue failure never blocks the comment
        _LOG.exception("issue delivery failed")
        errors.append(f"issue delivery failed: {_first_line(exc)}")
        outcome["issue_action"] = "failed"
        outcome["issue_write_failed"] = True
    finally:
        if store is not None:
            outcome["_issue_store"] = store  # swept + flushed after the comment (F1)
            outcome["issue_write_failed"] = outcome.get("issue_write_failed") or store.write_failed


def _deliver_notifications(
    outcome: dict[str, Any],
    *,
    summary: Summary,
    record: Any,
    inputs: Any,
    client: Any,
    repo: str,
    online: bool,
    now: datetime,
    budget: Any,
    notes: list[str],
    errors: list[str],
) -> None:
    """Email + chat (v1.3 §10). Never raises; never blocks the issue or the comment."""
    from .deliver.incidents import coordinate_incident
    from .deliver.notify import (
        ChatSender,
        SmtpSender,
        notify_config,
        plan_notification,
        platform_path,
        summarize_burst,
    )
    from .deliver.sticky import BUDGET_EXCEEDED
    from .deliver.suppress import evaluate

    config = notify_config(inputs, os.environ)
    scrub = config.scrubber()
    status: dict[str, str] = {}
    outcome["notify_status"] = status
    try:
        context = outcome["context"]
        live = bool(outcome.get("live"))
        decision = outcome.get("decision") or evaluate(
            summary, record, context, inputs, existing=None, now=now
        )
        platform = platform_path(decision)
        burst = None
        if config.configured and platform is not None and online:
            try:  # informational only: names the workflows / branches in the burst
                runs = client.list_runs(repo, status="failure", per_page=100)
                burst = summarize_burst(
                    runs,
                    run_id=summary.run.run_id,
                    workflow=summary.run.workflow_name,
                    branch=summary.run.head_branch,
                    now=now,
                    window_minutes=inputs.quiet_window_minutes,
                )
            except Exception as exc:  # noqa: BLE001
                notes.append(f"burst summary unavailable ({type(exc).__name__})")
        plan = plan_notification(
            config,
            summary=summary,
            record=record,
            decision=decision,
            context=context,
            route_notify=list(outcome["plan"].notify),
            severity=outcome["severity"],
            inputs=inputs,
            owners=list(outcome.get("owners", [])),
            owner_resolved_by=outcome.get("owner_resolved_by"),
            issue_url=outcome.get("issue_url"),
            previous_updated_at=outcome.get("issue_previous_updated_at"),
            now=now,
            burst=burst,
        )
        outcome["notify_plan"] = plan
        notes.extend(scrub(n) for n in plan.notes)
        if plan.send and plan.platform is not None:
            # One message per burst: coordinated through a platform-incident issue.
            if not online:
                outcome["incident_line"] = "not checked (no API access)"
            else:
                incident = coordinate_incident(
                    client,
                    repo,
                    kind="infra_widespread" if plan.platform == "A2" else "infra_runner",
                    summary=summary,
                    workflows=burst.workflows if burst else [summary.run.workflow_name],
                    branches=burst.branches if burst else [summary.run.head_branch],
                    now=now,
                    window_minutes=inputs.quiet_window_minutes,
                    live=live,
                    enabled=bool(inputs.create_issues),
                    budget=budget,
                    clock=_delivery_clock,
                )
                notes.extend(incident.notes)
                errors.extend(incident.errors)
                outcome["incident_line"] = (
                    f"#{incident.number}" if incident.number is not None else "none"
                ) + f" ({incident.reason})"
                if not incident.send:
                    plan.send = False
                    plan.reason = incident.reason
        if not plan.send:
            for channel, url in (("chat", config.chat_url), ("email", config.smtp_url)):
                if url:
                    status[channel] = f"skipped ({plan.reason})"
            return

        failed = False
        if config.chat_url:
            chat = ChatSender(
                config.chat_url, payload_field=config.payload_field, transport=_notify_chat_transport
            )
            problem = chat.config_error()
            if problem:
                status["chat"] = "skipped (configuration)"
                notes.append(problem)
            elif not live:
                status["chat"] = "would send"
            else:
                timeout = _send_timeout(budget)
                if timeout <= 0:
                    status["chat"] = "skipped (budget)"
                    errors.append(f"chat skipped: {BUDGET_EXCEEDED}")
                else:
                    sent = chat.send(plan.chat_text, timeout=timeout)
                    if sent.ok:
                        status["chat"] = "sent"
                        outcome["delivered_to"].append("chat")
                    else:
                        status["chat"] = "failed"
                        errors.append(scrub(sent.detail))
                        failed = True
        if config.smtp_url:
            smtp = SmtpSender(config.smtp_url, smtp_factory=_notify_smtp_factory)
            problem = smtp.config_error()
            notes.extend(smtp.config_notes())
            if problem:
                status["email"] = "skipped (configuration)"
                notes.append(problem)
            elif not plan.emails:
                status["email"] = "skipped (no email recipients)"
            elif not live:
                status["email"] = f"would send to {', '.join(plan.emails)}"
            else:
                timeout = _send_timeout(budget)
                if timeout <= 0:
                    status["email"] = "skipped (budget)"
                    errors.append(f"email skipped: {BUDGET_EXCEEDED}")
                else:
                    message = smtp.message(plan.subject, plan.email_body, plan.emails)
                    sent = smtp.send(message, timeout=timeout)
                    if sent.ok:
                        status["email"] = f"sent to {', '.join(plan.emails)}"
                        outcome["delivered_to"].append("email")
                    else:
                        status["email"] = "failed"
                        errors.append(scrub(sent.detail))
                        failed = True
        _settle_delivery_error(outcome, notify_failed=failed)
    except Exception as exc:  # noqa: BLE001
        _LOG.error("notification delivery failed: %s", type(exc).__name__)
        errors.append(scrub(f"notification delivery failed: {_first_line(exc)}"))


def _send_timeout(budget: Any) -> float:
    """min(10s, what is left of the delivery budget)."""
    from .deliver.notify import SEND_TIMEOUT_S

    remaining = budget.seconds - (_delivery_clock() - budget.started_at)
    return max(0.0, min(SEND_TIMEOUT_S, remaining))


def _settle_delivery_error(outcome: dict[str, Any], *, notify_failed: bool) -> None:
    """A notification failure counts only when it was the only channel attempted."""
    if not outcome.get("live"):
        return
    delivered = outcome["delivered_to"]
    if outcome.get("suppressed_by") == "delivery_error" and delivered:
        outcome["suppressed_by"] = None  # something landed after all
        return
    issue_action = str(outcome.get("issue_action") or "")
    issue_attempted = bool(issue_action) and not issue_action.startswith(("none", "would"))
    other_attempted = bool(outcome.get("comment_action")) or issue_attempted
    if notify_failed and not delivered and not other_attempted and outcome.get("suppressed_by") is None:
        outcome["suppressed_by"] = "delivery_error"


def _notification_lines(outcome: dict[str, Any]) -> list[str]:
    plan = outcome.get("notify_plan")
    status = outcome.get("notify_status") or {}
    lines = ["", "## Notifications", ""]
    if plan is None or plan.reason == "none configured":
        return lines + ["- notifications: none configured"]
    lines.append(f"- decision: {'send' if plan.send else 'skip'} ({plan.reason})")
    if plan.platform:
        lines.append(f"- platform path: {plan.platform}")
    if outcome.get("incident_line"):
        lines.append(f"- platform incident: {outcome['incident_line']}")
    for channel in ("chat", "email"):
        if channel in status:
            lines.append(f"- {channel}: {status[channel]}")
    if plan.send:
        lines.append(f"- audience: {', '.join(plan.audience) or 'none'}")
        lines.append(f"- email subject: {plan.subject}")
    return lines


def _finish_issue(
    outcome: dict[str, Any], *, now: datetime, notes: list[str], errors: list[str]
) -> None:
    """Stale sweep + index flush, after the comment and inside the budget. Never raises."""
    store = outcome.pop("_issue_store", None)
    if store is None:
        return
    try:
        if outcome.get("live"):
            ref = outcome.get("issue_ref")
            store.sweep_stale(now, exclude={ref} if ref else set())
    except Exception as exc:  # noqa: BLE001
        _LOG.exception("stale sweep failed")
        errors.append(f"stale sweep failed: {_first_line(exc)}")
    finally:
        store.close()
        notes.extend(store.notes)
        errors.extend(store.errors)


def _issue_line(outcome: dict[str, Any]) -> str:
    action = outcome.get("issue_action") or "none"
    ref = outcome.get("issue_ref")
    if ref is None:
        return action
    url = outcome.get("issue_url")
    return f"{action} — #{ref}" + (f" ({url})" if url else "")


_FEEDBACK_REPORT_FILE = "feedback-report.md"


def _cmd_feedback(args: argparse.Namespace) -> int:
    """``/resolved`` handler (v1.3 §12). Reads the comment from the event file only."""
    from .deliver.feedback import (
        allowed_associations,
        allowed_permissions,
        is_bot,
        parse_resolved,
        why_not_resolved,
    )
    from .deliver.issues import IssuesHistoryStore, marker_fingerprint
    from .deliver.render_comment import safe_text
    from .deliver.sticky import DeliveryBudget, fingerprint_of

    live = bool(args.live) and not args.offline and not args.dry_run
    notes: list[str] = []
    errors: list[str] = []
    actions: list[str] = []
    facts: dict[str, str] = {"mode": "LIVE" if live else ("offline" if args.offline else "dry run")}
    client = None
    try:
        event = json.loads(Path(args.event).read_text(encoding="utf-8"))
        comment = event.get("comment") or {}
        issue = event.get("issue") or {}
        user = comment.get("user") or {}
        login = str(user.get("login") or "")
        association = str(comment.get("author_association") or "NONE").upper()
        facts.update(
            author=safe_text(f"@{login}", 100),
            association=association,
            issue=f"#{issue.get('number')}" + (" (pull request)" if "pull_request" in issue else ""),
        )
        command = parse_resolved(comment.get("body"))
        repo = (
            args.repo
            or str((event.get("repository") or {}).get("full_name") or "")
            or (os.environ.get("GITHUB_REPOSITORY") or "").strip()
        )
        trusted = False
        if event.get("action") != "created":
            notes.append(f"skipped: action is {event.get('action')!r}, not 'created'")
        elif is_bot(user):
            notes.append("skipped: comment author is a bot")
        elif command is None:
            notes.append(f"skipped: {why_not_resolved(comment.get('body'))}")
        elif association in allowed_associations(os.environ):
            trusted = True
        elif args.offline or not repo:
            notes.append(
                f"skipped: author association {association} may not resolve failures "
                "(repository permission not checked)"
            )
        else:
            # Private org members show up as CONTRIBUTOR / NONE: fall back to their repo role.
            client = _delivery_client(args)
            try:
                perm = client.get_collaborator_permission(repo, login)
            except Exception as exc:  # noqa: BLE001 — never allow on error
                perm = None
                notes.append(f"repository permission lookup failed ({type(exc).__name__})")
            role = perm.role_name if perm is not None else None
            base = perm.permission if perm is not None else None
            shown = f"role {role or 'none'} / base {base or 'none'}"
            facts["permission"] = shown
            allowed = allowed_permissions(os.environ)
            # A custom role (role_name "dev-write") counts through its base permission.
            if any(value and value.lower() in allowed for value in (role, base)):
                trusted = True
            else:
                notes.append(
                    f"skipped: author association {association} and repository permission "
                    f"({shown}) may not resolve failures"
                )
        if trusted:
            facts["text"] = command.text
            facts["target"] = f"#{command.target_issue}" if command.target_issue else "(from context)"
            if args.offline:
                notes.append("offline: target not looked up; nothing applied")
            elif not repo:
                errors.append("no repository (--repo / event / GITHUB_REPOSITORY)")
            else:
                client = client or _delivery_client(args)
                store = IssuesHistoryStore(
                    client,
                    repo,
                    budget=DeliveryBudget(started_at=_delivery_clock()),
                    clock=_delivery_clock,
                    dry_run=not live,
                )
                targets = _feedback_targets(
                    client, repo, issue, command, store, notes, marker_fingerprint, fingerprint_of
                )
                handled = False
                for target in targets:
                    result = store.resolve_issue(target, text=command.text, author=login)
                    actions.append(f"#{result.number}: {result.action}")
                    handled = handled or result.action in {"resolved", "unchanged"}
                notes.extend(store.notes)
                errors.extend(store.errors)
                if handled and live and comment.get("id") is not None:
                    reacted = client.create_reaction(repo, int(comment["id"]), "+1")
                    actions.append("reaction +1: " + ("added" if reacted else "not added (best-effort)"))
                elif targets and not live:
                    actions.append("reaction +1: would add")
    except Exception as exc:  # noqa: BLE001 — never fail the workflow
        _LOG.error("feedback failed: %s", type(exc).__name__)
        errors.append(f"feedback failed: {_first_line(exc)}")
    finally:
        if client is not None:
            client.close()
    _write_feedback_report(Path(args.out), facts, actions, notes, errors)
    return 1 if (args.strict and errors) else 0


def _feedback_targets(client, repo, issue, command, store, notes, marker_fingerprint, fingerprint_of):
    """Which fingerprint issue(s) a /resolved comment applies to."""
    from .deliver.sticky import _updated_key

    number = issue.get("number")
    if marker_fingerprint(issue.get("body")):
        if command.target_issue is not None:
            notes.append(f"#{command.target_issue} ignored: the comment is on fingerprint issue #{number}")
        fresh = client.get_issue(repo, int(number))
        return [fresh] if fresh else []
    if "pull_request" not in issue:
        notes.append(f"skipped: #{number} is neither an RCA fingerprint issue nor a pull request")
        return []
    if command.target_issue is not None:
        target = client.get_issue(repo, command.target_issue)
        if target is None or "pull_request" in target or not marker_fingerprint(target.get("body")):
            notes.append(f"skipped: #{command.target_issue} is not an RCA fingerprint issue")
            return []
        return [target]
    ours = [c for c in client.list_issue_comments(repo, int(number)) if fingerprint_of(c.get("body"))]
    if not ours:
        notes.append(f"skipped: no RCA comment on pull request #{number}")
        return []
    chosen = max(ours, key=_updated_key)
    if len(ours) > 1:
        notes.append(f"{len(ours)} RCA comments on #{number}; used the most recently updated ({chosen.get('id')})")
    coarse = fingerprint_of(chosen.get("body"))
    targets = store.open_issues_for_coarse(coarse)
    if not targets:
        notes.append(f"no open fingerprint issue for the RCA comment on #{number}")
    return targets


def _write_feedback_report(
    out: Path, facts: dict[str, str], actions: list[str], notes: list[str], errors: list[str]
) -> None:
    from .redact import redact_text

    lines = [f"# RCA feedback ({facts.get('mode', 'dry run')})", ""]
    for key in ("issue", "author", "association", "permission", "target", "text"):
        if key in facts:
            lines.append(f"- {key}: {facts[key]}")
    lines += ["", "## Actions", ""] + ([f"- {a}" for a in actions] or ["- none"])
    if notes or errors:
        lines += ["", "## Notes", ""] + [f"- {n}" for n in notes] + [f"- error: {e}" for e in errors]
    text = redact_text("\n".join(lines) + "\n")[0]
    try:
        out.mkdir(parents=True, exist_ok=True)
        (out / _FEEDBACK_REPORT_FILE).write_text(text, encoding="utf-8")
    except Exception:  # noqa: BLE001
        _LOG.exception("failed to write feedback report")
    sys.stdout.write(text)


def _cmd_migrate_history(args: argparse.Namespace) -> int:
    """One-shot: create issues for cache fingerprints not yet tracked (v1.3 §8.1).

    Idempotent by fingerprint; paced; capped by --migrate-limit; read-only on the
    cache directory. Without --live it only lists what would be created.
    """
    import math

    from .deliver.issues import IssuesHistoryStore, default_labels
    from .deliver.sticky import DeliveryBudget
    from .history import CacheHistoryStore

    out = Path(args.out)
    live = bool(args.live) and not args.offline and not args.dry_run
    lines = [
        "# RCA history migration" + (" (LIVE)" if live else " (dry run)"),
        "",
    ]
    errors: list[str] = []
    try:
        root = Path(args.history_dir)
        repo = args.repo or (os.environ.get("GITHUB_REPOSITORY") or "").strip() or None
        if not root.is_dir():  # never create it: the cache store would mkdir
            lines.append(f"- history dir {root} not found; nothing to migrate")
        else:
            records = sorted(
                CacheHistoryStore(root).all_records(), key=lambda r: (r.first_seen, r.fingerprint)
            )
            lines.append(f"- cache records: {len(records)}")
            if args.offline or not repo:
                why = "offline" if args.offline else "no repository (--repo / GITHUB_REPOSITORY)"
                lines.append(f"- not looked up ({why}); nothing created")
            else:
                client = _delivery_client(args)
                store = IssuesHistoryStore(
                    client,
                    repo,
                    budget=DeliveryBudget(started_at=0.0, seconds=math.inf),  # pacing, not budget
                    clock=_delivery_clock,
                    dry_run=not live,
                )
                try:
                    created = 0
                    for rec in records:
                        short = rec.fingerprint[:12]
                        if store.get(rec.fingerprint) is not None:
                            lines.append(f"- {short}: already tracked; left untouched")
                            continue
                        if created >= args.migrate_limit:
                            lines.append(
                                f"- limit {args.migrate_limit} reached; re-run to continue"
                            )
                            break
                        if live and created > 0:
                            _migrate_sleep(_MIGRATE_PACE_S)
                        result = store.sync(rec, labels=default_labels(rec.category))
                        ref = f" #{result.number}" if result.number is not None else ""
                        lines.append(f"- {short}: {result.action}{ref}")
                        if result.action in {"created", "would create issue"}:
                            created += 1
                finally:
                    store.close()
                    client.close()
                lines += [f"- note: {n}" for n in store.notes]
                errors += store.errors
    except Exception as exc:  # noqa: BLE001 — never fail the workflow
        _LOG.exception("history migration failed")
        errors.append(f"migration failed: {_first_line(exc)}")
    lines += [f"- error: {e}" for e in errors]
    text = "\n".join(lines) + "\n"
    try:
        out.mkdir(parents=True, exist_ok=True)
        (out / _MIGRATION_REPORT_FILE).write_text(text, encoding="utf-8")
    except Exception:  # noqa: BLE001
        _LOG.exception("failed to write migration report")
    sys.stdout.write(text)
    return 1 if (args.strict and errors) else 0


def _deliver_comment(
    outcome: dict[str, Any],
    *,
    summary: Summary,
    record: Any,
    inputs: Any,
    client: Any,
    repo: str,
    online: bool,
    now: datetime,
    budget: Any,
    notes: list[str],
    errors: list[str],
) -> None:
    """Stage A → find existing → Stage B/C → render → post (live) or describe (dry run)."""
    from .deliver.render_comment import render_comment
    from .deliver.sticky import (
        BUDGET_EXCEEDED,
        CHANNELS,
        LOOKUP_FAILED,
        find_existing,
        post_or_update,
    )
    from .deliver.suppress import SUPPRESSED_DELIVERY_ERROR, evaluate

    context = outcome["context"]
    plan = outcome["plan"]
    live = outcome["live"]
    delivered: list[str] = outcome["delivered_to"]
    write_failed = False

    decision = evaluate(summary, record, context, inputs, existing=None, now=now)
    target: int | str | None = None
    if plan.comment == "pr":
        target = context.pr_number
    elif plan.comment == "commit":
        target = context.commit_sha or None

    body: str | None = None
    why_not: str | None = None
    if decision.suppressed_by is not None:
        why_not = f"suppressed by {decision.suppressed_by}"
    elif plan.comment is None:
        why_not = f"the routing plan has no comment channel ({'; '.join(plan.notes)})"
    elif target is None:
        why_not = f"no {plan.comment} comment target"
    else:
        channel = CHANNELS[plan.comment]
        existing = None
        lookup_failed = False
        if online:
            existing, found_notes = find_existing(
                client,
                repo,
                target_kind=plan.comment,
                target=target,
                fingerprint_coarse=summary.fingerprint_coarse,
                branch=context.branch,
            )
            notes.extend(found_notes)
            if existing is not None and existing.reactions is not None:
                outcome["feedback"] = dict(existing.reactions)
            lookup_failed = any(n.startswith(LOOKUP_FAILED) for n in found_notes)
        else:
            notes.append("existing comments not looked up (no API access)")
        decision = evaluate(summary, record, context, inputs, existing=existing, now=now)
        try:
            body = render_comment(
                summary, record, context, decision, issue_ref=outcome.get("issue_ref")
            )
        except Exception as exc:  # noqa: BLE001 — the preview/report still get written
            _LOG.exception("comment rendering failed")
            errors.append(f"comment rendering failed: {_first_line(exc)}")
            why_not = "comment rendering failed"
        if body is not None:
            outcome["comment_target"] = f"{channel.label} on {target}"
            if live and lookup_failed:
                # Never create blind: an existing comment we could not see would be duplicated.
                outcome["comment_action"] = "skipped"
                errors.append(f"{channel.label} skipped: existing comments could not be listed")
                write_failed = True
            elif live:
                result = post_or_update(
                    client,
                    repo,
                    target,
                    body,
                    existing,
                    target_kind=plan.comment,
                    clock=_delivery_clock,
                    budget=budget,
                )
                outcome["comment_action"] = result.action
                if result.action in {"created", "updated", "unchanged"}:
                    delivered.append(channel.delivered_as)
                    outcome["comment_url"] = result.url
                else:
                    errors.append(result.error or f"{channel.label} not delivered")
                    write_failed = write_failed or result.action == "failed"
            elif existing is None:
                outcome["comment_action"] = "would create" + ("" if online else " (not looked up)")
            elif existing.body == body:
                outcome["comment_action"] = "unchanged"
                outcome["comment_url"] = existing.html_url
            else:
                outcome["comment_action"] = "would edit"
                outcome["comment_url"] = existing.html_url

    if decision.add_flaky_label and context.pr_number is not None:
        if not live:
            outcome["label_action"] = f"would add `{_FLAKY_LABEL}`"
        elif budget.exceeded(_delivery_clock()):
            outcome["label_action"] = "skipped"
            errors.append(f"label {_FLAKY_LABEL} skipped: {BUDGET_EXCEEDED}")
        else:
            try:
                client.add_labels(repo, context.pr_number, [_FLAKY_LABEL])
                delivered.append(f"label:{_FLAKY_LABEL}")
                outcome["label_action"] = f"added `{_FLAKY_LABEL}`"
            except Exception as exc:  # noqa: BLE001 — best-effort, recorded
                outcome["label_action"] = "failed"
                scope = " (the token needs `pull-requests: write`)" if getattr(
                    exc, "status_code", None
                ) == 403 else ""
                errors.append(f"label {_FLAKY_LABEL} failed: {_first_line(exc)}{scope}")
                write_failed = True

    suppressed_by = decision.suppressed_by
    write_failed = write_failed or bool(outcome.get("issue_write_failed"))
    if suppressed_by is None and live and write_failed and not delivered:
        suppressed_by = SUPPRESSED_DELIVERY_ERROR  # A5: a write was attempted, nothing landed
    outcome.update(
        suppressed_by=suppressed_by, decision=decision, body=body, why_not=why_not
    )


def _delivery_preview(report: Any, outcome: dict[str, Any]) -> str:
    """Header (redacted) + the comment body verbatim between markers.

    Written in both modes: a preview in a dry run, an audit trail when live. The
    body between the markers is byte-identical to what is (or would be) posted.
    """
    from .redact import redact_text

    live = bool(outcome.get("live"))
    plan = outcome.get("plan")
    decision = outcome.get("decision")
    context = outcome.get("context")
    state = outcome.get("state")
    lines = [
        "# RCA delivery report (LIVE)" if live else "# RCA delivery preview (dry run)",
        "",
        "Live delivery: the GitHub writes below were made."
        if live
        else "No GitHub writes were made. This is what delivery would do for this run.",
        "",
        f"- trigger: `{report.trigger}`",
        f"- severity: `{report.severity}`",
        f"- suppressed_by: `{report.suppressed_by or 'none'}`",
        f"- delivered_to: {', '.join(report.delivered_to) or 'none'}",
    ]
    if context is not None:
        pr = f"#{context.pr_number}" if context.pr_number is not None else "none"
        lines.append(
            f"- branch: `{context.branch}` (default `{context.default_branch}`) · PR: {pr}"
            f" · fork: {str(context.is_fork).lower()} · draft: {str(context.pr_is_draft).lower()}"
        )
    if plan is not None:
        if outcome.get("body") is not None:
            comment = f"{outcome.get('comment_target')} → {outcome.get('comment_action')}"
            if outcome.get("comment_url"):
                comment += f" ({outcome['comment_url']})"
        else:
            comment = f"{plan.comment or 'none'} — not posted: {outcome.get('why_not')}"
        issue = "yes" if outcome.get("open_issue") else "no"
        lines += [
            "",
            "## Route plan",
            "",
            "- job summary: yes (written by collect)",
            f"- comment: {comment}",
            f"- issue policy: {plan.issue or 'none'} · would open an issue: {issue}",
            f"- issue: {_issue_line(outcome)}",
            f"- owner: {', '.join(outcome.get('owners') or []) or 'none'} "
            f"(resolved by {outcome.get('owner_resolved_by') or 'none'})",
            f"- notify intent: {', '.join(plan.notify) or 'none'}",
        ]
        if outcome.get("feedback") is not None:
            fb = outcome["feedback"]
            lines.append(f"- feedback: 👍 {fb.get('up', 0)} · 👎 {fb.get('down', 0)}")
        if outcome.get("label_action"):
            lines.append(f"- label: {outcome['label_action']}")
    if decision is not None:
        lines += [
            "",
            "## Modifiers",
            "",
            f"- dedupe: {decision.dedupe or 'none'}",
            f"- unverified banner: {str(decision.unverified_banner).lower()}",
            f"- omit root cause: {str(decision.omit_root_cause).lower()}",
            f"- ci:flaky label: {str(decision.add_flaky_label).lower()}",
            f"- platform notification once: {str(decision.notify_platform_once).lower()}",
        ]
        lines += [f"- {reason}" for reason in decision.reasons]
    if state is not None:
        lines += [
            "",
            "## Default-branch state",
            "",
            f"- source: {state.source} · consecutive failures: {state.consecutive_failures}"
            f" · red for: {_hours(state.red_duration_hours)} · last green: {state.last_green_sha}",
        ]
    if report.notes or report.errors:
        lines += ["", "## Notes", ""]
        lines += [f"- {note}" for note in report.notes]
        lines += [f"- error: {err}" for err in report.errors]
    if context is not None:
        lines += _notification_lines(outcome)
    header = "\n".join(lines) + "\n"
    header, _ = redact_text(header)
    notify_plan = outcome.get("notify_plan")
    if notify_plan is not None and notify_plan.send and notify_plan.chat_text:
        # Verbatim: byte-identical to the JSON field value POSTed to the webhook.
        header += f"\n## Chat text\n\n{CHAT_BEGIN}\n{notify_plan.chat_text}\n{CHAT_END}\n"

    body = outcome.get("body")
    if body is None:
        verb = "No comment was posted" if live else "No comment would be posted"
        tail = (
            "\n## Comment body\n\n"
            f"{verb}: {outcome.get('why_not') or 'delivery could not be planned'}.\n"
        )
        return header + redact_text(tail)[0]
    return header + "\n## Comment body\n\n" + f"{COMMENT_BEGIN}\n{body}{COMMENT_END}\n"


def _hours(value: float | None) -> str:
    return "unknown" if value is None else f"{value:.1f}h"


def _write_back_delivery(summary_path: Path, out: Path, report: Any) -> None:
    """Set only the ``delivery`` key (v1.3 §15). Never regenerate, never drop ``analysis``.

    Like ``analyze``: when --out is elsewhere, the input is copied to
    <out>/summary.json first and only that copy is updated.
    """
    dest = out / "summary.json"
    src = Path(summary_path)
    if src.resolve() != dest.resolve():
        dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    payload = json.loads(dest.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("summary.json is not an object")
    payload["delivery"] = report.model_dump(mode="json")
    dest.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _as_flag(value: object, *, default: bool) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() not in {"false", "0", "no", "off", ""}


def _append_run_telemetry(args: argparse.Namespace, summary: Summary, record: Any) -> None:
    """Best-effort per-run telemetry line. Never raises into the analyze step."""
    try:
        from .analyze import display_status
        from .diagnose import terminal_cause_present
        from .outputs import analyze_decision
        from .telemetry import append_telemetry

        result = record.result
        diag = summary.diagnosis
        hist = summary.history
        rec = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "fingerprint": summary.fingerprint,
            "fingerprint_coarse": summary.fingerprint_coarse,
            "category": summary.classification.category,
            "confidence": summary.classification.confidence,
            "is_infra_vs_code": summary.classification.is_infra_vs_code,
            "short_circuit": summary.verdict.short_circuit,
            "requires_analysis": bool(summary.verdict.requires_analysis),
            "rule_id": diag.rule_id if diag is not None else None,
            "terminal_cause_present": bool(terminal_cause_present(summary)),
            "suspected_stage": diag.suspected_stage if diag is not None else None,
            "recurrence": hist.match if hist is not None else "new",
            "seen_count": hist.seen_count if hist is not None else 0,
            "model_called": bool(record.model_called),
            "analyze_decision": analyze_decision(record),
            "analysis_status": record.status,
            "display_status": display_status(record),
            "grounded": record.grounded,
            "source": result.source if result is not None else None,
            "rca_confidence": result.confidence if result is not None else None,
            "collector_version": summary.collector_version,
            "prompt_version": record.prompt_version,
        }
        append_telemetry(getattr(args, "history_dir", ".rca-history"), rec)
    except Exception:  # noqa: BLE001 — telemetry is best-effort
        _LOG.debug("telemetry skipped", exc_info=True)


def _cmd_telemetry(args: argparse.Namespace) -> int:
    from .telemetry import aggregate, format_summary, read_telemetry

    records = read_telemetry(getattr(args, "history_dir", ".rca-history"))
    text = format_summary(aggregate(records), getattr(args, "format", "json"))
    out = getattr(args, "out", None)
    if out:
        dest = Path(out)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


def _emit_failed_analysis(args: argparse.Namespace, note: str) -> None:
    from .analyze import write_analysis
    from .models import AnalysisRecord
    from .outputs import write_analysis_github_output

    out = Path(getattr(args, "out", None) or "rca")
    summary_path = Path(getattr(args, "summary", None) or (out / "summary.json"))
    record = AnalysisRecord(
        status="failed",
        notes=[note],
        analyzed_at=datetime.now(timezone.utc),
    )
    try:
        write_analysis(record, summary_path=summary_path, out_dir=out)
        write_analysis_github_output(record, summary=_summary_if_readable(summary_path))
    except Exception:
        out.mkdir(parents=True, exist_ok=True)
        (out / "analysis.json").write_text(
            record.model_dump_json(indent=2) + "\n", encoding="utf-8"
        )
        try:
            write_analysis_github_output(
                record, summary=_summary_if_readable(summary_path)
            )
        except Exception:
            _LOG.exception("failed to write analysis GITHUB_OUTPUT")


def _cmd_refingerprint(args: argparse.Namespace) -> int:
    from .history import CacheHistoryStore

    store = CacheHistoryStore(args.history_dir)
    ini = args.drain_config or str(default_config_path())
    records = store.all_records()
    changed = 0
    for rec in records:
        remasked = remask_templates(rec.templates, config_path=ini)
        new_hashes = [template_hash(item) for item in remasked]
        t1 = remasked[0] if remasked else ""
        new_fine = fingerprint_fine(remasked)
        new_coarse = fingerprint_coarse(t1 or None)
        new_mask = masking_config_hash(ini)
        differs = (
            new_fine != rec.fingerprint
            or new_coarse != rec.fingerprint_coarse
            or new_mask != rec.masking_config_hash
        )
        if not differs:
            continue
        changed += 1
        if args.dry_run:
            print(
                f"would change {rec.fingerprint} -> {new_fine} "
                f"(coarse {rec.fingerprint_coarse} -> {new_coarse})"
            )
            continue
        updated = rec.model_copy(
            update={
                "fingerprint": new_fine,
                "fingerprint_coarse": new_coarse,
                "templates": remasked,
                "template_hashes": new_hashes,
                "masking_config_hash": new_mask,
            }
        )
        old_path = store._path(rec.fingerprint)
        if old_path.exists() and rec.fingerprint != new_fine:
            old_path.unlink()
        store._path(new_fine).write_text(
            updated.model_dump_json(indent=2) + "\n", encoding="utf-8"
        )
    print(f"{changed} of {len(records)} records {'would change' if args.dry_run else 'updated'}")
    store.close()
    return 0


def _fetch_train(
    repo: str,
    run_id: int,
    *,
    api_url: str | None,
    token: str | None,
) -> dict[str, Any]:
    """Load a successful run and every job log needed to train Drain3."""
    notes: list[str] = []
    with GitHubClient(api_url=api_url, token=token) as client:
        run = client.get_run(repo, run_id)
        jobs = client.list_jobs(repo, run_id)
        logs: dict[int, str | None] = {}
        for job in jobs:
            if job.get("conclusion") != "success":
                continue
            job_id = job.get("id")
            if job_id is None:
                continue
            logs[int(job_id)] = client.get_job_log(repo, int(job_id))
        notes.extend(client.collection_notes)
        return {
            "repo": repo,
            "run": run,
            "jobs": jobs,
            "logs": logs,
            "notes": notes,
            "from_fixture": False,
        }


def _fetch_live(
    repo: str,
    run_id: int,
    *,
    api_url: str | None,
    token: str | None,
    analyze_pipeline: bool = True,
    max_pipeline: int = MAX_PIPELINE_LOG_ARTIFACTS,
) -> dict[str, Any]:
    notes: list[str] = []
    with GitHubClient(api_url=api_url, token=token) as client:
        run = client.get_run(repo, run_id)
        jobs = client.list_jobs(repo, run_id)
        pulls: list[dict[str, Any]] = []
        sha = run.get("head_sha")
        if sha and not run.get("pull_requests"):
            try:
                pulls = client.list_commit_pulls(repo, sha)
            except GitHubAPIError as exc:
                notes.append(f"commit pulls unavailable: {exc}")
        logs: dict[int, str | None] = {}
        for job in jobs:
            if job.get("conclusion") not in _NON_SUCCESS_CONCLUSIONS:
                continue
            job_id = int(job["id"])
            logs[job_id] = client.get_job_log(repo, job_id)
        same_sha: list[dict[str, Any]] = []
        if sha:
            try:
                same_sha = client.list_runs(repo, head_sha=str(sha))
            except GitHubAPIError as exc:
                notes.append(f"same-SHA runs unavailable: {exc}")
        _enrich_same_sha_jobs(client, repo, current=run, same_sha=same_sha, notes=notes)
        branch_runs: list[dict[str, Any]] = []
        last_success: dict[str, Any] | None = None
        pull_obj: dict[str, Any] | None = None
        compare_obj: dict[str, Any] | None = None
        optional = client.optional_collection_allowed
        workflow_id = run.get("workflow_id")
        branch = run.get("head_branch")
        if optional and workflow_id and branch:
            try:
                successes = client.list_runs(
                    repo,
                    workflow_id=workflow_id,
                    branch=str(branch),
                    status="success",
                    per_page=1,
                )
                last_success = successes[0] if successes else None
            except GitHubAPIError as exc:
                notes.append(f"last success unavailable: {exc}")
            try:
                branch_runs = client.list_runs(
                    repo,
                    workflow_id=workflow_id,
                    branch=str(branch),
                    per_page=10,
                )
            except GitHubAPIError as exc:
                notes.append(f"branch runs unavailable: {exc}")
        elif not optional:
            notes.append("skipped branch history (rate limit)")
        prs = run.get("pull_requests") or pulls
        pr_num = prs[0].get("number") if prs else None
        if optional and pr_num:
            try:
                pull_obj = client.get_pull(repo, int(pr_num))
            except GitHubAPIError as exc:
                notes.append(f"pull unavailable: {exc}")
        merge_base = None
        if pull_obj and isinstance(pull_obj.get("base"), dict):
            merge_base = pull_obj["base"].get("sha")
        hist_sha = (last_success or {}).get("head_sha") if last_success else None
        if last_success and last_success.get("id") == run.get("id"):
            hist_sha = None
            last_success = None
        base_sha, _basis = resolve_compare_base(hist_sha, merge_base)
        if optional and base_sha and sha:
            try:
                compare_obj = client.compare(repo, str(base_sha), str(sha))
            except GitHubAPIError as exc:
                notes.append(f"compare unavailable: {exc}")
        elif not optional:
            notes.append("skipped compare (rate limit)")
        failed_stage = _failed_stage_from_jobs(jobs)
        artifact_infos, junit_report, pipeline_zips, green_zips = _collect_artifacts_live(
            client,
            repo,
            run_id,
            notes,
            optional=optional,
            last_success_run_id=(last_success or {}).get("id"),
            failed_stage=failed_stage,
            max_pipeline=max_pipeline,
            analyze_pipeline=analyze_pipeline,
        )
        notes.extend(client.collection_notes)
        return {
            "repo": repo,
            "run": run,
            "jobs": jobs,
            "pulls": pulls,
            "logs": logs,
            "same_sha_runs": same_sha,
            "branch_runs": branch_runs,
            "last_success": last_success,
            "compare": compare_obj,
            "pull": pull_obj,
            "artifacts": artifact_infos,
            "junit": junit_report,
            "pipeline_zips": pipeline_zips,
            "green_pipeline_zips": green_zips,
            "from_fixture": False,
            "notes": notes,
        }


def _load_fixture(path: Path) -> dict[str, Any]:
    run = json.loads((path / "run.json").read_text(encoding="utf-8"))
    jobs_payload = json.loads((path / "jobs.json").read_text(encoding="utf-8"))
    jobs = jobs_payload["jobs"] if isinstance(jobs_payload, dict) else jobs_payload
    pulls: list[dict[str, Any]] = []
    pulls_path = path / "pulls.json"
    if pulls_path.exists():
        loaded = json.loads(pulls_path.read_text(encoding="utf-8"))
        if isinstance(loaded, list):
            pulls = loaded
    meta: dict[str, Any] = {}
    meta_path = path / "meta.json"
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    same_sha: list[dict[str, Any]] = []
    same_path = path / "same_sha_runs.json"
    if same_path.exists():
        loaded_runs = json.loads(same_path.read_text(encoding="utf-8"))
        if isinstance(loaded_runs, dict):
            same_sha = list(loaded_runs.get("workflow_runs") or [])
        elif isinstance(loaded_runs, list):
            same_sha = loaded_runs
    logs: dict[int, str | None] = {}
    logs_dir = path / "logs"
    if logs_dir.is_dir():
        for file in logs_dir.iterdir():
            if not file.is_file():
                continue
            stem = file.stem
            if not stem.isdigit():
                continue
            if file.suffix == ".unavailable":
                logs[int(stem)] = None
            else:
                logs[int(stem)] = file.read_text(encoding="utf-8-sig")
    artifact_infos, junit_report = _load_fixture_artifacts(path)
    pipeline_zips, green_zips = _load_fixture_pipeline_zips(path)
    return {
        "repo": meta.get("repo"),
        "run": run,
        "jobs": jobs,
        "pulls": pulls,
        "logs": logs,
        "same_sha_runs": same_sha,
        "branch_runs": _optional_runs(path / "branch_runs.json"),
        "last_success": _optional_obj(path / "last_success.json"),
        "compare": _optional_obj(path / "compare.json"),
        "pull": _optional_obj(path / "pull.json"),
        "artifacts": artifact_infos,
        "junit": junit_report,
        "pipeline_zips": pipeline_zips,
        "green_pipeline_zips": green_zips,
        "from_fixture": True,
        "notes": [
            "replayed from fixture (no network)",
        ],
    }


def _optional_obj(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    loaded = json.loads(path.read_text(encoding="utf-8"))
    return loaded if isinstance(loaded, dict) else None


def _optional_runs(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(loaded, dict):
        return list(loaded.get("workflow_runs") or [])
    if isinstance(loaded, list):
        return loaded
    return []


def _enrich_same_sha_jobs(
    client: GitHubClient,
    repo: str,
    *,
    current: dict[str, Any],
    same_sha: list[dict[str, Any]],
    notes: list[str],
) -> None:
    workflow = str(current.get("name") or "")
    current_id = current.get("id")
    for item in same_sha:
        if item.get("id") == current_id:
            continue
        if item.get("conclusion") != "success":
            continue
        if str(item.get("name") or "") != workflow:
            continue
        if not client.optional_collection_allowed:
            notes.append("skipped same-SHA job lookup (rate limit)")
            return
        run_id = item.get("id")
        if run_id is None:
            continue
        try:
            item["jobs"] = client.list_jobs(repo, int(run_id))
        except GitHubAPIError as exc:
            notes.append(f"same-SHA jobs unavailable for run {run_id}: {exc}")


def _artifact_info(raw: Mapping[str, Any], *, parsed: bool = False) -> ArtifactInfo:
    from .pipeline_logs import match_pipeline_artifact_name

    size = raw.get("size_bytes")
    if size is None:
        size = raw.get("size_in_bytes") or 0
    name = str(raw.get("name") or "artifact")
    kind = "other"
    stage = None
    matched = match_pipeline_artifact_name(name)
    if matched is not None:
        kind, stage = matched
    elif artifact_looks_like_junit(name):
        kind = "junit"
    skipped = None
    if int(size or 0) > ARTIFACT_DOWNLOAD_MAX_BYTES:
        skipped = f"size {size} > {ARTIFACT_DOWNLOAD_MAX_BYTES}"
    return ArtifactInfo(
        name=name,
        size_bytes=int(size or 0),
        expired=bool(raw.get("expired")),
        parsed=parsed,
        kind=kind,  # type: ignore[arg-type]
        stage=stage,
        skipped_reason=skipped,
    )


def _collect_artifacts_live(
    client: GitHubClient,
    repo: str,
    run_id: int,
    notes: list[str],
    *,
    optional: bool,
    last_success_run_id: int | None = None,
    failed_stage: str | None = None,
    max_pipeline: int = MAX_PIPELINE_LOG_ARTIFACTS,
    analyze_pipeline: bool = True,
) -> tuple[
    list[ArtifactInfo],
    JUnitReport | None,
    list[tuple[str, str | None, bytes]],
    dict[str, bytes],
]:
    if not optional:
        notes.append("skipped artifacts (rate limit)")
        return [], None, [], {}
    try:
        raw_artifacts = client.list_artifacts(repo, run_id)
    except GitHubAPIError as exc:
        notes.append(f"artifacts unavailable: {exc}")
        return [], None, [], {}
    from .pipeline_logs import match_pipeline_artifact_name, rank_pipeline_artifacts

    # TODO(7e): optional, flag-guarded (RCA_GATE_PIPELINE_DOWNLOAD, default off) —
    # when a cheap job-log-only pre-verdict already yields a high-confidence
    # signature WITH a terminal cause at the failed step, and the failed step is
    # not itself a docker/pipeline step, skip downloading pipeline-log (not
    # JUnit) zips here and record a collection_notes line. Deferred to keep the
    # collect ordering low-risk; JUnit download and current behaviour unchanged.
    infos: list[ArtifactInfo] = []
    reports: list[JUnitReport | None] = []
    pipeline_zips: list[tuple[str, str | None, bytes]] = []
    pipeline_names = rank_pipeline_artifacts(
        [
            str(raw.get("name") or "")
            for raw in raw_artifacts
            if match_pipeline_artifact_name(str(raw.get("name") or ""))
        ],
        failed_stage=failed_stage,
    )[: max(0, max_pipeline if analyze_pipeline else 0)]
    raw_by_name = {str(raw.get("name") or ""): raw for raw in raw_artifacts}
    for raw in raw_artifacts:
        size = int(raw.get("size_in_bytes") or raw.get("size_bytes") or 0)
        name = str(raw.get("name") or "artifact")
        expired = bool(raw.get("expired"))
        parsed = False
        skipped = None
        kind, stage = "other", None
        matched = match_pipeline_artifact_name(name)
        if matched is not None:
            kind, stage = matched
        elif artifact_looks_like_junit(name):
            kind = "junit"
        if size > ARTIFACT_DOWNLOAD_MAX_BYTES:
            skipped = f"size {size} > {ARTIFACT_DOWNLOAD_MAX_BYTES}"
            notes.append(
                f"skipped artifact {name!r} ({size} bytes > {ARTIFACT_DOWNLOAD_MAX_BYTES} bytes)"
            )
        elif (
            not expired
            and artifact_looks_like_junit(name)
            and client.optional_collection_allowed
            and raw.get("id") is not None
        ):
            blob = client.download_artifact_zip(repo, int(raw["id"]))
            if blob:
                report = parse_junit_from_zip(blob, source_artifact=name)
                if report is not None:
                    reports.append(report)
                    parsed = True
        infos.append(
            ArtifactInfo(
                name=name,
                size_bytes=size,
                expired=expired,
                parsed=parsed,
                kind=kind,  # type: ignore[arg-type]
                stage=stage,
                skipped_reason=skipped,
            )
        )
    for name in pipeline_names:
        raw = raw_by_name.get(name)
        if raw is None or raw.get("id") is None or raw.get("expired"):
            continue
        size = int(raw.get("size_in_bytes") or raw.get("size_bytes") or 0)
        if size > ARTIFACT_DOWNLOAD_MAX_BYTES:
            continue
        if not client.optional_collection_allowed:
            notes.append("skipped remaining pipeline-log artifacts (rate limit)")
            break
        blob = client.download_artifact_zip(repo, int(raw["id"]))
        if not blob:
            continue
        matched = match_pipeline_artifact_name(name)
        stage = matched[1] if matched else None
        pipeline_zips.append((name, stage, blob))
        for item in infos:
            if item.name == name:
                item.parsed = True
    green_zips: dict[str, bytes] = {}
    remaining = max(0, max_pipeline - len(pipeline_zips))
    if (
        remaining > 0
        and last_success_run_id
        and pipeline_zips
        and client.optional_collection_allowed
    ):
        try:
            green_arts = client.list_artifacts(repo, int(last_success_run_id))
        except GitHubAPIError as exc:
            notes.append(f"last-green artifacts unavailable: {exc}")
            green_arts = []
        wanted = {name for name, _stage, _blob in pipeline_zips}
        for raw in green_arts:
            if remaining <= 0:
                break
            name = str(raw.get("name") or "")
            if name not in wanted or raw.get("id") is None or raw.get("expired"):
                continue
            size = int(raw.get("size_in_bytes") or raw.get("size_bytes") or 0)
            if size > ARTIFACT_DOWNLOAD_MAX_BYTES:
                continue
            blob = client.download_artifact_zip(repo, int(raw["id"]))
            if not blob:
                continue
            green_zips[name] = blob
            remaining -= 1
    elif last_success_run_id and pipeline_zips and remaining <= 0:
        notes.append("skipped last-green pipeline logs (zip cap)")
    return infos, merge_junit_reports(reports), pipeline_zips, green_zips


def _load_fixture_artifacts(path: Path) -> tuple[list[ArtifactInfo], JUnitReport | None]:
    infos: list[ArtifactInfo] = []
    art_json = path / "artifacts.json"
    if art_json.exists():
        loaded = json.loads(art_json.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            loaded = loaded.get("artifacts") or []
        if isinstance(loaded, list):
            infos = [_artifact_info(item) for item in loaded if isinstance(item, dict)]
    reports: list[JUnitReport | None] = []
    search_roots = [path / "artifacts", path]
    seen: set[Path] = set()
    for root in search_roots:
        if not root.exists():
            continue
        for file in root.rglob("*"):
            if not file.is_file() or file in seen:
                continue
            seen.add(file)
            rel = str(file.relative_to(root)) if root != path else file.name
            if file.suffix.lower() == ".zip":
                report = parse_junit_from_zip(
                    file.read_bytes(), source_artifact=file.stem
                )
                if report is not None:
                    reports.append(report)
                    _mark_parsed(infos, file.stem)
            elif file.suffix.lower() == ".xml" and (
                "junit" in file.name.lower() or "test-results" in rel.replace("\\", "/")
            ):
                try:
                    reports.append(
                        parse_junit_xml(
                            file.read_text(encoding="utf-8-sig"),
                            source_artifact=file.name,
                        )
                    )
                    _mark_parsed(infos, file.name)
                    if not any(item.name in {file.name, "test-results"} for item in infos):
                        infos.append(
                            ArtifactInfo(
                                name="test-results",
                                size_bytes=file.stat().st_size,
                                parsed=True,
                            )
                        )
                except Exception:  # noqa: BLE001
                    continue
    report = merge_junit_reports(reports)
    if report is not None:
        for item in infos:
            if artifact_looks_like_junit(item.name):
                item.parsed = True
    return infos, report


def _mark_parsed(infos: list[ArtifactInfo], name: str) -> None:
    for item in infos:
        if item.name == name or name.startswith(item.name):
            item.parsed = True
            matched = None
            try:
                from .pipeline_logs import match_pipeline_artifact_name

                matched = match_pipeline_artifact_name(item.name)
            except Exception:
                matched = None
            if matched is not None:
                item.kind = "pipeline_logs"
                item.stage = matched[1]
            elif artifact_looks_like_junit(item.name):
                item.kind = "junit"


def _failed_stage_from_jobs(jobs: list[dict[str, Any]]) -> str | None:
    from .diagnose import _stage_from_step

    for job in jobs:
        if job.get("conclusion") not in _ANALYSE_CONCLUSIONS:
            continue
        for step in job.get("steps") or []:
            if not isinstance(step, dict):
                continue
            if step.get("conclusion") == "failure":
                return _stage_from_step(str(step.get("name") or ""))
        return _stage_from_step(str(job.get("name") or ""))
    return None


def _load_fixture_pipeline_zips(
    path: Path,
) -> tuple[list[tuple[str, str | None, bytes]], dict[str, bytes]]:
    from .pipeline_logs import match_pipeline_artifact_name

    zips: list[tuple[str, str | None, bytes]] = []
    green: dict[str, bytes] = {}
    search = [path / "artifacts", path]
    seen: set[Path] = set()
    for root in search:
        if not root.is_dir():
            continue
        for file in root.glob("*.zip"):
            if file in seen:
                continue
            seen.add(file)
            matched = match_pipeline_artifact_name(file.stem)
            if matched is None:
                continue
            zips.append((file.stem, matched[1], file.read_bytes()))
    green_dir = path / "last_green_artifacts"
    if green_dir.is_dir():
        for file in green_dir.glob("*.zip"):
            green[file.stem] = file.read_bytes()
    return zips, green


def _parse_pipeline_bundle(
    bundle: dict[str, Any],
    *,
    failed_stage: str | None,
    analyze: bool,
    limit: int,
    notes: list[str],
) -> tuple[list[Any], LastGreenCompare | None]:
    from .pipeline_logs import compare_green_fail, parse_pipeline_zip, rank_pipeline_artifacts

    if not analyze:
        return [], None
    raw_zips: list[tuple[str, str | None, bytes]] = list(bundle.get("pipeline_zips") or [])
    names = rank_pipeline_artifacts([name for name, _s, _b in raw_zips], failed_stage=failed_stage)
    by_name = {name: (stage, blob) for name, stage, blob in raw_zips}
    streams = []
    for name in names[:limit]:
        stage, blob = by_name[name]
        try:
            streams.extend(parse_pipeline_zip(blob, artifact_name=name, stage=stage))
        except Exception as exc:  # noqa: BLE001
            notes.append(f"pipeline-log {name!r} parse failed: {exc}")
    last_green = None
    green = bundle.get("green_pipeline_zips") or {}
    if streams:
        primary = streams[0].artifact_name
        fail_lines = [
            line
            for stream in streams
            if stream.artifact_name == primary
            for window in stream.windows
            for line in window.content.splitlines()
        ]
        if primary in green:
            try:
                g_streams = parse_pipeline_zip(
                    green[primary], artifact_name=primary, stage=streams[0].stage
                )
                green_lines = [
                    line
                    for stream in g_streams
                    for window in stream.windows
                    for line in window.content.splitlines()
                ]
                last_green = compare_green_fail(
                    green_lines, fail_lines, artifact_name=primary
                )
            except Exception as exc:  # noqa: BLE001
                notes.append(f"last-green compare failed: {exc}")
                last_green = LastGreenCompare(
                    artifact_name=primary,
                    available=False,
                    skipped_reason=str(exc),
                )
        elif bundle.get("last_success"):
            last_green = LastGreenCompare(
                artifact_name=primary,
                available=False,
                skipped_reason="green artifact missing",
            )
    return streams, last_green


def _maybe_fetch_code_context(
    summary: Summary, bundle: dict[str, Any], args: argparse.Namespace
) -> Summary:
    from .code_context import fetch_code_context
    from .diagnose import (
        apply_verdict,
        diagnose,
        is_blast_radius_skip,
        should_fetch_code_context,
    )

    verdict = diagnose(summary)
    if is_blast_radius_skip(summary) and not should_fetch_code_context(summary, verdict):
        summary.code_context = fetch_code_context(
            summary,
            verdict,
            get_file=lambda _repo, _path, _ref: None,
            repo="",
        )
        return apply_verdict(summary, verdict)
    if bundle.get("from_fixture"):
        return summary
    repo = bundle.get("repo") or getattr(args, "repo", None)
    if not repo or not should_fetch_code_context(summary, verdict):
        return summary
    try:
        with GitHubClient(api_url=getattr(args, "api_url", None), token=getattr(args, "token", None)) as client:
            _fill_first_failing_files(summary, client, str(repo))
            summary.code_context = fetch_code_context(
                summary,
                verdict,
                get_file=client.get_file,
                repo=str(repo),
            )
    except Exception as exc:  # noqa: BLE001
        summary.collection_notes.append(f"code_context skipped: {exc}")
        return summary
    return apply_verdict(summary, diagnose(summary))


def _fill_first_failing_files(summary: Summary, client: GitHubClient, repo: str) -> None:
    hist = summary.history
    if hist is None or not hist.first_failing_sha or summary.changes is None:
        return
    target = hist.first_failing_sha
    for commit in summary.changes.commits:
        sha = commit.sha or ""
        if not (target.startswith(sha) or sha.startswith(target) or sha == target):
            continue
        if commit.files:
            return
        try:
            commit.files = client.list_commit_files(repo, target)
        except Exception as exc:  # noqa: BLE001
            summary.collection_notes.append(f"commit files unavailable: {exc}")
        return


def _build_summary(
    bundle: dict[str, Any],
    *,
    extra_notes: list[str],
    args: argparse.Namespace | None = None,
) -> Summary:
    run = bundle["run"]
    jobs: list[dict[str, Any]] = bundle["jobs"]
    pulls: list[dict[str, Any]] = bundle.get("pulls") or []
    logs: dict[int, str | None] = bundle.get("logs") or {}
    notes: list[str] = list(bundle.get("notes") or [])
    notes.extend(extra_notes)

    candidates = _select_failed_jobs(jobs, logs)
    analysed = candidates[:MAX_FAILED_JOBS_ANALYSED]
    failed_jobs: list[FailedJob] = []
    raw_logs: list[str] = []
    cleaned_lines: list[list[str]] = []
    conclusions: list[str | None] = []
    failed_step_names: list[str | None] = []
    log_line_counts: list[int] = []
    for job in analysed:
        raw = logs.get(int(job["id"]))
        failed_job = _failed_job_from_api(job, raw, notes)
        failed_jobs.append(failed_job)
        raw_logs.append(raw or "")
        cleaned_lines.append(_cleaned_lines(raw, failed_job))
        conclusions.append(job.get("conclusion"))
        failed_step_names.append(failed_job.failed_step_name)
        log_line_counts.append(failed_job.log_lines_raw)

    current_id = run.get("id")
    prior = [
        item
        for item in (bundle.get("same_sha_runs") or [])
        if item.get("id") != current_id
    ]
    hit = classify_failure(
        raw_logs=raw_logs,
        cleaned_lines=cleaned_lines,
        conclusions=conclusions,
        failed_step_names=failed_step_names,
        log_line_counts=log_line_counts,
        workflow_name=str(run.get("name") or ""),
        run_attempt=int(run.get("run_attempt") or 1),
        prior_same_sha_runs=prior,
        no_failed_jobs=len(candidates) == 0,
        job_names=[str(job.get("name") or "") for job in analysed],
    )
    if hit.reason:
        notes.append(hit.reason)

    pr_number, pr_url, is_fork, head_repo = _pr_context(run, pulls)
    history, changes = _history_and_changes(bundle, run, failed_jobs, notes)
    # 7c: order each job's stack traces so a trace naming a changed application
    # source path leads. Reorder only; nothing is dropped.
    if changes is not None and changes.files:
        from .extract import order_stack_traces_by_paths

        for failed_job in failed_jobs:
            if failed_job.stack_traces:
                failed_job.stack_traces = order_stack_traces_by_paths(
                    failed_job.stack_traces, changes.files
                )
    drain_report = None
    fine = ""
    coarse = ""
    drain_dir = str(_resolve_drain_dir(args))
    try:
        if cleaned_lines and analysed:
            job_name = str(analysed[0].get("name") or "job")
            workflow = str(run.get("name") or "workflow")
            key = f"{workflow}_{job_name}"
            novelty_result = novelty(
                cleaned_lines[0], key, drain_dir=drain_dir, workflow=workflow
            )
            drain_report = novelty_result.report
            fine = novelty_result.fingerprint_fine
            coarse = novelty_result.fingerprint_coarse
            if novelty_result.fallback_file:
                notes.append(
                    f"drain fallback {novelty_result.fallback_file} (job key missing)"
                )
            elif drain_report.fingerprint_degraded:
                notes.append("Drain3 baseline unavailable; novelty is tri-state and fingerprint is degraded.")
        else:
            notes.append("no cleaned lines for Drain3")
    except Exception as exc:  # noqa: BLE001
        notes.append(f"Drain3 skipped: {exc}")

    analyze_pipeline = True
    max_pipeline = MAX_PIPELINE_LOG_ARTIFACTS
    if args is not None:
        analyze_pipeline = bool(getattr(args, "analyze_pipeline_logs", True))
        max_pipeline = int(
            getattr(args, "max_pipeline_log_artifacts", None) or MAX_PIPELINE_LOG_ARTIFACTS
        )
    failed_stage = _failed_stage_from_jobs(jobs)
    pipeline_streams, last_green = _parse_pipeline_bundle(
        bundle,
        failed_stage=failed_stage,
        analyze=analyze_pipeline,
        limit=max_pipeline,
        notes=notes,
    )
    if pipeline_streams:
        groups = []
        if drain_report is not None:
            groups.append(drain_report.templates)
        for stream in pipeline_streams:
            groups.append(stream.templates)
        available = bool(drain_report and drain_report.baseline_available) or bool(
            last_green and last_green.available
        )
        try:
            fine, coarse, _hash_input = merge_fingerprint_inputs(*groups, available=available)
        except Exception as exc:  # noqa: BLE001
            notes.append(f"pipeline fingerprint merge skipped: {exc}")
        for item in bundle.get("artifacts") or []:
            if getattr(item, "name", None) in {s.artifact_name for s in pipeline_streams}:
                item.parsed = True
                item.kind = "pipeline_logs"

    backend = getattr(args, "history_backend", None)
    if not backend:
        backend = "none" if bundle.get("from_fixture") else "cache"
    history_dir = getattr(args, "history_dir", None) or ".rca-history"
    store = None
    try:
        store = open_store(backend, history_dir)
        mask_hash = ""
        try:
            mask_hash = masking_config_hash(str(default_config_path()))
        except Exception:
            mask_hash = drain_report.masking_config_hash if drain_report else ""
        templates_for_hash = [
            t.template
            for t in (drain_report.templates if drain_report else [])
            if t.tier in {"T1", "T2", "T3"}
        ]
        for stream in pipeline_streams:
            for tmpl in stream.templates:
                if tmpl.tier in {"T1", "T2", "T3"} or tmpl.has_error_match:
                    templates_for_hash.append(tmpl.template)
        hit_rec = lookup_recurrence(
            store,
            fine=fine,
            coarse=coarse,
            templates=templates_for_hash,
            mask_hash=mask_hash,
        )
        history.match = hit_rec.match  # type: ignore[assignment]
        history.match_similarity = hit_rec.similarity
        history.config_drift = hit_rec.config_drift
        if hit_rec.record is not None:
            rec = hit_rec.record
            history.seen_count = rec.count
            history.first_seen = rec.first_seen
            history.branches_seen = list(rec.branches)
            history.previous_summary = rec.last_summary
            history.previous_resolution = rec.resolution
            history.resolution_verified = rec.human_verified
            current_branch = str(run.get("head_branch") or "")
            others = [b for b in rec.branches if b and b != current_branch]
            history.cross_branch = bool(others)
            if history.cross_branch:
                hit.is_flaky = True
            if hit_rec.config_drift:
                notes.append(
                    f"Masking config changed since this record was written "
                    f"({rec.masking_config_hash} → {mask_hash}); matched by similarity only."
                )
        if fine:
            now = datetime.now(timezone.utc)
            record = FailureRecord(
                fingerprint=fine,
                fingerprint_coarse=coarse,
                first_seen=now,
                last_seen=now,
                count=1,
                branches=[str(run.get("head_branch") or "")],
                run_ids=[int(run.get("id") or 0)],
                category=hit.category,
                templates=templates_for_hash,
                template_hashes=[template_hash(t) for t in templates_for_hash],
                masking_config_hash=mask_hash or (drain_report.masking_config_hash if drain_report else ""),
                last_summary=(
                    drain_report.templates[0].template
                    if drain_report and drain_report.templates
                    else hit.category
                ),
            )
            store.upsert(record)
        history.backend = "cache" if backend == "cache" else "none"  # type: ignore[assignment]
    except Exception as exc:  # noqa: BLE001
        notes.append(f"history store degraded: {exc}")
        history.backend_degraded = True
        history.backend = "none"
    finally:
        if store is not None:
            try:
                store.close()
            except Exception:
                pass

    summary = Summary(
        collector_version=COLLECTOR_VERSION,
        collected_at=datetime.now(timezone.utc),
        run=RunMeta(
            run_id=int(run.get("id") or 0),
            run_attempt=int(run.get("run_attempt") or 1),
            workflow_name=str(run.get("name") or "unknown"),
            html_url=str(run.get("html_url") or ""),
            event=str(run.get("event") or "unknown"),
            actor=_actor_login(run),
            head_sha=str(run.get("head_sha") or ""),
            head_branch=str(run.get("head_branch") or ""),
            pr_number=pr_number,
            pr_url=pr_url,
            is_fork=is_fork,
            head_repo=head_repo,
            failed_job_total=len(candidates),
            failed_jobs_analysed=len(analysed),
        ),
        verdict=_verdict_from_hit(hit),
        classification=_classification_from_hit(hit),
        failed_jobs=failed_jobs,
        junit=bundle.get("junit"),
        artifacts=list(bundle.get("artifacts") or []),
        pipeline_logs=pipeline_streams,
        last_green_compare=last_green,
        drain=drain_report,
        changes=changes,
        history=history,
        fingerprint=fine,
        fingerprint_coarse=coarse,
        kubernetes=None,
        budget_report=BudgetReport(),
        collection_notes=notes,
    )
    # Defect B (Step 9): when the failed step's own log is uninformative, the
    # cause lives in the pipeline stream — seed primary_failure_line from it so
    # anchoring/grounding use the real cause, not the job's benign output.
    try:
        from .diagnose import _first_pipeline_error_line, _job_logs_are_exit_only

        primary = summary.failed_jobs[0] if summary.failed_jobs else None
        if (
            primary is not None
            and not (primary.primary_failure_line or "").strip()
            and _job_logs_are_exit_only(summary)
        ):
            pipeline_line = _first_pipeline_error_line(summary)
            if pipeline_line:
                primary.primary_failure_line = pipeline_line
    except Exception as exc:  # noqa: BLE001
        summary.collection_notes.append(f"pipeline primary line skipped: {exc}")

    try:
        from .diagnose import apply_verdict, diagnose

        summary = apply_verdict(summary, diagnose(summary))
    except Exception as exc:  # noqa: BLE001
        summary.collection_notes.append(f"diagnose skipped: {exc}")
    return summary


def _history_and_changes(
    bundle: dict[str, Any],
    run: dict[str, Any],
    failed_jobs: list[FailedJob],
    notes: list[str],
) -> tuple[HistoryContext, Any]:
    head_sha = str(run.get("head_sha") or "")
    current_id = run.get("id")
    try:
        current_int = int(current_id) if current_id is not None else None
    except (TypeError, ValueError):
        current_int = None
    compare = bundle.get("compare")
    commit_shas = None
    if isinstance(compare, dict):
        commit_shas = [str(c.get("sha") or "") for c in (compare.get("commits") or [])]
    hist = summarize_history(
        head_sha=head_sha,
        now=datetime.now(timezone.utc),
        last_success=bundle.get("last_success"),
        branch_runs=bundle.get("branch_runs") or [],
        same_sha=bundle.get("same_sha_runs") or [],
        exclude_id=current_int,
        commit_shas=commit_shas,
    )
    history = HistoryContext(
        last_success_run_id=hist.last_success_id,
        last_success_sha=hist.last_success_sha,
        last_success_at=hist.last_success_at,
        last_success_age_hours=hist.last_success_age_hours,
        blame_range=hist.blame_range,
        first_failing_sha=hist.first_failing_sha,
        recent_outcomes=hist.recent_outcomes,
        same_sha_runs=hist.same_sha_ids,
        backend="none",
    )
    pull = bundle.get("pull")
    merge_base = None
    if isinstance(pull, dict) and isinstance(pull.get("base"), dict):
        merge_base = pull["base"].get("sha")
    base_sha, basis = resolve_compare_base(hist.last_success_sha, merge_base)
    from_fixture = bool(bundle.get("from_fixture"))
    if from_fixture and compare is None:
        if hist.last_success_sha:
            notes.append("compare payload missing; changes omitted")
        return history, None
    if basis == "head_only" and compare is None:
        changes = change_context_from_compare(
            None,
            head_sha=head_sha,
            range_basis="head_only",
            base_sha=None,
            pull=pull if isinstance(pull, dict) else None,
        )
        return history, changes
    if not isinstance(compare, dict):
        notes.append("compare payload missing; changes omitted")
        return history, None
    stack_paths = _stack_paths(failed_jobs)
    changes = change_context_from_compare(
        compare,
        head_sha=head_sha,
        range_basis=basis,
        base_sha=base_sha,
        pull=pull if isinstance(pull, dict) else None,
        stack_paths=stack_paths,
    )
    return history, changes


def _stack_paths(jobs: list[FailedJob]) -> list[str]:
    found: list[str] = []
    pattern = re.compile(r'File "([^"]+)"|\(([^():]+):\d+')
    for job in jobs:
        for trace in job.stack_traces:
            for line in trace.content.splitlines():
                match = pattern.search(line)
                if not match:
                    continue
                path = match.group(1) or match.group(2)
                if path and path not in found:
                    found.append(path)
    return found


def _cleaned_lines(raw: str | None, failed_job: FailedJob) -> list[str]:
    if raw is None:
        return []
    keep_post = bool(failed_job.failed_step_name and _is_post_step(failed_job.failed_step_name))
    return clean_log(raw, keep_post_cleanup=keep_post).lines


_SHORT_CIRCUITS = {
    "infra_runner",
    "infra_widespread",
    "flake_same_sha_passed",
    "no_failed_jobs",
}


def _verdict_from_hit(hit: ClassificationHit) -> Verdict:
    short = hit.short_circuit if hit.short_circuit in _SHORT_CIRCUITS else None
    return Verdict(
        short_circuit=short,  # type: ignore[arg-type]
        requires_analysis=hit.requires_analysis,
        reason=hit.reason,
    )


def _classification_from_hit(hit: ClassificationHit) -> Classification:
    confidence = hit.confidence if hit.confidence in ("high", "medium", "low") else "low"
    side = hit.is_infra_vs_code if hit.is_infra_vs_code in ("infra", "code", "unknown") else "unknown"
    return Classification(
        category=hit.category,
        confidence=confidence,  # type: ignore[arg-type]
        matched_pattern=hit.matched_pattern,
        matched_line=hit.matched_line,
        other_matches=hit.other_matches,
        is_infra_vs_code=side,  # type: ignore[arg-type]
        is_flaky=hit.is_flaky,
    )


def _select_failed_jobs(
    jobs: list[dict[str, Any]],
    logs: dict[int, str | None],
) -> list[dict[str, Any]]:
    failures = [j for j in jobs if j.get("conclusion") in _ANALYSE_CONCLUSIONS]
    cancelled = [j for j in jobs if j.get("conclusion") == "cancelled"]
    if not failures and cancelled:
        timeouts = []
        for job in cancelled:
            raw = logs.get(int(job["id"]))
            if raw and _TIMEOUT_SIGNATURE in raw:
                timeouts.append(job)
        failures = timeouts or cancelled

    def _key(job: dict[str, Any]) -> tuple[str, int]:
        stamp = job.get("completed_at") or job.get("started_at") or job.get("created_at") or ""
        return (str(stamp), int(job.get("id") or 0))

    return sorted(failures, key=_key)


def _failed_job_from_api(
    job: dict[str, Any],
    raw_log: str | None,
    notes: list[str],
) -> FailedJob:
    steps = [_step_info(s) for s in (job.get("steps") or [])]
    failed_step = next((s for s in steps if s.conclusion == "failure"), None)
    keep_post = bool(failed_step and _is_post_step(failed_step.name))
    cleaned: CleanResult | None = None
    log_unavailable = raw_log is None
    extracted_windows = []
    extracted_stacks = []
    extracted_errors = []
    extracted_annotations: list[str] = []
    extracted_exit: int | None = None
    excerpt_lines: list[str] = []
    primary_failure_line: str | None = None
    if raw_log is not None:
        cleaned = clean_log(raw_log, keep_post_cleanup=keep_post)
        extracted = extract_from_lines(
            cleaned.lines,
            failed_step_name=failed_step.name if failed_step else None,
        )
        extracted_windows = extracted.windows
        extracted_stacks = extracted.stack_traces
        extracted_errors = extracted.error_lines
        extracted_annotations = extracted.annotations
        extracted_exit = extracted.exit_code
        primary_failure_line = extracted.primary_failure_line
        excerpt_lines = list(extracted.excerpt_lines)
        grouped = failed_step_excerpt_lines(
            raw_log.splitlines(),
            step_name=failed_step.name if failed_step else None,
        )
        if grouped:
            excerpt_lines = grouped

    queue_seconds = _seconds_between(job.get("created_at"), job.get("started_at"))
    if queue_seconds is not None and queue_seconds >= QUEUE_SECONDS_THRESHOLD:
        notes.append(
            f"job {job.get('name')!r} queued {queue_seconds}s "
            f"(threshold {QUEUE_SECONDS_THRESHOLD}s)"
        )

    runner = RunnerInfo(
        name=job.get("runner_name"),
        group=job.get("runner_group_name"),
        labels=_job_labels(job),
        image=cleaned.setup.image if cleaned else None,
        os=cleaned.setup.os if cleaned else None,
        disk_free_at_start=cleaned.setup.disk_free_at_start if cleaned else None,
    )
    return FailedJob(
        job_id=int(job.get("id") or 0),
        name=str(job.get("name") or ""),
        failed_step_name=failed_step.name if failed_step else None,
        failed_step_number=failed_step.number if failed_step else None,
        exit_code=extracted_exit,
        duration_seconds=_seconds_between(job.get("started_at"), job.get("completed_at")),
        log_unavailable=log_unavailable,
        queue_seconds=queue_seconds,
        runner=runner,
        steps=steps,
        log_lines_raw=cleaned.lines_raw if cleaned else 0,
        log_lines_clean=cleaned.lines_clean if cleaned else 0,
        log_bytes=cleaned.bytes_raw if cleaned else 0,
        windows=extracted_windows,
        stack_traces=extracted_stacks,
        error_lines=extracted_errors,
        annotations=extracted_annotations,
        failed_step_excerpt=(
            FailedStepExcerpt(
                name=(failed_step.name if failed_step else "") or "unknown",
                lines=excerpt_lines,
            )
            if excerpt_lines
            else None
        ),
        primary_failure_line=primary_failure_line,
    )


def _step_info(step: dict[str, Any]) -> StepInfo:
    duration = _seconds_between(step.get("started_at"), step.get("completed_at"))
    name = str(step.get("name") or "")
    conclusion = str(step.get("conclusion") or "")
    cache_miss = False
    lowered = name.lower()
    if (
        conclusion == "success"
        and duration is not None
        and duration < 1
        and "cache" in lowered
        and "restore" in lowered
    ):
        cache_miss = True
    return StepInfo(
        number=int(step.get("number") or 0),
        name=name,
        conclusion=conclusion,
        duration_seconds=duration,
        suspected_cache_miss=cache_miss,
    )


def _is_post_step(name: str) -> bool:
    lowered = name.strip().lower()
    return lowered.startswith("post ") or lowered == "post job cleanup"


def _pr_context(
    run: dict[str, Any],
    pulls: list[dict[str, Any]],
) -> tuple[int | None, str | None, bool, str | None]:
    head_repo_obj = run.get("head_repository") or {}
    repo_obj = run.get("repository") or {}
    head_repo = head_repo_obj.get("full_name")
    base_repo = repo_obj.get("full_name")
    is_fork = bool(
        head_repo_obj.get("fork")
        or (head_repo and base_repo and head_repo != base_repo)
    )
    prs = run.get("pull_requests") or pulls
    if not prs:
        return None, None, is_fork, head_repo
    pr = prs[0]
    number = pr.get("number")
    html_url = pr.get("html_url") or pr.get("url")
    return (
        int(number) if number is not None else None,
        str(html_url) if html_url else None,
        is_fork,
        head_repo,
    )


def _actor_login(run: dict[str, Any]) -> str:
    actor = run.get("actor")
    if isinstance(actor, dict):
        return str(actor.get("login") or "unknown")
    if actor:
        return str(actor)
    return "unknown"


def _job_labels(job: dict[str, Any]) -> list[str]:
    labels = job.get("labels") or []
    out: list[str] = []
    for item in labels:
        if isinstance(item, dict):
            name = item.get("name")
            if name:
                out.append(str(name))
        else:
            out.append(str(item))
    return out


def _seconds_between(start: Any, end: Any) -> int | None:
    if not start or not end:
        return None
    try:
        a = isoparse(str(start))
        b = isoparse(str(end))
    except (ValueError, TypeError):
        return None
    return max(0, int((b - a).total_seconds()))


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _stub_summary(
    *,
    note: str,
    run_id: int = 0,
    requires_analysis: bool = True,
) -> Summary:
    return Summary(
        collector_version=COLLECTOR_VERSION,
        collected_at=datetime.now(timezone.utc),
        run=RunMeta(
            run_id=run_id,
            run_attempt=1,
            workflow_name="unknown",
            html_url="",
            event="unknown",
            actor="unknown",
            head_sha="",
            head_branch="",
            failed_job_total=0,
            failed_jobs_analysed=0,
        ),
        verdict=Verdict(
            short_circuit=None,
            requires_analysis=requires_analysis,
            reason=note,
        ),
        classification=Classification(
            category="unknown",
            confidence="low",
            matched_pattern=None,
            matched_line=None,
            is_infra_vs_code="unknown",
        ),
        failed_jobs=[],
        fingerprint="",
        fingerprint_coarse="",
        kubernetes=None,
        budget_report=BudgetReport(),
        collection_notes=[note],
    )


def _safe_emit(
    summary: Summary, out: Path, *, token_budget: str | int | None = None
) -> None:
    try:
        _emit(summary, out, token_budget=token_budget)
    except Exception:
        _LOG.exception("failed to emit summary; writing failure outputs")
        try:
            out.mkdir(parents=True, exist_ok=True)
            write_failure_outputs(out)
        except Exception:
            _LOG.exception("failed to write GITHUB_OUTPUT")


def _emit(summary: Summary, out: Path, *, token_budget: str | int | None = None) -> None:
    out.mkdir(parents=True, exist_ok=True)
    redacted, n_redacted = redact_summary(summary)
    if n_redacted:
        redacted.collection_notes.append(f"Secrets redacted: {n_redacted} matches in content.")
    cap: int | None
    try:
        cap = int(token_budget) if token_budget not in (None, "") else None
    except (TypeError, ValueError):
        cap = None
    budgeted = apply_budget(redacted, total_cap=cap)
    markdown = render_markdown(budgeted)
    (out / "summary.json").write_text(budgeted.model_dump_json(indent=2), encoding="utf-8")
    (out / "summary.md").write_text(markdown, encoding="utf-8")
    write_github_output(budgeted, out)


if __name__ == "__main__":
    raise SystemExit(main())
