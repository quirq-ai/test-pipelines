"""JUnit XML in, normalized Results out: the result sink (plan §5.2, like ResultSink for ResultDB).

Accepts the dialects the test adapters emit: a `<testsuites>` root or a bare `<testsuite>`,
suites nested in suites, and `<testcase>` elements with an optional `<failure>`, `<error>` or
`<skipped>` child. Different runners fill `classname` and `name` differently, so the test id is
`<classname>::<name>` when a classname exists, else `<suite>::<name>`, else the name alone.

The parser is the standard library's, which never fetches external entities; with Expat 2.4.1 or
newer (what current Python builds use) it also refuses entity-expansion bombs (billion laughs). TODO(expert): add size limits per report
if adapters start emitting very large reports.
"""
from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from qqresults.errors import Error
from qqresults.model import Result, Status

# What a Result keeps is published for good: in the bundle artifact and on the write-once
# `results` branch. So by default it holds the structured fields and the head of the failure
# message only, never the <testcase> element with its <system-out>/<system-err>, which can hold
# anything a test printed (audit R3). The head of a message (the assertion and the first frames)
# is what tells failures apart; the rest is in the job log, which can be deleted.
MAX_MESSAGE = 1_000     # characters of a failure or skip message kept on the normalized Result
MAX_MESSAGE_LINES = 20  # and lines of it, so a long traceback keeps only its head
MAX_RAW = 16_000        # characters of the original <testcase> element kept as raw, opt-in only

_CAPTURED = re.compile(r"<(system-out|system-err)\b[^>]{0,200}?(?:/>|>.*?(?:</\1\s*>|$))",
                       re.DOTALL | re.IGNORECASE)


class JUnitError(Error):
    """The report is not JUnit XML. The message says which file and why."""


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [{len(text) - limit} characters truncated]"


def _cap_message(text: str) -> str:
    """The first MAX_MESSAGE_LINES lines and MAX_MESSAGE characters, without captured output,
    plus a marker saying how much was cut.

    <system-out>/<system-err> are siblings of <failure>, so they are never part of a message;
    a runner that pastes them into the message text as markup still has them cut out here. The
    text is cut to a bounded size before that, so a huge message cannot make the search slow.
    """
    lines = text.split("\n")
    cut = ""
    if len(lines) > MAX_MESSAGE_LINES:
        text = "\n".join(lines[:MAX_MESSAGE_LINES])
        cut = f"\n... [{len(lines) - MAX_MESSAGE_LINES} lines truncated]"
    total = len(text)
    text = _CAPTURED.sub("[captured output removed]", text[:2 * MAX_MESSAGE])   # room for markup
    if total > 2 * MAX_MESSAGE or len(text) > MAX_MESSAGE:
        text = text[:MAX_MESSAGE]
        cut = f"\n... [{max(total - len(text), 1)} characters truncated]"
    return text + cut


def _duration(value: str | None) -> float | None:
    """Seconds, or None when absent or not a finite non-negative number."""
    try:
        seconds = float(value) if value else None
    except ValueError:
        return None
    if seconds is None or not math.isfinite(seconds) or seconds < 0:
        return None
    return round(seconds, 6)


def _message(child: ET.Element) -> str:
    message = child.get("message") or ""
    text = (child.text or "").strip()
    if text and text != message:
        message = f"{message}\n{text}" if message else text
    return message


def _outcome(case: ET.Element) -> tuple[Status, str]:
    """The worst outcome among the case's children, with every message of that kind kept."""
    for tag, status in (("error", Status.CRASH), ("failure", Status.FAIL), ("skipped", Status.SKIP)):
        children = case.findall(tag)
        if children:
            return status, _cap_message("\n\n".join(_message(c) for c in children))
    return Status.PASS, ""


def _cases(element: ET.Element, suite: str):
    """Yield (suite name, testcase) for every testcase under element, at any depth."""
    for child in element:
        if child.tag == "testcase":
            yield suite, child
        elif child.tag == "testsuite":
            yield from _cases(child, child.get("name") or suite)


def parse(data: bytes, run_id: str, source: str = "", keep_raw: bool = False) -> list[Result]:
    """Normalize one JUnit XML report into Results for run_id.

    raw stays empty unless keep_raw: the element (with its captured output) is then kept, up to
    MAX_RAW characters, and published with the rest of the bundle.
    """
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
            raw=_truncate(ET.tostring(case, encoding="unicode"), MAX_RAW) if keep_raw else "",
        ))
    return results


def parse_file(path: Path, run_id: str, source: str = "", keep_raw: bool = False) -> list[Result]:
    try:
        data = path.read_bytes()
    except OSError as e:
        raise JUnitError(f"{path}: cannot read: {e.strerror}") from None
    return parse(data, run_id, source or str(path), keep_raw=keep_raw)
