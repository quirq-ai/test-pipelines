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
MAX_TYPE = 200          # characters of the `type` attribute of <failure>/<error> kept

_CAPTURED = re.compile(r"<(system-out|system-err)\b[^>]*?(?:/>|>.*?(?:</\1\s*>|$))",
                       re.DOTALL | re.IGNORECASE)


# Words that say nothing about a failure's kind (see retry.py): compared ignoring case, trailing
# punctuation and any namespace (`a.b.Exception` is `Exception`). The root exception classes are
# generic, and so are runner categories: some runners write one fixed `type` for every failure
# (an `assert` for an assertion and for an unrelated panic alike, a `timeout`), or begin every
# message with the same word (`thread '<name>' panicked at`, `Traceback`, `thrown:`), so equal
# words on both sides say nothing about whether it is the same failure (audit N1).
GENERIC_KINDS = frozenset({"failed", "fail", "failure", "error", "def", "[captured",
                           "exception", "throwable", "baseexception",
                           "assert", "timeout", "timed", "panicked", "panic", "thread",
                           "traceback", "thrown", "abort", "aborted", "signal", "killed"})
_TRAILING = ":.!?;,"


# A kind is an exception class (audit M1): an identifier, or a namespaced one (`a.b.C`, `a::C`),
# whose last part is CamelCase and ends in one of _CLASS_SUFFIXES with something before it
# (`ValueError`, not `Error`, `Terror`, `parse_error` or `testCodeFailure`), with an optional
# trailing colon. Anything else (a runner category, a file path, prose) carries no kind. So do the
# assertion classes (_ASSERTION_CLASSES, and any whose last part contains `assert` in any case):
# runners raise them for every failed check, so two say nothing about whether it is one failure.
_CLASS_SUFFIXES = ("Error", "Exception", "Failure", "Fault", "Panic")
_CLASS_LIKE = re.compile(r"\A(?:[^\W\d]\w*(?:\.|::))*[A-Z][A-Za-z0-9]*:?\Z")
_ASSERTION_CLASSES = frozenset({"assertionerror", "assertionfailederror", "comparisonfailure",
                                "expectationfailedexception", "multiplefailureserror"})


def _last_part(kind: str) -> str:
    return re.split(r"\.|::", kind.rstrip(_TRAILING))[-1]


def exception_class(kind: str) -> bool:
    """A type or word that names an exception class (see _CLASS_LIKE), an assertion class too."""
    last = _last_part(kind)
    return (bool(_CLASS_LIKE.match(kind))
            and any(last.endswith(x) and len(last) > len(x) for x in _CLASS_SUFFIXES))


def informative_type(kind: str) -> bool:
    """True only for an exception class (exception_class) that says what failed: not an
    assertion class, and not a generic word (GENERIC_KINDS, such as the root `BaseException`).
    A runner category (`assert`, `testCodeFailure`, `test failure`) carries no kind."""
    last = _last_part(kind).casefold()
    return (exception_class(kind) and last not in GENERIC_KINDS
            and last not in _ASSERTION_CLASSES and "assert" not in last)


# A <failure> that reports a timeout, an abort or a signal is a CRASH (model.Status: a timeout or
# a dead process is not an assertion), whatever tag the runner chose, so it never exonerates
# (audit N1). The rule is deliberately narrow, so an ordinary assertion that mentions one of
# these words stays a FAIL:
#   - the `type` is not an exception class (exception_class) and, split into words at anything
#     that is not a letter or digit and at camelCase humps and compared in any case, has one of
#     _CRASH_TYPE_WORDS as a whole word (`timeout`, `test timeout`, `test abort`, `x.Timeout`,
#     `testTimeoutFailure`; not `TimeoutError`, which is an exception class the test raised), or
#   - the failure has no type, or one that is not an exception class (`assert`, `test failure`,
#     `testCodeFailure`), and the first line of its message, in any case, after leading spaces, quotes
#     and one generic prefix (`Failed:`, `thrown:`, `Error:`, `failure:`), begins with a
#     timeout, abort or signal report: `Timeout`, `Timed out`, `Exceeded timeout`, `test timed
#     out`, `abort`/`aborted`, `process aborted`, `killed by signal`, `terminated by signal`,
#     `caught`/`received`/`fatal signal`, `signal <number>` followed by a signal name
#     (`signal: 11, SIGSEGV`), or a signal name such as `SIGSEGV`.
# A failure whose type is an exception class (`AssertionError`, `TimeoutError`) is an assertion whatever its
# message says (`timeout: expected 3 to equal 5`, `Aborted transactions: 2 != 3`). A message
# that begins with `timeout` used as a value (`timeout == 5`, `timeout is None`, `timeout in (1,
# 2)`, `timeout.seconds`, `timeout[0]`, `timeout(...)`) stays a FAIL, as does `signal 5 != 3`;
# an assertion message that begins `assert`, `expected` or an exception class never matches.
# Limits: a report that names the timeout later in the line (`test x: timeout after 5s`), or as
# an exception class (`TimeoutError`), stays a FAIL, and its kind is compared as usual. With no
# informative type, an assertion message that happens to begin with one of these words in
# another sense (`Aborted transactions: 2 != 3`) becomes a CRASH. That error only ever blocks: a
# CRASH never exonerates, and one that passes on a retry is FLAKY, as a FAIL would be.
_CRASH_TYPE_WORDS = frozenset({"timeout", "timedout", "abort", "aborted", "signal", "sigsegv",
                               "sigabrt", "sigbus", "sigfpe", "sigill", "sigkill", "sigterm"})
