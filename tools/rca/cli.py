"""CLI: collect (live or --from-fixture) and capture a run into a fixture dir."""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dateutil.parser import isoparse

from .budget import apply_budget
from .cleaner import CleanResult, clean_log
from .classify import ClassificationHit, classify_failure
from .config import (
    COLLECTOR_VERSION,
    MAX_FAILED_JOBS_ANALYSED,
    QUEUE_SECONDS_THRESHOLD,
)
from .changes import change_context_from_compare, resolve_compare_base
from .drain_index import (
    default_config_path,
    fingerprint_coarse,
    fingerprint_fine,
    masking_config_hash,
    novelty,
    remask_templates,
    template_hash,
    train as drain_train,
)
from .extract import extract_from_lines
from .github_api import GitHubAPIError, GitHubClient
from .history import (
    lookup_recurrence,
    open_store,
    summarize as summarize_history,
)
from .models import (
    BudgetReport,
    Classification,
    FailedJob,
    FailureRecord,
    HistoryContext,
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
        parser.error(f"unknown command {args.cmd}")
    except Exception as exc:  # noqa: BLE001 — collector must not fail the workflow
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
                args.repo, args.run_id, api_url=args.api_url, token=args.token
            )
            notes.extend(bundle.pop("notes", []))
        summary = _build_summary(bundle, extra_notes=notes, args=args)
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
    drain_dir = args.drain_dir or ".drain"
    notes: list[str] = []
    try:
        if args.from_fixture:
            bundle = _load_fixture(Path(args.from_fixture))
        else:
            if not args.run_id or not args.repo:
                raise ValueError("train requires --from-fixture or both --run-id and --repo")
            bundle = _fetch_live(
                args.repo, args.run_id, api_url=args.api_url, token=args.token
            )
            notes.extend(bundle.pop("notes", []))
        workflow = str(bundle["run"].get("name") or "workflow")
        jobs = bundle["jobs"]
        logs = bundle.get("logs") or {}
        trained = 0
        for job in jobs:
            if job.get("conclusion") != "success":
                continue
            raw = logs.get(int(job["id"]))
            if not raw:
                continue
            cleaned = clean_log(raw).lines
            key = f"{workflow}_{job.get('name') or 'job'}"
            drain_train(cleaned, key, drain_dir=drain_dir)
            trained += 1
        note = f"Drain3 trained {trained} job log(s) into {drain_dir}"
        notes.append(note)
        _LOG.info(note)
    except Exception as exc:  # noqa: BLE001
        _LOG.exception("train failed")
        note = f"train error: {exc}"
        if args.strict:
            raise
    _safe_emit(
        _stub_summary(
            note=note,
            run_id=int(args.run_id or 0),
            requires_analysis=False,
        ),
        Path(args.out),
        token_budget=getattr(args, "token_budget", None),
    )
    return 0


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


def _fetch_live(
    repo: str,
    run_id: int,
    *,
    api_url: str | None,
    token: str | None,
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
        notes.append("Artifacts and JUnit not collected in this slice.")
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
        "from_fixture": True,
        "notes": [
            "replayed from fixture (no network)",
            "Artifacts and JUnit not collected in this slice.",
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
    )
    if hit.reason:
        notes.append(hit.reason)

    pr_number, pr_url, is_fork, head_repo = _pr_context(run, pulls)
    history, changes = _history_and_changes(bundle, run, failed_jobs, notes)
    drain_report = None
    fine = ""
    coarse = ""
    drain_dir = getattr(args, "drain_dir", None) or ".drain"
    try:
        if cleaned_lines and analysed:
            job_name = str(analysed[0].get("name") or "job")
            workflow = str(run.get("name") or "workflow")
            key = f"{workflow}_{job_name}"
            novelty_result = novelty(cleaned_lines[0], key, drain_dir=drain_dir)
            drain_report = novelty_result.report
            fine = novelty_result.fingerprint_fine
            coarse = novelty_result.fingerprint_coarse
            if drain_report.fingerprint_degraded:
                notes.append("Drain3 baseline unavailable; novelty is tri-state and fingerprint is degraded.")
        else:
            notes.append("no cleaned lines for Drain3")
    except Exception as exc:  # noqa: BLE001
        notes.append(f"Drain3 skipped: {exc}")

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
        drain=drain_report,
        changes=changes,
        history=history,
        fingerprint=fine,
        fingerprint_coarse=coarse,
        kubernetes=None,
        budget_report=BudgetReport(),
        collection_notes=notes,
    )
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
    if raw_log is not None:
        cleaned = clean_log(raw_log, keep_post_cleanup=keep_post)
        extracted = extract_from_lines(cleaned.lines)
        extracted_windows = extracted.windows
        extracted_stacks = extracted.stack_traces
        extracted_errors = extracted.error_lines
        extracted_annotations = extracted.annotations
        extracted_exit = extracted.exit_code

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
