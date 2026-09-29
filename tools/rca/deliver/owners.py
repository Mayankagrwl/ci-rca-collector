"""Failure ownership: CODEOWNERS (last match wins) + the v1.3 §9 precedence. Pure.

No client, no HTTP, no file I/O: callers pass the CODEOWNERS text (read at the
default branch by the loader) and get back who owns the failure and why.

Pattern semantics follow GitHub's CODEOWNERS (gitignore-style): a leading or
middle ``/`` anchors to the repo root, a slash-less pattern matches at any depth,
a trailing ``/`` means everything under that directory, ``*`` never crosses
``/``, ``**`` does, and ``dir/*`` matches direct children only. Lines GitHub
would reject (``!`` negation, ``[...]`` ranges, malformed owners) are skipped
with a note — never fatal.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

MAX_OWNERS = 10
MAX_SUSPECTED_PATHS = 20
MAX_CHANGED_PATHS = 50
MAX_ASSIGNEES = 10
INFRA_SHORT_CIRCUITS = frozenset({"infra_runner", "infra_widespread"})
_CATCH_ALL = frozenset({"*", "**", "/*", "/**"})
_USER_RE = re.compile(r"^@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?$")
_TEAM_RE = re.compile(r"^@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?/[A-Za-z0-9._-]+$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_INLINE_COMMENT_RE = re.compile(r"\s#")
_DRIVE_RE = re.compile(r"^[A-Za-z]:$")


@dataclass(frozen=True)
class OwnerRule:
    pattern: str
    owners: tuple[str, ...]
    line_no: int
    regex: re.Pattern[str] = field(compare=False, repr=False, default=re.compile(""))

    @property
    def catch_all(self) -> bool:
        return self.pattern in _CATCH_ALL


@dataclass(frozen=True)
class OwnerMatch:
    owners: tuple[str, ...]
    pattern: str
    line_no: int
    catch_all: bool
    path: str = ""  # the repo path that matched


@dataclass
class OwnerResolution:
    owners: list[str]
    resolved_by: str  # platform_team | codeowners_suspected | codeowners_changes | actor | default_notify | none
    notes: list[str] = field(default_factory=list)


# ---- parsing --------------------------------------------------------------------------------


def is_valid_owner(token: str) -> bool:
    return bool(_USER_RE.match(token) or _TEAM_RE.match(token) or _EMAIL_RE.match(token))


def parse_codeowners(text: str | None) -> tuple[list[OwnerRule], list[str]]:
    """(rules, notes). Invalid / unsupported lines are skipped with a note."""
    rules: list[OwnerRule] = []
    notes: list[str] = []
    body = (text or "").lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    for line_no, raw in enumerate(body.split("\n"), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        comment = _INLINE_COMMENT_RE.search(line)
        if comment:
            line = line[: comment.start()].rstrip()
        tokens = line.split()
        if not tokens:
            continue
        pattern, owners = tokens[0], tuple(tokens[1:])
        if pattern.startswith("\\#"):
            pattern = pattern[1:]
        if pattern.startswith("!") or "[" in pattern or "]" in pattern:
            notes.append(f"CODEOWNERS line {line_no}: unsupported pattern {pattern!r} skipped")
            continue
        bad = [o for o in owners if not is_valid_owner(o)]
        if bad:
            notes.append(f"CODEOWNERS line {line_no}: invalid owner {bad[0]!r}; line skipped")
            continue
        try:
            regex = pattern_regex(pattern)
        except re.error:
            notes.append(f"CODEOWNERS line {line_no}: unparseable pattern {pattern!r} skipped")
            continue
        rules.append(OwnerRule(pattern, owners, line_no, regex))
    return rules, notes


def pattern_regex(pattern: str) -> re.Pattern[str]:
    """gitignore-style CODEOWNERS pattern → a regex over repo-relative paths."""
    if pattern in {"/", "*", "**", "/*", "/**"}:
        return re.compile(r"^.+$")
    dir_only = pattern.endswith("/")
    core = pattern.strip("/")
    anchored = pattern.startswith("/") or "/" in core
    segments = core.split("/")
    parts: list[str] = []
    for index, seg in enumerate(segments):
        last = index == len(segments) - 1
        if seg == "**":
            parts.append(".*" if last else "(?:.*/)?")
            continue
        parts.append(_glob(seg) + ("" if last else "/"))
    body = "".join(parts)
    prefix = "^" if anchored else "^(?:.*/)?"
    if dir_only:
        suffix = "/.*$"
    elif segments[-1] == "*":
        suffix = "$"  # dir/* — direct children only
    else:
        suffix = "(?:/.*)?$"  # a file, or a directory and everything under it
    return re.compile(prefix + body + suffix)


def _glob(segment: str) -> str:
    out = []
    i = 0
    while i < len(segment):
        char = segment[i]
        if char == "*":
            if segment[i : i + 2] == "**":
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(char))
        i += 1
    return "".join(out)


# ---- matching ---------------------------------------------------------------------------------


def owners_for(path: str, rules: list[OwnerRule]) -> OwnerMatch | None:
    """The last matching rule wins (v1.3 §9). Case-sensitive."""
    path = path.lstrip("/")
    for rule in reversed(rules):
        if rule.regex.match(path):
            return OwnerMatch(rule.owners, rule.pattern, rule.line_no, rule.catch_all, path)
    return None


def repo_relative_candidates(path: str, *, workspace: str | None) -> list[str]:
    """Repo-relative spellings of ``path``, longest first."""
    p = (path or "").strip().replace("\\", "/")
    if p.startswith("file://"):
        p = p[len("file://") :]
    if workspace:
        ws = workspace.strip().replace("\\", "/").rstrip("/")
        if ws and p.startswith(ws + "/"):
            return [p[len(ws) + 1 :]]
    segs = [s for s in p.split("/") if s and s != "."]
    if segs and _DRIVE_RE.match(segs[0]):
        segs = segs[1:]
    if not p.startswith("/") and not _DRIVE_RE.match(p.split("/")[0]) and ".." not in segs:
        return ["/".join(segs)] if segs else []
    segs = [s for s in segs if s != ".."]
    return ["/".join(segs[i:]) for i in range(len(segs))]


def resolve_path_owners(
    path: str,
    rules: list[OwnerRule],
    *,
    workspace: str | None,
    known_files: list[str] | None = None,
    notes: list[str] | None = None,
) -> OwnerMatch | None:
    """Pick one candidate: a known repo file, else the longest non-catch-all match, else catch-all.

    A known file maps only when it is a suffix of a candidate (always safe). The
    reverse — a known file *ending in* the path — is allowed only for the original
    relative path with ≥ 2 segments and exactly one such known file; a bare or
    ambiguous name (``pom.xml``, ``index.ts``) must never borrow another file's owner.
    """
    candidates = repo_relative_candidates(path, workspace=workspace)
    known = [k.strip("/") for k in (known_files or []) if k]
    for cand in candidates:
        hits = [k for k in known if cand == k or cand.endswith("/" + k)]
        if hits:
            return owners_for(max(hits, key=len), rules)
    original = _original_relative(path, workspace)
    if original and original.count("/") >= 1:
        reverse = [k for k in known if k.endswith("/" + original)]
        if len(reverse) == 1:
            return owners_for(reverse[0], rules)
        if len(reverse) > 1 and notes is not None:
            notes.append(
                f"{original} matches {len(reverse)} changed files; not mapped (ambiguous)"
            )
    catch_all: OwnerMatch | None = None
    for cand in candidates:
        match = owners_for(cand, rules)
        if match is None:
            continue
        if not match.catch_all:
            return match
        if catch_all is None:
            catch_all = match
    return catch_all


def _original_relative(path: str, workspace: str | None) -> str | None:
    """The path as given, when it was already repo-relative (or workspace-relative)."""
    candidates = repo_relative_candidates(path, workspace=workspace)
    p = (path or "").strip().replace("\\", "/")
    if p.startswith("file://"):
        p = p[len("file://") :]
    ws = (workspace or "").strip().replace("\\", "/").rstrip("/")
    first = p.split("/", 1)[0]
    relative = not p.startswith("/") and not _DRIVE_RE.match(first)
    if (relative or (ws and p.startswith(ws + "/"))) and len(candidates) == 1:
        return candidates[0]
    return None


# ---- resolution (v1.3 §9) -------------------------------------------------------------------------


def split_owners(value: str | None) -> list[str]:
    return [t for t in re.split(r"[\s,]+", value or "") if t]


def assignable_logins(owners: list[str]) -> list[str]:
    """``@user`` owners as bare logins; teams and emails can't be issue assignees."""
    logins = [o[1:] for o in owners if _USER_RE.match(o)]
    return _dedupe(logins)[:MAX_ASSIGNEES]


