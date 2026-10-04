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
    assert bad.raw.startswith("<testcase") and bad.source == "pytest.xml"
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
    assert len(r.message) < 4_100 and r.message.endswith("characters truncated]")
