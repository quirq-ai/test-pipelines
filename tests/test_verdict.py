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


def test_fail_then_pass_is_flaky_and_does_not_fail():
    v = verdict.compute(RUN, [res("a", "FAIL"), res("a", "PASS")])
    assert v.passed and v.counts == {"FLAKY": 1}


def test_no_results_is_not_a_pass():
    run = Run(id="r", repo="o/x", kind="gate", commit="c", results_found=False)
    v = verdict.compute(run, [])
    assert not v.passed and "missing signal" in v.reason