def resolve_owner(
    summary: Any,
    analysis: Any,
    context: Any,
    inputs: Any,
    rules: list[OwnerRule] | None,
    *,
    workspace: str | None,
) -> OwnerResolution:
    """First step that yields owners wins: platform → suspected → changes → actor → default."""
    notes: list[str] = []
    cls = summary.classification
    infra = cls.is_infra_vs_code == "infra" or summary.verdict.short_circuit in INFRA_SHORT_CIRCUITS
    if infra:
        team = split_owners(inputs.platform_team)
        if team:
            return OwnerResolution(_cap(team), "platform_team", notes)
        notes.append("infra failure without platform-team: never blamed on the actor or CODEOWNERS")
        return _default(inputs, notes)

    known = list(summary.changes.files) if summary.changes is not None else []
    if rules is None:
        notes.append("CODEOWNERS unavailable; file-based ownership skipped")
    else:
        suspected = _dedupe(_trusted_analysis_files(analysis) + _diagnosis_files(summary))
        owners = _union(suspected[:MAX_SUSPECTED_PATHS], rules, workspace, known, notes)
        if owners:
            return OwnerResolution(_cap(owners), "codeowners_suspected", notes)
        if known:
            notes.append("changes.files carry no change counts; used in listed order")
            owners = _union(_dedupe(known)[:MAX_CHANGED_PATHS], rules, workspace, known, notes)
            if owners:
                return OwnerResolution(_cap(owners), "codeowners_changes", notes)

    actor = (context.actor or "").strip()
    if actor and not actor.endswith("[bot]") and context.trigger != "schedule":
        return OwnerResolution([actor if actor.startswith("@") else f"@{actor}"], "actor", notes)
    if actor and actor.endswith("[bot]"):
        notes.append("actor is a bot; not an owner")
    elif actor and context.trigger == "schedule":
        notes.append("schedule trigger: the cron actor is not the owner")
    return _default(inputs, notes)


