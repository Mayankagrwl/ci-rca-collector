"""Step 13 AC7 — pure modules import no HTTP; plus the read-only ``ref_is_tag``."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from tools.rca.github_api import GitHubAPIError, GitHubClient

_ROOT = Path(__file__).resolve().parents[3]
_DELIVER = _ROOT / "tools" / "rca" / "deliver"
_FORBIDDEN = ("github_api", "httpx")


@pytest.mark.parametrize("module", ["severity", "suppress"])
def test_pure_modules_do_not_import_http(module: str) -> None:
    code = (
        "import sys\n"
        f"import tools.rca.deliver.{module}\n"
        "bad = [m for m in sys.modules if m == 'httpx' or m.endswith('github_api')]\n"
        "print(','.join(bad))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], cwd=_ROOT, capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == ""


@pytest.mark.parametrize("module", ["__init__", "severity", "suppress"])
def test_pure_module_source_has_no_http_imports(module: str) -> None:
    tree = ast.parse((_DELIVER / f"{module}.py").read_text(encoding="utf-8"))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.append(node.module or "")
            names.extend(alias.name for alias in node.names)
    assert not [n for n in names if any(bad in n for bad in _FORBIDDEN)]


_WRITE_METHODS = {
    "create_issue_comment",
    "update_issue_comment",
    "create_commit_comment",
    "update_commit_comment",
    "create_issue",
    "update_issue",
    "add_labels",
    "create_reaction",
    "_request",
}


@pytest.mark.parametrize("module", ["__init__", "targets", "severity", "suppress"])
def test_deliver_calls_no_github_write_method(module: str) -> None:
    tree = ast.parse((_DELIVER / f"{module}.py").read_text(encoding="utf-8"))
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert not called & _WRITE_METHODS


# ---- github_api.ref_is_tag --------------------------------------------------


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("RCA_GITHUB_API_URL", "GITHUB_API_URL", "GITHUB_SERVER_URL", "GH_HOST",
                 "RCA_GITHUB_HOST", "RCA_SSL_VERIFY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("COMMON_ACTIONS_PAT", "test-token")


def _client(status: int, seen: list[httpx.Request]) -> GitHubClient:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json={"ref": "refs/tags/x"})

    return GitHubClient(transport=httpx.MockTransport(handler), sleep=lambda _d: None)


def test_ref_is_tag_200_true_404_false() -> None:
    seen: list[httpx.Request] = []
    with _client(200, seen) as client:
        assert client.ref_is_tag("acme/widgets", "v1.2.0") is True
    with _client(404, seen) as client:
        assert client.ref_is_tag("acme/widgets", "main") is False
    assert seen[0].method == "GET"
    assert seen[0].url.path == "/repos/acme/widgets/git/ref/tags/v1.2.0"
    assert seen[0].content == b""


def test_ref_is_tag_encodes_name() -> None:
    seen: list[httpx.Request] = []
    with _client(200, seen) as client:
        client.ref_is_tag("acme/widgets", "release/1.0 beta#2")
    assert seen[0].url.raw_path.decode() == "/repos/acme/widgets/git/ref/tags/release/1.0%20beta%232"


@pytest.mark.parametrize("status", [403, 422, 301])
def test_ref_is_tag_other_status_raises(status: int) -> None:
    with _client(status, []) as client, pytest.raises(GitHubAPIError) as info:
        client.ref_is_tag("acme/widgets", "v1")
    assert info.value.status_code == status
