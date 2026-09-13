"""Change context from a compare payload. GitHub REST is fetched by github_api.py."""

from __future__ import annotations

import fnmatch
import re
from typing import Any, Iterable, Literal, Mapping, Sequence

from dateutil.parser import isoparse

from .config import MAX_COMMITS, PR_BODY_EXCERPT_CHARS
from .models import ChangeContext, CommitInfo

RangeBasis = Literal["last_success", "merge_base", "head_only"]

LOCKFILE_NAMES = (
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "poetry.lock",
    "Pipfile.lock",
    "go.sum",
    "Gemfile.lock",
    "Cargo.lock",
)

_REVERT_SUBJECT = re.compile(r'^Revert "')
_QUOTED_VERSION = re.compile(r'"([^"]+)":\s*"(\d+\.[^"]*)"')
_GO_SUM = re.compile(r"^([^\s]+)\s+(v[0-9][^\s]*)")
_YARN = re.compile(r'^"?([^@\s"]+)@([^":]+)"?:')
_SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)")


def resolve_compare_base(
    last_success_sha: str | None,
    merge_base_sha: str | None,
) -> tuple[str | None, RangeBasis]:
    if last_success_sha:
        return last_success_sha, "last_success"
    if merge_base_sha:
        return merge_base_sha, "merge_base"
    return None, "head_only"


def classify_path(path: str) -> str:
    posix = path.replace("\\", "/")
    name = posix.rsplit("/", 1)[-1]
    lowered = posix.lower()
    if (
        posix.startswith(".github/workflows/")
        or posix.startswith(".github/actions/")
        or name in {"action.yml", "action.yaml"}
    ):
        return "ci_config"
    if (
        name.startswith("Dockerfile")
        or name.endswith(".dockerfile")
        or name == ".dockerignore"
        or fnmatch.fnmatch(name, "Dockerfile*")
    ):
        return "container"
    if name in LOCKFILE_NAMES:
        return "lockfile"
    if (
        name in {
            "package.json",
            "pyproject.toml",
            "go.mod",
            "pom.xml",
            "Gemfile",
            "Cargo.toml",
        }
        or fnmatch.fnmatch(name, "requirements*.txt")
        or fnmatch.fnmatch(name, "build.gradle*")
    ):
        return "dependency"
    if (
        name in {"Makefile", "tsconfig.json"}
        or fnmatch.fnmatch(name, "webpack.*")
        or fnmatch.fnmatch(name, "vite.*")
        or fnmatch.fnmatch(name, "babel.*")
        or fnmatch.fnmatch(name, ".eslintrc*")
    ):
        return "build_config"
    if (
        name.endswith(".tf")
        or "/helm/" in posix
        or posix.startswith("helm/")
        or "/k8s/" in posix
        or posix.startswith("k8s/")
        or (_under_deploy(posix) and name.endswith((".yaml", ".yml")))
    ):
        return "infra"
    if _is_test_path(lowered, name):
        return "test"
    return "source"


def _under_deploy(posix: str) -> bool:
    return any(part in {"deploy", "deployments", "deployment"} for part in posix.split("/"))


def _is_test_path(lowered: str, name: str) -> bool:
    parts = lowered.split("/")
    if any(p in {"test", "tests", "spec", "__tests__"} for p in parts[:-1]):
        return True
    if ".test." in name or ".spec." in name:
        return True
    if name.startswith("test_") and name.endswith(".py"):
        return True
    if name.endswith("_test.go"):
        return True
    return False


def is_revert(subject: str, body: str = "") -> bool:
    if _REVERT_SUBJECT.match(subject):
        return True
    return "This reverts commit" in (body or "")


