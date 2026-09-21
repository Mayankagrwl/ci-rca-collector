"""First-error window skips the GitHub step script preamble."""

from __future__ import annotations

from tools.rca.extract import extract_from_lines, failed_step_excerpt_lines


def _noisy_lines() -> list[str]:
    script = [
        "for i in $(seq 1 20000); do echo \"INFO processing record $i of 20000 [ok]\"; done",
        "echo \"Exception: Connection refused to db:5432\"",
        "echo \"ERROR failed to flush buffer: connection reset by peer\"",
        "exit 1",
        "shell: /usr/bin/bash -e {0}",
        "env:",
        "  pythonLocation: /opt/hostedtoolcache/Python/3.12.7/x64",
    ]
    info = [f"INFO processing record {i} of 20000 [ok]" for i in range(1, 81)]
    errors = [
        "Exception: Connection refused to db:5432",
        "Retrying connection in 1s",
        "Retrying connection in 2s",
        "Retrying connection in 4s",
        "ERROR failed to flush buffer: connection reset by peer",
    ]
    cleanup = [f"INFO cleanup task {i} complete" for i in range(1, 201)]
    return script + info + errors + cleanup


def test_noisy_window_skips_script_and_centers_on_executed_error() -> None:
    extracted = extract_from_lines(_noisy_lines())
    window = next(item for item in extracted.windows if item.label == "first_error")
    assert "Exception: Connection refused" in window.content
    assert "ERROR failed to flush buffer" in window.content
    assert "for i in $(seq 1 20000)" not in window.content
    assert "INFO processing record 1 of 20000" not in window.content


def test_semantic_already_exists_is_first_error() -> None:
    lines = [
        "##[group]Run Check Version in Artifactory",
        "Checking 3.1.21",
        "This release already exists on Artifactory. You need to update package.json",
        "##[error]Process completed with exit code 1.",
        "##[endgroup]",
        "Not enough permissions to delete",
    ]
    extracted = extract_from_lines(lines, failed_step_name="Check Version in Artifactory")
    texts = [item.text for item in extracted.error_lines]
    assert any("already exists on Artifactory" in text for text in texts)
    window = next(item for item in extracted.windows if item.label in {"first_error", "merged"})
    assert "already exists on Artifactory" in window.content
    assert extracted.excerpt_lines
    assert any("already exists on Artifactory" in line for line in extracted.excerpt_lines)
    excerpt = failed_step_excerpt_lines(
        lines, step_name="Check Version in Artifactory"
    )
    assert any("already exists on Artifactory" in line for line in excerpt)
    assert len(excerpt) <= 40
