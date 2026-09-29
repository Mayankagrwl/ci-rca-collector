"""Delivery-owned recurring-failure issues + index issue (delivery spec v1.3 §8.0–§8.2).

One GitHub issue per **fine** fingerprint, holding a bot-owned body whose fenced
JSON block after ``<!-- rca-record -->`` is the ``FailureRecord``. An index issue
(``[RCA] fingerprint index``) maps fingerprint → issue number; a label scan is
the fallback. The Search API is never used.

``IssuesHistoryStore`` implements the ``history.HistoryStore`` protocol, but it
is constructed by delivery with an injected client — never by ``open_store`` —
so collect keeps ``history-backend: cache`` (§8.0). Nothing here raises: every
lookup or write failure becomes a note or an error on the store.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from dateutil import parser as date_parser

from ..classify import _side
from ..drain_index import template_hash
from ..github_api import GitHubAPIError
from ..models import FailureRecord, Summary
from ..redact import redact_text
from .owners import assignable_logins
from .render_comment import category_words, fence_for, safe_text
from .sticky import BUDGET_EXCEEDED, DeliveryBudget

LABEL_FINGERPRINT = "rca-fingerprint"
LABEL_INDEX = "rca-index"
LABEL_INFRA = "rca:infra"
LABEL_FLAKY = "rca:flaky"
INDEX_TITLE = "[RCA] fingerprint index"
ISSUE_MARKER_PREFIX = "<!-- rca-issue:fp="
INDEX_MARKER = "<!-- rca-index -->"
RECORD_MARKER = "<!-- rca-record -->"
BODY_CAP = 65_536
TITLE_CAP = 120
HEADLINE_CAP = 300
STALE_DAYS = 14
STALE_SWEEP_MAX = 5
ISSUES_SCOPE = "issues: write"
STALE_COMMENT = "No recurrence in 14 days — closing. Reopens automatically if it recurs."
MAINTAINED_NOTE = (
    "_This issue is maintained by the RCA bot. Comment below; edits to this body are overwritten._"
)
_FENCE_OPEN_RE = re.compile(r"^(`{3,})json\s*$")


def label_for_category(category: str | None) -> str:
    return f"rca:{(category or 'unknown').strip() or 'unknown'}"


# ---- record ------------------------------------------------------------------------------


def _clean(text: str | None) -> str | None:
    """Redact + defuse a stored string. Idempotent, so re-reads never drift."""
    if text is None:
        return None
    return safe_text(text, 10_000) if text.strip() else text


def sanitize_record(record: FailureRecord) -> FailureRecord:
    """Every free-text field that ends up in an issue body, made safe (AC #25)."""
    return record.model_copy(
        update={
            "templates": [t for t in (_clean(x) for x in record.templates) if t],
            "last_summary": _clean(record.last_summary),
            "branches": [b for b in (_clean(x) for x in record.branches) if b],
            "resolution": _clean(record.resolution),
        }
    )


def record_from_summary(summary: Summary, headline: str | None, now: datetime) -> FailureRecord:
    """The FailureRecord for this run. ``last_summary`` is the rendered headline, not a template."""
    drain = summary.drain
    templates = [t.template for t in (drain.templates if drain is not None else []) if t.template]
    record = FailureRecord(
        fingerprint=summary.fingerprint,
        fingerprint_coarse=summary.fingerprint_coarse,
        first_seen=now,
        last_seen=now,
        count=1,
        branches=[summary.run.head_branch] if summary.run.head_branch else [],
        run_ids=[summary.run.run_id],
        category=summary.classification.category or "unknown",
        templates=templates,
        template_hashes=[template_hash(t) for t in templates],  # hashed before sanitizing
        masking_config_hash=(drain.masking_config_hash if drain is not None else "") or "",
        last_summary=redact_text(headline)[0] if headline else None,
    )
    return sanitize_record(record)


# ---- body format -------------------------------------------------------------------------


def issue_marker(fingerprint: str) -> str:
    return f"{ISSUE_MARKER_PREFIX}{fingerprint} -->"


def marker_fingerprint(body: str | None) -> str | None:
    first = (body or "").lstrip("﻿").strip().split("\n", 1)[0].strip()
    if first.startswith(ISSUE_MARKER_PREFIX) and first.endswith(" -->"):
        return first[len(ISSUE_MARKER_PREFIX) : -len(" -->")] or None
    return None


def parse_record_block(body: str | None) -> Any:
    """The JSON value in the fence right after ``<!-- rca-record -->``; None when absent/bad."""
    text = (body or "").replace("\r\n", "\n")
    _, marker, after = text.partition(RECORD_MARKER)
    if not marker:
        return None
    lines = after.lstrip("\n").split("\n")
    if not lines:
        return None
    opener = _FENCE_OPEN_RE.match(lines[0].strip())
    if not opener:
        return None
    fence = opener.group(1)
    content: list[str] = []
    for line in lines[1:]:
        if line.strip() == fence:
            try:
                return json.loads("\n".join(content))
            except ValueError:
                return None
        content.append(line)
    return None


def parse_record(body: str | None) -> FailureRecord | None:
    payload = parse_record_block(body)
    if not isinstance(payload, dict):
        return None
    try:
        return FailureRecord.model_validate(payload)
    except Exception:  # noqa: BLE001 — unreadable record == no record
        return None


def _record_block(payload: Any) -> list[str]:
    text = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False, default=str)
    lines = text.split("\n")
    fence = fence_for(lines)
    return [RECORD_MARKER, f"{fence}json", *lines, fence]


