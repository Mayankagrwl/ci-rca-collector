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
    STGPT_CLIENT_APP_NAME,
    TOKEN_BUDGET_TOTAL,
    resolve_stgpt_api_key,
    resolve_stgpt_api_url,
)
from .models import AnalysisRecord, AnalysisResult, Summary
from .prompt import build_evidence, build_messages
from .redact import redact_text
from .stgpt_client import (
    ChatResult,
    StgptError,
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
}
ChatFn = Callable[[str, Sequence[Mapping[str, str]]], ChatResult]


def cache_key(summary: Summary) -> str:
    """sha256(fine|category|masking_hash|prompt_version)[:16]."""
    fine = summary.fingerprint or ""
    category = summary.classification.category or ""
    masking = ""
    if summary.drain is not None and summary.drain.masking_config_hash:
        masking = summary.drain.masking_config_hash
    raw = f"{fine}|{category}|{masking}|{PROMPT_VERSION}"
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
        gated = _gate(summary)
        if gated is not None:
            return base.model_copy(update=gated)

        cached = _cache_get(cache_dir, key)
        if cached is not None:
            return cached.model_copy(
                update={
                    "status": "cached",
                    "cache_hit": True,
                    "analyzed_at": now,
                    "prompt_version": PROMPT_VERSION,
                    "fingerprint": summary.fingerprint,
                }
            )

        if (
            chat_fn is None
            and from_completion is None
            and not resolve_stgpt_api_key(api_key)
        ):
            return base.model_copy(
                update={
                    "status": "gated",
                    "notes": ["skipped: missing STGPT_API"],
                }
            )

        evidence = build_evidence(summary, cap_tokens=TOKEN_BUDGET_TOTAL)
        caller = chat_fn or _make_chat_fn(
            api_key=api_key,
            from_completion=from_completion,
            transport=transport,
            url=url,
            client_app_name=client_app_name,
        )
        record = _run_personas(evidence, caller, base)
        redacted = redact_record(record)
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
    dest_md.write_text(_upsert_diagnosis(existing, record), encoding="utf-8")


def redact_record(record: AnalysisRecord) -> AnalysisRecord:
    payload = record.model_dump(mode="json")
    cleaned, _ = _redact_walk(payload)
    return AnalysisRecord.model_validate(cleaned)


def _gate(summary: Summary) -> dict[str, Any] | None:
    if summary.verdict.requires_analysis is False:
        return {
            "status": "gated",
            "notes": ["skipped: requires_analysis is false"],
        }
    if summary.verdict.short_circuit:
        return {
            "status": "gated",
            "notes": [f"skipped: short_circuit={summary.verdict.short_circuit}"],
        }
    return None


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
    app = client_app_name or STGPT_CLIENT_APP_NAME

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


def _upsert_diagnosis(markdown: str, record: AnalysisRecord) -> str:
    block = _diagnosis_markdown(record)
    marker = "## AI diagnosis"
    if marker in markdown:
        prefix = markdown[: markdown.index(marker)].rstrip()
        return prefix + "\n\n" + block
    body = markdown.rstrip()
    if body:
        return body + "\n\n" + block
    return block


def _diagnosis_markdown(record: AnalysisRecord) -> str:
    lines = ["## AI diagnosis", "", f"**Status:** {record.status}"]
    if record.persona:
        lines.append(f"**Persona:** {record.persona}")
    if record.cache_hit:
        lines.append("**Cache:** hit")
    result = record.result
    if result is not None:
        if result.cannot_determine:
            lines.append("**Cannot determine:** true")
        lines.extend(
            [
                f"**Confidence:** {result.confidence}",
                "",
                f"**Root cause:** {result.root_cause}",
                "",
                f"**Suggested fix:** {result.suggested_fix}",
            ]
        )
        if result.citations:
            lines.extend(["", "**Citations:**"])
            for cite in result.citations:
                loc = f" ({cite.source}" + (f":{cite.line}" if cite.line else "") + ")"
                lines.append(f"- `{cite.quote}`{loc}")
    if record.notes:
        lines.extend(["", "**Notes:** " + "; ".join(record.notes)])
    return "\n".join(lines) + "\n"
