"""Verdicts, computed mechanically from Results (plan §5.4). Nobody types a verdict.

A test's results within one run decide its status: all expected is EXPECTED, all unexpected is
UNEXPECTED, and a mix (it failed, then passed with the same inputs) is FLAKY. A run passes when
it found results and no test is UNEXPECTED.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable

from qqresults.model import CaseVerdict, Result, Run, Verdict, VerdictStatus


def by_test(results: Iterable[Result]) -> dict[str, list[Result]]:
    grouped: dict[str, list[Result]] = defaultdict(list)
    for r in results:
        grouped[r.test_id].append(r)
    return dict(grouped)


def test_status(results: list[Result]) -> VerdictStatus:
    if all(r.expected for r in results):
        return VerdictStatus.EXPECTED
    if any(r.expected for r in results):
        return VerdictStatus.FLAKY
    return VerdictStatus.UNEXPECTED


def compute(run: Run, results: Iterable[Result]) -> Verdict:
    statuses = {test: test_status(rs) for test, rs in sorted(by_test(results).items())}
    counts = Counter(s.value for s in statuses.values())
    tests = [CaseVerdict(test_id=t, status=s.value) for t, s in statuses.items()
             if s is not VerdictStatus.EXPECTED]
    if not run.results_found:
        return Verdict(run_id=run.id, passed=False, counts=dict(counts), tests=tests,
                       reason="no test results were found; a missing signal is not a pass")
    unexpected = counts.get(VerdictStatus.UNEXPECTED.value, 0)
    return Verdict(
        run_id=run.id,
        passed=unexpected == 0,
        counts=dict(sorted(counts.items())),
        tests=tests,
        reason=f"{unexpected} unexpected test(s)" if unexpected else "",
    )
