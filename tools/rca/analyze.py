"""Phase 2 analyze: gate, cache, trinity→alfred, citation check, write artifacts."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import (
    PERSONAS,
    PROMPT_VERSION,
    SCHEMA_VERSION,
    TOKEN_BUDGET_ANALYZE,
    resolve_stgpt_api_key,
    resolve_stgpt_api_url,
    resolve_stgpt_client_app_name,
)
from .models import AnalysisCitation, AnalysisRecord, AnalysisResult, Summary
from .prompt import (
    build_evidence,
    build_messages,
    failed_step_anchor_text,
    focused_evidence,
)
from .redact import redact_text
from .stgpt_client import (
    ChatResult,
    StgptError,
    bridge_error_message,
    flatten_user_content,
    post_chat,
    public_request_url,
)

_LOG = logging.getLogger(__name__)
_FENCE_BLOCK = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)
_PREVIEW_CHARS = 240
_PROSE_CAP = 1500
_KEY_ALIASES = {
    "rootCause": "root_cause",
    "root_cause": "root_cause",
    "cause": "root_cause",
    "diagnosis": "root_cause",
    "suggestedFix": "suggested_fix",
    "suggested_fix": "suggested_fix",
    "fix": "suggested_fix",
    "cannotDetermine": "cannot_determine",
    "cannot_determine": "cannot_determine",
    "confidence": "confidence",
    "citations": "citations",
}
_CITATION_SOURCES = {
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
}
ChatFn = Callable[[str, Sequence[Mapping[str, str]]], ChatResult]
_INFRA_FLAKE_SHORT = frozenset(
    {"infra_runner", "infra_widespread", "flake_same_sha_passed"}
)


def cache_key(summary: Summary) -> str:
    """sha256(fine|category|masking|prompt_version|winning_stream_id|rule_id)[:16]."""
    fine = summary.fingerprint or ""
    category = summary.classification.category or ""
    masking = ""
    if summary.drain is not None and summary.drain.masking_config_hash:
        masking = summary.drain.masking_config_hash
    stream = ""
    rule = ""
    if summary.diagnosis is not None:
        stream = summary.diagnosis.winning_stream_id or ""
        rule = summary.diagnosis.rule_id or ""
    raw = f"{fine}|{category}|{masking}|{PROMPT_VERSION}|{stream}|{rule}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def analyze_summary(
    summary: Summary,
    *,
    api_key: str | None = None,
    from_completion: Path | str | list[Any] | dict[str, Any] | None = None,
    cache_dir: Path | str | None = None,
    transport: Any = None,
    url: str | None = None,
    client_app_name: str | None = None,
    chat_fn: ChatFn | None = None,
) -> AnalysisRecord:
    """Run gate → cache → bridge. Never raises; returns a record including failed."""
    now = datetime.now(timezone.utc)
    key = cache_key(summary)
    base = AnalysisRecord(
        status="failed",
        prompt_version=PROMPT_VERSION,
        fingerprint=summary.fingerprint,
        schema_version=summary.schema_version or SCHEMA_VERSION,
        analyzed_at=now,
    )
    try:
        cached = _cache_get(cache_dir, key)
        if cached is not None:
            return cached.model_copy(
                update={
                    "status": "cached",
                    "cache_hit": True,
                    "analyzed_at": now,
                    "prompt_version": PROMPT_VERSION,
                    "fingerprint": summary.fingerprint,
                    "model_called": True,
                }
            )

        if (
            chat_fn is None
            and from_completion is None
            and not resolve_stgpt_api_key(api_key)
        ):
            return skipped_record(summary, "missing_stgpt_key")

        evidence = build_evidence(summary, cap_tokens=TOKEN_BUDGET_ANALYZE)
        caller = chat_fn or _make_chat_fn(
            api_key=api_key,
            from_completion=from_completion,
            transport=transport,
            url=url,
            client_app_name=client_app_name,
        )
        record = _run_personas(evidence, caller, base)
        record = _apply_grounding_gate(summary, record, evidence, caller)
        redacted = redact_record(record)
        redacted = finalize_user_card(summary, redacted)
        redacted = _attach_validation(summary, redacted, evidence)
        if redacted.status == "ok":
            _cache_put(cache_dir, key, redacted)
        return redacted
    except Exception as exc:  # noqa: BLE001 — analyze must not fail the workflow
        _LOG.exception("analyze error")
        return redact_record(
            base.model_copy(update={"status": "failed", "notes": [str(exc)]})
        )


def write_analysis(
    record: AnalysisRecord,
    *,
    summary_path: Path,
    out_dir: Path,
) -> None:
    """Write analysis.json, merge into summary.json, append ## AI diagnosis."""
    out_dir.mkdir(parents=True, exist_ok=True)
    src_summary = None
    try:
        src_summary = Summary.model_validate_json(Path(summary_path).read_text(encoding="utf-8"))
    except Exception:
        src_summary = None
    if src_summary is not None:
        record = finalize_user_card(src_summary, record)

    analysis_path = out_dir / "analysis.json"
    analysis_dump = json.loads(record.model_dump_json())
    analysis_dump.pop("raw_completion", None)
    analysis_dump.pop("stgpt_responses", None)
    analysis_path.write_text(json.dumps(analysis_dump, indent=2) + "\n", encoding="utf-8")

    dest_json = out_dir / "summary.json"
    src = Path(summary_path)
    if src.exists() and src.resolve() != dest_json.resolve():
        dest_json.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    payload: dict[str, Any] = {}
    if dest_json.exists():
        try:
            loaded = json.loads(dest_json.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                payload = loaded
        except json.JSONDecodeError:
            payload = {}
    dumped = json.loads(record.model_dump_json())
    dumped.pop("raw_completion", None)
    dumped.pop("stgpt_responses", None)
    payload["analysis"] = dumped
    dest_json.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    raw = record.raw_completion
    if raw:
        text, _ = redact_text(raw)
        (out_dir / "raw-completion.txt").write_text(text + "\n", encoding="utf-8")

    if record.stgpt_responses:
        responses, _ = _redact_walk(list(record.stgpt_responses))
        (out_dir / "stgpt-response.json").write_text(
            json.dumps(responses, indent=2) + "\n", encoding="utf-8"
        )

    dest_md = out_dir / "summary.md"
    src_md = src.with_name("summary.md")
    if src_md.exists() and (
        not dest_md.exists() or src_md.resolve() != dest_md.resolve()
    ):
        dest_md.write_text(src_md.read_text(encoding="utf-8"), encoding="utf-8")
    existing = dest_md.read_text(encoding="utf-8") if dest_md.exists() else ""
    dest_md.write_text(_upsert_diagnosis(existing, record, summary=src_summary), encoding="utf-8")


def redact_record(record: AnalysisRecord) -> AnalysisRecord:
    payload = record.model_dump(mode="json")
    cleaned, _ = _redact_walk(payload)
    return AnalysisRecord.model_validate(cleaned)


def _contradicts_infra_flake(summary: Summary, result: AnalysisResult) -> bool:
    """Keep a high-confidence infra/flake rule when the model invents a code bug."""
    short = summary.verdict.short_circuit
    if short not in _INFRA_FLAKE_SHORT:
        return False
    side = (result.infra_or_code or "").strip().lower()
    return side == "code"


def is_grounded(result: AnalysisResult, anchor_text: str) -> bool:
    """True iff any non-empty citation quote is a substring of the anchor text.

    Judged by the quote text itself, not the model's self-reported ``source``.
    """
    if not anchor_text:
        return False
    for cite in result.citations:
        quote = (cite.quote or "").strip()
        if quote and quote in anchor_text:
            return True
    return False


def _ask_once(
    chat_fn: ChatFn,
    persona: str,
    evidence: str,
) -> AnalysisResult | None:
    """Single focused re-ask: one persona, one call. None unless it validates ok."""
    notes: list[str] = []
    messages = build_messages(evidence)
    chat = _call_chat(chat_fn, persona, messages, notes)
    if chat is None or not _is_2xx(chat.status_code):
        return None
    if bridge_error_message(chat.body):
        return None
    completion = chat.completion
    if not (isinstance(completion, str) and completion.strip()):
        return None
    outcome = _interpret_completion(completion, evidence)
    if outcome.status == "ok" and outcome.result is not None:
        return outcome.result
    return None


def _apply_grounding_gate(
    summary: Summary,
    record: AnalysisRecord,
    evidence: str,
    chat_fn: ChatFn,
) -> AnalysisRecord:
    """Anchor an ``ok`` model answer to the failed step (Step 2 citation gate).

    Ungrounded → focused re-ask once (only when there were templates/pipeline
    to drop) → if still ungrounded, prefer a grounded deterministic card.
    Never raises: any failure falls back to the pre-gate record.
    """
    if record.status != "ok" or record.result is None:
        return record
    try:
        anchor = failed_step_anchor_text(summary)
        if is_grounded(record.result, anchor):
            return record

        focused = focused_evidence(summary, cap_tokens=TOKEN_BUDGET_ANALYZE)
        if focused != evidence:
            persona = record.persona or PERSONAS[0]
            retry = _ask_once(chat_fn, persona, focused)
            if retry is not None and is_grounded(retry, anchor):
                notes = list(record.notes)
                notes.append("focused re-ask grounded on failed step")
                return record.model_copy(update={"result": retry, "notes": notes})

        det = analysis_result_from_summary(summary, source="hybrid")
        if is_grounded(det, anchor):
            notes = list(record.notes)
            notes.append(f"model root_cause: {record.result.root_cause}")
            if (record.result.suggested_fix or "").strip():
                notes.append(f"model suggested_fix: {record.result.suggested_fix}")
            return record.model_copy(update={"result": det, "notes": notes})
        return record
    except Exception:  # noqa: BLE001 — the gate must not fail the workflow
        _LOG.exception("grounding gate error")
        return record


def _run_personas(evidence: str, chat_fn: ChatFn, base: AnalysisRecord) -> AnalysisRecord:
    notes: list[str] = []
    last_id: str | None = None
    last_completion: str | None = None
    last_persona: str | None = None
    fallback_used = False
    stgpt_responses: list[dict[str, Any]] = []

    for index, persona in enumerate(PERSONAS):
        last_persona = persona
        if index > 0:
            fallback_used = True
            notes.append(f"fallback to {persona}")

        messages = build_messages(evidence)
        chat = _call_chat(chat_fn, persona, messages, notes)
        if chat is None:
            if any("prompt_empty" in note for note in notes):
                return _record(
                    base,
                    status="failed",
                    persona=persona,
                    result=None,
                    fallback_used=fallback_used,
                    response_id=None,
                    notes=notes,
                    raw_completion=None,
                    stgpt_responses=stgpt_responses,
                )
            continue
        if not _is_2xx(chat.status_code):
            notes.append(_http_failure_note(chat, persona))
            continue
        last_id = chat.response_id
        stgpt_responses.append(_stgpt_debug(chat, persona))
        notes.append(_response_meta_note(chat, persona, messages))
        api_error = bridge_error_message(chat.body)
        if api_error:
            notes.append(_api_error_note(persona, api_error, chat.client_app_name))
            return _record(
                base,
                status="failed",
                persona=persona,
                result=None,
                fallback_used=fallback_used,
                response_id=last_id,
                notes=notes,
                raw_completion=None,
                stgpt_responses=stgpt_responses,
            )
        completion = chat.completion
        if not (isinstance(completion, str) and completion.strip()):
            return _record(
                base,
                status="failed",
                persona=persona,
                result=None,
                fallback_used=fallback_used,
                response_id=last_id,
                notes=notes,
                raw_completion=None,
                stgpt_responses=stgpt_responses,
            )

        last_completion = completion
        outcome = _interpret_completion(completion, evidence)
        if outcome.status == "ok" and outcome.result is not None:
            return _record(
                base,
                status="ok",
                persona=persona,
                result=outcome.result,
                fallback_used=fallback_used,
                response_id=last_id,
                notes=notes,
                raw_completion=completion,
                stgpt_responses=stgpt_responses,
            )

        notes.append(_parse_note(persona, outcome.why, completion))
        repair_messages = build_messages(
            evidence,
            prior_completion=completion,
            repair_reason=outcome.why,
        )
        repaired = _call_chat(chat_fn, persona, repair_messages, notes)
        if repaired is not None and _is_2xx(repaired.status_code):
            stgpt_responses.append(_stgpt_debug(repaired, persona))
            notes.append(_response_meta_note(repaired, persona, repair_messages))
            repair_error = bridge_error_message(repaired.body)
            if repair_error:
                notes.append(
                    _api_error_note(f"{persona} repair", repair_error, repaired.client_app_name)
                )
                return _record(
                    base,
                    status="failed",
                    persona=persona,
                    result=None,
                    fallback_used=fallback_used,
                    response_id=repaired.response_id,
                    notes=notes,
                    raw_completion=None,
                    stgpt_responses=stgpt_responses,
                )
        if (
            repaired is not None
            and _is_2xx(repaired.status_code)
            and isinstance(repaired.completion, str)
            and repaired.completion.strip()
        ):
            last_id = repaired.response_id
            last_completion = repaired.completion
            outcome = _interpret_completion(repaired.completion, evidence)
            if outcome.status == "ok" and outcome.result is not None:
                return _record(
                    base,
                    status="ok",
                    persona=persona,
                    result=outcome.result,
                    fallback_used=fallback_used,
                    response_id=last_id,
                    notes=notes,
                    raw_completion=last_completion,
                    stgpt_responses=stgpt_responses,
                )
            notes.append(_parse_note(f"{persona} repair", outcome.why, last_completion))
        elif repaired is not None and not _is_2xx(repaired.status_code):
            notes.append(_http_failure_note(repaired, persona))

        return _soft_fallback(
            base,
            outcome=outcome,
            persona=persona,
            fallback_used=fallback_used,
            response_id=last_id,
            notes=notes,
            raw_completion=last_completion,
            stgpt_responses=stgpt_responses,
        )

    if last_completion:
        outcome = _interpret_completion(last_completion, evidence)
        notes.append(_parse_note(last_persona or "analyze", outcome.why, last_completion))
        return _soft_fallback(
            base,
            outcome=outcome,
            persona=last_persona,
            fallback_used=fallback_used,
            response_id=last_id,
            notes=notes,
            raw_completion=last_completion,
            stgpt_responses=stgpt_responses,
        )
    return _record(
        base,
        status="failed",
        persona=last_persona,
        result=None,
        fallback_used=fallback_used,
        response_id=last_id,
        notes=notes or ["analyze failed"],
        raw_completion=None,
        stgpt_responses=stgpt_responses,
    )


def _api_error_note(label: str, message: str, client_app_name: str | None) -> str:
    name = resolve_stgpt_client_app_name(client_app_name)
    note = (
        f"{label}: {message} "
        f"clientAppName_repr={name!r} clientAppName_len={len(name)}"
    )
    redacted, _ = redact_text(note)
    return redacted


def _response_meta_note(
    chat: ChatResult,
    persona: str,
    messages: Sequence[Mapping[str, str]],
) -> str:
    keys = ",".join(str(k) for k in chat.body.keys()) if chat.body else ""
    completion = chat.completion if isinstance(chat.completion, str) else ""
    uchars = chat.user_message_chars
    if uchars is None:
        uchars = len(flatten_user_content(messages))
    duration = chat.duration_ms if chat.duration_ms is not None else ""
    rid = chat.response_id or ""
    note = (
        f"{persona}: keys=[{keys}] completion_len={len(completion)} "
        f"responseId={rid} duration_ms={duration} user_message_chars={uchars}"
    )
    if not completion.strip():
        note += " empty completion"
    redacted, _ = redact_text(note)
    return redacted


def _stgpt_debug(chat: ChatResult, persona: str) -> dict[str, Any]:
    return {
        "persona": persona,
        "status_code": chat.status_code,
        "duration_ms": chat.duration_ms,
        "user_message_chars": chat.user_message_chars,
        "completion_len": len(chat.completion) if isinstance(chat.completion, str) else 0,
        "responseId": chat.response_id,
        "keys": list(chat.body.keys()) if chat.body else [],
        "url": public_request_url(chat.url),
        "body": dict(chat.body) if chat.body else {},
    }


def _http_failure_note(chat: ChatResult, persona: str) -> str:
    loc = public_request_url(chat.url)
    snippet = _body_snippet(chat.body)
    parts = [f"HTTP {chat.status_code}", f"persona={persona}"]
    if loc:
        parts.append(f"url={loc}")
    if snippet:
        parts.append(f"body={snippet}")
    note, _ = redact_text(" ".join(parts))
    return note


def _body_snippet(body: Mapping[str, Any], *, limit: int = 200) -> str:
    if not body:
        return ""
    raw = body.get("raw")
    if isinstance(raw, str) and len(body) == 1:
        text = raw
    else:
        try:
            text = json.dumps(dict(body), ensure_ascii=False)
        except (TypeError, ValueError):
            text = str(body)
    text, _ = redact_text(text)
    text = " ".join(text.split())
    if len(text) > limit:
        return text[:limit] + "…"
    return text


class _Outcome:
    def __init__(self, status: str, why: str, result: AnalysisResult | None) -> None:
        self.status = status
        self.why = why
        self.result = result


def _is_2xx(status_code: int) -> bool:
    return 200 <= status_code < 300


def _call_chat(
    chat_fn: ChatFn,
    persona: str,
    messages: Sequence[Mapping[str, str]],
    notes: list[str],
) -> ChatResult | None:
    try:
        return chat_fn(persona, messages)
    except StgptError as exc:
        notes.append(f"{persona}: bridge error: {exc}")
        return None


SKIP_REASONS = {
    "deterministic_sufficient": (
        "Deterministic diagnosis was sufficient; STGPT was not called."
    ),
    "short_circuit": "A short-circuit verdict was reached; STGPT was not called.",
    "analyze_disabled": "Analyze input is false; STGPT was not called.",
    "missing_stgpt_key": "STGPT API key is not set; STGPT was not called.",
    "mode_not_collect": "Analyze is only run in collect mode.",
    "policy_never": "Analyze policy is 'never'; STGPT was not called.",
    "history_resolution": (
        "An exact fingerprint match carried a recorded resolution; STGPT was not called."
    ),
}

# Verdicts the collector solves on its own; the model adds nothing (Step 4 gate).
_HARD_SHORT_CIRCUITS = frozenset(
    {"infra_runner", "infra_widespread", "flake_same_sha_passed", "no_failed_jobs"}
)


def _as_bool(value: object, default: bool | None = None) -> bool | None:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off", ""}:
        return False
    return default


def _deterministic_sufficient(summary: Summary) -> bool:
    """True when the deterministic card is trustworthy AND anchored at the failed step.

    High-confidence signature category + a terminal cause present + the card is
    grounded on the failed-step anchor + the winning rule is not R14/R18. Any
    miss (or internal error) returns False, so the model is still called.
    """
    try:
        from .diagnose import SIGNATURE_CATEGORIES, terminal_cause_present
        from .prompt import failed_step_anchor_text

        cls = summary.classification
        if cls.confidence != "high" or cls.category not in SIGNATURE_CATEGORIES:
            return False
        if not terminal_cause_present(summary):
            return False
        rule = summary.diagnosis.rule_id if summary.diagnosis is not None else None
        if rule in {"R14", "R18"}:
            return False
        card = analysis_result_from_summary(summary, source="deterministic")
        return is_grounded(card, failed_step_anchor_text(summary))
    except Exception:  # noqa: BLE001 — fail open toward analysis, never crash
        _LOG.exception("deterministic-sufficient gate error")
        return False


def decide_stgpt_call(
    summary: Summary,
    *,
    analyze_enabled: bool | str | None = True,
    requires_analysis: bool | str | None = None,
    stgpt_key_present: bool | str | None = None,
    mode: str | None = "collect",
    from_completion: object | None = None,
    api_key: str | None = None,
    policy: str | None = "auto",
) -> tuple[bool, str | None]:
    """Whether to call STGPT. Returns (call, reason_code).

    Requires mode=collect, analyze not explicitly false, and a key (or
    --from-completion). ``policy`` then refines the choice: ``always`` calls
    whenever possible; ``never`` never calls; ``auto`` (default) skips only when
    the deterministic answer is trustworthy AND anchored at the failed step
    (hard short-circuit, an anchored high-confidence signature, or an exact
    history resolution). ``requires_analysis`` is accepted for CLI compatibility
    and never gates the model on its own.
    """
    _ = requires_analysis
    if (mode or "collect") != "collect":
        return False, "mode_not_collect"
    if _as_bool(analyze_enabled, True) is False:
        return False, "analyze_disabled"
    key_present = _as_bool(stgpt_key_present, None)
    if key_present is None:
        key_present = bool(resolve_stgpt_api_key(api_key))
    if from_completion is None and not key_present:
        return False, "missing_stgpt_key"

    mode_policy = (policy or "auto").strip().lower()
    if mode_policy == "always":
        return True, None
    if mode_policy == "never":
        return False, "policy_never"

    # auto — skip only when the deterministic answer is trustworthy and anchored.
    if from_completion is not None:
        return True, None
    if summary.verdict.short_circuit in _HARD_SHORT_CIRCUITS:
        return False, "short_circuit"
    if _deterministic_sufficient(summary):
        return False, "deterministic_sufficient"
    hist = summary.history
    if (
        hist is not None
        and hist.match == "exact"
        and (hist.previous_resolution or "").strip()
    ):
        return False, "history_resolution"
    return True, None


def skipped_record(summary: Summary, reason_code: str) -> AnalysisRecord:
    """Build a skip record. Does not call STGPT."""
    human = SKIP_REASONS.get(reason_code, "STGPT was not called.")
    if reason_code == "short_circuit" and summary.verdict.short_circuit:
        human = (
            f"Short-circuit {summary.verdict.short_circuit}; STGPT was not called."
        )
    if reason_code == "deterministic_sufficient" and summary.diagnosis is not None:
        human = (
            f"{human} See deterministic rule {summary.diagnosis.rule_id}: "
            f"{summary.diagnosis.one_liner}"
        )
    return AnalysisRecord(
        status="skipped",
        prompt_version=PROMPT_VERSION,
        fingerprint=summary.fingerprint,
        schema_version=summary.schema_version or SCHEMA_VERSION,
        result=analysis_result_from_summary(summary, source="deterministic"),
        notes=[reason_code, human],
        analyzed_at=datetime.now(timezone.utc),
        reason_code=reason_code,
        model_called=False,
    )


def analysis_result_from_summary(
    summary: Summary, *, source: str = "deterministic"
) -> AnalysisResult:
    """Fill the four display fields from collector diagnosis. Never invent files."""
    from .diagnose import DeterministicVerdict, user_facing

    diag = summary.diagnosis
    verdict = DeterministicVerdict(
        category=summary.classification.category or "unknown",
        confidence=summary.classification.confidence or "low",
        is_infra_vs_code=summary.classification.is_infra_vs_code or "unknown",
        requires_analysis=summary.verdict.requires_analysis,
        rule_id=diag.rule_id if diag is not None else "R18",
        one_liner=(diag.one_liner if diag is not None else "") or "",
        suspected_stage=diag.suspected_stage if diag is not None else None,
        suspected_files=list(diag.suspected_files if diag is not None else []),
        citations=list(diag.citations if diag is not None else []),
        is_flaky=summary.classification.is_flaky,
        short_circuit=summary.verdict.short_circuit,
        fix_one_liner=diag.fix_one_liner if diag is not None else None,
    )
    result = user_facing(verdict, summary)
    result.source = source
    return result


def citations_from_summary(summary: Summary) -> list[AnalysisCitation]:
    """Citations only from collector evidence. Never fabricate files."""
    from .diagnose import display_citation_quotes

    quotes = display_citation_quotes(summary)
    out: list[AnalysisCitation] = []
    lockfiles = set()
    if summary.changes is not None:
        from .changes import classify_path

        lockfiles = {
            path
            for path in summary.changes.files
            if classify_path(path) in {"lockfile", "dependency", "ci_config", "container"}
        }
    pipeline_bits = []
    for stream in summary.pipeline_logs:
        for window in stream.windows:
            pipeline_bits.append(window.content)
    error_bits = []
    for job in summary.failed_jobs:
        for err in job.error_lines:
            error_bits.append(err.text)
        for window in job.windows:
            error_bits.append(window.content)
    last_green = []
    if summary.last_green_compare is not None:
        last_green.extend(summary.last_green_compare.novel_templates)
    last_sha = (
        summary.history.last_success_sha if summary.history is not None else None
    )
    for quote in quotes:
        source: str = "deterministic_rule"
        line = None
        if quote in lockfiles:
            source = "change_context"
        elif any(quote in bit for bit in pipeline_bits):
            source = "pipeline_logs"
        elif last_sha and quote == last_sha:
            source = "history"
        elif quote in last_green:
            source = "last_green_compare"
        elif any(quote in bit for bit in error_bits):
            source = "first_error_window"
            for job in summary.failed_jobs:
                for err in job.error_lines:
                    if err.text.strip() == quote:
                        line = err.line_number
                        break
        elif summary.diagnosis is not None and quote == summary.diagnosis.one_liner:
            continue
        out.append(
            AnalysisCitation(
                quote=quote[:240],
                source=source,  # type: ignore[arg-type]
                line=line,
            )
        )
        if len(out) >= 5:
            break
    return out


def ensure_display_result(
    summary: Summary, record: AnalysisRecord, *, source: str = "ai"
) -> AnalysisRecord:
    """Never leave root_cause / suggested_fix empty after a successful collect."""
    _ = source
    return finalize_user_card(summary, record)


def finalize_user_card(summary: Summary, record: AnalysisRecord) -> AnalysisRecord:
    """Pick the user-facing card, stamp grounding, and guarantee it is never blank.

    The confidence cap and ``grounded`` flag live here so they apply to the
    final chosen result whether its source is ai, hybrid, or deterministic; the
    never-blank guard then guarantees a non-empty root_cause + suggested_fix for
    every status (Step 5).
    """
    record = _finalize_card(summary, record)
    record = _stamp_grounding(summary, record)
    return _ensure_nonblank(summary, record)


def _ensure_nonblank(summary: Summary, record: AnalysisRecord) -> AnalysisRecord:
    """Fill root_cause / suggested_fix for every status; never leave them empty."""
    result = record.result
    root = (result.root_cause or "").strip() if result is not None else ""
    fix = (result.suggested_fix or "").strip() if result is not None else ""
    if result is not None and root and fix:
        return record

    try:
        det = analysis_result_from_summary(summary, source="deterministic")
    except Exception:  # noqa: BLE001 — never fail the workflow while rendering
        _LOG.exception("deterministic fallback error")
        det = None

    if result is None:
        base = (
            det.model_copy()
            if det is not None
            else AnalysisResult(
                root_cause="",
                suggested_fix="",
                confidence="low",
                source="deterministic",
            )
        )
    else:
        base = result.model_copy()
    if not (base.root_cause or "").strip():
        base.root_cause = (
            (det.root_cause if det is not None else "") or _last_resort_root(summary)
        )
    if not (base.suggested_fix or "").strip():
        base.suggested_fix = (
            (det.suggested_fix if det is not None else "") or _last_resort_fix(summary)
        )
    if not (base.source or "").strip():
        base.source = "deterministic"
    return record.model_copy(update={"result": base})


def _last_resort_root(summary: Summary) -> str:
    diag = summary.diagnosis
    if diag is not None and (diag.one_liner or "").strip():
        return diag.one_liner
    return "The collector could not determine a specific root cause from the evidence."


def _last_resort_fix(summary: Summary) -> str:
    from .diagnose import fix_for_rule

    diag = summary.diagnosis
    return fix_for_rule(diag.rule_id if diag is not None else None)


def _stamp_grounding(summary: Summary, record: AnalysisRecord) -> AnalysisRecord:
    """Record whether the final result cites the failed step; cap ungrounded high."""
    result = record.result
    if result is None:
        return record
    try:
        grounded = is_grounded(result, failed_step_anchor_text(summary))
    except Exception:  # noqa: BLE001 — never fail the workflow on the gate
        _LOG.exception("grounding stamp error")
        return record
    updates: dict[str, Any] = {"grounded": grounded}
    if not grounded and result.confidence == "high":
        updates["result"] = result.model_copy(update={"confidence": "medium"})
    return record.model_copy(update=updates)


def _attach_validation(
    summary: Summary, record: AnalysisRecord, evidence: str
) -> AnalysisRecord:
    """Post-hoc grounding (eval-spec §4) + validation/cost telemetry, then §4.4
    display policy. No model call. Never crashes: on error, grounding=None +
    caveat and the RCA is still published (a broken validator suppresses nothing).
    """
    try:
        from . import validate
        from .models import AnalysisResult

        result = record.result
        scored = result if result is not None else AnalysisResult(
            root_cause="", suggested_fix="", confidence="low"
        )
        grounding = validate.check_grounding(scored, evidence, summary)
        telemetry = _validation_telemetry(record)
        call = _call_telemetry(record, evidence)
        notes = list(record.notes)

        is_model = (
            result is not None
            and record.model_called
            and (result.source or "") in {"ai", "hybrid", "mixed"}
        )
        updates: dict[str, Any] = {
            "grounding": grounding,
            "validation_telemetry": telemetry,
            "call_telemetry": call,
        }
        if is_model and result is not None:
            action = validate.display_action(grounding)
            rate = grounding.grounding_rate
            if action == "strip":
                dropped = set(grounding.ungrounded_citations)
                kept = [c for c in result.citations if c.quote not in dropped]
                notes.append(
                    f"grounding: stripped {len(dropped)} ungrounded citation(s) "
                    f"(evidence rate {rate:.2f})"
                )
                summary.collection_notes.append(notes[-1])
                updates["result"] = result.model_copy(update={"citations": kept})
                updates["notes"] = notes
            elif action == "fallback":
                det = analysis_result_from_summary(summary, source="deterministic")
                if (result.root_cause or "").strip():
                    notes.append(f"model root_cause: {result.root_cause}")
                notes.append(
                    f"grounding: model answer not grounded in evidence "
                    f"(rate {rate:.2f}); using deterministic card"
                )
                summary.collection_notes.append(notes[-1])
                updates["result"] = det
                updates["status"] = "unvalidated"  # → needs-review (Step 5)
                updates["notes"] = notes
        record = record.model_copy(update=updates)
        return redact_record(record)
    except Exception as exc:  # noqa: BLE001 — a broken validator must not suppress
        _LOG.exception("grounding validation error")
        note = f"grounding validation skipped: {exc}"
        try:
            summary.collection_notes.append(note)
        except Exception:  # noqa: BLE001
            pass
        return record.model_copy(
            update={"grounding": None, "notes": list(record.notes) + [note]}
        )


def _validation_telemetry(record: AnalysisRecord) -> Any:
    from .models import ValidationTelemetry

    notes = list(record.notes)
    repair_attempted = any("repair" in note.lower() for note in notes)
    parse_error: str | None = None
    for note in notes:
        if "parse_error" in note.lower():
            parse_error = note[:200]
            break
    valid_first_try = (
        record.status in {"ok", "cached"} and not repair_attempted and parse_error is None
    )
    return ValidationTelemetry(
        schema_valid_first_try=valid_first_try,
        repair_attempted=repair_attempted,
        repair_succeeded=repair_attempted and record.status in {"ok", "cached"},
        parse_error=parse_error,
        fallback_used=bool(record.fallback_used),
    )


def _call_telemetry(record: AnalysisRecord, evidence: str) -> Any:
    from .models import CallTelemetry

    latency: int | None = None
    for resp in record.stgpt_responses or []:
        duration = resp.get("duration_ms") if isinstance(resp, dict) else None
        if isinstance(duration, (int, float)):
            latency = (latency or 0) + int(duration)
    completion = record.raw_completion if isinstance(record.raw_completion, str) else ""
    return CallTelemetry(
        prompt_tokens_est=len(evidence) // 4,
        completion_tokens_est=len(completion) // 4,
        latency_ms=latency,
        total_pipeline_ms=latency,
        short_circuited=not record.model_called,
        cache_hit=bool(record.cache_hit),
    )


def _finalize_card(summary: Summary, record: AnalysisRecord) -> AnalysisRecord:
    """Model card when ok; deterministic templates when the model is unusable.

    Deterministic output is input + fallback, not the user-facing answer, except
    when the model fails / is empty / cannot_determine, or when it contradicts
    a high-confidence infra/flake short-circuit.
    """
    det = analysis_result_from_summary(summary, source="deterministic")
    if record.status in {"skipped", "gated"}:
        result = record.result
        if (
            result is None
            or not (result.root_cause or "").strip()
            or not (result.suggested_fix or "").strip()
        ):
            return record.model_copy(update={"result": det})
        return record

    result = record.result
    already = (
        result is not None
        and (result.root_cause or "").strip()
        and (result.suggested_fix or "").strip()
        and (result.source or "") in {"ai", "mixed", "deterministic", "hybrid"}
    )
    if already and result is not None:
        if (
            record.status in {"ok", "cached"}
            and result.source == "ai"
            and _contradicts_infra_flake(summary, result)
        ):
            notes = list(record.notes)
            notes.append(f"model root_cause: {result.root_cause}")
            if (result.suggested_fix or "").strip():
                notes.append(f"model suggested_fix: {result.suggested_fix}")
            kept = det.model_copy(update={"source": "mixed"})
            return record.model_copy(update={"result": kept, "notes": notes})
        return record

    usable = (
        record.status in {"ok", "cached"}
        and result is not None
        and bool((result.root_cause or "").strip())
        and not result.cannot_determine
    )
    if usable and result is not None:
        if _contradicts_infra_flake(summary, result):
            notes = list(record.notes)
            notes.append(f"model root_cause: {result.root_cause}")
            if (result.suggested_fix or "").strip():
                notes.append(f"model suggested_fix: {result.suggested_fix}")
            kept = det.model_copy(update={"source": "mixed"})
            return record.model_copy(update={"result": kept, "notes": notes})
        source = "mixed" if (result.used_deterministic_rule or "").strip() else "ai"
        filled = result.model_copy(update={"source": source})
        if not (filled.suggested_fix or "").strip():
            filled.suggested_fix = det.suggested_fix
            filled.source = "mixed"
        if not filled.citations:
            filled.citations = det.citations
        if not filled.used_deterministic_rule:
            filled.used_deterministic_rule = det.used_deterministic_rule
        return record.model_copy(update={"result": filled})

    notes = list(record.notes)
    if result is not None and (result.root_cause or "").strip():
        notes.append(f"model root_cause: {result.root_cause}")
    # Safety net (Step 9): when the model cannot determine a cause AND the
    # deterministic card is weak (low-confidence + ungrounded, or its cause is a
    # benign line), do not ship it as a confident answer — say so and let the
    # section render needs-review, keeping the model's explanation in notes.
    if _deterministic_too_weak(summary, det, result):
        cannot = AnalysisResult(
            root_cause="Cannot determine the root cause from the current evidence.",
            suggested_fix="Inspect the failed step and pipeline logs; compare with last green.",
            confidence="low",
            source="deterministic",
            cannot_determine=True,
        )
        return record.model_copy(
            update={"result": cannot, "status": "unvalidated", "notes": notes}
        )
    return record.model_copy(update={"result": det, "notes": notes})


def _deterministic_too_weak(
    summary: Summary, det: AnalysisResult, model_result: AnalysisResult | None
) -> bool:
    """True when a cannot-determine model result should not fall back to ``det``."""
    if model_result is None or not model_result.cannot_determine:
        return False
    from .extract import is_benign_line

    if is_benign_line(det.root_cause or ""):
        return True
    if (det.confidence or "").strip().lower() == "low":
        try:
            return not is_grounded(det, failed_step_anchor_text(summary))
        except Exception:  # noqa: BLE001
            return True
    return False


def _record(
    base: AnalysisRecord,
    *,
    status: str,
    persona: str | None,
    result: AnalysisResult | None,
    fallback_used: bool,
    response_id: str | None,
    notes: list[str],
    raw_completion: str | None,
    stgpt_responses: list[dict[str, Any]] | None = None,
) -> AnalysisRecord:
    return base.model_copy(
        update={
            "status": status,
            "persona": persona,
            "result": result,
            "fallback_used": fallback_used,
            "response_id": response_id,
            "notes": notes,
            "raw_completion": raw_completion,
            "stgpt_responses": list(stgpt_responses or []),
            "model_called": True,
        }
    )


def _soft_fallback(
    base: AnalysisRecord,
    *,
    outcome: _Outcome,
    persona: str | None,
    fallback_used: bool,
    response_id: str | None,
    notes: list[str],
    raw_completion: str | None,
    stgpt_responses: list[dict[str, Any]] | None = None,
) -> AnalysisRecord:
    result = outcome.result
    if result is None and raw_completion and raw_completion.strip():
        result = _prose_result(raw_completion)
    if result is not None:
        result = result.model_copy(update={"cannot_determine": True})
    return _record(
        base,
        status="unvalidated",
        persona=persona,
        result=result,
        fallback_used=fallback_used,
        response_id=response_id,
        notes=notes,
        raw_completion=raw_completion,
        stgpt_responses=stgpt_responses,
    )


def _parse_note(label: str | None, why: str, completion: str) -> str:
    preview, _ = redact_text(completion)
    preview = " ".join(preview.split())[:_PREVIEW_CHARS]
    note, _ = redact_text(f"{label}: {why}; completion_preview={preview}")
    return note


def _prose_result(completion: str) -> AnalysisResult:
    text, _ = redact_text(completion.strip())
    return AnalysisResult(
        root_cause=text[:_PROSE_CAP],
        suggested_fix="",
        confidence="low",
        citations=[],
        cannot_determine=True,
    )


def _interpret_completion(completion: str, evidence: str) -> _Outcome:
    payload = _first_json_object(_strip_fences(completion))
    if not isinstance(payload, dict):
        return _Outcome("parse_error", "parse_error: no JSON object", _prose_result(completion))
    normalized = _normalize_payload(payload)
    try:
        result = AnalysisResult.model_validate(normalized)
    except Exception as exc:
        root = normalized.get("root_cause")
        if isinstance(root, str) and root.strip():
            result = AnalysisResult(
                root_cause=root.strip()[:_PROSE_CAP],
                suggested_fix=str(normalized.get("suggested_fix") or ""),
                confidence=_confidence(normalized.get("confidence")),
                citations=[],
                cannot_determine=True,
            )
            return _Outcome("unvalidated", f"parse_error: {exc}", result)
        return _Outcome("parse_error", f"parse_error: {exc}", _prose_result(completion))

    if not result.citations:
        result = result.model_copy(update={"cannot_determine": True})
        return _Outcome("unvalidated", "missing citations", result)
    missing = [
        cite.quote
        for cite in result.citations
        if cite.quote and cite.quote not in evidence
    ]
    if missing:
        result = result.model_copy(update={"cannot_determine": True})
        return _Outcome("unvalidated", "ungrounded: " + "; ".join(missing[:3]), result)
    return _Outcome("ok", "ok", result)


def _strip_fences(text: str) -> str:
    match = _FENCE_BLOCK.search(text)
    if match:
        return match.group(1).strip()
    return text.strip()


def _first_json_object(text: str) -> dict[str, Any] | None:
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(text, index)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _normalize_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    mapped: dict[str, Any] = {}
    for key, value in payload.items():
        mapped[_KEY_ALIASES.get(str(key), str(key))] = value
    root = mapped.get("root_cause")
    if not isinstance(root, str):
        for key in ("rootCause", "cause", "diagnosis"):
            value = payload.get(key)
            if isinstance(value, str):
                mapped["root_cause"] = value
                break
    fix = mapped.get("suggested_fix")
    if not isinstance(fix, str):
        for key in ("suggestedFix", "fix"):
            value = payload.get(key)
            if isinstance(value, str):
                mapped["suggested_fix"] = value
                break
        else:
            mapped["suggested_fix"] = ""
    mapped["confidence"] = _confidence(mapped.get("confidence"))
    mapped["citations"] = _normalize_citations(mapped.get("citations"))
    files = mapped.get("suspected_files")
    if isinstance(files, list):
        mapped["suspected_files"] = [str(item) for item in files if item]
    elif files:
        mapped["suspected_files"] = [str(files)]
    else:
        mapped["suspected_files"] = []
    stage = mapped.get("suspected_stage")
    mapped["suspected_stage"] = str(stage) if isinstance(stage, str) and stage.strip() else None
    side = mapped.get("infra_or_code")
    if isinstance(side, str) and side.strip().lower() in {"infra", "code", "unknown"}:
        mapped["infra_or_code"] = side.strip().lower()
    else:
        mapped["infra_or_code"] = None
    rule = mapped.get("used_deterministic_rule")
    mapped["used_deterministic_rule"] = (
        str(rule) if isinstance(rule, str) and rule.strip() else None
    )
    flag = mapped.get("cannot_determine")
    if isinstance(flag, str):
        mapped["cannot_determine"] = flag.strip().lower() in {"1", "true", "yes"}
    elif flag is None:
        mapped["cannot_determine"] = False
    else:
        mapped["cannot_determine"] = bool(flag)
    return mapped


def _confidence(value: Any) -> str:
    if isinstance(value, str) and value.strip().lower() in {"high", "medium", "low"}:
        return value.strip().lower()
    return "low"


def _normalize_citations(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        quote = item.get("quote") or item.get("text") or ""
        source = item.get("source") or item.get("section")
        if not isinstance(quote, str) or not quote:
            continue
        if source not in _CITATION_SOURCES:
            continue
        line = item.get("line")
        try:
            line_n = int(line) if line is not None else None
        except (TypeError, ValueError):
            line_n = None
        out.append({"quote": quote, "source": source, "line": line_n})
    return out


def _make_chat_fn(
    *,
    api_key: str | None,
    from_completion: Path | str | list[Any] | dict[str, Any] | None,
    transport: Any,
    url: str | None,
    client_app_name: str | None,
) -> ChatFn:
    if from_completion is not None:
        queue = _load_completions(from_completion)
        return queue
    key = resolve_stgpt_api_key(api_key)
    if not key:
        raise StgptError("missing STGPT_API")
    bridge = resolve_stgpt_api_url(url)
    app = resolve_stgpt_client_app_name(client_app_name)

    def _call(persona: str, messages: Sequence[Mapping[str, str]]) -> ChatResult:
        return post_chat(
            bridge,
            key,
            app,
            persona,
            messages,
            transport=transport,
        )

    return _call


def _load_completions(source: Path | str | list[Any] | dict[str, Any]) -> ChatFn:
    if isinstance(source, (str, Path)) and not isinstance(source, list):
        path = Path(source)
        payload = json.loads(path.read_text(encoding="utf-8"))
    else:
        payload = source
    if isinstance(payload, list):
        items = list(payload)
    else:
        items = [payload]
    remaining = [_as_chat_result(item) for item in items]

    def _call(persona: str, messages: Sequence[Mapping[str, str]]) -> ChatResult:
        if remaining:
            return remaining.pop(0)
        return ChatResult(200, {}, None, None)

    return _call


def _as_chat_result(item: Any) -> ChatResult:
    if isinstance(item, str):
        return ChatResult(200, {"completion": item}, item, None)
    if not isinstance(item, dict):
        return ChatResult(200, {}, None, None)
    if "completion" in item:
        raw = item["completion"]
        if isinstance(raw, dict):
            text = json.dumps(raw)
        elif raw is None:
            text = None
        else:
            text = str(raw)
        rid = item.get("id") or item.get("response_id")
        return ChatResult(
            int(item.get("status_code") or 200),
            item,
            text,
            str(rid) if rid else None,
        )
    return ChatResult(200, item, json.dumps(item), None)


def _cache_path(cache_dir: Path | str | None, key: str) -> Path | None:
    if cache_dir is None:
        return None
    root = Path(cache_dir)
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{key}.json"


def _cache_get(cache_dir: Path | str | None, key: str) -> AnalysisRecord | None:
    path = _cache_path(cache_dir, key)
    if path is None or not path.exists():
        return None
    try:
        record = AnalysisRecord.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if record.status not in {"ok", "cached"} or record.result is None:
        return None
    return record


def _cache_put(cache_dir: Path | str | None, key: str, record: AnalysisRecord) -> None:
    path = _cache_path(cache_dir, key)
    if path is None:
        return
    path.write_text(record.model_dump_json(indent=2) + "\n", encoding="utf-8")


def _redact_walk(value: Any) -> tuple[Any, int]:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        items = []
        total = 0
        for item in value:
            redacted, n = _redact_walk(item)
            items.append(redacted)
            total += n
        return items, total
    if isinstance(value, dict):
        mapped: dict[str, Any] = {}
        total = 0
        for key, item in value.items():
            redacted, n = _redact_walk(item)
            mapped[key] = redacted
            total += n
        return mapped, total
    return value, 0


_DIAGNOSIS_HEADER = "## AI Diagnosis"
_DISPLAY_STATUS = {
    "ok": "ok",
    "cached": "cached",
    "skipped": "deterministic",
    "gated": "deterministic",
}
_REVIEW_HINT = "answer not grounded to the failed step"


def display_status(record: AnalysisRecord) -> str:
    """User-facing status: ok | cached | deterministic | needs-review.

    Internal dead-end-looking states (skipped/failed/…) are normalized so the
    section always reads as a diagnosis, not a gap. The machine ``status`` field
    and the ``analysis-status`` output are unchanged.
    """
    return _DISPLAY_STATUS.get(record.status, "needs-review")


def _display_source(record: AnalysisRecord, *, model_called: bool) -> str:
    """Always one of ai | deterministic | hybrid | cached; never empty/unknown."""
    result = record.result
    src = ((result.source if result is not None else "") or "").strip().lower()
    if src == "mixed":
        return "hybrid"
    if src in {"ai", "deterministic", "hybrid", "cached"}:
        return src
    if record.status == "cached":
        return "cached"
    return "ai" if model_called else "deterministic"


def _upsert_diagnosis(
    markdown: str, record: AnalysisRecord, *, summary: Summary | None = None
) -> str:
    block = _diagnosis_markdown(record, summary=summary)
    marker = _DIAGNOSIS_HEADER
    if marker in markdown:
        prefix = markdown[: markdown.index(marker)].rstrip()
        return prefix + "\n\n" + block
    body = markdown.rstrip()
    if body:
        return body + "\n\n" + block
    return block


def _diagnosis_markdown(
    record: AnalysisRecord, *, summary: Summary | None = None
) -> str:
    called = "yes" if record.model_called else "no"
    if record.status in {"ok", "cached", "unvalidated"}:
        called = "yes"
    if record.status in {"skipped", "gated"}:
        called = "no"
    disp = display_status(record)
    lines = [
        _DIAGNOSIS_HEADER,
        "",
        f"**Status:** {disp}",
        f"**Model called:** {called}",
    ]
    reason = record.reason_code or (
        record.notes[0] if record.notes and record.status in {"skipped", "gated"} else None
    )
    if reason:
        lines.append(f"**Reason code:** {reason}")
    if record.persona:
        lines.append(f"**Persona:** {record.persona}")
    if record.cache_hit:
        lines.append("**Cache:** hit")

    result = record.result
    lines.append(f"**Source:** {_display_source(record, model_called=called == 'yes')}")
    if result is not None and result.cannot_determine:
        lines.append("**Cannot determine:** true")

    confidence = (result.confidence if result is not None else "low") or "low"
    if disp == "needs-review" and confidence == "high":
        confidence = "medium"
    root_cause = (result.root_cause if result is not None else "") or (
        "The collector could not determine a specific root cause from the evidence."
    )
    suggested_fix = (result.suggested_fix if result is not None else "") or (
        "Inspect the collector evidence; compare with last green."
    )
    lines.extend(
        [
            f"**Confidence:** {confidence}",
            "",
            f"**Root cause:** {root_cause}",
            "",
            f"**Suggested fix:** {suggested_fix}",
            "",
            "**Citations:**",
        ]
    )
    citations = result.citations if result is not None else []
    if citations:
        for cite in citations:
            loc = f" ({cite.source}" + (f":{cite.line}" if cite.line else "") + ")"
            lines.append(f"- `{cite.quote}`{loc}")
    else:
        lines.append("- (none)")

    render_notes = list(record.notes)
    render_notes.append(f"internal_status={record.status}")
    if disp == "needs-review":
        render_notes.append(f"review: {_REVIEW_HINT}")
    lines.extend(["", "**Notes:** " + "; ".join(render_notes)])
    return "\n".join(lines) + "\n"
