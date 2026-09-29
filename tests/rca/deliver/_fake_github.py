"""In-memory fake GitHub REST API for delivery tests (no network).

Served to a real ``GitHubClient`` through ``httpx.MockTransport`` so request
methods, paths, query strings and JSON bodies are exactly what production sends.
Every request is recorded; ``transcript()`` renders them one per line.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qs

import httpx

from tools.rca.github_api import GitHubClient

API = "https://api.github.com"
WEB = "https://github.example"
_REPO_PATH = re.compile(r"^/repos/(?P<owner>[^/]+)/(?P<name>[^/]+)(?P<rest>/.*)?$")


@dataclass
class Recorded:
    method: str
    path: str
    query: str
    json: Any = None

    def line(self) -> str:
        query = f"?{self.query}" if self.query else ""
        payload = ""
        if isinstance(self.json, dict):
            shown = {}
            for key, value in self.json.items():
                if isinstance(value, str) and len(value) > 60:
                    first = value.split("\n", 1)[0]
                    shown[key] = f"<{len(value)} chars; first line {first!r}>"
                else:
                    shown[key] = value
            payload = " " + json.dumps(shown, ensure_ascii=False)
        return f"{self.method:5} {self.path}{query}{payload}"


@dataclass
class Fault:
    """Inject a failure on the next matching request (consumed once).

    ``status``: return that HTTP status. ``timeout``: raise before any effect.
    ``lost_response``: apply the write, then raise a timeout (it landed).
    """

    method: str
    path_re: str
    status: int | None = None
    timeout: bool = False
    lost_response: bool = False
    times: int = 1


@dataclass
class FakeGitHub:
    default_branch: str = "main"
    tags: set[str] = field(default_factory=set)
    runs: list[dict[str, Any]] = field(default_factory=list)
    page_size: int = 100
    # Stamps start 6h before real "now": old enough to be outside any quiet window,
    # recent enough for stale-sweep maths; tests seed explicit times when they care.
    start: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=6)
    )
    issue_comments: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    commit_comments: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    labels: dict[int, list[str]] = field(default_factory=dict)
    issues: dict[int, dict[str, Any]] = field(default_factory=dict)
    reactions: list[tuple[int, str]] = field(default_factory=list)  # (comment_id, content)
    # Repo files served by the contents API: {(path, ref): text}; ref None = any ref.
    files: dict[tuple[str, str | None], str] = field(default_factory=dict)
    # Paths the contents API reports as over 1 MB (content "", encoding "none").
    large_files: set[str] = field(default_factory=set)
    # Repo roles for GET .../collaborators/{login}/permission; missing login → 404.
    permissions: dict[str, str] = field(default_factory=dict)
    # When set, POST .../assignees silently drops logins outside it (like GitHub).
    assignable: set[str] | None = None
    requests: list[Recorded] = field(default_factory=list)
    faults: list[Fault] = field(default_factory=list)
    # Called after each request is served (e.g. to simulate a concurrent run).
    after_request: list[Any] = field(default_factory=list)
    _next_id: int = 1000
    _next_issue: int = 100
    _ticks: int = 0

    # ---- client ----------------------------------------------------------------------

    def client(self) -> GitHubClient:
        return GitHubClient(
            api_url=API, transport=httpx.MockTransport(self.handle), sleep=lambda _d: None
        )

    def transcript(self, *, since: int = 0) -> str:
        return "\n".join(r.line() for r in self.requests[since:])

    def writes(self) -> list[Recorded]:
        return [r for r in self.requests if r.method in {"POST", "PATCH", "PUT", "DELETE"}]

    # ---- seeding -----------------------------------------------------------------------

    def seed_issue_comment(self, pr: int, body: str, *, login: str = "someone") -> dict[str, Any]:
        comment = self._new_comment(body, f"{WEB}/acme/widgets/pull/{pr}", login)
        self.issue_comments.setdefault(pr, []).append(comment)
        return comment

    def seed_commit_comment(self, sha: str, body: str, *, login: str = "someone") -> dict[str, Any]:
        comment = self._new_comment(body, f"{WEB}/acme/widgets/commit/{sha}", login)
        self.commit_comments.setdefault(sha, []).append(comment)
        return comment

    def seed_issue(
        self,
        title: str,
        body: str,
        *,
        labels: list[str],
        state: str = "open",
        number: int | None = None,
    ) -> dict[str, Any]:
        if number is None:
            self._next_issue += 1
            number = self._next_issue
        stamp = self._stamp()
        issue = {
            "number": number,
            "title": title,
            "body": body,
            "state": state,
            "labels": [{"name": name} for name in labels],
            "assignees": [],
            "html_url": f"{WEB}/acme/widgets/issues/{number}",
            "created_at": stamp,
            "updated_at": stamp,
        }
        self.issues[number] = issue
        self._next_issue = max(self._next_issue, number)
        return issue

    def issue_comments_on(self, number: int) -> list[str]:
        return [c["body"] for c in self.issue_comments.get(number, [])]

    def search_calls(self) -> list[Recorded]:
        return [r for r in self.requests if r.path.startswith("/search")]

    # ---- transport ---------------------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8")) if request.content else None
        self.requests.append(
            Recorded(request.method, request.url.path, request.url.query.decode(), body)
        )
        fault = self._take_fault(request)
        if fault is not None and fault.timeout:
            raise httpx.ReadTimeout("fake timeout", request=request)
        if fault is not None and fault.status is not None:
            return httpx.Response(fault.status, json={"message": f"fake {fault.status}"})
        response = self._route(request, body)
        for hook in list(self.after_request):
            hook(self, self.requests[-1])
        if fault is not None and fault.lost_response:
            raise httpx.ReadTimeout("fake timeout after write", request=request)
        return response

    def _take_fault(self, request: httpx.Request) -> Fault | None:
        for fault in self.faults:
            if fault.times > 0 and fault.method == request.method and re.search(
                fault.path_re, request.url.path
            ):
                fault.times -= 1
                return fault
        return None

    def _route(self, request: httpx.Request, body: Any) -> httpx.Response:
        if request.url.path.startswith("/search"):
            return httpx.Response(200, json={"total_count": 0, "items": []})
        match = _REPO_PATH.match(request.url.path)
        if not match:
            return _not_found()
        rest = match.group("rest") or ""
        method = request.method
        if method == "GET" and rest == "":
            return httpx.Response(200, json={"full_name": "acme/widgets", "default_branch": self.default_branch})
        if method == "GET" and (m := re.fullmatch(r"/git/ref/tags/(.+)", rest)):
            return httpx.Response(200, json={"ref": f"refs/tags/{m[1]}"}) if m[1] in self.tags else _not_found()
        if method == "GET" and (m := re.fullmatch(r"/actions/runs/(\d+)", rest)):
            return httpx.Response(200, json={"id": int(m[1]), "workflow_id": 7})
        if method == "GET" and re.fullmatch(r"/actions/(?:workflows/\d+/)?runs", rest):
            return httpx.Response(200, json={"workflow_runs": list(self.runs)})
        if m := re.fullmatch(r"/issues/(\d+)/comments", rest):
            items = self.issue_comments.setdefault(int(m[1]), [])
            if method == "GET":
                return self._page(request, items)
            if method == "POST":
                comment = self._new_comment(body["body"], f"{WEB}/acme/widgets/pull/{m[1]}", "bot")
                if int(m[1]) in self.issues:  # a comment bumps the issue's updated_at
                    self.issues[int(m[1])]["updated_at"] = comment["created_at"]
                items.append(comment)
                return httpx.Response(201, json=comment)
        if method == "GET" and (m := re.fullmatch(r"/collaborators/([^/]+)/permission", rest)):
            role = self.permissions.get(m[1])
            if role is None:
                return _not_found()
            legacy = {"maintain": "write", "triage": "read"}.get(role, role)
            return httpx.Response(200, json={"permission": legacy, "role_name": role})
        if method == "GET" and (m := re.fullmatch(r"/contents/(.+)", rest)):
            ref = parse_qs(request.url.query.decode()).get("ref", [None])[0]
            if m[1] in self.large_files:
                return httpx.Response(
                    200, json={"type": "file", "encoding": "none", "content": "", "size": 2_000_000}
                )
            text = self.files.get((m[1], ref), self.files.get((m[1], None)))
            if text is None:
                return _not_found()
            encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
            return httpx.Response(200, json={"type": "file", "encoding": "base64", "content": encoded})
        if method == "POST" and (m := re.fullmatch(r"/issues/(\d+)/assignees", rest)):
            issue = self.issues.get(int(m[1]))
            if issue is None:
                return _not_found()
            for login in body.get("assignees", []):
                if self.assignable is not None and login not in self.assignable:
                    continue
                if login not in [a["login"] for a in issue["assignees"]]:
                    issue["assignees"].append({"login": login})
            return httpx.Response(201, json=dict(issue))
        if rest == "/issues":
            if method == "GET":
                return self._list_issues(request)
            if method == "POST":
                issue = self.seed_issue(body["title"], body.get("body") or "", labels=body.get("labels") or [])
                return httpx.Response(201, json=dict(issue))
        if m := re.fullmatch(r"/issues/(\d+)", rest):
            issue = self.issues.get(int(m[1]))
            if issue is None:
                return _not_found()
            if method == "GET":
                return httpx.Response(200, json=dict(issue))
            if method == "PATCH":
                for key in ("title", "body", "state", "state_reason"):
                    if key in body:
                        issue[key] = body[key]
                if "labels" in body:
                    issue["labels"] = [{"name": n} for n in body["labels"]]
                issue["updated_at"] = self._stamp()
                return httpx.Response(200, json=dict(issue))
        if method == "POST" and (m := re.fullmatch(r"/issues/comments/(\d+)/reactions", rest)):
            for items in self.issue_comments.values():
                for comment in items:
                    if comment["id"] == int(m[1]):
                        content = body.get("content")
                        self.reactions.append((int(m[1]), content))
                        rollup = comment.setdefault("reactions", {})
                        rollup[content] = rollup.get(content, 0) + 1
                        return httpx.Response(201, json={"id": len(self.reactions), "content": content})
            return _not_found()
        if method == "PATCH" and (m := re.fullmatch(r"/issues/comments/(\d+)", rest)):
            return self._patch(self.issue_comments, int(m[1]), body)
        if m := re.fullmatch(r"/commits/([^/]+)/comments", rest):
            items = self.commit_comments.setdefault(m[1], [])
            if method == "GET":
                return self._page(request, items)
            if method == "POST":
                comment = self._new_comment(body["body"], f"{WEB}/acme/widgets/commit/{m[1]}", "bot")
                items.append(comment)
                return httpx.Response(201, json=comment)
        if method == "PATCH" and (m := re.fullmatch(r"/comments/(\d+)", rest)):
            return self._patch(self.commit_comments, int(m[1]), body)
        if method == "POST" and (m := re.fullmatch(r"/issues/(\d+)/labels", rest)):
            current = self.labels.setdefault(int(m[1]), [])
            for label in body.get("labels", []):
                if label not in current:
                    current.append(label)
            return httpx.Response(200, json=[{"name": name} for name in current])
        return _not_found()

    def _list_issues(self, request: httpx.Request) -> httpx.Response:
        query = parse_qs(request.url.query.decode())
        wanted = [x for x in query.get("labels", [""])[0].split(",") if x]
        state = query.get("state", ["open"])[0]
        items = [
            i
            for i in sorted(self.issues.values(), key=lambda i: -i["number"])  # newest first
            if (state == "all" or i["state"] == state)
            and all(name in [l["name"] for l in i["labels"]] for name in wanted)
        ]
        return self._page(request, items)

    def _page(self, request: httpx.Request, items: list[dict[str, Any]]) -> httpx.Response:
        query = parse_qs(request.url.query.decode())
        per_page = min(int(query.get("per_page", ["30"])[0]), self.page_size)
        page = int(query.get("page", ["1"])[0])
        chunk = items[(page - 1) * per_page : page * per_page]
        headers = {}
        if page * per_page < len(items):
            nxt = request.url.copy_merge_params({"page": str(page + 1)})
            headers["Link"] = f'<{nxt}>; rel="next"'
        return httpx.Response(200, json=[dict(c) for c in chunk], headers=headers)

    def _patch(self, store: dict[Any, list[dict[str, Any]]], comment_id: int, body: Any) -> httpx.Response:
        for items in store.values():
            for comment in items:
                if comment["id"] == comment_id:
                    comment["body"] = body["body"]
                    comment["updated_at"] = self._stamp()
                    return httpx.Response(200, json=dict(comment))
        return _not_found()

    def _new_comment(self, body: str, parent_url: str, login: str) -> dict[str, Any]:
        self._next_id += 1
        stamp = self._stamp()
        return {
            "id": self._next_id,
            "body": body,
            "html_url": f"{parent_url}#comment-{self._next_id}",
            "user": {"login": login},
            "author_association": "OWNER",
            "reactions": {"+1": 0, "-1": 0, "total_count": 0},
            "created_at": stamp,
            "updated_at": stamp,
        }

    def _stamp(self) -> str:
        self._ticks += 1
        return (self.start + timedelta(minutes=self._ticks)).isoformat().replace("+00:00", "Z")


def _not_found() -> httpx.Response:
    return httpx.Response(404, json={"message": "Not Found"})