def issue_title(record: FailureRecord, *, fork: bool = False) -> str:
    words = category_words(record.category)
    headline = safe_text(record.last_summary, HEADLINE_CAP, fork=fork) if record.last_summary else ""
    title = f"[RCA] {words}: {headline}" if headline else f"[RCA] {words}"
    return title if len(title) <= TITLE_CAP else title[: TITLE_CAP - 1].rstrip() + "…"


def _paragraphs(
    record: FailureRecord,
    *,
    fix: str | None,
    run_url: str | None,
    fork: bool,
    owners: list[str] | None,
) -> list[str]:
    """Body prose: the fix on its own paragraph, then history, then the owner line."""
    paragraphs: list[str] = []
    if fix:
        paragraphs.append(f"**Suggested fix** — {safe_text(fix, HEADLINE_CAP, fork=fork).rstrip('.')}.")
    history = [
        f"First seen {_day(record.first_seen)}, last seen {_day(record.last_seen)}; "
        f"seen {record.count}× on {', '.join(f'`{b}`' for b in record.branches) or 'unknown branches'}."
    ]
    latest = record.run_ids[-1] if record.run_ids else None
    if latest is not None:
        history.append(f"Latest run: [run {latest}]({run_url})." if run_url else f"Latest run: run {latest}.")
    if record.resolution:
        history.append(f"Resolution: {safe_text(record.resolution, HEADLINE_CAP, fork=fork)}")
    paragraphs.append(" ".join(history))
    line = owner_line(owners)
    if line:
        paragraphs.append(line)
    return paragraphs


def owner_line(owners: list[str] | None) -> str | None:
    """``Owner: `@org/team`, `@user``` — code spans, so nobody is pinged (pings are Step 18)."""
    shown = [safe_text(o, 100).replace("`", "") for o in (owners or []) if o]
    return "Owner: " + ", ".join(f"`{o}`" for o in shown) if shown else None


