"""GitHub REST client. Host and token come from config.resolve_* only."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

import httpx

from .config import RATE_LIMIT_OPTIONAL_FLOOR, resolve_github_api_url, resolve_github_token

_API_VERSION = "2022-11-28"
_ACCEPT = "application/vnd.github+json"
_DEFAULT_TIMEOUT = 30.0
_MAX_ATTEMPTS = 3
_JOBS_PER_PAGE = 100


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

    def _request(
        self,
        method: str,
        url: str,
        *,
        follow_redirects: bool = False,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        last_error: Exception | None = None
        secondary_slept = False
        for attempt in range(_MAX_ATTEMPTS):
            try:
                response = self._client.request(
                    method,
                    url,
                    follow_redirects=follow_redirects,
                    headers=headers,
                )
            except httpx.TimeoutException as exc:
                last_error = GitHubAPIError(f"timeout talking to GitHub API ({method} {url})")
                if attempt == _MAX_ATTEMPTS - 1:
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

    def _fetch_redirect_body(self, location: str) -> str | None:
        # Signed blob URLs must not inherit the GitHub Authorization header.
        anon_kwargs: dict[str, Any] = {
            "timeout": self._timeout,
            "follow_redirects": True,
            "headers": {"Accept": "text/plain, */*"},
        }
        if self._transport is not None:
            anon_kwargs["transport"] = self._transport
        try:
            with httpx.Client(**anon_kwargs) as anon:
                response = anon.get(location)
        except httpx.HTTPError:
            return None
        if response.status_code >= 400:
            return None
        return response.text

    def _fetch_redirect_bytes(self, location: str) -> bytes | None:
        anon_kwargs: dict[str, Any] = {
            "timeout": self._timeout,
            "follow_redirects": True,
            "headers": {"Accept": "application/zip, application/octet-stream, */*"},
        }
        if self._transport is not None:
            anon_kwargs["transport"] = self._transport
        try:
            with httpx.Client(**anon_kwargs) as anon:
                response = anon.get(location)
        except httpx.HTTPError:
            return None
        if response.status_code >= 400:
            return None
        return response.content


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
