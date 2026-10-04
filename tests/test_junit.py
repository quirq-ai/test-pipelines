import time

import pytest

from qqresults import junit
from qqresults.model import Status


def parse(junit_dir, name):
    return junit.parse_file(junit_dir / name, "run-1", source=name)


def test_pytest_report_is_normalized(junit_dir):
    rs = {r.test_id: r for r in parse(junit_dir, "pytest.xml")}
    assert list(rs) == ["tests.test_greet::test_hello", "tests.test_greet::test_goodbye",
                        "tests.test_greet::test_setup", "tests.test_greet::test_later"]
    assert rs["tests.test_greet::test_hello"].status == Status.PASS
    assert rs["tests.test_greet::test_hello"].expected
    bad = rs["tests.test_greet::test_goodbye"]
    assert (bad.status, bad.expected) == (Status.FAIL, False)
    assert bad.message.startswith("AssertionError: assert 'bye' == 'goodbye'\ndef test_goodbye")
    assert rs["tests.test_greet::test_setup"].status == Status.CRASH
    skipped = rs["tests.test_greet::test_later"]
    assert (skipped.status, skipped.expected, skipped.message.split("\n")[0]) == (
        Status.SKIP, True, "not yet")
    assert bad.duration_s == 0.002
    assert bad.raw == "" and bad.source == "pytest.xml"     # kept only with keep_raw (audit R3)
    assert all(r.run_id == "run-1" for r in rs.values())


@pytest.mark.parametrize("name, ids", [
    ("vitest.xml", ["src/lib/search.test.ts::search > finds a file",
                    "src/lib/search.test.ts::search > ranks titles first"]),
    ("jest-junit.xml", ["Button renders::Button renders"]),
    ("gotestsum.xml", ["example.com/pkg/store::TestPut",
                       "example.com/pkg/store::TestPut/overwrite_is_refused"]),
    ("bare-suite.xml", ["typecheck::tsc --noEmit"]),
])
def test_runner_dialects(junit_dir, name, ids):
    assert [r.test_id for r in parse(junit_dir, name)] == ids


def test_vitest_failure_keeps_message(junit_dir):
    failed = [r for r in parse(junit_dir, "vitest.xml") if not r.expected]
    assert len(failed) == 1 and failed[0].message.startswith("expected 2 to be 1\nAssertionError")


def test_nested_suites_and_missing_classname():
    data = b"""<testsuites><testsuite name="outer"><testsuite name="inner">
        <testcase name="a"/></testsuite><testcase name="b"/></testsuite></testsuites>"""
    assert [r.test_id for r in junit.parse(data, "r")] == ["inner::a", "outer::b"]


@pytest.mark.parametrize("name, error", [
    ("not-junit.xml", "root element is <coverage>"),
    ("truncated.xml", "not well-formed XML"),
])
def test_bad_reports_fail_loudly(junit_dir, name, error):
    with pytest.raises(junit.JUnitError, match=error):
        parse(junit_dir, name)


def test_entity_bomb_is_refused():
    bomb = b"""<?xml version="1.0"?><!DOCTYPE l [<!ENTITY a "aaaaaaaaaa">
      <!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;"><!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;">
      <!ENTITY d "&c;&c;&c;&c;&c;&c;&c;&c;&c;&c;"><!ENTITY e "&d;&d;&d;&d;&d;&d;&d;&d;&d;&d;">
      <!ENTITY f "&e;&e;&e;&e;&e;&e;&e;&e;&e;&e;"><!ENTITY g "&f;&f;&f;&f;&f;&f;&f;&f;&f;&f;">
      <!ENTITY h "&g;&g;&g;&g;&g;&g;&g;&g;&g;&g;"><!ENTITY i "&h;&h;&h;&h;&h;&h;&h;&h;&h;&h;">]>
      <testsuite><testcase name="&i;"/></testsuite>"""
    with pytest.raises(junit.JUnitError):
        junit.parse(bomb, "r")


def test_long_messages_are_truncated():
    data = b'<testsuite name="s"><testcase name="a"><failure message="' + b"x" * 10_000 + b'"/></testcase></testsuite>'
    (r,) = junit.parse(data, "r")
    assert len(r.message) < 1_100 and r.message.endswith("characters truncated]")


CAPTURED = b"""<testsuite name="s"><testcase name="a"><failure message="assert 1 == 2">Traceback
""" + b"".join(b"  frame %d\n" % i for i in range(100)) + b"""</failure>
    <system-out>TOKEN=hunter2 printed by the test</system-out>
    <system-err>password: hunter3</system-err></testcase></testsuite>"""