def render_issue_body(
    record: FailureRecord,
    *,
    fix: str | None = None,
    run_url: str | None = None,
    fork: bool = False,
    owners: list[str] | None = None,
) -> tuple[str, list[str]]:
    """(body, notes). Shrinks prose → templates → last_summary until ≤ BODY_CAP (JSON stays valid)."""
    notes: list[str] = []
    record = sanitize_record(record)
    prose = "\n\n".join(_paragraphs(record, fix=fix, run_url=run_url, fork=fork, owners=owners))

    # Sanitized once: redaction is the expensive step, and a rebuild must not repeat it.
    words = category_words(record.category)
    headline = safe_text(record.last_summary, HEADLINE_CAP, fork=fork) if record.last_summary else ""

    def build(rec: FailureRecord, prose_text: str, head: str) -> str:
        heading = f"### {words}: {head}" if head else f"### {words}"
        lines = [issue_marker(rec.fingerprint), heading, "", prose_text, "", MAINTAINED_NOTE, ""]
        lines += _record_block(json.loads(rec.model_dump_json()))
        return "\n".join(lines) + "\n"

    body = build(record, prose, headline)
    if len(body) > BODY_CAP:
        prose = f"Seen {record.count}×."
        body = build(record, prose, headline)
        notes.append("issue body over the cap: prose shortened")
    if len(body) > BODY_CAP and record.templates:
        # Largest template prefix that fits (binary search; each build is a full render).
        full = list(record.templates)
        lo, hi = 0, len(full) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if len(build(record.model_copy(update={"templates": full[:mid]}), prose, headline)) <= BODY_CAP:
                lo = mid
            else:
                hi = mid - 1
        record = record.model_copy(update={"templates": full[:lo]})
        body = build(record, prose, headline)
        notes.append(f"issue body over the cap: dropped {len(full) - lo} templates")
    if len(body) > BODY_CAP:
        record = record.model_copy(update={"last_summary": None})
        body = build(record, prose, "")
        notes.append("issue body over the cap: last_summary dropped")
    assert len(body) <= BODY_CAP, "issue body exceeds the 65,536 character cap"
    return body, notes


def render_index_body(index: dict[str, int]) -> str:
    lines = [
        INDEX_MARKER,
        "### RCA fingerprint index",
        "Maps each failure fingerprint to its tracking issue. "
        "Maintained by the RCA bot; edits to this body are overwritten.",
        "",
        *_record_block({fp: int(num) for fp, num in sorted(index.items())}),
    ]
    return "\n".join(lines) + "\n"


def recurrence_comment(summary: Summary, *, fork: bool = False) -> str:
    run = summary.run
    branch = safe_text(run.head_branch or "", 120, fork=fork).replace("`", "")
    sha = (run.head_sha or "")[:7]
    link = f"[run {run.run_id}]({run.html_url})"
    return f"Recurred in {link} on `{branch}` at `{sha}`."


# ---- store --------------------------------------------------------------------------------


@dataclass
class IssueResult:
    action: str  # created | updated | reopened | unchanged | failed | skipped | would …
    number: int | None = None
    url: str | None = None
    count_before: int | None = None
    count_after: int | None = None
    delivered: list[str] = field(default_factory=list)
    error: str | None = None
    # The issue's updated_at *before* this run touched it — the notification quiet-window
    # clock. Never this run's own write (that would make every run look quiet).
    previous_updated_at: datetime | None = None


@dataclass
class _WriteArgs:
    labels: list[str] | None
    fix: str | None
    run_url: str | None
    recurrence: str | None
    fork: bool
    owners: list[str]


def _issue_is_ours(issue: dict[str, Any]) -> bool:
    return isinstance(issue, dict) and "pull_request" not in issue


