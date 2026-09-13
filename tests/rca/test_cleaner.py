"""Stage 4 log cleaning."""

from __future__ import annotations

from pathlib import Path

from tools.rca.cleaner import clean_log

RAW_LOG = """\
2026-09-08T12:34:56.7891234Z \x1b[36m##[group]Operating System\x1b[0m
2026-09-08T12:34:56.7891234Z Ubuntu
2026-09-08T12:34:56.7891234Z 24.04.2
2026-09-08T12:34:56.7891234Z LTS
2026-09-08T12:34:56.7891234Z ##[endgroup]
2026-09-08T12:34:56.7891234Z ##[group]Runner Image
2026-09-08T12:34:56.7891234Z Image: ubuntu-24.04
2026-09-08T12:34:56.7891234Z Version: 20260818.1.0
2026-09-08T12:34:56.7891234Z ##[endgroup]
2026-09-08T12:34:56.7891234Z Available disk space: 42 GB
2026-09-08T12:34:56.7891234Z ##[debug]secret internals
2026-09-08T12:34:56.7891234Z Download action repository 'actions/checkout@v4'
2026-09-08T12:34:57.0000000Z ##[group]Run npm install
2026-09-08T12:34:57.1000000Z downloading\rprogress 50%\rprogress 100%
2026-09-08T12:34:58.0000000Z npm ERR! ERESOLVE could not resolve
2026-09-08T12:34:58.1000000Z ##[error]Process completed with exit code 1
2026-09-08T12:34:58.2000000Z Error: Boom
2026-09-08T12:34:58.3000000Z     at parseNode (src/parser/node.ts:88:19)
2026-09-08T12:34:58.4000000Z     at Object.parse (src/parser/index.ts:23:5)
2026-09-08T12:34:58.5000000Z \tat org.foo.Bar.baz(Bar.java:12)
2026-09-08T12:34:58.6000000Z Caused by: java.io.IOException: nope
2026-09-08T12:34:58.7000000Z   File "app.py", line 3, in <module>
2026-09-08T12:34:59.0000000Z Post job cleanup
2026-09-08T12:34:59.1000000Z Cleaning up orphan processes
2026-09-08T12:34:59.2000000Z post-step evidence should vanish
"""


def test_strips_timestamp_ansi_and_cr() -> None:
    result = clean_log(RAW_LOG)
    joined = "\n".join(result.lines)
    assert "2026-09-08T" not in joined
    assert "\x1b[" not in joined
    assert "downloading" not in joined
    assert "progress 50%" not in joined
    assert "progress 100%" in joined


def test_drops_noise_keeps_error_marker() -> None:
    result = clean_log(RAW_LOG)
    joined = "\n".join(result.lines)
    assert "##[group]" not in joined
    assert "##[debug]" not in joined
    assert "Download action repository" not in joined
    assert "##[error]Process completed with exit code 1" in joined
    assert "npm ERR! ERESOLVE could not resolve" in joined


def test_extracts_setup_facts_before_drop() -> None:
    result = clean_log(RAW_LOG)
    assert result.setup.image == "ubuntu-24.04@20260818.1.0"
    assert result.setup.os == "Ubuntu 24.04.2 LTS"
    assert result.setup.disk_free_at_start == "42 GB"


def test_drops_post_cleanup_by_default() -> None:
    result = clean_log(RAW_LOG)
    joined = "\n".join(result.lines)
    assert "Post job cleanup" not in joined
    assert "post-step evidence should vanish" not in joined


def test_keeps_post_cleanup_when_failure_is_post() -> None:
    result = clean_log(RAW_LOG, keep_post_cleanup=True)
    joined = "\n".join(result.lines)
    assert "Post job cleanup" in joined
    assert "post-step evidence should vanish" in joined


def test_joins_stack_continuations() -> None:
    result = clean_log(RAW_LOG)
    stack = next(line for line in result.lines if line.startswith("Error: Boom"))
    assert "at parseNode" in stack
    assert "at Object.parse" in stack
    assert "org.foo.Bar.baz" in stack
    assert "Caused by: java.io.IOException: nope" in stack
    assert 'File "app.py"' in stack
    assert result.lines_clean < result.lines_raw
    assert result.bytes_raw == len(RAW_LOG.encode("utf-8"))


def test_drops_setup_body_keeps_facts() -> None:
    result = clean_log(RAW_LOG)
    joined = "\n".join(result.lines)
    assert "Ubuntu" not in joined
    assert "Image: ubuntu-24.04" not in joined
    assert result.setup.image == "ubuntu-24.04@20260818.1.0"


def test_does_not_join_shell_header_to_file() -> None:
    raw = (
        "2026-09-12T12:00:00.0000000Z ##[group]Run python bad.py\n"
        "2026-09-12T12:00:00.0000000Z shell: /usr/bin/bash -e {0}\n"
        '2026-09-12T12:00:00.0000000Z   File "bad.py", line 1\n'
        "2026-09-12T12:00:00.0000000Z SyntaxError: '(' was never closed\n"
    )
    result = clean_log(raw)
    file_rec = next(line for line in result.lines if 'File "' in line)
    assert "shell:" not in file_rec


def test_strips_utf8_bom() -> None:
    raw = "\ufeff" + RAW_LOG
    result = clean_log(raw)
    joined = "\n".join(result.lines)
    assert "\ufeff" not in joined
    assert "2026-09-08T" not in joined


def test_cleaner_has_no_github_ids() -> None:
    source = (
        Path(__file__).resolve().parents[2] / "tools" / "rca" / "cleaner.py"
    ).read_text(encoding="utf-8")
    assert "job_id" not in source
    assert "run_id" not in source
    assert "github_api" not in source