def test_by_default_only_the_head_of_the_message_is_kept():
    (r,) = junit.parse(CAPTURED, "r")
    assert r.raw == ""
    assert "hunter2" not in r.to_json() and "hunter3" not in r.to_json()
    lines = r.message.split("\n")
    assert lines[0] == "assert 1 == 2" and lines[1] == "Traceback"
    assert len(lines) == junit.MAX_MESSAGE_LINES + 1 and lines[-1] == "... [82 lines truncated]"


def test_captured_output_pasted_into_a_message_is_cut_out():
    data = b"""<testsuite name="s"><testcase name="a"><failure message="boom">before
        &lt;system-out&gt;TOKEN=hunter2&lt;/system-out&gt; after
        &lt;SYSTEM-ERR attr="x"&gt;unterminated hunter3</failure></testcase></testsuite>"""
    (r,) = junit.parse(data, "r")
    assert "hunter2" not in r.message and "hunter3" not in r.message
    assert "before" in r.message and "after" in r.message
    assert r.message.count("[captured output removed]") == 2


def test_keep_raw_keeps_the_element_capped():
    (r,) = junit.parse(CAPTURED, "r", keep_raw=True)
    assert r.raw.startswith("<testcase") and "<system-out>TOKEN=hunter2" in r.raw
    assert len(r.message.split("\n")) == junit.MAX_MESSAGE_LINES + 1   # capped all the same
    big = b'<testsuite name="s"><testcase name="a"><system-out>' + b"x" * 20_000 + \
        b"</system-out></testcase></testsuite>"
    (r,) = junit.parse(big, "r", keep_raw=True)
    assert len(r.raw) < junit.MAX_RAW + 50 and r.raw.endswith("characters truncated]")


@pytest.mark.parametrize("time, expected", [("1.5", 1.5), ("1,5", None), ("nan", None),
                                            ("inf", None), ("-1", None), ("", None)])
def test_durations(time, expected):
    (r,) = junit.parse(f'<testsuite name="s"><testcase name="a" time="{time}"/></testsuite>'.encode(), "r")
    assert r.duration_s == expected


def test_every_message_is_kept_and_error_wins():
    data = b"""<testsuite name="s"><testcase name="a"><failure message="one"/>
        <failure message="two"/><error message="boom"/></testcase></testsuite>"""
    (r,) = junit.parse(data, "r")
    assert r.status == "CRASH" and r.message == "boom"
    (r,) = junit.parse(data.replace(b'<error message="boom"/>', b""), "r")
    assert r.message == "one\n\ntwo"


def test_a_huge_message_full_of_unclosed_markup_is_capped_quickly():
    data = (b'<testsuite name="s"><testcase name="a"><failure message="'
            + b"&lt;system-out " * 200_000 + b'"/></testcase></testsuite>')
    start = time.monotonic()
    (r,) = junit.parse(data, "r")
    assert time.monotonic() - start < 2 and len(r.message) < 1_100


def test_pasted_captured_output_with_long_attributes_is_still_cut():
    body = "a" * 10 + '<system-out x="' + "y" * 300 + '">SECRET</system-out>'
    data = (b'<testsuite name="s"><testcase name="a"><failure><![CDATA[' + body.encode()
            + b']]></failure></testcase></testsuite>')
    (r,) = junit.parse(data, "r")
    assert "SECRET" not in r.message


def test_the_failure_type_is_kept_short_and_stripped():
    xml = (b'<testsuite name="s"><testcase classname="c" name="a">'
           b'<failure type="  java.lang.AssertionError " message="expected 1"/></testcase>'
           b'<testcase classname="c" name="b"><error type="' + b"X" * 500 + b'"/></testcase>'
           b'<testcase classname="c" name="c"><failure message="Failed"/></testcase>'
           b'<testcase classname="c" name="d"><skipped type="pytest.skip"/></testcase>'
           b'<testcase classname="c" name="e"><failure type="A"/><failure type="B"/>'
           b'<failure type="A"/></testcase>'
           b'<testcase classname="c" name="f"><failure type="A"/><failure message="x"/>'
           b'</testcase></testsuite>')
    types = {r.test_id: r.failure_type for r in junit.parse(xml, "r")}
    assert types == {"c::a": "java.lang.AssertionError", "c::b": "X" * junit.MAX_TYPE,
                     "c::c": "", "c::d": "", "c::e": "A / B",
                     "c::f": ""}


def _failure_status(attrs: str, text: str = "") -> str:
    xml = (f'<testsuite name="s"><testcase classname="c" name="a"><failure {attrs}>{text}'
           f'</failure></testcase></testsuite>').encode()
    (r,) = junit.parse(xml, "r")
    return r.status