class IssuesHistoryStore:
    """HistoryStore over GitHub issues. Constructed by delivery only (§8.0)."""

    def __init__(
        self,
        client: Any,
        repo: str,
        *,
        budget: DeliveryBudget,
        clock: Callable[[], float],
        dry_run: bool = False,
    ) -> None:
        self.client = client
        self.repo = repo
        self.budget = budget
        self.clock = clock
        self.dry_run = dry_run
        self.notes: list[str] = []
        self.errors: list[str] = []
        self.write_failed = False
        self._index: dict[str, int] | None = None
        self._index_issue: dict[str, Any] | None = None
        self._index_state = "unloaded"  # ok | missing | corrupt | error
        self._index_dirty = False
        self._scan: list[dict[str, Any]] | None = None
        self._scan_failed = False

    # -- HistoryStore protocol --

    def get(self, fingerprint: str) -> FailureRecord | None:
        issue = self._find(fingerprint)
        return parse_record(issue.get("body")) if issue is not None else None

    def find_by_coarse(self, coarse: str) -> list[FailureRecord]:
        records = []
        for issue in self._scan_issues():
            record = parse_record(issue.get("body"))
            if record is not None and record.fingerprint_coarse == coarse:
                records.append(record)
        return records

    def upsert(self, record: FailureRecord) -> None:
        self.sync(record)

    def close(self) -> None:
        """Flush the index once (batched)."""
        try:
            self._flush_index()
        except Exception as exc:  # noqa: BLE001
            self._error("index write", exc)

    # -- delivery API --

    def sync(
        self,
        record: FailureRecord,
        *,
        labels: list[str] | None = None,
        fix: str | None = None,
        run_url: str | None = None,
        recurrence: str | None = None,
        fork: bool = False,
        owners: list[str] | None = None,
    ) -> IssueResult:
        """Create or maintain the issue for ``record.fingerprint``. Never raises.

        ``owners`` adds the body's owner line; ``@user`` owners are assigned best-effort.
        """
        try:
            return self._sync(
                record,
                _WriteArgs(labels, fix, run_url, recurrence, fork, list(owners or [])),
            )
        except Exception as exc:  # noqa: BLE001
            self._error("issue sync", exc)
            return IssueResult("failed", error=self.errors[-1])

    def sweep_stale(self, now: datetime, *, exclude: set[int] | None = None) -> list[int]:
        """Close ≤ STALE_SWEEP_MAX open issues with no recurrence in STALE_DAYS (live only)."""
        closed: list[int] = []
        if self.dry_run:
            return closed
        cutoff = now - timedelta(days=STALE_DAYS)
        for issue in self._scan_issues():
            if len(closed) >= STALE_SWEEP_MAX:
                break
            number = issue.get("number")
            if issue.get("state") != "open" or number in (exclude or set()):
                continue
            record = parse_record(issue.get("body"))
            if record is None or record.resolution or _aware(record.last_seen) >= cutoff:
                continue
            if not self._budget_ok(f"stale close #{number}"):
                break
            try:
                self.client.create_issue_comment(self.repo, number, STALE_COMMENT)
                self.client.update_issue(self.repo, number, state="closed")
                issue["state"] = "closed"
                closed.append(number)
            except Exception as exc:  # noqa: BLE001
                self._error(f"stale close #{number}", exc)
                break
        if closed:
            self.notes.append(f"stale sweep closed {', '.join(f'#{n}' for n in closed)}")
        return closed

    # -- internals: lookup --

    def _find(self, fingerprint: str) -> dict[str, Any] | None:
        index = self._load_index()
        number = index.get(fingerprint)
        if number is not None:
            try:
                issue = self.client.get_issue(self.repo, number)
            except Exception as exc:  # noqa: BLE001
                issue = None
                self.notes.append(f"index entry #{number} unreadable: {_describe(exc)}")
            if issue is not None and _issue_is_ours(issue) and marker_fingerprint(issue.get("body")) == fingerprint:
                return issue
            self.notes.append(f"index entry for {fingerprint[:12]} is stale; falling back to label scan")
        matches = self._scan_matches(fingerprint)
        if not matches:
            return None
        found = matches[0]
        if index.get(fingerprint) != found.get("number"):
            self._set_index(fingerprint, int(found["number"]))
        return found

    def _scan_issues(self, *, refresh: bool = False) -> list[dict[str, Any]]:
        if self._scan is None or refresh:
            try:
                items = self.client.list_issues(self.repo, labels=[LABEL_FINGERPRINT], state="all")
                self._scan = [i for i in items if _issue_is_ours(i)]
            except Exception as exc:  # noqa: BLE001
                self.notes.append(f"label scan failed: {_describe(exc)}")
                self._scan = []
                self._scan_failed = True
        return self._scan

    def _scan_matches(self, fingerprint: str, *, refresh: bool = False, open_only: bool = False) -> list[dict[str, Any]]:
        matches = [
            i
            for i in self._scan_issues(refresh=refresh)
            if marker_fingerprint(i.get("body")) == fingerprint
            and (not open_only or i.get("state") == "open")
        ]
        return sorted(matches, key=lambda i: int(i.get("number") or 0))

    def _load_index(self) -> dict[str, int]:
        if self._index is not None:
            return self._index
        self._index = {}
        try:
            candidates = [
                i
                for i in self.client.list_issues(self.repo, labels=[LABEL_INDEX], state="all")
                if _issue_is_ours(i) and i.get("title") == INDEX_TITLE
            ]
        except Exception as exc:  # noqa: BLE001
            self._index_state = "error"
            self.notes.append(f"index lookup failed: {_describe(exc)}")
            return self._index
        if not candidates:
            self._index_state = "missing"
            self.notes.append("index issue not found; using label scan")
            return self._index
        self._index_issue = min(candidates, key=lambda i: int(i.get("number") or 0))
        payload = parse_record_block(self._index_issue.get("body"))
        if isinstance(payload, dict) and all(isinstance(v, int) for v in payload.values()):
            self._index = {str(k): int(v) for k, v in payload.items()}
            self._index_state = "ok"
        else:
            self._index_state = "corrupt"
            self.notes.append(f"index issue #{self._index_issue.get('number')} unreadable; will be repaired")
        return self._index

    def _set_index(self, fingerprint: str, number: int) -> None:
        index = self._load_index()
        if index.get(fingerprint) != number:
            index[fingerprint] = number
            self._index_dirty = True

    def _flush_index(self) -> None:
        index = self._load_index()
        if self._index_state in {"corrupt", "missing"} and self._scan is not None:
            # Repair: fold in every fingerprint the label scan saw (lowest number wins).
            for issue in sorted(self._scan, key=lambda i: -int(i.get("number") or 0)):
                fp = marker_fingerprint(issue.get("body"))
                if fp and index.get(fp) != issue.get("number"):
                    index[fp] = int(issue["number"])
                    self._index_dirty = True
        if self._index_state == "corrupt":
            self._index_dirty = True
        if not self._index_dirty or not index or self._index_state == "error":
            return
        body = render_index_body(index)
        if len(body) > BODY_CAP:
            self.notes.append("index over the body cap; not written (label scan still works)")
            return
        if self.dry_run:
            verb = "update" if self._index_issue is not None else "create"
            self.notes.append(f"would {verb} the index issue ({len(index)} entries)")
            return
        if not self._budget_ok("index write"):
            return
        if self._index_issue is None:
            created = self.client.create_issue(self.repo, INDEX_TITLE, body, labels=[LABEL_INDEX])
            self._index_issue = created
            self.notes.append(f"index issue created as #{created.get('number')}")
        else:
            self.client.update_issue(self.repo, int(self._index_issue["number"]), body=body)
        self._index_state = "ok"
        self._index_dirty = False

    # -- internals: write --

    def _sync(self, record: FailureRecord, args: "_WriteArgs") -> IssueResult:
        record = sanitize_record(record)
        issue = self._find(record.fingerprint)
        if issue is None and self._scan_failed:
            # Never create blind: an existing issue we could not see would be duplicated.
            self.errors.append("issue skipped: existing issues could not be listed")
            self.write_failed = True
            return IssueResult("skipped", error=self.errors[-1])
        if issue is None:
            return self._create(record, args)
        return self._update(issue, record, args)

    def _update(self, issue: dict[str, Any], record: FailureRecord, args: "_WriteArgs") -> IssueResult:
        number = int(issue["number"])
        url = issue.get("html_url")
        prior = parse_record(issue.get("body"))
        if prior is None:
            self.notes.append(f"#{number} has no readable record; rewriting it from this run")
            merged, before = record, None
        else:
            merged, before = prior.absorb(record), prior.count
        bumped = before is None or merged.count != before
        body, notes = render_issue_body(
            merged, fix=args.fix, run_url=args.run_url, fork=args.fork, owners=args.owners
        )
        self.notes.extend(notes)
        title = issue_title(merged, fork=args.fork)
        closed = issue.get("state") == "closed"
        result = IssueResult("unchanged", number, url, before, merged.count)
        result.previous_updated_at = _parse_time(issue.get("updated_at"))
        if not closed and body == issue.get("body") and title == issue.get("title"):
            self._assign(issue, args.owners, result)
            return result
        if self.dry_run:
            result.action = f"would reopen #{number}" if closed else (
                f"would update issue #{number} (count {before}→{merged.count})"
            )
            return result
        if not self._budget_ok(f"issue #{number} update"):
            return IssueResult("skipped", number, url, before, merged.count, error=BUDGET_EXCEEDED)
        try:
            self.client.update_issue(
                self.repo, number, title=title, body=body, state="open" if closed else None
            )
        except Exception as exc:  # noqa: BLE001
            return self._failed(f"issue #{number} update", exc, number, url)
        issue.update(body=body, title=title, state="open")
        result.action = "reopened" if closed else "updated"
        result.delivered.append("issue:reopened" if closed else "issue:updated")
        if bumped and args.recurrence and self._budget_ok(f"issue #{number} recurrence comment"):
            try:
                self.client.create_issue_comment(self.repo, number, args.recurrence)
            except Exception as exc:  # noqa: BLE001 — the issue itself is up to date
                self._error(f"issue #{number} recurrence comment", exc)
        self._set_index(record.fingerprint, number)
        self._assign(issue, args.owners, result)
        return result

    def _create(self, record: FailureRecord, args: "_WriteArgs") -> IssueResult:
        body, notes = render_issue_body(
            record, fix=args.fix, run_url=args.run_url, fork=args.fork, owners=args.owners
        )
        self.notes.extend(notes)
        title = issue_title(record, fork=args.fork)
        labels = args.labels or default_labels(record.category)
        if self.dry_run:
            return IssueResult("would create issue", count_after=record.count)
        if not self._budget_ok("issue create"):
            return IssueResult("skipped", error=BUDGET_EXCEEDED)
        try:
            # Never pass assignees here: an unassignable login must not fail the create.
            created = self.client.create_issue(self.repo, title, body, labels=labels)
        except GitHubAPIError as exc:
            if exc.status_code is None:
                # Timeout: it may have landed. Look once more; never create twice.
                landed = self._scan_matches(record.fingerprint, refresh=True)
                if landed:
                    created = landed[0]
                else:
                    return self._failed("issue create", exc, None, None, " (not retried)")
            else:
                return self._failed("issue create", exc, None, None)
        except Exception as exc:  # noqa: BLE001
            return self._failed("issue create", exc, None, None)
        number = int(created["number"])
        result = IssueResult("created", number, created.get("html_url"), None, record.count)
        result.delivered.append("issue:created")
        self._set_index(record.fingerprint, number)
        kept = self._self_heal(record.fingerprint, number, result)
        if kept is not None:
            # Ours was closed as a duplicate: record this occurrence on the kept issue.
            update = self._update(kept, record, args)
            result.delivered.extend(update.delivered)
            result.count_before, result.count_after = update.count_before, update.count_after
            result.previous_updated_at = update.previous_updated_at
            result.url = update.url or result.url
            if update.error:
                result.error = update.error
        else:
            self._assign(created, args.owners, result)
        return result

    def _self_heal(self, fingerprint: str, number: int, result: IssueResult) -> dict[str, Any] | None:
        """Two concurrent runs may both create: keep the lowest, close the rest as duplicates.

        Returns the kept issue when it is not the one just created (else None).
        """
        open_matches = self._scan_matches(fingerprint, refresh=True, open_only=True)
        numbers = [int(i["number"]) for i in open_matches]
        if number not in numbers:
            numbers.append(number)
        if len(numbers) < 2:
            return None
        keep = min(numbers)
        for dup in sorted(n for n in numbers if n != keep):
            if not self._budget_ok(f"duplicate close #{dup}"):
                break
            try:
                self.client.create_issue_comment(self.repo, dup, f"Duplicate of #{keep}")
                self.client.update_issue(self.repo, dup, state="closed")
                result.delivered.append("issue:duplicate-closed")
                self.notes.append(f"closed #{dup} as a duplicate of #{keep}")
            except Exception as exc:  # noqa: BLE001
                self._error(f"duplicate close #{dup}", exc)
        self._set_index(fingerprint, keep)
        kept = next((i for i in open_matches if int(i["number"]) == keep), None)
        result.number = keep
        if kept is not None:
            result.url = kept.get("html_url")
        return kept if keep != number else None

    def _assign(self, issue: dict[str, Any], owners: list[str], result: IssueResult) -> None:
        """Best-effort: assign ``@user`` owners when the issue has nobody assigned yet."""
        logins = assignable_logins(owners)
        if not logins or self.dry_run:
            return
        number = int(issue["number"])
        if issue.get("assignees"):
            self.notes.append(f"#{number} already has assignees; left as is")
            return
        if self.budget.exceeded(self.clock()):
            self.notes.append(f"could not assign #{number}: {BUDGET_EXCEEDED}")
            return
        try:
            response = self.client.add_assignees(self.repo, number, logins)
        except Exception as exc:  # noqa: BLE001 — a note, never a delivery error
            self.notes.append(f"could not assign #{number} to {', '.join(logins)}: {_describe(exc)}")
            return
        # GitHub answers 201 but silently drops logins it cannot assign.
        present = {
            str(a.get("login", "")).lower()
            for a in (response.get("assignees") or [])
            if isinstance(a, dict)
        }
        landed = [login for login in logins if login.lower() in present]
        for login in logins:
            if login not in landed:
                self.notes.append(f"GitHub did not assign {login} to #{number} (not assignable)")
        issue["assignees"] = list(response.get("assignees") or [])
        if landed:
            result.delivered.append("issue:assigned")

    # -- internals: bookkeeping --

    def _budget_ok(self, what: str) -> bool:
        if self.budget.exceeded(self.clock()):
            self.errors.append(f"{what} skipped: {BUDGET_EXCEEDED}")
            return False
        return True

    def _error(self, what: str, exc: BaseException) -> None:
        status = getattr(exc, "status_code", None)
        if status == 403:
            self.errors.append(f"{what} forbidden (403): the token needs `{ISSUES_SCOPE}`")
        else:
            self.errors.append(f"{what} failed: {_describe(exc)}")

    def _failed(self, what: str, exc: BaseException, number: int | None, url: str | None, suffix: str = "") -> IssueResult:
        self._error(what, exc)
        self.write_failed = True
        return IssueResult("failed", number, url, error=self.errors[-1] + suffix)


