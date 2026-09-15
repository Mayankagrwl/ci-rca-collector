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
from .stgpt_client import ChatResult, StgptError, post_chat, public_request_url

_LOG = logging.getLogger(__name__)
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)
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

        if from_completion is None and not resolve_stgpt_api_key(api_key):
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
    analysis_path.write_text(
        record.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )

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
    payload["analysis"] = json.loads(record.model_dump_json())
    dest_json.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

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
    last_ungrounded: AnalysisResult | None = None
    last_id: str | None = None
    notes: list[str] = []
    fallback_used = False
    last_persona: str | None = None

    for index, persona in enumerate(PERSONAS):
        last_persona = persona
        if index > 0:
            fallback_used = True
            notes.append(f"fallback to {persona}")
        completion_text: str | None = None
        reason_text = "validation failed"
        for attempt in range(2):
            if attempt == 0:
                messages = build_messages(evidence)
            else:
                if not completion_text:
                    break
                messages = build_messages(
                    evidence,
                    prior_completion=completion_text,
                    repair_reason=reason_text,
                )
            try:
                chat = chat_fn(persona, messages)
            except StgptError as exc:
                notes.append(f"{persona}: bridge error: {exc}")
                return base.model_copy(
                    update={
                        "status": "failed",
                        "persona": persona,
                        "fallback_used": fallback_used,
                        "notes": notes,
                    }
                )
            if not (200 <= chat.status_code < 300):
                notes.append(_http_failure_note(chat, persona))
                return base.model_copy(
                    update={
                        "status": "failed",
                        "persona": persona,
                        "fallback_used": fallback_used,
                        "response_id": chat.response_id,
                        "notes": notes,
                    }
                )
            last_id = chat.response_id
            completion_text = chat.completion
            parsed, why, structured = _parse_and_validate(chat.completion, evidence)
            if parsed is not None:
                return base.model_copy(
                    update={
                        "status": "ok",
                        "persona": persona,
                        "result": parsed,
                        "fallback_used": fallback_used,
                        "response_id": last_id,
                        "notes": notes,
                    }
                )
            reason_text = why
            label = f"{persona} repair" if attempt else persona
            notes.append(f"{label}: {why}")
            if structured is not None:
                last_ungrounded = structured

    if last_ungrounded is not None:
        return base.model_copy(
            update={
                "status": "unvalidated",
                "persona": last_persona,
                "result": last_ungrounded,
                "fallback_used": fallback_used,
                "response_id": last_id,
                "notes": notes,
            }
        )
    return base.model_copy(
        update={
            "status": "failed",
            "persona": last_persona,
            "fallback_used": fallback_used,
            "response_id": last_id,
            "notes": notes or ["analyze failed"],
        }
    )


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


def _parse_and_validate(
    completion: str | None, evidence: str
) -> tuple[AnalysisResult | None, str, AnalysisResult | None]:
    result = _parse_result(completion)
    if result is None:
        return None, "parse_error", None
    missing = [
        cite.quote
        for cite in result.citations
        if cite.quote and cite.quote not in evidence
    ]
    if missing:
        return None, "ungrounded: " + "; ".join(missing[:3]), result
    return result, "ok", result


def _parse_result(completion: str | None) -> AnalysisResult | None:
    if completion is None:
        return None
    text = completion.strip()
    if not text:
        return None
    text = _FENCE.sub("", text).strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            payload = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    if not isinstance(payload, dict):
        return None
    try:
        return AnalysisResult.model_validate(payload)
    except Exception:
        return None


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
