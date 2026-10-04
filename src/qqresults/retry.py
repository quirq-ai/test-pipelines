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

    QQ_SIDE          "change" or "base"

Its exit code is ignored: failing tests are what is being measured. The base side runs in a git
worktree of the base commit, inside the same job and environment, so the command must test the
code in its working directory ($PWD). Anything installed into the environment (an editable
install, a build output outside the tree) would otherwise make the base side test the change's
code and wrongly exonerate it. For that, give a setup command: it runs in the base worktree
before the base tests, and again in the change's checkout afterwards to restore it. Unlike the
test command, its exit code counts: if it fails on the base side, nothing is exonerated (the base
tests could be testing the change's code), and if the restore fails, the step fails, because the
steps after it would test the wrong code.
TODO(expert): run the base side hermetically once remote-build provides executors (V0-RBE-01).

The base is the tested commit without this change. For a pull request (GitHub tests a merge of
the change onto its branch) and a merge-queue entry (a merge of the change onto the entries ahead
of it) that is the tested commit's first parent, not the PR's or queue's base_sha: an entry
ahead may fix a test this change breaks again, and comparing with the target branch would then
exonerate the regression. For a push it is the commit before the push (`before`).
TODO(expert): a rebase merge queue tests the PR's commits rebased, where the first parent is the
PR's own previous commit; use the commit below the rebased range once a rebase queue is used.
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
from qqresults.model import CaseVerdict, Result, Run, RunKind, Verdict, VerdictStatus
from qqresults.policy import Policy

Runner = Callable[[str, Path, dict[str, str]], int | None]   # None counts as 0


class RetryError(Error):
    pass


class RestoreError(Error):
    """The setup command failed to put the change back; not caught, so the step fails."""


def shell(cmd: str, cwd: Path, env: dict[str, str]) -> int:
    return subprocess.run(cmd, shell=True, cwd=cwd, env={**os.environ, **env}, check=False).returncode


def run_setup(setup: str, cwd: Path, side: str, runner: Runner = shell) -> None:
    code = runner(setup, cwd, {"QQ_SIDE": side}) or 0
    if code != 0:
        raise (RetryError if side == "base" else RestoreError)(
            f"setup command failed on the {side} side (exit {code})")


def git(cwd: Path, *args: str) -> str:
    p = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    if p.returncode != 0:
        raise RetryError(f"git {' '.join(args)}: {p.stderr.strip() or p.returncode}")
    return p.stdout.strip()


def run_tests(cmd: str, cwd: Path, tests: list[str], run: Run, runner: Runner = shell,
              side: str = "change") -> list[Result]:
    """Run cmd once in cwd and return its results for `tests`."""
    with tempfile.TemporaryDirectory(prefix="qq-retry-") as tmp:
        out = Path(tmp) / "junit"
        out.mkdir()
        listing = Path(tmp) / "tests.txt"
        listing.write_text("".join(t + "\n" for t in tests), encoding="utf-8")
        runner(cmd, cwd, {"QQ_JUNIT_DIR": str(out), "QQ_RETRY_TESTS": str(listing), "QQ_SIDE": side})
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


def _passed(test_id: str, results: list[Result]) -> bool:
    """The test has results here and all of them are expected (a skip alone is not a pass)."""
    rs = [r for r in results if r.test_id == test_id]
    return bool(rs) and verdict.test_status(rs) is VerdictStatus.EXPECTED and any(
        r.status == "PASS" for r in rs)


def decide(run: Run, results: list[Result], retries: list[bundle.Bundle],
           base: bundle.Bundle | None, base_error: str = "") -> Verdict:
    first = verdict.compute(run, results)
    if not results or not run.results_found:
        return first
    tests = []
    for case in first.tests:
        t = case.test_id
        passed_on = next((i for i, b in enumerate(retries, 1) if _passed(t, b.results)), None)
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
            why = f"could not run without the change: {base_error}" if base_error else "not compared with base"
            tests.append(CaseVerdict(t, VerdictStatus.UNEXPECTED.value, f"still fails on retry; {why}"))
    counts = dict(first.counts)
    counts.pop(VerdictStatus.UNEXPECTED.value, None)
    for c in tests:
        counts[c.status] = counts.get(c.status, 0) + 1
    unexpected = counts.get(VerdictStatus.UNEXPECTED.value, 0)
    reason = f"{unexpected} unexpected test(s)" if unexpected else ""
    if base_error:
        reason = f"{reason}; base comparison failed: {base_error}".lstrip("; ")
    return Verdict(run_id=run.id, passed=unexpected == 0, counts=dict(sorted(counts.items())),
                   tests=tests, reason=reason,
                   inputs=[b.run.id for b in retries] + ([base.run.id] if base else []))


@dataclass
class Rechecked:
    verdict: Verdict
    retries: list[bundle.Bundle]
    base: bundle.Bundle | None


def recheck(run: Run, results: list[Result], cmd: str, cwd: Path, policy: Policy,
            base_commit: str = "", runner: Runner = shell, setup: str = "") -> Rechecked:
    """Retry the failed tests, then compare the still-failing ones with base."""
    retries: list[bundle.Bundle] = []
    failing = [c.test_id for c in verdict.compute(run, results).tests
               if c.status == VerdictStatus.UNEXPECTED.value]
    if not failing:
        return Rechecked(decide(run, results, [], None), [], None)
    remaining = list(failing)
    for n in range(1, policy.retry_failed + 1):
        child = child_run(run, "retry", n)
        rs = run_tests(cmd, cwd, remaining, child, runner, side="change")
        child = Run.from_dict({**child.to_dict(), "results_found": bool(rs)})
        retries.append(bundle.Bundle(child, rs, verdict.compute(child, rs)))
        remaining = [t for t in remaining if not _passed(t, rs)]
        if not remaining:
            break
    base = None
    base_error = ""
    base_commit = base_commit or default_base(run)
    if remaining and policy.compare_with_base:
        if not base_commit:
            base_error = "no base commit known"
        else:
            try:
                base = run_on_base(run, remaining, cmd, cwd, base_commit, runner, setup)
            except RetryError as e:   # keep the run and its retries; never exonerate without data
                base_error = str(e)
    return Rechecked(decide(run, results, retries, base, base_error), retries, base)


def default_base(run: Run) -> str:
    """The tested commit without this change (see the module docstring)."""
    if run.kind in (RunKind.GATE.value, RunKind.PRESUBMIT.value) and run.change:
        return f"{run.commit}^1"
    return run.base_commit


def run_on_base(run: Run, tests: list[str], cmd: str, cwd: Path, base_commit: str,
                runner: Runner = shell, setup: str = "") -> bundle.Bundle:
    try:
        git(cwd, "cat-file", "-e", f"{base_commit}^{{commit}}")
    except RetryError:
        if base_commit.endswith("^1"):   # a shallow checkout: fetch the commit with its parent
            git(cwd, "fetch", "--quiet", "--depth", "2", "origin", base_commit[:-2])
        else:
            git(cwd, "fetch", "--quiet", "--depth", "1", "origin", base_commit)
    child = child_run(run, "base", commit=git(cwd, "rev-parse", f"{base_commit}^{{commit}}"))
    with tempfile.TemporaryDirectory(prefix="qq-base-") as tmp:
        tree = Path(tmp) / "base"
        git(cwd, "worktree", "add", "--quiet", "--detach", str(tree), child.commit)
        try:
            if setup:
                run_setup(setup, tree, "base", runner)
            rs = run_tests(cmd, tree, tests, child, runner, side="base")
        finally:
            subprocess.run(["git", "-C", str(cwd), "worktree", "remove", "--force", str(tree)],
                           capture_output=True)
            subprocess.run(["git", "-C", str(cwd), "worktree", "prune"], capture_output=True)
            if setup:   # put the change's environment back for the steps after this one
                run_setup(setup, cwd, "change", runner)
    child = Run.from_dict({**child.to_dict(), "results_found": bool(rs)})
    return bundle.Bundle(child, rs, verdict.compute(child, rs))