def change_context_from_compare(
    payload: Mapping[str, Any] | None,
    *,
    head_sha: str,
    range_basis: RangeBasis,
    base_sha: str | None,
    pull: Mapping[str, Any] | None = None,
    stack_paths: Sequence[str] = (),
) -> ChangeContext:
    if range_basis == "head_only" or payload is None:
        ctx = ChangeContext(
            base_sha=base_sha,
            head_sha=head_sha,
            range_basis=range_basis,
        )
        return _with_pr(ctx, pull)

    commits_raw = list(payload.get("commits") or [])
    total_commits = int(payload.get("total_commits") or len(commits_raw) or 0)
    files_raw = list(payload.get("files") or [])
    truncated = bool(payload.get("truncated")) or (
        total_commits > len(commits_raw) if commits_raw or total_commits else False
    )
    if len(files_raw) >= 300:
        truncated = True

    merge_base = payload.get("merge_base_commit") or {}
    if isinstance(merge_base, Mapping) and merge_base.get("sha") and not base_sha:
        base_sha = str(merge_base["sha"])

    commits = [_commit_info(item) for item in reversed(commits_raw)]
    commits = commits[:MAX_COMMITS]

    paths = [str(f.get("filename") or "") for f in files_raw if f.get("filename")]
    classes = _unique_classes(paths)
    additions = sum(int(f.get("additions") or 0) for f in files_raw)
    deletions = sum(int(f.get("deletions") or 0) for f in files_raw)
    lockfile_deltas = {
        name: deltas
        for name, deltas in (
            (str(f.get("filename")), lockfile_delta_from_patch(str(f.get("filename") or ""), f.get("patch")))
            for f in files_raw
        )
        if deltas
    }
    stack_diffs = _stack_diffs(files_raw, stack_paths)
    diffstat = _diffstat(files_raw)

    ctx = ChangeContext(
        base_sha=base_sha,
        head_sha=head_sha,
        range_basis=range_basis,
        range_truncated=truncated,
        total_commits=total_commits,
        commits=commits,
        files_changed=len(paths),
        additions=additions,
        deletions=deletions,
        diffstat=diffstat,
        classes=classes,
        lockfile_deltas=lockfile_deltas,
        stack_trace_diffs=stack_diffs,
    )
    return _with_pr(ctx, pull)


def _unique_classes(paths: Iterable[str]) -> list[str]:
    order = (
        "ci_config",
        "container",
        "dependency",
        "lockfile",
        "build_config",
        "infra",
        "test",
        "source",
    )
    present = {classify_path(p) for p in paths if p}
    return [c for c in order if c in present]


def _commit_info(item: Mapping[str, Any]) -> CommitInfo:
    sha = str(item.get("sha") or "")[:7]
    inner = item.get("commit") if isinstance(item.get("commit"), Mapping) else {}
    message = str((inner or {}).get("message") or item.get("message") or "")
    subject = message.split("\n", 1)[0]
    author = None
    user = item.get("author")
    if isinstance(user, Mapping) and user.get("login"):
        author = str(user["login"])
    elif isinstance(inner, Mapping):
        a = inner.get("author")
        if isinstance(a, Mapping) and a.get("name"):
            author = str(a["name"])
    authored = None
    if isinstance(inner, Mapping):
        a = inner.get("author") if isinstance(inner.get("author"), Mapping) else {}
        authored = (a or {}).get("date")
    try:
        authored_at = isoparse(str(authored)) if authored else isoparse("1970-01-01T00:00:00Z")
    except (ValueError, TypeError):
        authored_at = isoparse("1970-01-01T00:00:00Z")
    parents = item.get("parents") or []
    files = item.get("files")
    files_changed = len(files) if isinstance(files, list) else 0
    return CommitInfo(
        sha=sha,
        subject=subject,
        author=author,
        authored_at=authored_at,
        files_changed=files_changed,
        is_revert=is_revert(subject, message),
        is_merge=isinstance(parents, list) and len(parents) > 1,
    )


def _with_pr(ctx: ChangeContext, pull: Mapping[str, Any] | None) -> ChangeContext:
    if not pull:
        return ctx
    labels = []
    for item in pull.get("labels") or []:
        if isinstance(item, Mapping) and item.get("name"):
            labels.append(str(item["name"]))
        elif isinstance(item, str):
            labels.append(item)
    body = pull.get("body") or ""
    excerpt = str(body)[:PR_BODY_EXCERPT_CHARS] if body else None
    ctx.pr_title = pull.get("title")
    ctx.pr_labels = labels
    ctx.pr_is_draft = bool(pull.get("draft"))
    ctx.pr_body_excerpt = excerpt
    return ctx


