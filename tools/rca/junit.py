"""Parse JUnit XML from run artifacts. Source-agnostic (no GitHub client)."""

from __future__ import annotations

import fnmatch
import io
import re
import zipfile
from typing import Sequence
from xml.etree import ElementTree

from .config import JUNIT_FAILURE_CAP
from .models import JUnitFailure, JUnitReport

_JUNIT_ARTIFACT_NAME = re.compile(
    r"junit|test-results|surefire|test-report",
    re.IGNORECASE,
)
_BODY_LINES = 20


def artifact_looks_like_junit(name: str) -> bool:
    return bool(_JUNIT_ARTIFACT_NAME.search(name or ""))


def is_junit_member(name: str) -> bool:
    path = name.replace("\\", "/")
    base = path.rsplit("/", 1)[-1]
    if fnmatch.fnmatch(base.lower(), "*junit*.xml"):
        return True
    lowered = f"/{path.lower()}"
    if "/test-results/" in lowered and path.lower().endswith(".xml"):
        return True
    if "/surefire-reports/" in lowered and path.lower().endswith(".xml"):
        return True
    return False


def parse_junit_xml(text: str, *, source_artifact: str | None = None) -> JUnitReport:
    root = ElementTree.fromstring(text)
    suites = _suites(root)
    failures: list[JUnitFailure] = []
    total_tests = 0
    total_failures = 0
    for suite in suites:
        total_tests += _int_attr(suite, "tests")
        total_failures += _int_attr(suite, "failures") + _int_attr(suite, "errors")
        for case in suite.iter():
            if not _tag(case).endswith("testcase"):
                continue
            fail_node = next(
                (
                    child
                    for child in list(case)
                    if _tag(child).endswith("failure") or _tag(child).endswith("error")
                ),
                None,
            )
            if fail_node is None:
                continue
            body = (fail_node.text or "").strip()
            body_lines = body.splitlines()[:_BODY_LINES]
            failures.append(
                JUnitFailure(
                    classname=case.attrib.get("classname") or "",
                    name=case.attrib.get("name") or "",
                    message=fail_node.attrib.get("message"),
                    body="\n".join(body_lines) if body_lines else None,
                )
            )
    if total_failures == 0:
        total_failures = len(failures)
    if total_tests == 0:
        total_tests = len(failures)
    return JUnitReport(
        total_failures=total_failures,
        total_tests=total_tests or None,
        failures=failures[:JUNIT_FAILURE_CAP],
        source_artifact=source_artifact,
    )


def parse_junit_from_zip(data: bytes, *, source_artifact: str | None = None) -> JUnitReport | None:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return None
    reports: list[JUnitReport] = []
    for info in archive.infolist():
        if info.is_dir() or not is_junit_member(info.filename):
            continue
        try:
            text = archive.read(info).decode("utf-8-sig")
            reports.append(parse_junit_xml(text, source_artifact=source_artifact))
        except (ElementTree.ParseError, UnicodeDecodeError, KeyError):
            continue
    return merge_junit_reports(reports)


def merge_junit_reports(reports: Sequence[JUnitReport | None]) -> JUnitReport | None:
    usable = [item for item in reports if item is not None]
    if not usable:
        return None
    failures: list[JUnitFailure] = []
    total_failures = 0
    total_tests = 0
    source = None
    for report in usable:
        total_failures += report.total_failures
        total_tests += report.total_tests or 0
        failures.extend(report.failures)
        source = source or report.source_artifact
    return JUnitReport(
        total_failures=total_failures,
        total_tests=total_tests or None,
        failures=failures[:JUNIT_FAILURE_CAP],
        source_artifact=source,
    )


def _suites(root: ElementTree.Element) -> list[ElementTree.Element]:
    tag = _tag(root)
    if tag.endswith("testsuite"):
        return [root]
    if tag.endswith("testsuites"):
        return [child for child in list(root) if _tag(child).endswith("testsuite")]
    return [root]


def _tag(element: ElementTree.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _int_attr(element: ElementTree.Element, name: str) -> int:
    raw = element.attrib.get(name)
    if raw is None or raw == "":
        return 0
    try:
        return int(raw)
    except ValueError:
        return 0
