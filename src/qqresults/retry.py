"""Retry, then compare with base (plan §3 P5, flakes.toml [verdict]); V0-TST-03.

Failed tests are rerun with the change (`retry_failed` times). Tests that still fail are run
without the change, at the base commit. Only failures that pass without the change fail it:

    passed on a retry                     FLAKY        does not fail the change
    failed on retries and also on base    EXONERATED   does not fail the change
    failed on retries, passed on base     UNEXPECTED   fails the change
    no base result (a new test, no data)  UNEXPECTED   a missing signal never exonerates

The rerun command comes from the caller (the adapter or builder), so this module never names a
test runner. It runs through the shell with:

    QQ_JUNIT_DIR     an empty directory; the command writes its JUnit XML there
    QQ_RETRY_TESTS   a file listing the failed test ids, one per line; a command that can select
                     tests should run only those, and one that cannot reruns everything
                     (results for other tests are ignored)

Its exit code is ignored: failing tests are what is being measured. The base side runs in a git
worktree of the base commit, inside the same job and environment.
TODO(expert): run the base side hermetically once remote-build provides executors (V0-RBE-01).
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from qqresults import bundle, junit, verdict
from qqresults.errors import Error
from qqresults.model import CaseVerdict, Result, Run, Verdict, VerdictStatus
from qqresults.policy import Policy

Runner = Callable[[str, Path, dict[str, str]], None]


class RetryError(Error):
    pass


def shell(cmd: str, cwd: Path, env: dict[str, str]) -> None:
    subprocess.run(cmd, shell=True, cwd=cwd, env={**os.environ, **env}, check=False)


def git(cwd: Path, *args: str) -> str:
    p = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    if p.returncode != 0:
        raise RetryError(f"git {' '.join(args)}: {p.stderr.strip() or p.returncode}")
    return p.stdout.strip()


def run_tests(cmd: str, cwd: Path, tests: list[str], run: Run, runner: Runner = shell) -> list[Result]:
    """Run cmd once in cwd and return its results for `tests`."""
    with tempfile.TemporaryDirectory(prefix="qq-retry-") as tmp:
        out = Path(tmp) / "junit"
        out.mkdir()
        listing = Path(tmp) / "tests.txt"
        listing.write_text("".join(t + "\n" for t in tests), encoding="utf-8")
        runner(cmd, cwd, {"QQ_JUNIT_DIR": str(out), "QQ_RETRY_TESTS": str(listing)})
        wanted = set(tests)
        results = []
        for path in sorted(out.rglob("*.xml")):
            results += [r for r in junit.parse_file(path, run.id, source=path.relative_to(out).as_posix())
                        if r.test_id in wanted]
        return results


def child_run(parent: Run, role: str, n: int = 0, commit: str = "") -> Run:
    suffix = f"{role}{n}" if n else role
    return Run.from_dict({**parent.to_dict(), "id": f"{parent.id}/{suffix}", "parent": parent.id,
                          "role": role, "commit": commit or parent.commit,
                          "results_found": True})


def decide(run: Run, results: list[Result], retries: list[bundle.Bundle],
           base: bundle.Bundle | None) -> Verdict:
    first = verdict.compute(run, results)
    if not results or not run.results_found:
        return first
    tests = []
    for case in first.tests:
        t = case.test_id
        passed_on = next((i for i, b in enumerate(retries, 1)
                          if any(r.test_id == t and r.expected for r in b.results)), None)
        base_rs = [r for r in base.results if r.test_id == t] if base else []
        if passed_on:
            tests.append(CaseVerdict(t, VerdictStatus.FLAKY.value, f"passed on retry {passed_on}"))
        elif base_rs and not any(r.expected for r in base_rs):
            tests.append(CaseVerdict(t, VerdictStatus.EXONERATED.value,
                                     f"also fails without the change, at {base.run.commit[:12]}"))
        elif base_rs:
            tests.append(CaseVerdict(t, VerdictStatus.UNEXPECTED.value,
                                     "fails with the change and passes without it"))
        elif base:
            tests.append(CaseVerdict(t, VerdictStatus.UNEXPECTED.value,
                                     "no result without the change (a new test, or no signal)"))
        else:
            tests.append(CaseVerdict(t, VerdictStatus.UNEXPECTED.value,
                                     "still fails on retry; not compared with base"))
    counts = dict(first.counts)
    counts.pop(VerdictStatus.UNEXPECTED.value, None)
    for c in tests:
        counts[c.status] = counts.get(c.status, 0) + 1
    unexpected = counts.get(VerdictStatus.UNEXPECTED.value, 0)
    return Verdict(run_id=run.id, passed=unexpected == 0, counts=dict(sorted(counts.items())),
                   tests=tests, reason=f"{unexpected} unexpected test(s)" if unexpected else "",
                   inputs=[b.run.id for b in retries] + ([base.run.id] if base else []))


@dataclass
class Rechecked:
    verdict: Verdict
    retries: list[bundle.Bundle]
    base: bundle.Bundle | None


def recheck(run: Run, results: list[Result], cmd: str, cwd: Path, policy: Policy,
            base_commit: str = "", runner: Runner = shell) -> Rechecked:
    """Retry the failed tests, then compare the still-failing ones with base."""
    retries: list[bundle.Bundle] = []
    failing = [c.test_id for c in verdict.compute(run, results).tests
               if c.status == VerdictStatus.UNEXPECTED.value]
    if not failing:
        return Rechecked(decide(run, results, [], None), [], None)
    remaining = list(failing)
    for n in range(1, policy.retry_failed + 1):
        child = child_run(run, "retry", n)
        rs = run_tests(cmd, cwd, remaining, child, runner)
        child = Run.from_dict({**child.to_dict(), "results_found": bool(rs)})
        retries.append(bundle.Bundle(child, rs, verdict.compute(child, rs)))
        remaining = [t for t in remaining if not any(r.test_id == t and r.expected for r in rs)]
        if not remaining:
            break
    base = None
    base_commit = base_commit or run.base_commit
    if remaining and policy.compare_with_base and base_commit:
        base = run_on_base(run, remaining, cmd, cwd, base_commit, runner)
    return Rechecked(decide(run, results, retries, base), retries, base)


def run_on_base(run: Run, tests: list[str], cmd: str, cwd: Path, base_commit: str,
                runner: Runner = shell) -> bundle.Bundle:
    try:
        git(cwd, "cat-file", "-e", f"{base_commit}^{{commit}}")
    except RetryError:
        git(cwd, "fetch", "--quiet", "--depth", "1", "origin", base_commit)
    child = child_run(run, "base", commit=git(cwd, "rev-parse", f"{base_commit}^{{commit}}"))
    with tempfile.TemporaryDirectory(prefix="qq-base-") as tmp:
        tree = Path(tmp) / "base"
        git(cwd, "worktree", "add", "--quiet", "--detach", str(tree), child.commit)
        try:
            rs = run_tests(cmd, tree, tests, child, runner)
        finally:
            git(cwd, "worktree", "remove", "--force", str(tree))
    child = Run.from_dict({**child.to_dict(), "results_found": bool(rs)})
    return bundle.Bundle(child, rs, verdict.compute(child, rs))
