"""Non-recurrence history: last success, first failing SHA, outcomes."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from tools.rca.history import CacheHistoryStore, open_store, summarize
from tools.rca.models import FailureRecord

NOW = datetime(2026, 9, 8, 16, 0, tzinfo=timezone.utc)


def _run(
    ident: int,
    sha: str,
    conclusion: str,
    *,
    when: str,
) -> dict:
    return {
        "id": ident,
        "head_sha": sha,
        "conclusion": conclusion,
        "created_at": when,
        "updated_at": when,
        "name": "CI",
    }


def test_last_success_age_and_outcomes() -> None:
    last = _run(10, "aaa1111bbbb", "success", when="2026-09-08T12:00:00Z")
    hist = summarize(
        head_sha="ccc3333dddd",
        now=NOW,
        last_success=last,
        branch_runs=[
            _run(12, "ccc3333dddd", "failure", when="2026-09-08T15:00:00Z"),
            _run(11, "bbb2222eeee", "failure", when="2026-09-08T14:00:00Z"),
            last,
        ],
        same_sha=[_run(12, "ccc3333dddd", "failure", when="2026-09-08T15:00:00Z")],
        exclude_id=12,
    )
    assert hist.last_success_sha == "aaa1111bbbb"
    assert hist.last_success_id == 10
    assert hist.last_success_age_hours == 4.0
    assert hist.blame_range == "aaa1111..ccc3333"
    assert hist.recent_outcomes == "✓✗✗"
    assert hist.first_failing_sha == "bbb2222eeee"
    assert hist.same_sha_ids == [12]


def test_first_failing_none_when_no_intermediate_runs() -> None:
    last = _run(10, "aaa1111bbbb", "success", when="2026-09-08T12:00:00Z")
    hist = summarize(
        head_sha="ccc3333dddd",
        now=NOW,
        last_success=last,
        branch_runs=[
            _run(12, "ccc3333dddd", "failure", when="2026-09-08T15:00:00Z"),
            last,
        ],
        same_sha=[],
        exclude_id=12,
    )
    assert hist.last_success_sha == "aaa1111bbbb"
    assert hist.first_failing_sha is None


def test_first_failing_from_complete_commit_list() -> None:
    last = _run(10, "aaa1111bbbb", "success", when="2026-09-08T12:00:00Z")
    hist = summarize(
        head_sha="ccc3333dddd",
        now=NOW,
        last_success=last,
        branch_runs=[
            _run(12, "ccc3333dddd", "failure", when="2026-09-08T15:00:00Z"),
            last,
        ],
        same_sha=[],
        exclude_id=12,
        commit_shas=["ccc3333dddd"],
    )
    assert hist.first_failing_sha == "ccc3333dddd"


def test_cache_upsert_is_idempotent(tmp_path: Path) -> None:
    store = CacheHistoryStore(tmp_path)
    rec = FailureRecord(
        fingerprint="abc123abc123abcd",
        fingerprint_coarse="def456def456def0",
        first_seen=NOW,
        last_seen=NOW,
        count=1,
        branches=["main"],
        run_ids=[99],
        category="dependency",
        templates=["npm ERR!"],
        template_hashes=["aaaaaaaaaaaaaaaa"],
        masking_config_hash="aaaaaaaaaaaa",
    )
    store.upsert(rec)
    store.upsert(rec)
    loaded = store.get("abc123abc123abcd")
    assert loaded is not None
    assert loaded.count == 1
    assert loaded.run_ids == [99]
    store.close()


def test_none_store_is_a_noop() -> None:
    store = open_store("none", None)
    assert store.get("x") is None
    store.upsert(
        FailureRecord(
            fingerprint="x",
            fingerprint_coarse="y",
            first_seen=NOW,
            last_seen=NOW,
            category="unknown",
            masking_config_hash="a" * 12,
        )
    )
    assert store.find_by_coarse("y") == []
    store.close()


def test_history_module_has_no_github_ids() -> None:
    source = (
        Path(__file__).resolve().parents[2] / "tools" / "rca" / "history.py"
    ).read_text(encoding="utf-8")
    assert "job_id" not in source
    assert "run_id" not in source
    assert "github_api" not in source