_SIG_NAME = r"sig(?:segv|abrt|bus|fpe|ill|kill|term|trap|sys|pipe|alrm|int|quit)\b"
_CRASH_MESSAGE = re.compile(
    r"""\A(?:(?:failed|thrown|error|failure)\s*:\s*)?["'`]*\s*(?:"""
    r"timeout\b(?!\s*(?:[=!<>]=|is\b|in\b|\.\w|\[|\())"
    r"|timed\s+out\b|exceeded\s+(?:the\s+)?timeout\b|test\s+timed\s+out\b"
    r"|(?:process\s+|test\s+)?abort(?:ed)?\b"
    r"|(?:killed|terminated|aborted)\s+by\s+signal\b"
    r"|(?:caught|received|fatal)\s+signal\b"
    rf"|signal\s*:?\s*\d+\W*{_SIG_NAME}"
    rf"|{_SIG_NAME})",
    re.IGNORECASE)
_ESCAPES = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|[@-Z\\-_])|\[[0-9;]+m")


def _reports_crash(child: ET.Element) -> bool:
    """A <failure> that reports a timeout, an abort or a signal (see _CRASH_TYPE_WORDS)."""
    kind = " ".join((child.get("type") or "").split())
    if exception_class(kind):
        return False    # an exception class (`TimeoutError` too): an assertion, whatever the message says
    # Not a class: a runner category, split into words at anything that is not a letter or digit
    # and at camelCase humps (node's `testTimeoutFailure` is `test timeout failure`).
    words = re.split(r"[^0-9a-z]+", re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", kind).casefold())
    if _CRASH_TYPE_WORDS.intersection(words):
        return True
    line = _ESCAPES.sub("", _message(child)).strip().split("\n", 1)[0]
    return bool(_CRASH_MESSAGE.match(line.strip()))


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


def _failure_type(children: list[ET.Element]) -> str:
    """The `type` attribute of <failure>/<error> (an exception class, where the runner writes
    one), or "" unless every one has it. Several different types are kept in order, joined by
    " / "."""
    types = dict.fromkeys(" ".join((c.get("type") or "").split()) for c in children)
    if "" in types:
        return ""
    return " / ".join(types)[:MAX_TYPE].strip()


def _outcome(case: ET.Element) -> tuple[Status, str, str]:
    """The worst outcome among the case's children, with every message of that kind kept, and
    the failure type (for a FAIL or CRASH only). A <failure> that reports a timeout, an abort or
    a signal is a CRASH (see _CRASH_TYPE_WORDS)."""
    for tag, status in (("error", Status.CRASH), ("failure", Status.FAIL), ("skipped", Status.SKIP)):
        children = case.findall(tag)
        if status is Status.FAIL and any(map(_reports_crash, children)):
            status = Status.CRASH
        if children:
            return (status, _cap_message("\n\n".join(_message(c) for c in children)),
                    "" if status is Status.SKIP else _failure_type(children))
    return Status.PASS, "", ""


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
        status, message, failure_type = _outcome(case)
        results.append(Result(
            run_id=run_id,
            test_id=test_id,
            status=status.value,
            expected=status in (Status.PASS, Status.SKIP),
            duration_s=_duration(case.get("time")),
            message=message,
            failure_type=failure_type,
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
