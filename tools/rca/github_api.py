"""GitHub REST client. Host and token come from config.resolve_* only."""

from __future__ import annotations

import base64
import logging
import ssl
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

import httpx

from .config import (
    RATE_LIMIT_OPTIONAL_FLOOR,
    resolve_github_api_url,
    resolve_github_token,
    resolve_ssl_verify,
)

_LOG = logging.getLogger(__name__)

_API_VERSION = "2022-11-28"
_ACCEPT = "application/vnd.github+json"
_DEFAULT_TIMEOUT = 30.0
_MAX_ATTEMPTS = 3
_JOBS_PER_PAGE = 100
_LIST_PER_PAGE = 100
_MAX_LIST_PAGES = 10
# Reactions graduated from preview, but older GHES still requires the preview media type.
_REACTIONS_ACCEPT = (
    "application/vnd.github.squirrel-girl-preview+json, application/vnd.github+json"
)


class GitHubAPIError(Exception):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class GitHubClient:
    """Thin httpx wrapper. Follow log redirects; honour rate-limit headers."""

    def __init__(
        self,
        *,
        api_url: str | None = None,
        token: str | None = None,
        timeout: float = _DEFAULT_TIMEOUT,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.api_url = resolve_github_api_url(api_url)
        token_value = resolve_github_token(token)
        self.verify = resolve_ssl_verify()
        headers: dict[str, str] = {
            "Accept": _ACCEPT,
            "X-GitHub-Api-Version": _API_VERSION,
            "User-Agent": "ci-rca-collector",
        }
        if token_value:
            headers["Authorization"] = f"Bearer {token_value}"
        client_kwargs: dict[str, Any] = {
            "base_url": self.api_url.rstrip("/") + "/",
            "headers": headers,
            "timeout": timeout,
            "follow_redirects": False,
            "verify": self.verify,
        }
        if transport is not None:
            client_kwargs["transport"] = transport
        self._client = httpx.Client(**client_kwargs)
        self._timeout = timeout
        self._transport = transport
        self._sleep = sleep
        self.rate_limit_remaining: int | None = None
        self.rate_limit_reset: int | None = None
        self.collection_notes: list[str] = []
        if self.verify is False:
            note = (
                f"TLS verification disabled (RCA_SSL_VERIFY=false); "
                f"API base {self.api_url}"
            )
            self.collection_notes.append(note)
            _LOG.warning(note)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> GitHubClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def optional_collection_allowed(self) -> bool:
        if self.rate_limit_remaining is None:
            return True
        return self.rate_limit_remaining >= RATE_LIMIT_OPTIONAL_FLOOR

    def _note_rate_limit(self) -> None:
        if self.rate_limit_remaining is None:
            return
        if self.rate_limit_remaining < RATE_LIMIT_OPTIONAL_FLOOR:
            note = (
                f"x-ratelimit-remaining={self.rate_limit_remaining}; "
                "stopping optional collection"
            )
            if note not in self.collection_notes:
                self.collection_notes.append(note)

    def _record_rate_headers(self, response: httpx.Response) -> None:
        remaining = response.headers.get("x-ratelimit-remaining")
        reset = response.headers.get("x-ratelimit-reset")
        if remaining is not None and remaining != "":
            try:
                self.rate_limit_remaining = int(remaining)
            except ValueError:
                pass
        if reset is not None and reset != "":
            try:
                self.rate_limit_reset = int(reset)
            except ValueError:
                pass
        self._note_rate_limit()

    def _ssl_error(self, exc: BaseException, *, method: str, url: str) -> GitHubAPIError:
        note = (
            f"SSL error talking to GitHub API at {self.api_url} "
            f"({method} {url}): {exc}"
        )
        if note not in self.collection_notes:
            self.collection_notes.append(note)
        _LOG.error(note)
        return GitHubAPIError(note)

    def _anon_client_kwargs(self, headers: dict[str, str]) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "timeout": self._timeout,
            "follow_redirects": True,
            "headers": headers,
            "verify": self.verify,
        }
        if self._transport is not None:
            kwargs["transport"] = self._transport
        return kwargs

    def _request(
        self,
        method: str,
        url: str,
        *,
        follow_redirects: bool = False,
        headers: dict[str, str] | None = None,
        json: Any | None = None,
        idempotent: bool = True,
    ) -> httpx.Response:
        # idempotent=False is for requests that *create* something (comments, issues,
        # reactions). A timeout or a dropped connection after the request was sent is an
        # ambiguous outcome: GitHub may already have created the resource, so a blind retry
        # would post duplicates. Those requests therefore fail fast on timeout / protocol
        # errors. ConnectError (nothing was sent), 5xx and retry-after are still retried.
        # Do not "simplify" this back to always-retry.
        last_error: Exception | None = None
        secondary_slept = False
        request_kwargs: dict[str, Any] = {
            "follow_redirects": follow_redirects,
            "headers": headers,
        }
        if json is not None:
            # Omitted entirely when absent so GET requests stay byte-identical.
            request_kwargs["json"] = json
        for attempt in range(_MAX_ATTEMPTS):
            try:
                response = self._client.request(method, url, **request_kwargs)
            except httpx.TimeoutException as exc:
                last_error = GitHubAPIError(f"timeout talking to GitHub API ({method} {url})")
                if not idempotent or attempt == _MAX_ATTEMPTS - 1:
                    raise last_error from exc
                self._sleep(2**attempt)
                continue
            except (httpx.ConnectError, httpx.ProtocolError, ssl.SSLError) as exc:
                if _is_ssl_error(exc):
                    raise self._ssl_error(exc, method=method, url=url) from exc
                last_error = GitHubAPIError(
                    f"connection error talking to GitHub API ({method} {url}): {exc}"
                )
                ambiguous = not isinstance(exc, httpx.ConnectError)
                if (ambiguous and not idempotent) or attempt == _MAX_ATTEMPTS - 1:
                    raise last_error from exc
                self._sleep(2**attempt)
                continue

            self._record_rate_headers(response)

            if response.status_code >= 500:
                last_error = GitHubAPIError(
                    f"GitHub API {response.status_code} on {method} {url}",
                    status_code=response.status_code,
                )
                if attempt == _MAX_ATTEMPTS - 1:
                    raise last_error
                self._sleep(2**attempt)
                continue

            if response.status_code == 403:
                remaining = response.headers.get("x-ratelimit-remaining")
                retry_after = response.headers.get("retry-after")
                if remaining == "0":
                    raise GitHubAPIError(
                        "GitHub API rate limit exhausted",
                        status_code=403,
                    )
                if retry_after and not secondary_slept:
                    try:
                        wait = int(retry_after)
                    except ValueError:
                        wait = 1
                    secondary_slept = True
                    self._sleep(wait)
                    continue
                raise GitHubAPIError(
                    "GitHub API forbidden",
                    status_code=403,
                )

            return response

        raise last_error or GitHubAPIError("GitHub API request failed")

    def get_run(self, repo: str, run_id: int) -> dict[str, Any]:
        response = self._request("GET", f"repos/{repo}/actions/runs/{run_id}")
        if response.status_code >= 400:
            raise GitHubAPIError(
                f"failed to fetch run {run_id} ({response.status_code})",
                status_code=response.status_code,
            )
        data = response.json()
        if not isinstance(data, dict):
            raise GitHubAPIError("run payload was not an object")
        return data

    def list_jobs(self, repo: str, run_id: int) -> list[dict[str, Any]]:
        jobs: list[dict[str, Any]] = []
        url: str | None = f"repos/{repo}/actions/runs/{run_id}/jobs?per_page={_JOBS_PER_PAGE}"
        while url:
            response = self._request("GET", url)
            if response.status_code >= 400:
                raise GitHubAPIError(
                    f"failed to list jobs for run {run_id} ({response.status_code})",
                    status_code=response.status_code,
                )
            payload = response.json()
            page = payload.get("jobs", payload) if isinstance(payload, dict) else payload
            if not isinstance(page, list):
                raise GitHubAPIError("jobs payload was not a list")
            jobs.extend(page)
            url = _next_link(response.headers.get("link") or response.headers.get("Link"))
        return jobs

    def list_runs(
        self,
        repo: str,
        *,
        head_sha: str | None = None,
        branch: str | None = None,
        status: str | None = None,
        per_page: int = 50,
        workflow_id: int | str | None = None,
    ) -> list[dict[str, Any]]:
        if workflow_id is not None:
            path = f"repos/{repo}/actions/workflows/{workflow_id}/runs"
        else:
            path = f"repos/{repo}/actions/runs"
        query = [f"per_page={per_page}"]
        if head_sha:
            query.append(f"head_sha={quote(str(head_sha), safe='')}")
        if branch:
            query.append(f"branch={quote(branch, safe='')}")
        if status:
            query.append(f"status={quote(status, safe='')}")
        path = f"{path}?{'&'.join(query)}"
        response = self._request("GET", path)
        if response.status_code >= 400:
            raise GitHubAPIError(
                f"failed to list runs ({response.status_code})",
                status_code=response.status_code,
            )
        payload = response.json()
        if isinstance(payload, dict):
            runs = payload.get("workflow_runs", [])
            return runs if isinstance(runs, list) else []
        return []

    def compare(self, repo: str, base: str, head: str) -> dict[str, Any]:
        path = (
            f"repos/{repo}/compare/{quote(base, safe='')}...{quote(head, safe='')}"
        )
        response = self._request("GET", path)
        if response.status_code >= 400:
            raise GitHubAPIError(
                f"failed to compare {base}...{head} ({response.status_code})",
                status_code=response.status_code,
            )
        payload = response.json()
        if not isinstance(payload, dict):
            raise GitHubAPIError("compare payload was not an object")
        return payload

    def get_pull(self, repo: str, number: int) -> dict[str, Any] | None:
        response = self._request("GET", f"repos/{repo}/pulls/{number}")
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise GitHubAPIError(
                f"failed to fetch pull {number} ({response.status_code})",
                status_code=response.status_code,
            )
        payload = response.json()
        return payload if isinstance(payload, dict) else None

    def list_commit_pulls(self, repo: str, sha: str) -> list[dict[str, Any]]:
        response = self._request("GET", f"repos/{repo}/commits/{sha}/pulls")
        if response.status_code == 404:
            return []
        if response.status_code >= 400:
            raise GitHubAPIError(
                f"failed to list pulls for {sha} ({response.status_code})",
                status_code=response.status_code,
            )
        data = response.json()
        if not isinstance(data, list):
            return []
        return data

    def list_artifacts(self, repo: str, run_id: int) -> list[dict[str, Any]]:
        artifacts: list[dict[str, Any]] = []
        url: str | None = (
            f"repos/{repo}/actions/runs/{run_id}/artifacts?per_page={_JOBS_PER_PAGE}"
        )
        while url:
            response = self._request("GET", url)
            if response.status_code >= 400:
                raise GitHubAPIError(
                    f"failed to list artifacts for run {run_id} ({response.status_code})",
                    status_code=response.status_code,
                )
            payload = response.json()
            page = payload.get("artifacts", payload) if isinstance(payload, dict) else payload
            if not isinstance(page, list):
                raise GitHubAPIError("artifacts payload was not a list")
            artifacts.extend(page)
            url = _next_link(response.headers.get("link") or response.headers.get("Link"))
        return artifacts

    def download_artifact_zip(self, repo: str, artifact_id: int) -> bytes | None:
        """Download an artifact zip. Follows the short-lived 302; does not cache it."""
        path = f"repos/{repo}/actions/artifacts/{artifact_id}/zip"
        try:
            response = self._request("GET", path, follow_redirects=False)
        except GitHubAPIError as exc:
            if exc.status_code in (404, 410):
                return None
            raise
        if response.status_code in (404, 410):
            return None
        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("location") or response.headers.get("Location")
            if not location:
                return None
            return self._fetch_redirect_bytes(location)
        if response.status_code >= 400:
            return None
        return response.content

    def get_file(
        self,
        repo: str,
        path: str,
        ref: str,
        *,
        allow_empty: bool = False,
        meta: dict[str, Any] | None = None,
    ) -> str | None:
        """Fetch a file at *ref*. 404/403 return None; never raises for those.

        An existing empty file is ``None`` too unless ``allow_empty`` (then ``""``).
        Files over 1 MB come back with ``content: ""`` (``encoding: "none"``): that is
        ``None``, never an empty file, and ``meta["too_large"]`` is set when given.
        """
        quoted = quote(path.lstrip("/"), safe="/")
        url = f"repos/{repo}/contents/{quoted}?ref={quote(str(ref), safe='')}"
        try:
            response = self._request("GET", url)
        except GitHubAPIError as exc:
            if exc.status_code in (404, 403):
                return None
            raise
        if response.status_code in (404, 403):
            return None
        if response.status_code >= 400:
            return None
        try:
            payload = response.json()
        except ValueError:
            text = response.text
            return text if isinstance(text, str) else None
        if not isinstance(payload, dict):
            return None
        if payload.get("type") and payload.get("type") != "file":
            return None
        content = payload.get("content")
        if content == "" and (payload.get("encoding") == "none" or (payload.get("size") or 0) > 0):
            if meta is not None:
                meta["too_large"] = True
            return None
        if allow_empty and content == "":
            return ""
        if not isinstance(content, str) or not content.strip():
            return None
        try:
            return base64.b64decode(content).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return None

    def list_commit_files(self, repo: str, sha: str) -> list[str]:
        """Filenames changed in a single commit. 404/403 → empty list."""
        url = f"repos/{repo}/commits/{quote(str(sha), safe='')}"
        try:
            response = self._request("GET", url)
        except GitHubAPIError as exc:
            if exc.status_code in (404, 403):
                return []
            raise
        if response.status_code in (404, 403) or response.status_code >= 400:
            return []
        try:
            payload = response.json()
        except ValueError:
            return []
        if not isinstance(payload, dict):
            return []
        names: list[str] = []
        for item in payload.get("files") or []:
            if isinstance(item, dict) and item.get("filename"):
                names.append(str(item["filename"]))
        return names

    def get_job_log(self, repo: str, job_id: int) -> str | None:
        """Fetch plaintext job logs. Follows the short-lived 302; does not cache it."""
        path = f"repos/{repo}/actions/jobs/{job_id}/logs"
        try:
            response = self._request("GET", path, follow_redirects=False)
        except GitHubAPIError as exc:
            if exc.status_code in (404, 410):
                return None
            raise
        if response.status_code in (404, 410):
            return None
        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("location") or response.headers.get("Location")
            if not location:
                return None
            return self._fetch_redirect_body(location)
        if response.status_code >= 400:
            return None
        return response.text

    # ---- Delivery (Phase 3) read / write methods. Capability only; no callers in collect.

    def _json_object(self, response: httpx.Response, what: str) -> dict[str, Any]:
        if response.status_code >= 400:
            raise GitHubAPIError(
                f"failed to {what} ({response.status_code})",
                status_code=response.status_code,
            )
        payload = response.json()
        if not isinstance(payload, dict):
            raise GitHubAPIError(f"{what}: payload was not an object")
        return payload

    def _paginate(self, url: str, what: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        next_url: str | None = url
        pages = 0
        while next_url and pages < _MAX_LIST_PAGES:
            response = self._request("GET", next_url)
            if response.status_code >= 400:
                raise GitHubAPIError(
                    f"failed to {what} ({response.status_code})",
                    status_code=response.status_code,
                )
            page = response.json()
            if not isinstance(page, list):
                raise GitHubAPIError(f"{what}: payload was not a list")
            items.extend(page)
            pages += 1
            next_url = _next_link(response.headers.get("link") or response.headers.get("Link"))
        return items

    def get_repo(self, repo: str) -> dict[str, Any]:
        response = self._request("GET", f"repos/{repo}")
        return self._json_object(response, f"fetch repo {repo}")

    def ref_is_tag(self, repo: str, name: str) -> bool:
        """True when ``name`` is a tag. 200 → True, 404 → False, anything else raises.

        ``/`` is kept literal: nested ref names (``release/1.0``) are path segments.
        """
        url = f"repos/{repo}/git/ref/tags/{quote(name, safe='/')}"
        response = self._request("GET", url)
        if response.status_code == 200:
            return True
        if response.status_code == 404:
            return False
        raise GitHubAPIError(
            f"failed to look up tag {name} ({response.status_code})",
            status_code=response.status_code,
        )

    def list_issue_comments(self, repo: str, issue_number: int) -> list[dict[str, Any]]:
        return self._paginate(
            f"repos/{repo}/issues/{issue_number}/comments?per_page={_LIST_PER_PAGE}",
            f"list comments on #{issue_number}",
        )

    def create_issue_comment(self, repo: str, issue_number: int, body: str) -> dict[str, Any]:
        response = self._request(
            "POST",
            f"repos/{repo}/issues/{issue_number}/comments",
            json={"body": body},
            idempotent=False,
        )
        return self._json_object(response, f"create comment on #{issue_number}")

    def update_issue_comment(self, repo: str, comment_id: int, body: str) -> dict[str, Any]:
        response = self._request(
            "PATCH",
            f"repos/{repo}/issues/comments/{comment_id}",
            json={"body": body},
        )
        return self._json_object(response, f"update issue comment {comment_id}")

    def list_commit_comments(self, repo: str, sha: str) -> list[dict[str, Any]]:
        return self._paginate(
            f"repos/{repo}/commits/{quote(str(sha), safe='')}/comments"
            f"?per_page={_LIST_PER_PAGE}",
            f"list comments on commit {sha}",
        )

    def create_commit_comment(self, repo: str, sha: str, body: str) -> dict[str, Any]:
        response = self._request(
            "POST",
            f"repos/{repo}/commits/{quote(str(sha), safe='')}/comments",
            json={"body": body},
            idempotent=False,
        )
        return self._json_object(response, f"create comment on commit {sha}")

    def update_commit_comment(self, repo: str, comment_id: int, body: str) -> dict[str, Any]:
        response = self._request(
            "PATCH",
            f"repos/{repo}/comments/{comment_id}",
            json={"body": body},
        )
        return self._json_object(response, f"update commit comment {comment_id}")

    def list_issues(
        self,
        repo: str,
        labels: list[str] | None = None,
        state: str = "open",
    ) -> list[dict[str, Any]]:
        query = [f"state={quote(state, safe='')}", f"per_page={_LIST_PER_PAGE}"]
        if labels:
            query.append(f"labels={quote(','.join(labels), safe=',')}")
        return self._paginate(f"repos/{repo}/issues?{'&'.join(query)}", "list issues")

    def create_issue(
        self,
        repo: str,
        title: str,
        body: str,
        labels: list[str] | None = None,
        assignees: list[str] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"title": title, "body": body}
        if labels:
            payload["labels"] = list(labels)
        if assignees:
            payload["assignees"] = list(assignees)
        response = self._request(
            "POST", f"repos/{repo}/issues", json=payload, idempotent=False
        )
        return self._json_object(response, "create issue")

    def get_issue(self, repo: str, number: int) -> dict[str, Any] | None:
        """One issue by number (read-only). 404 → None."""
        response = self._request("GET", f"repos/{repo}/issues/{int(number)}")
        if response.status_code == 404:
            return None
        return self._json_object(response, f"fetch issue #{number}")

    def get_collaborator_permission(self, repo: str, username: str) -> str | None:
        """A user's role on the repo (read-only). ``role_name`` or ``permission``; 404 → None."""
        url = f"repos/{repo}/collaborators/{quote(username, safe='')}/permission"
        response = self._request("GET", url)
        if response.status_code == 404:
            return None
        payload = self._json_object(response, f"collaborator permission for {username}")
        value = payload.get("role_name") or payload.get("permission")
        return str(value) if value else None

    def add_assignees(self, repo: str, number: int, assignees: list[str]) -> dict[str, Any]:
        """Add assignees to an issue (additive; never removes existing ones)."""
        response = self._request(
            "POST",
            f"repos/{repo}/issues/{int(number)}/assignees",
            json={"assignees": list(assignees)},
        )
        return self._json_object(response, f"assign issue #{number}")

    def update_issue(self, repo: str, number: int, **fields: Any) -> dict[str, Any]:
        payload = {key: value for key, value in fields.items() if value is not None}
        response = self._request("PATCH", f"repos/{repo}/issues/{number}", json=payload)
        return self._json_object(response, f"update issue #{number}")

    def add_labels(self, repo: str, issue_number: int, labels: list[str]) -> dict[str, Any]:
        """Add labels. GitHub returns a list; it is wrapped as {"labels": [...]}."""
        response = self._request(
            "POST",
            f"repos/{repo}/issues/{issue_number}/labels",
            json={"labels": list(labels)},
        )
        if response.status_code >= 400:
            raise GitHubAPIError(
                f"failed to add labels to #{issue_number} ({response.status_code})",
                status_code=response.status_code,
            )
        payload = response.json()
        return {"labels": payload if isinstance(payload, list) else []}

    def create_reaction(self, repo: str, comment_id: int, content: str) -> dict[str, Any]:
        """React to an issue comment. Best-effort: any failure returns {} and never raises."""
        try:
            response = self._request(
                "POST",
                f"repos/{repo}/issues/comments/{comment_id}/reactions",
                json={"content": content},
                headers={"Accept": _REACTIONS_ACCEPT},
                idempotent=False,
            )
        except GitHubAPIError as exc:
            _LOG.info("reaction on comment %s skipped: %s", comment_id, exc)
            return {}
        if not 200 <= response.status_code < 300:
            _LOG.info("reaction on comment %s skipped (%s)", comment_id, response.status_code)
            return {}
        try:
            payload = response.json()
        except ValueError:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _fetch_redirect_body(self, location: str) -> str | None:
        # Signed blob URLs must not inherit the GitHub Authorization header.
        try:
            with httpx.Client(
                **self._anon_client_kwargs({"Accept": "text/plain, */*"})
            ) as anon:
                response = anon.get(location)
        except httpx.HTTPError as exc:
            if _is_ssl_error(exc):
                self._ssl_error(exc, method="GET", url=location)
            return None
        if response.status_code >= 400:
            return None
        return response.text

    def _fetch_redirect_bytes(self, location: str) -> bytes | None:
        try:
            with httpx.Client(
                **self._anon_client_kwargs(
                    {"Accept": "application/zip, application/octet-stream, */*"}
                )
            ) as anon:
                response = anon.get(location)
        except httpx.HTTPError as exc:
            if _is_ssl_error(exc):
                self._ssl_error(exc, method="GET", url=location)
            return None
        if response.status_code >= 400:
            return None
        return response.content


def _is_ssl_error(exc: BaseException) -> bool:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ssl.SSLError):
            return True
        text = str(current).lower()
        if "ssl" in text or "certificate" in text:
            return True
        current = current.__cause__ or current.__context__
    return False


def _next_link(link_header: str | None) -> str | None:
    if not link_header:
        return None
    for part in link_header.split(","):
        if 'rel="next"' not in part:
            continue
        start = part.find("<")
        end = part.find(">", start + 1)
        if start == -1 or end == -1:
            return None
        return part[start + 1 : end].strip()
    return None