def lockfile_delta_from_patch(filename: str, patch: str | None) -> list[str]:
    """Turn a lockfile patch into version transitions. Never return the raw lockfile."""
    name = filename.replace("\\", "/").rsplit("/", 1)[-1]
    if name not in LOCKFILE_NAMES or not patch:
        return []
    removed: dict[str, str] = {}
    added: dict[str, str] = {}
    for line in patch.splitlines():
        if line.startswith("+++") or line.startswith("---") or line.startswith("@@"):
            continue
        if line.startswith("-"):
            for pkg, ver in _versions_on_line(name, line[1:]):
                removed[pkg] = ver
        elif line.startswith("+"):
            for pkg, ver in _versions_on_line(name, line[1:]):
                added[pkg] = ver
    rows: list[tuple[int, str]] = []
    for pkg in sorted(set(removed) | set(added)):
        old, new = removed.get(pkg), added.get(pkg)
        if old and new and old != new:
            kind = _bump_kind(old, new)
            rank = {"major": 0, "minor": 1, "patch": 2, "changed": 3}.get(kind, 3)
            rows.append((rank, f"{pkg} {old} → {new} ({kind})"))
        elif new and not old:
            rows.append((4, f"+ {pkg} {new} (added)"))
        elif old and not new:
            rows.append((5, f"- {pkg} {old} (removed)"))
    rows.sort(key=lambda item: (item[0], item[1]))
    rendered = [text for _, text in rows]
    extra = len(rendered) - 15
    if extra > 0:
        rendered = rendered[:15] + [f"… {extra} more changes"]
    return rendered


def _versions_on_line(lock_name: str, line: str) -> list[tuple[str, str]]:
    stripped = line.strip()
    found: list[tuple[str, str]] = []
    if lock_name == "go.sum":
        m = _GO_SUM.match(stripped)
        if m:
            found.append((m.group(1), m.group(2)))
        return found
    if lock_name in {"yarn.lock", "pnpm-lock.yaml"}:
        m = _YARN.match(stripped)
        if m:
            found.append((m.group(1), m.group(2)))
    for m in _QUOTED_VERSION.finditer(stripped):
        pkg, ver = m.group(1), m.group(2)
        if pkg in {"version", "resolved", "integrity"}:
            continue
        found.append((pkg, ver))
    return found


def _bump_kind(old: str, new: str) -> str:
    a = _SEMVER.match(old)
    b = _SEMVER.match(new)
    if not a or not b:
        return "changed"
    ai = [int(a.group(i)) for i in range(1, 4)]
    bi = [int(b.group(i)) for i in range(1, 4)]
    if bi[0] != ai[0]:
        return "major"
    if bi[1] != ai[1]:
        return "minor"
    if bi[2] != ai[2]:
        return "patch"
    return "changed"


def _diffstat(files: Sequence[Mapping[str, Any]], *, cap: int = 20) -> str | None:
    if not files:
        return None
    lines = []
    for item in files[:cap]:
        name = item.get("filename") or ""
        add = int(item.get("additions") or 0)
        delete = int(item.get("deletions") or 0)
        lines.append(f"{name:30} | {add + delete:4} {'+' * min(add, 10)}{'-' * min(delete, 10)}")
    extra = len(files) - cap
    if extra > 0:
        lines.append(f"… {extra} more files")
    return "\n".join(lines)


def _stack_diffs(
    files: Sequence[Mapping[str, Any]],
    stack_paths: Sequence[str],
) -> dict[str, str]:
    if not stack_paths:
        return {}
    wanted = {p.replace("\\", "/") for p in stack_paths}
    out: dict[str, str] = {}
    for item in files:
        name = str(item.get("filename") or "").replace("\\", "/")
        if name not in wanted and not any(name.endswith(p) or p.endswith(name) for p in wanted):
            continue
        patch = item.get("patch")
        if not patch:
            continue
        hunk = "\n".join(str(patch).splitlines()[:100])
        out[name] = hunk
        if len(out) >= 3:
            break
    return out