# AUDIT-R5 N1: a <failure> that reports a timeout, an abort or a signal is a CRASH.
@pytest.mark.parametrize("attrs, text", [
    ('type="timeout"', ""),                                     # Rust libtest
    ('type="test timeout"', ""),                                # cargo-nextest
    ('type="test abort"', ""),
    ('type="TIMEOUT"', ""),
    ('type="x.Timeout"', ""),
    ('type="SIGSEGV"', ""),
    ('message="thrown: &quot;Exceeded timeout of 5000 ms for a test."', ""),   # jest
    ('message="Failed: Timeout &gt;1.0s"', ""),                 # pytest-timeout
    ('message="Timeout of 2000ms exceeded."', ""),              # mocha
    ('message="Timeout"', ""),
    ('message="timed out after 5s"', ""),
    ('message="test timed out after 60s"', ""),
    ('', "Aborted (core dumped)"),
    ('message="process aborted with signal 6 (SIGABRT)"', ""),
    ('message="killed by signal 9"', ""),
    ('message="Fatal signal 11"', ""),
    ('message="signal: 11, SIGSEGV: invalid memory reference"', ""),
    ('message="SIGSEGV"', ""),
    ('message="  \'Timeout\'"', ""),
    ('message="[31mTimeout[0m"', ""),     # an ANSI code whose ESC the runner dropped
    # with a type that carries no kind, the message decides
    ('type="assert" message="timed out after 5s"', ""),
    ('type="test failure" message="timeout: expected 3 to equal 5"', ""),
    ('type="failure" message="Aborted (core dumped)"', ""),
    # AUDIT M1: node --test writes fixed camelCase types; split at the humps
    ('type="testTimeoutFailure" message="test timed out after 50ms"', ""),
    ('type="testTimeoutFailure"', ""),
    ('type="hookFailed" message="Timeout"', ""),
], ids=["libtest-timeout", "nextest-timeout", "nextest-abort", "TIMEOUT", "namespaced",
        "sig-type", "jest", "pytest-timeout", "mocha", "bare", "timed-out", "test-timed-out",
        "aborted-text", "process-aborted", "killed", "fatal-signal", "signal-number", "sig-name",
        "quoted", "ansi", "assert-type-timed-out", "category-type-timeout",
        "generic-type-aborted", "node-timeout", "node-timeout-no-message",
        "node-hook-timeout"])
def test_a_failure_that_reports_a_timeout_abort_or_signal_is_a_crash(attrs, text):
    assert _failure_status(attrs, text) == Status.CRASH


@pytest.mark.parametrize("attrs, text", [
    ('message="assert timeout == 5"', ""),
    ('message="timeout == 5"', ""),
    ('message="timeout is None"', ""),
    ('message="timeout in (1, 2)"', ""),
    ('message="timeout.seconds"', ""),
    ('message="timeout_s"', ""),
    ('message="AssertionError: timeout must be positive"', ""),
    ('message="expected abort() to be called"', ""),
    ('message="TimeoutError: read"', ""),
    ('type="TimeoutError" message="x"', ""),
    ('type="assert"', ""),
    ('type="test failure"', ""),
    ('type="AssertionError" message="expected 1"', "timeout after 5s"),   # first line only
    ('message="the call aborted"', ""),
    ('message="signals: 3 != 4"', ""),
    # review of the N1 fix: an exception-class type keeps an assertion a FAIL
    ('message="signal 5 != 3"', ""),
    ('message="signal: 5 != 3"', ""),
    ('type="AssertionError" message="timeout: expected 3 to equal 5"', ""),
    ('type="AssertionError" message="Aborted transactions: 2 != 3"', ""),
    ('type="AssertionError" message="Timeout"', ""),
    ('type="java.lang.AssertionError" message="SIGSEGV handler not installed"', ""),
    # AUDIT M1: node's fixed types that are not a timeout
    ('type="testCodeFailure" message="Expected values to be strictly equal"', ""),
    ('type="hookFailed" message="boom"', ""),
    ('type="TimeoutFailure" message="x"', ""),
], ids=["assert-timeout", "timeout-eq", "timeout-is", "timeout-in", "timeout-attr",
        "timeout-underscore", "assertion-mentions", "expected-abort", "TimeoutError-message",
        "TimeoutError-type", "assert-type", "nextest-failure", "body-only", "later-in-line",
        "signals", "signal-compare", "signal-colon-compare", "typed-timeout-prefix",
        "typed-aborted", "typed-Timeout", "typed-sig-name", "node-code", "node-hook",
        "class-TimeoutFailure"])
def test_an_assertion_that_mentions_a_timeout_stays_a_fail(attrs, text):
    assert _failure_status(attrs, text) == Status.FAIL


def test_an_error_stays_a_crash_and_a_skip_a_skip():
    xml = (b'<testsuite name="s"><testcase name="a"><error type="timeout"/></testcase>'
           b'<testcase name="b"><skipped message="Timeout"/></testcase></testsuite>')
    assert [r.status for r in junit.parse(xml, "r")] == [Status.CRASH, Status.SKIP]
