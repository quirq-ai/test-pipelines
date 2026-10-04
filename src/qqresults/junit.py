"""JUnit XML in, normalized Results out: the result sink (plan §5.2, like ResultSink for ResultDB).

Accepts the dialects the test adapters emit: a `<testsuites>` root or a bare `<testsuite>`,
suites nested in suites, and `<testcase>` elements with an optional `<failure>`, `<error>` or
`<skipped>` child. Different runners fill `classname` and `name` differently, so the test id is
`<classname>::<name>` when a classname exists, else `<suite>::<name>`, else the name alone.

The parser is the standard library's, which never fetches external entities; with Expat 2.4.1 or
newer (what current CPython builds use) it also refuses entity-expansion bombs (billion laughs). TODO(expert): add size limits per report
if adapters start emitting very large reports.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from qqresults.errors import Error
from qqresults.model import Result, Status

MAX_MESSAGE = 4_000     # characters of a failure message kept on the normalized Result
MAX_RAW = 16_000        # characters of the original <testcase> element kept as raw


class JUnitError(Error):
    """The report is not JUnit XML. The message says which file and why."""


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [{len(text) - limit} characters truncated]"


def _duration(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return round(float(value.replace(",", "")), 6)
    except ValueError:
        return None


def _outcome(case: ET.Element) -> tuple[Status, str]:
    for tag, status in (("failure", Status.FAIL), ("error", Status.CRASH), ("skipped", Status.SKIP)):
        child = case.find(tag)
        if child is not None:
            message = child.get("message") or ""
            text = (child.text or "").strip()
            if text and text != message:
                message = f"{message}\n{text}" if message else text
            return status, _truncate(message, MAX_MESSAGE)
    return Status.PASS, ""


def _cases(element: ET.Element, suite: str):
    """Yield (suite name, testcase) for every testcase under element, at any depth."""
    for child in element:
        if child.tag == "testcase":
            yield suite, child
        elif child.tag == "testsuite":
            yield from _cases(child, child.get("name") or suite)


def parse(data: bytes, run_id: str, source: str = "") -> list[Result]:
    """Normalize one JUnit XML report into Results for run_id."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise JUnitError(f"{source or 'report'}: not well-formed XML: {e}") from None
    if root.tag not in ("testsuites", "testsuite"):
        raise JUnitError(f"{source or 'report'}: root element is <{root.tag}>, "
                         "expected <testsuites> or <testsuite>")
    top = (root.get("name") or "") if root.tag == "testsuite" else ""

    results = []
    for suite, case in _cases(root, top):
        name = case.get("name") or ""
        classname = case.get("classname") or ""
        prefix = classname or suite
        test_id = f"{prefix}::{name}" if prefix else name
        if not test_id:
            raise JUnitError(f"{source or 'report'}: a <testcase> has no name")
        status, message = _outcome(case)
        results.append(Result(
            run_id=run_id,
            test_id=test_id,
            status=status.value,
            expected=status in (Status.PASS, Status.SKIP),
            duration_s=_duration(case.get("time")),
            message=message,
            file=case.get("file") or "",
            source=source,
            raw=_truncate(ET.tostring(case, encoding="unicode"), MAX_RAW),
        ))
    return results


def parse_file(path: Path, run_id: str, source: str = "") -> list[Result]:
    try:
        data = path.read_bytes()
    except OSError as e:
        raise JUnitError(f"{path}: cannot read: {e.strerror}") from None
    return parse(data, run_id, source or str(path))
