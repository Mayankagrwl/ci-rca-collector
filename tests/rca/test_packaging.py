"""Slice G packaging: reusable workflow, self-test, consumer stubs."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_self_test_workflow_is_offline() -> None:
    text = (ROOT / ".github" / "workflows" / "self-test.yml").read_text(encoding="utf-8")
    assert "--from-fixture" in text
    assert "tests/rca/fixtures" in text
    assert "github.st.com" not in text
    assert "api.github.com" not in text


def test_consumer_stub_for_github_com() -> None:
    text = (ROOT / "examples" / "rca-collect.yml").read_text(encoding="utf-8")
    assert "workflow_run" in text
    assert '"CI"' in text and "Test Failure Scenarios" in text
    assert "actions: read" in text
    assert "contents: read" in text
    assert "rca-${{ github.event.workflow_run.head_branch }}" in text
    assert "secrets.COMMON_ACTIONS_PAT || github.token" in text
    assert "github-api-url: ''" in text
    assert "STGPT_API: ${{ secrets.STGPT_API }}" in text
    assert "analyze: true" in text
    assert "steps.rca.outputs.requires-analysis == 'true'" in text
    assert "api-ai-bridge" not in text
    assert "github.st.com" not in text


def test_test_failures_example_is_not_a_repo_workflow() -> None:
    example = ROOT / "examples" / "test-failures.yml"
    enabled = ROOT / ".github" / "workflows" / "test-failures.yml"
    assert example.is_file()
    assert not enabled.exists()
    text = example.read_text(encoding="utf-8")
    assert "Test Failure Scenarios" in text
    assert "noisy" in text
    assert "secrets" in text
