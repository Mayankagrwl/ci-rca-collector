"""Branch memory (non-recurrence) and fingerprint HistoryStore.

Source-agnostic: operates on generic run records and FailureRecord.
Does not import GitHub clients.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from dateutil.parser import isoparse

from .drain_index import jaccard
from .models import FailureRecord

_SUCCESS = "success"
_FAIL = frozenset({"failure", "timed_out"})


@dataclass(frozen=True)
class BranchHistory:
    last_success_id: int | None = None
    last_success_sha: str | None = None
    last_success_at: datetime | None = None
    last_success_age_hours: float | None = None
    blame_range: str | None = None
    first_failing_sha: str | None = None
    recent_outcomes: str | None = None
    same_sha_ids: list[int] = field(default_factory=list)


def _as_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = isoparse(str(value))
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _short(sha: str | None) -> str:
    if not sha:
        return ""
    return sha[:7]


def _record_id(record: Mapping[str, Any]) -> int | None:
    raw = record.get("id")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _sha(record: Mapping[str, Any]) -> str:
    return str(record.get("head_sha") or record.get("sha") or "")


def _conclusion(record: Mapping[str, Any]) -> str:
    return str(record.get("conclusion") or "")


def _when(record: Mapping[str, Any]) -> datetime | None:
    return _as_dt(
        record.get("updated_at") or record.get("created_at") or record.get("run_started_at")
    )


def _newest_first(records: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    def key(item: Mapping[str, Any]) -> str:
        stamp = _when(item)
        return stamp.isoformat() if stamp else ""

    return sorted(records, key=key, reverse=True)


def summarize(
    *,
    head_sha: str,
    now: datetime,
    last_success: Mapping[str, Any] | None,
    branch_runs: Sequence[Mapping[str, Any]],
    same_sha: Sequence[Mapping[str, Any]],
    exclude_id: int | None = None,
    commit_shas: Sequence[str] | None = None,
) -> BranchHistory:
    """Derive last-green SHA, recent ✓✗ string, and first-failing SHA when unambiguous."""
    ordered = _newest_first(branch_runs)
    success = last_success
    if success is not None and exclude_id is not None and _record_id(success) == exclude_id:
        success = None
    if success is None:
        for item in ordered:
            if exclude_id is not None and _record_id(item) == exclude_id:
                continue
            if _conclusion(item) == _SUCCESS:
                success = item
                break

    success_sha = _sha(success) if success else None
    success_id = _record_id(success) if success else None
    success_at = _when(success) if success else None
    age = None
    if success_at is not None:
        aware_now = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
        age = max(0.0, (aware_now - success_at).total_seconds() / 3600.0)

    blame = None
    if success_sha and head_sha:
        blame = f"{_short(success_sha)}..{_short(head_sha)}"

    outcomes = _recent_outcomes(ordered, limit=10)
    first_fail = narrow_first_failing(
        ordered,
        head_sha=head_sha,
        last_success_sha=success_sha,
        exclude_id=exclude_id,
        commit_shas=commit_shas,
    )
    same_ids = [i for i in (_record_id(item) for item in same_sha) if i is not None]
    return BranchHistory(
        last_success_id=success_id,
        last_success_sha=success_sha or None,
        last_success_at=success_at,
        last_success_age_hours=age,
        blame_range=blame,
        first_failing_sha=first_fail,
        recent_outcomes=outcomes,
        same_sha_ids=same_ids,
    )


def _recent_outcomes(newest_first: Sequence[Mapping[str, Any]], *, limit: int) -> str | None:
    if not newest_first:
        return None
    slice_ = list(newest_first)[:limit]
    # Chronological: oldest on the left, current on the right.
    slice_.reverse()
    marks = []
    for item in slice_:
        marks.append("✓" if _conclusion(item) == _SUCCESS else "✗")
    return "".join(marks) if marks else None


def narrow_first_failing(
    branch_runs: Sequence[Mapping[str, Any]],
    *,
    head_sha: str,
    last_success_sha: str | None,
    exclude_id: int | None,
    commit_shas: Sequence[str] | None = None,
) -> str | None:
    """Set the first failing SHA only when the boundary is unambiguous.

    If ``commit_shas`` (oldest→newest in the compare range, excluding the last-green
    commit) is provided, every SHA must have a run or the result is None.
    Without that list, require at least one intermediate failed run between last
    success and the current head; a single hop is treated as a possible batch push.
    """
    if not last_success_sha:
        return None
    by_sha: dict[str, str] = {}
    for item in branch_runs:
        ident = _record_id(item)
        if exclude_id is not None and ident == exclude_id:
            # Still record the current head so a provided commit list can resolve.
            sha = _sha(item)
            if sha:
                by_sha.setdefault(sha, _conclusion(item) or "failure")
            continue
        sha = _sha(item)
        if sha and sha not in by_sha:
            by_sha[sha] = _conclusion(item)

    if commit_shas:
        return _from_commit_list(commit_shas, by_sha, last_success_sha, head_sha)

    # Walk newest→oldest until last success. Intermediate = runs strictly between.
    ordered = _newest_first(branch_runs)
    between: list[str] = []
    saw_success = False
    for item in ordered:
        ident = _record_id(item)
        sha = _sha(item)
        if not sha:
            continue
        if sha == last_success_sha or sha.startswith(last_success_sha[:7]):
            saw_success = True
            break
        if exclude_id is not None and ident == exclude_id:
            continue
        between.append(sha)
    if not saw_success or not between:
        return None
    # Chronological: last item is the oldest after last success.
    oldest_after = between[-1]
    conclusion = by_sha.get(oldest_after, "")
    if conclusion not in _FAIL:
        return None
    return oldest_after


def _from_commit_list(
    commit_shas: Sequence[str],
    by_sha: Mapping[str, str],
    last_success_sha: str,
    head_sha: str,
) -> str | None:
    # Range commits typically include last-green..head. Skip the green base.
    pending = [s for s in commit_shas if s and not s.startswith(last_success_sha[:7])]
    if not pending:
        pending = [head_sha] if head_sha else []
    if not pending:
        return None
    first_fail: str | None = None
    for sha in pending:
        full = _match_sha(sha, by_sha)
        if full is None:
            return None
        conclusion = by_sha.get(full, "")
        if conclusion == _SUCCESS:
            first_fail = None
            continue
        if first_fail is None:
            first_fail = full
    return first_fail


def _match_sha(prefix: str, by_sha: Mapping[str, str]) -> str | None:
    if prefix in by_sha:
        return prefix
    for key in by_sha:
        if key.startswith(prefix) or prefix.startswith(key[:7]):
            return key
    return None


class HistoryStore(Protocol):
    def get(self, fingerprint: str) -> FailureRecord | None: ...

    def find_by_coarse(self, coarse: str) -> list[FailureRecord]: ...

    def upsert(self, record: FailureRecord) -> None: ...

    def close(self) -> None: ...


class NoneHistoryStore:
    def get(self, fingerprint: str) -> FailureRecord | None:
        return None

    def find_by_coarse(self, coarse: str) -> list[FailureRecord]:
        return []

    def upsert(self, record: FailureRecord) -> None:
        return None

    def close(self) -> None:
        return None

    def all_records(self) -> list[FailureRecord]:
        return []


class CacheHistoryStore:
    """JSON files under a local directory. Weak durability (Phase 1 cache)."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, fingerprint: str) -> Path:
        safe = fingerprint.replace("/", "_")
        return self.root / f"{safe}.json"

    def get(self, fingerprint: str) -> FailureRecord | None:
        path = self._path(fingerprint)
        if not path.exists():
            return None
        return FailureRecord.model_validate_json(path.read_text(encoding="utf-8"))

    def find_by_coarse(self, coarse: str) -> list[FailureRecord]:
        return [rec for rec in self.all_records() if rec.fingerprint_coarse == coarse]

    def upsert(self, record: FailureRecord) -> None:
        existing = self.get(record.fingerprint)
        merged = existing.absorb(record) if existing is not None else record
        self._path(merged.fingerprint).write_text(
            merged.model_dump_json(indent=2) + "\n",
            encoding="utf-8",
        )

    def close(self) -> None:
        return None

    def all_records(self) -> list[FailureRecord]:
        records: list[FailureRecord] = []
        for path in sorted(self.root.glob("*.json")):
            try:
                records.append(
                    FailureRecord.model_validate_json(path.read_text(encoding="utf-8"))
                )
            except Exception:
                continue
        return records