def _default(inputs: Any, notes: list[str]) -> OwnerResolution:
    fallback = split_owners(inputs.default_notify)
    if fallback:
        return OwnerResolution(_cap(fallback), "default_notify", notes)
    return OwnerResolution([], "none", notes)


def _trusted_analysis_files(analysis: Any) -> list[str]:
    if analysis is None or getattr(analysis, "result", None) is None:
        return []
    from ..analyze import display_status  # lazy: analyze pulls in the STGPT client

    if display_status(analysis) not in {"ok", "cached"}:
        return []
    return list(analysis.result.suspected_files or [])


def _diagnosis_files(summary: Any) -> list[str]:
    diag = summary.diagnosis
    return list(diag.suspected_files or []) if diag is not None else []


def _union(
    paths: list[str],
    rules: list[OwnerRule],
    workspace: str | None,
    known: list[str],
    notes: list[str] | None = None,
) -> list[str]:
    owners: list[str] = []
    for path in paths:
        match = resolve_path_owners(path, rules, workspace=workspace, known_files=known, notes=notes)
        if match is None:
            continue
        owners.extend(match.owners)  # an explicitly unowned match adds nothing
    return _dedupe(owners)


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _cap(owners: list[str]) -> list[str]:
    return _dedupe(owners)[:MAX_OWNERS]


__all__ = [
    "OwnerMatch",
    "OwnerResolution",
    "OwnerRule",
    "assignable_logins",
    "is_valid_owner",
    "owners_for",
    "parse_codeowners",
    "pattern_regex",
    "repo_relative_candidates",
    "resolve_owner",
    "resolve_path_owners",
    "split_owners",
]
