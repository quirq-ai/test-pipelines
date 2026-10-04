"""Retry, then compare with base (plan §3 P5, flakes.toml [verdict]); V0-TST-03.

Failed tests are rerun with the change (`retry_failed` times). Tests that still fail are run
without the change, at the base commit, `retry_failed + 1` times. Only an assertion failure
that looks the same without the change, on every one of those runs, does not fail it:

    passed on a retry                         FLAKY        does not fail the change
    FAIL on retries and on every base run,    EXONERATED   does not fail the change
      every message of the same kind
    failed on retries, passed on any base run UNEXPECTED   fails the change (flaky on base too)
    CRASH on any base run, whatever the       UNEXPECTED   no signal: the base side crashed, and
      change did                                           its worktree may lack files the
                                                           change's checkout has
    CRASH with the change, FAIL on base       UNEXPECTED   no signal: a different failure
    FAIL everywhere, failures of different    UNEXPECTED   no signal: fails differently
      kinds
    FAIL everywhere, a failure with no kind   UNEXPECTED   no signal: no kind to compare
      (see below)
    no base result (a new test, no data)      UNEXPECTED   a missing signal never exonerates

A single base run could exonerate a real regression: a test that is flaky on base happens to
fail there, or crashes there because the base worktree lacks gitignored or generated files or
submodules. So the base side runs as often as the change side did, and only a FAIL, every time,
of the same kind as with the change, counts. A CRASH (a JUnit <error>: setup, a fixture, an
import, a timeout) is never evidence on the base side, even when the change crashes too: a
fixture that reads a generated file crashes on base because the worktree lacks the file, and
would hide a change that makes the same fixture crash for a real reason. The same read inside
the test body is a FAIL (pytest reports any exception there as a <failure>), so a FAIL must
also look like the change's: every failing result on both sides must have the same kind. The
kind is the `type` attribute of the <failure> (an exception class, where the runner writes one) when
every one of those results has one. Otherwise it is the first word of the message's first line
(an exception class such as `FileNotFoundError:`, or `assert`), after removing ANSI escape codes
and a leading pytest `E` marker. Some words carry no kind, and a failure with one is never
evidence: an empty message, `def` (a traceback with no message), `[captured` (a message that
was only captured output), a word with no letters, and generic words such as `Failed` or
`Error` (runners that write the same message for every failure). This is a heuristic: it tells
a missing file from a regression that raises something else, not two different failures of
one kind (two plain `assert`s). The first run at each base keeps its id
(`<run>/base`, `<run>/base2`); the extra runs are `<run>/base-run2`, `<run>/base2-run2` and so on.

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

The base is the tested commit without this change, which the backend decides (for a GitHub pull
request or merge-queue entry, the tested merge commit's first parent; for a push, `before`).
When the change also names its target branch's commit (base_sha) and that differs, the test must
fail there too: a failure is exonerated only if it fails at every base. That keeps both traps
closed: an entry queued ahead that fixes the test (base_sha fails, the first parent passes), and a
rebase queue testing a PR's last commit (the first parent is the PR's own earlier commit, which
fails; base_sha passes).
One case is not closed by that: in a rebase queue where an entry ahead fixes the test and the
PR's own earlier commit breaks it again, both bases fail. Neither base is "the entries ahead
without this PR"; that tree is the queue commit minus all of the PR's commits. So a run whose
base is its tested commit's first parent is compared with base only when that commit shows the
queue cannot be rebasing a multi-commit PR:

    two or more parents (a merge-commit queue,    compared; the first parent is the entries ahead
      or a pull request's merge ref)
    one parent, which is base_sha (squash or      compared; the parent is the entries ahead (none)
      rebase of one commit, nothing queued ahead)
    one parent that is not base_sha, or no        not compared: still-failing tests stay
      base_sha (rebase queue; also a squash         UNEXPECTED ("rebase-method queue: base not
      queue with entries ahead, which looks         derivable")
      the same)

The check applies to any run whose base is `<commit>^1` (what the GitHub backend records for a
pull request or a queue entry), whatever its kind. An explicit base (the caller's `base`) is
one more base the failure must also fail at; it never replaces the run's own bases or skips
this check.
TODO(expert): derive that base (the PR's commit count, from the queue branch's pr-<n> ref) once
the org's merge queue and its merge method are decided (ORG-03). The count alone is not enough:
a squash queue adds one commit whatever the PR holds, and the payload does not name the method.
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from qqresults import bundle, junit, verdict
from qqresults.errors import Error
from qqresults.model import CaseVerdict, Result, Run, Status, Verdict, VerdictStatus
from qqresults.policy import Policy

Runner = Callable[[str, Path, dict[str, str]], int | None]   # None counts as 0

REBASE_QUEUE = "rebase-method queue: base not derivable (TODO(expert), ORG-03)"


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
              side: str = "change", keep_raw: bool = False) -> list[Result]:
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
            source = path.relative_to(out).as_posix()
            results += [r for r in junit.parse_file(path, run.id, source=source, keep_raw=keep_raw)
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


# ANSI escape codes, with the ESC byte or without it (XML 1.0 cannot carry ESC, so a runner may
# drop it and leave `[31m`).
_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|[@-Z\\-_])|\[[0-9;]+m")
_E_MARKER = re.compile(r"E(?:\s+|$)")   # the `E   ` prefix some runners put on error lines
# Words that say nothing about a failure's kind (see the module docstring): compared without a
# trailing colon and ignoring case.
_GENERIC = frozenset({"failed", "fail", "failure", "error"})
_NOT_A_KIND = frozenset({"def", "[captured"})


def _kind(r: Result, by_type: bool) -> str:
    """The kind of a failure: its `type`, or the first word of its message's first line, e.g.
    `FileNotFoundError:` or `assert` (see the module docstring)."""
    if by_type:
        return r.failure_type.strip()
    line = _ANSI.sub("", r.message).strip().split("\n", 1)[0]
    words = _E_MARKER.sub("", line.strip(), count=1).split()
    return words[0] if words else ""


def _informative(kind: str) -> bool:
    return (kind not in _NOT_A_KIND and any(c.isalpha() for c in kind)
            and kind.rstrip(":").casefold() not in _GENERIC)


def _kinds(kinds: set[str]) -> str:
    return "/".join(sorted(k or "(no message)" for k in kinds))


def decide(run: Run, results: list[Result], retries: list[bundle.Bundle],
           bases: list[bundle.Bundle], base_error: str = "", not_retried: str = "",
           not_compared: str = "") -> Verdict:
    first = verdict.compute(run, results)
    if not results or not run.results_found:
        return first
    tests = []
    for case in first.tests:
        t = case.test_id
        passed_on = next((i for i, b in enumerate(retries, 1) if _passed(t, b.results)), None)
        per_base = [[r for r in b.results if r.test_id == t] for b in bases]
        # How it fails with the change (first run and retries) and without it (every base run).
        on_change = {r.status for b in [results] + [b.results for b in retries]
                     for r in b if r.test_id == t and not r.expected}
        on_base = {r.status for rs in per_base for r in rs}
        # Only an assertion failure is evidence; any other failing status on base (a CRASH: a
        # missing generated file, a fixture error) never exonerates, whatever the change did.
        # It is read only after the pass check below, so it never holds PASS or SKIP.
        crashed = sorted(on_base - {Status.FAIL.value})
        # The kind of each failure, with and without the change (see the module docstring): its
        # type when every one has a type, else the first word of its message.
        failing = [r for b in [results] + [b.results for b in retries]
                   for r in b if r.test_id == t and not r.expected]
        failing_on_base = [r for rs in per_base for r in rs]
        by_type = all(r.failure_type.strip() for r in failing + failing_on_base)
        kind_on_change = {_kind(r, by_type) for r in failing}
        kind_on_base = {_kind(r, by_type) for r in failing_on_base}
        no_kind = not all(map(_informative, kind_on_change | kind_on_base))
        if not_retried:
            tests.append(CaseVerdict(t, VerdictStatus.UNEXPECTED.value, f"not retried: {not_retried}"))
        elif passed_on:
            tests.append(CaseVerdict(t, VerdictStatus.FLAKY.value, f"passed on retry {passed_on}"))
        elif any(rs and any(r.expected for r in rs) for rs in per_base):
            tests.append(CaseVerdict(t, VerdictStatus.UNEXPECTED.value,
                                     "fails with the change and passes without it"))
        elif bases and not base_error and crashed:
            tests.append(CaseVerdict(t, VerdictStatus.UNEXPECTED.value, (
                f"no signal: the base side crashed ({'/'.join(crashed)} without the change), "
                "and a crash there may come from the base worktree, not the code")))
        elif bases and not base_error and all(per_base) and on_change != {Status.FAIL.value}:
            tests.append(CaseVerdict(t, VerdictStatus.UNEXPECTED.value, (
                f"no signal: {'/'.join(sorted(on_base))} without the change but "
                f"{'/'.join(sorted(on_change))} with it, so the base failure may not be this one")))
        elif bases and not base_error and all(per_base) and no_kind:
            tests.append(CaseVerdict(t, VerdictStatus.UNEXPECTED.value, (
                f"no signal: no kind to compare, the failure says nothing about what failed "
                f"({_kinds(kind_on_base)} vs {_kinds(kind_on_change)})")))
        elif bases and not base_error and all(per_base) and len(kind_on_change | kind_on_base) != 1:
            tests.append(CaseVerdict(t, VerdictStatus.UNEXPECTED.value, (
                f"no signal: fails differently without the change "
                f"({_kinds(kind_on_base)} vs {_kinds(kind_on_change)})")))
        elif bases and not base_error and all(per_base):
            at = ", ".join(dict.fromkeys(b.run.commit[:12] for b in bases))
            tests.append(CaseVerdict(t, VerdictStatus.EXONERATED.value, (
                f"also fails without the change, at {at}: {Status.FAIL.value} "
                f"on all {len(bases)} base run(s)")))
        elif bases and not base_error:
            tests.append(CaseVerdict(t, VerdictStatus.UNEXPECTED.value,
                                     "no result without the change (a new test, or no signal)"))
        else:
            why = (f"could not run without the change: {base_error}" if base_error else
                   f"not compared with base: {not_compared}" if not_compared else
                   "not compared with base")
            tests.append(CaseVerdict(t, VerdictStatus.UNEXPECTED.value, f"still fails on retry; {why}"))
    counts = dict(first.counts)
    counts.pop(VerdictStatus.UNEXPECTED.value, None)
    for c in tests:
        counts[c.status] = counts.get(c.status, 0) + 1
    unexpected = counts.get(VerdictStatus.UNEXPECTED.value, 0)
    reason = f"{unexpected} unexpected test(s)" if unexpected else ""
    if base_error:
        reason = f"{reason}; base comparison failed: {base_error}".lstrip("; ")
    if not_retried:
        reason = f"{reason}; not retried: {not_retried}".lstrip("; ")
    if not_compared:
        reason = f"{reason}; not compared with base: {not_compared}".lstrip("; ")
    return Verdict(run_id=run.id, passed=unexpected == 0, counts=dict(sorted(counts.items())),
                   tests=tests, reason=reason,
                   inputs=[b.run.id for b in retries + bases])


@dataclass
class Rechecked:
    verdict: Verdict
    retries: list[bundle.Bundle]
    bases: list[bundle.Bundle]


def recheck(run: Run, results: list[Result], cmd: str, cwd: Path, policy: Policy,
            base_commit: str = "", runner: Runner = shell, setup: str = "",
            keep_raw: bool = False) -> Rechecked:
    """Retry the failed tests, then compare the still-failing ones with base."""
    retries: list[bundle.Bundle] = []
    failing = [c.test_id for c in verdict.compute(run, results).tests
               if c.status == VerdictStatus.UNEXPECTED.value]
    if not failing:
        return Rechecked(decide(run, results, [], []), [], [])
    if len(failing) > policy.max_failures_to_retry:   # a broken change: fail fast
        return Rechecked(decide(run, results, [], [], not_retried=(
            f"{len(failing)} failures, over max_failures_to_retry ({policy.max_failures_to_retry})")),
            [], [])
    remaining = list(failing)
    for n in range(1, policy.retry_failed + 1):
        child = child_run(run, "retry", n)
        rs = run_tests(cmd, cwd, remaining, child, runner, side="change", keep_raw=keep_raw)
        child = Run.from_dict({**child.to_dict(), "results_found": bool(rs)})
        retries.append(bundle.Bundle(child, rs, verdict.compute(child, rs)))
        remaining = [t for t in remaining if not _passed(t, rs)]
        if not remaining:
            break
    bases: list[bundle.Bundle] = []
    base_error = not_compared = ""
    # Decided by the shape of the base, not the run's kind, which a caller can override.
    if remaining and policy.compare_with_base and run.base_commit == f"{run.commit}^1":
        try:
            if not queue_base_derivable(run, cwd):
                not_compared = REBASE_QUEUE
        except RetryError as e:              # cannot tell: never exonerate without knowing
            base_error = f"cannot read the tested commit's parents: {e}"
    if remaining and policy.compare_with_base and not (base_error or not_compared):
        # An explicit base is one more base, never a replacement for the run's own.
        commits = list(dict.fromkeys(candidate_bases(run) + ([base_commit] if base_commit else [])))
        if not commits:
            base_error = "no base commit known"
        for n, commit in enumerate(commits, 1):
            try:
                if _resolve(cwd, commit) in {b.run.commit for b in bases}:
                    continue                  # e.g. the first parent is base_sha itself
                bases += run_on_base(run, remaining, cmd, cwd, commit, runner, setup, n,
                                     runs=policy.retry_failed + 1, keep_raw=keep_raw)
            except RetryError as e:   # keep the run and its retries; never exonerate without data
                base_error = str(e)
                break
    return Rechecked(decide(run, results, retries, bases, base_error, not_compared=not_compared),
                     retries, bases)


def queue_base_derivable(run: Run, cwd: Path) -> bool:
    """Whether the tested commit's first parent is the tree without the change: the tested
    commit is a merge (a pull request's merge ref, or a merge-commit queue), or its one parent
    is base_sha (nothing queued ahead, one commit added). Anything else may be a rebase queue
    (see the module docstring)."""
    # Assumes any commit with two or more parents is a merge-commit queue entry (or a PR merge
    # ref) whose first parent is the entries ahead. A rebase queue never yields one: GitHub's
    # rebase method flattens a PR's merge commits, so its entries are all single-parent.
    # TODO(expert): read the queue's merge_method from GET /repos/{o}/{r}/rules/branches/
    # {base_ref} (the merge_queue rule). For SQUASH or MERGE, ^1 is the entries ahead, so a
    # squash entry with entries ahead could be compared again (ORG-03).
    try:
        git(cwd, "cat-file", "-e", f"{run.commit}^1^{{commit}}")
    except RetryError:   # a shallow checkout: fetch the commit with its parents
        git(cwd, "fetch", "--quiet", "--depth", "2", "origin", run.commit)
    parents = git(cwd, "rev-list", "--parents", "-n", "1", run.commit).split()[1:]
    if not parents:
        raise RetryError(f"{run.commit[:12]} has no parents")
    target = run.change.base_sha if run.change else ""
    return len(parents) > 1 or (bool(target) and parents[0] == (_resolve(cwd, target) or target))


def candidate_bases(run: Run) -> list[str]:
    """Every base a failure must also fail at to be exonerated (see the module docstring)."""
    target = run.change.base_sha if run.change else ""
    return [c for c in dict.fromkeys([run.base_commit, target]) if c]


def _resolve(cwd: Path, commit: str) -> str:
    try:
        return git(cwd, "rev-parse", f"{commit}^{{commit}}")
    except RetryError:
        return ""                             # not fetched yet: run_on_base fetches it


def run_on_base(run: Run, tests: list[str], cmd: str, cwd: Path, base_commit: str,
                runner: Runner = shell, setup: str = "", n: int = 1,
                runs: int = 1, keep_raw: bool = False) -> list[bundle.Bundle]:
    """Run the tests `runs` times at base_commit, in one worktree set up once."""
    try:
        git(cwd, "cat-file", "-e", f"{base_commit}^{{commit}}")
    except RetryError:
        if base_commit.endswith("^1"):   # a shallow checkout: fetch the commit with its parent
            git(cwd, "fetch", "--quiet", "--depth", "2", "origin", base_commit[:-2])
        else:
            git(cwd, "fetch", "--quiet", "--depth", "1", "origin", base_commit)
    child = child_run(run, "base", n if n > 1 else 0,
                      commit=git(cwd, "rev-parse", f"{base_commit}^{{commit}}"))
    with tempfile.TemporaryDirectory(prefix="qq-base-") as tmp:
        tree = Path(tmp) / "base"
        git(cwd, "worktree", "add", "--quiet", "--detach", str(tree), child.commit)
        try:
            if setup:
                run_setup(setup, tree, "base", runner)
            out = []
            for k in range(1, runs + 1):
                c = child if k == 1 else Run.from_dict({**child.to_dict(), "id": f"{child.id}-run{k}"})
                rs = run_tests(cmd, tree, tests, c, runner, side="base", keep_raw=keep_raw)
                c = Run.from_dict({**c.to_dict(), "results_found": bool(rs)})
                out.append(bundle.Bundle(c, rs, verdict.compute(c, rs)))
        finally:
            subprocess.run(["git", "-C", str(cwd), "worktree", "remove", "--force", str(tree)],
                           capture_output=True)
            subprocess.run(["git", "-C", str(cwd), "worktree", "prune"], capture_output=True)
            if setup:   # put the change's environment back for the steps after this one
                run_setup(setup, cwd, "change", runner)
    return out