def open_store(backend: str | None, path: str | Path | None) -> HistoryStore:
    name = (backend or "none").strip().lower()
    if name == "cache":
        return CacheHistoryStore(path or ".rca-history")
    return NoneHistoryStore()


@dataclass
class RecurrenceHit:
    record: FailureRecord | None
    match: str  # new | similar | exact
    similarity: float | None
    config_drift: bool = False


def _hash_ok(record: FailureRecord, current: str) -> bool:
    stored = record.masking_config_hash or ""
    if not stored:
        return False
    return stored == current


def lookup_recurrence(
    store: HistoryStore,
    *,
    fine: str,
    coarse: str,
    templates: Sequence[str],
    mask_hash: str,
) -> RecurrenceHit:
    try:
        exact = store.get(fine) if fine else None
        coarse_hits = store.find_by_coarse(coarse) if coarse else []
    except Exception:
        return RecurrenceHit(None, "new", None, False)

    if exact is not None and _hash_ok(exact, mask_hash):
        return RecurrenceHit(exact, "exact", 1.0, False)

    ok_coarse = [item for item in coarse_hits if _hash_ok(item, mask_hash)]
    if ok_coarse:
        best = max(ok_coarse, key=lambda item: item.last_seen)
        sim = jaccard(templates, best.templates)
        return RecurrenceHit(best, "similar", sim, False)

    pool: list[FailureRecord] = []
    if exact is not None:
        pool.append(exact)
    pool.extend(coarse_hits)
    extra = getattr(store, "all_records", None)
    if callable(extra):
        try:
            pool.extend(extra())
        except Exception:
            pass
    best_rec: FailureRecord | None = None
    best_j = 0.0
    seen: set[str] = set()
    for rec in pool:
        if rec.fingerprint in seen:
            continue
        seen.add(rec.fingerprint)
        score = jaccard(templates, rec.templates)
        if score > best_j:
            best_j = score
            best_rec = rec
    if best_rec is not None and best_j >= 0.6:
        drift = not _hash_ok(best_rec, mask_hash)
        return RecurrenceHit(best_rec, "similar", best_j, drift)
    return RecurrenceHit(None, "new", None, False)
