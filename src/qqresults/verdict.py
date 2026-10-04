"""Verdicts, computed mechanically from Results (plan §5.4). Nobody types a verdict.

Within one run, a test with any unexpected result is UNEXPECTED, else EXPECTED. A test id can
repeat inside one run for reasons other than a retry (two suites with the same names, a teardown
error reported next to the call), so repeats never make a test FLAKY on their own; FLAKY and
EXONERATED come only from explicit retries and base runs (V0-TST-03). A run passes when it has
results and no test is UNEXPECTED.
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
    return VerdictStatus.UNEXPECTED


def compute(run: Run, results: Iterable[Result]) -> Verdict:
    results = list(results)
    statuses = {test: test_status(rs) for test, rs in sorted(by_test(results).items())}
    counts = Counter(s.value for s in statuses.values())
    tests = [CaseVerdict(test_id=t, status=s.value) for t, s in statuses.items()
             if s is not VerdictStatus.EXPECTED]
    if not run.results_found or not results:
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