def default_labels(
    category: str | None, *, infra: bool | None = None, flaky: bool = False
) -> list[str]:
    labels = [LABEL_FINGERPRINT, label_for_category(category)]
    is_infra = infra if infra is not None else _side(category or "") == "infra"
    if is_infra:
        labels.append(LABEL_INFRA)
    if flaky:
        labels.append(LABEL_FLAKY)
    return labels


def _describe(exc: BaseException) -> str:
    status = getattr(exc, "status_code", None)
    if isinstance(exc, GitHubAPIError):
        return f"{exc} (status {status})" if status is not None else str(exc)
    return type(exc).__name__


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return _aware(date_parser.isoparse(value))
    except (ValueError, OverflowError):
        return None


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _day(value: datetime | str) -> str:
    if isinstance(value, str):
        try:
            value = date_parser.isoparse(value)
        except (ValueError, OverflowError):
            return value
    return _aware(value).strftime("%Y-%m-%d %H:%M UTC")


__all__ = [
    "BODY_CAP",
    "INDEX_TITLE",
    "IssueResult",
    "IssuesHistoryStore",
    "LABEL_FINGERPRINT",
    "LABEL_INDEX",
    "default_labels",
    "issue_marker",
    "issue_title",
    "marker_fingerprint",
    "owner_line",
    "parse_record",
    "parse_record_block",
    "record_from_summary",
    "recurrence_comment",
    "render_index_body",
    "render_issue_body",
    "sanitize_record",
]
