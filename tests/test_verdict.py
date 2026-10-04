from qqresults import verdict
from qqresults.model import Result, Run


def res(test, status):
    return Result(run_id="r", test_id=test, status=status, expected=status in ("PASS", "SKIP"))


RUN = Run(id="r", repo="o/x", kind="gate", commit="c")


def test_all_pass_passes():
    v = verdict.compute(RUN, [res("a", "PASS"), res("b", "SKIP")])
    assert v.passed and v.counts == {"EXPECTED": 2} and v.tests == []


def test_failure_fails_and_is_listed():
    v = verdict.compute(RUN, [res("a", "PASS"), res("b", "FAIL"), res("c", "CRASH")])
    assert not v.passed
    assert [(t.test_id, t.status) for t in v.tests] == [("b", "UNEXPECTED"), ("c", "UNEXPECTED")]
    assert v.reason == "2 unexpected test(s)"


def test_no_results_is_not_a_pass():
    run = Run(id="r", repo="o/x", kind="gate", commit="c", results_found=False)
    v = verdict.compute(run, [])
    assert not v.passed and "missing signal" in v.reason


def test_a_repeated_id_with_mixed_outcomes_is_not_flaky():
    # two suites with the same names, or a teardown error next to a passing call
    v = verdict.compute(RUN, [res("a", "FAIL"), res("a", "PASS")])
    assert not v.passed and v.counts == {"UNEXPECTED": 1}


def test_a_report_without_cases_is_not_a_pass():
    v = verdict.compute(RUN, [])
    assert not v.passed and "missing signal" in v.reason
