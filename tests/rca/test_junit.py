"""JUnit XML parsing from artifacts."""

from __future__ import annotations

import io
import zipfile

from tools.rca.junit import (
    artifact_looks_like_junit,
    is_junit_member,
    parse_junit_from_zip,
    parse_junit_xml,
)

_SAMPLE = """<?xml version="1.0" encoding="utf-8"?>
<testsuites>
  <testsuite name="t" tests="3" failures="2" errors="0">
    <testcase classname="t.test_x" name="test_pass" />
    <testcase classname="t.test_x" name="test_fail">
      <failure message="AssertionError: list mismatch">def test_fail():
    assert [1,2] == [1,2,3]
E   AssertionError: list mismatch
</failure>
    </testcase>
    <testcase classname="t.test_x" name="test_raise">
      <failure message="ValueError: boom in nested call">ValueError: boom in nested call
</failure>
    </testcase>
  </testsuite>
</testsuites>
"""


def test_parse_junit_xml_caps_failures() -> None:
    report = parse_junit_xml(_SAMPLE, source_artifact="test-results")
    assert report.total_failures == 2
    assert report.total_tests == 3
    assert report.source_artifact == "test-results"
    assert len(report.failures) == 2
    assert report.failures[0].classname == "t.test_x"
    assert report.failures[0].name == "test_fail"
    assert "list mismatch" in (report.failures[0].message or "")
    assert report.failures[0].body
    assert len(report.failures[0].body.splitlines()) <= 20


def test_parse_junit_from_zip() -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("junit.xml", _SAMPLE)
    report = parse_junit_from_zip(buf.getvalue(), source_artifact="test-results")
    assert report is not None
    assert report.total_failures == 2
    assert report.failures[0].name == "test_fail"


def test_junit_member_and_artifact_name_hints() -> None:
    assert is_junit_member("junit.xml")
    assert is_junit_member("foo/test-results/out.xml")
    assert is_junit_member("target/surefire-reports/TEST-Foo.xml")
    assert not is_junit_member("coverage.xml")
    assert artifact_looks_like_junit("test-results")
    assert artifact_looks_like_junit("junit-report")
    assert not artifact_looks_like_junit("playwright-traces")
