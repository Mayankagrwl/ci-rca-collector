"""``/resolved`` feedback (delivery spec v1.3 §12). The parser is pure.

A human closes the loop by commenting ``/resolved <what fixed it>`` (optionally
``/resolved #123 …`` to target one fingerprint issue). The ``feedback`` CLI
subcommand applies it: it records the resolution on the fingerprint issue's
record (``human_verified=True``), closes the issue as completed, and reacts 👍.

Comment text is untrusted input: it is only ever read from the event JSON file,
made safe with ``safe_text`` (redacted, @mentions and rule ids defused, capped),
and only honoured from trusted ``author_association`` values.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .render_comment import safe_text

RESOLVE_COMMAND = "/resolved"
TEXT_CAP = 300
ASSOCIATIONS_ENV = "RCA_RESOLVE_ASSOCIATIONS"
DEFAULT_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
_COMMAND_RE = re.compile(r"^/resolved(?:[ \t]+(?P<rest>.*))?$")
_TARGET_RE = re.compile(r"^#(?P<number>\d+)(?:[ \t]+(?P<rest>.*))?$")
_FENCE_RE = re.compile(r"^(`{3,}|~{3,})")


@dataclass(frozen=True)
class ResolvedCommand:
    target_issue: int | None
    text: str


def _first_line(body: str | None) -> tuple[str | None, list[str]]:
    """(first non-blank raw line, the lines after it)."""
    lines = (body or "").lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    for index, line in enumerate(lines):
        if line.strip():
            return line, lines[index + 1 :]
    return None, []


def parse_resolved(body: str | None) -> ResolvedCommand | None:
    """``/resolved [#N] <text>`` on the first non-blank line, else None. Never raises."""
    return _parse(body)[0]


def why_not_resolved(body: str | None) -> str:
    """Human reason ``parse_resolved`` returned None (for the report)."""
    return _parse(body)[1]


def _parse(body: str | None) -> tuple[ResolvedCommand | None, str]:
    try:
        first, rest_lines = _first_line(body)
        if first is None:
            return None, "empty comment"
        indent = len(first) - len(first.lstrip(" \t"))
        line = first.strip()
        if indent >= 4 or _FENCE_RE.match(line):
            return None, "command inside a code block"
        if line.startswith(">"):
            return None, "command inside a quote"
        match = _COMMAND_RE.match(line)
        if not match:
            return None, "first line is not a /resolved command"
        rest = (match.group("rest") or "").strip()
        target: int | None = None
        token = _TARGET_RE.match(rest)
        if token:
            target = int(token.group("number"))
            rest = (token.group("rest") or "").strip()
        raw = " ".join([rest, *rest_lines])
        text = safe_text(" ".join(raw.split()), TEXT_CAP)
        if not text:
            return None, "/resolved has no text (say what fixed it)"
        return ResolvedCommand(target, text), "ok"
    except Exception:  # noqa: BLE001 — a parser must never crash the handler
        return None, "unparseable comment"


def allowed_associations(env: Mapping[str, str]) -> frozenset[str]:
    raw = (env.get(ASSOCIATIONS_ENV) or "").strip()
    if not raw:
        return DEFAULT_ASSOCIATIONS
    return frozenset(part.strip().upper() for part in raw.split(",") if part.strip())


def is_bot(user: Mapping[str, Any] | None) -> bool:
    user = user or {}
    login = str(user.get("login") or "")
    return login.endswith("[bot]") or str(user.get("type") or "") == "Bot"


__all__ = [
    "ASSOCIATIONS_ENV",
    "DEFAULT_ASSOCIATIONS",
    "RESOLVE_COMMAND",
    "ResolvedCommand",
    "allowed_associations",
    "is_bot",
    "parse_resolved",
    "why_not_resolved",
]
