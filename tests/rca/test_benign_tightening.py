"""Step 9b — tighten benign patterns + benign-filter the classify layer.

The `using … service` echo must stay benign (uninformative-job path), but a real
error merely containing "using"+"service" must not be swallowed; and the
classify layer must never let a benign/progress line set the category.
"""

from __future__ import annotations

from tools.rca.classify import classify_lines, classify_union
from tools.rca.extract import _first_error_index, is_benign_line


# --- 1. echo still benign (Step 9 uninformative-job path) -------------------


def test_using_service_echo_still_benign() -> None:
    assert is_benign_line("Using unit test service: maven_unit_test_x") is True
    # Timestamp-prefixed form still benign (anchor matches after ts-strip).
    assert is_benign_line("2026-09-23T00:00:00Z Using unit test service: x") is True


# --- 2. real error no longer swallowed --------------------------------------


def test_error_containing_using_service_is_not_benign() -> None:
    err = "ERROR: connection failed using the auth service (500)"
    assert is_benign_line(err) is False
    # It is eligible as a first-error anchor / cause.
    assert _first_error_index([err]) == 0


def test_failed_using_service_is_not_benign() -> None:
    assert is_benign_line("Failed using the payment service") is False


def test_bare_downloading_prose_is_not_benign() -> None:
    # A sentence is not docker progress, even if it starts with a status word.
    assert is_benign_line("Downloading failed: connection reset by peer") is False
    # A hex-prefixed real error is not a docker layer line.
    assert is_benign_line("ERROR deadbeef123 occurred while downloading") is False
    # Genuine docker progress stays benign.
    assert is_benign_line("Downloading [====>] 1.2MB") is True
    assert is_benign_line("a1b2c3d4e5f6 Already exists 0B") is True


# --- 3. classify skips benign -----------------------------------------------


def test_classify_lines_skips_benign_and_picks_real_category() -> None:
    lines = ["a1b2c3d4e5f6 Already exists 0B", "npm ERR! ERESOLVE could not resolve"]
    assert classify_lines(lines).category == "dependency"


def test_classify_union_skips_benign_and_picks_real_category() -> None:
    lines = ["Using unit test service: x", "npm ERR! ERESOLVE could not resolve"]
    assert classify_union([("stream", lines)]).category == "dependency"


def test_benign_only_classifies_unknown() -> None:
    benign_only = [
        "Using unit test service: x",
        'time="t" level=warning msg="No services to build"',
        "a1b2c3d4e5f6 Already exists 0B",
    ]
    assert classify_lines(benign_only).category == "unknown"
    assert classify_union([("s", benign_only)]).category == "unknown"
