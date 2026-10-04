import subprocess
import sys
from pathlib import Path

import pytest

from qqresults import bundle, cli, retry, sink
from qqresults.backends import local
from qqresults.model import Result, Run
from qqresults.policy import Policy

RUNNER = Path(__file__).parent / "fixtures" / "fake_runner.py"
CMD = f'"{sys.executable}" "{RUNNER}"'


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def repo_with(tmp_path, base_cases, change_cases):
    """A git repo whose base commit and change commit hold different cases.txt."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "t")
    (repo / "cases.txt").write_text(base_cases)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    base = git(repo, "rev-parse", "HEAD")
    (repo / "cases.txt").write_text(change_cases)
    git(repo, "commit", "-q", "--allow-empty", "-am", "change")
    return repo, base


def first_run(repo, tmp_path, env=None):
    subprocess.run(f"{CMD} results/junit.xml", shell=True, cwd=repo, env=env)
    return local.run_from_args("o/x", git(repo, "rev-parse", "HEAD"), kind="presubmit", run_id="r1")


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_STATE", str(tmp_path / "state"))


def sink_it(repo, tmp_path, base, policy=Policy()):
    run = first_run(repo, tmp_path)
    return sink.sink(run, ["results/*.xml"], repo, tmp_path / "out", rerun_cmd=CMD,
                     policy=policy, base_commit=base)


def statuses(b):
    return {c.test_id: c.status for c in b.verdict.tests}


def test_planted_failure_that_also_fails_on_base_does_not_block(tmp_path, state):
    # V0-TST-03 done-when: the planted test fails at base and with the change.
    repo, base = repo_with(tmp_path, "t::ok pass\nt::planted fail\n",
                           "t::ok pass\nt::planted fail\nt::new pass\n")
    path, b = sink_it(repo, tmp_path, base)
    assert b.verdict.passed
    assert statuses(b) == {"t::planted": "EXONERATED"}
    assert b.verdict.tests[0].reason.startswith("also fails without the change")
    assert b.verdict.inputs == ["local/o/x/r1/retry1", "local/o/x/r1/base", "local/o/x/r1/base-run2"]
    # the retry and base runs are stored next to the change's run, each write-once
    names = sorted(p.name for p in (tmp_path / "out").iterdir())
    assert names == ["qq-results-local_o_x_r1", "qq-results-local_o_x_r1_base",
                     "qq-results-local_o_x_r1_base-run2", "qq-results-local_o_x_r1_retry1"]
    base_bundle = bundle.read(tmp_path / "out" / "qq-results-local_o_x_r1_base")
    assert base_bundle.run.commit == base and base_bundle.run.role == "base"
    assert base_bundle.run.parent == "local/o/x/r1"
    assert git(repo, "worktree", "list").count("\n") == 0   # the base worktree is gone


def test_a_failure_the_change_introduced_blocks(tmp_path, state):
    repo, base = repo_with(tmp_path, "t::ok pass\nt::broken pass\n", "t::ok pass\nt::broken fail\n")
    _, b = sink_it(repo, tmp_path, base)
    assert not b.verdict.passed
    assert statuses(b) == {"t::broken": "UNEXPECTED"}
    assert "passes without it" in b.verdict.tests[0].reason


def test_a_new_failing_test_blocks(tmp_path, state):
    repo, base = repo_with(tmp_path, "t::ok pass\n", "t::ok pass\nt::added fail\n")
    _, b = sink_it(repo, tmp_path, base)
    assert statuses(b) == {"t::added": "UNEXPECTED"} and not b.verdict.passed
    assert "no result without the change" in b.verdict.tests[0].reason


def test_flaky_passes_on_retry_without_running_base(tmp_path, state):
    repo, base = repo_with(tmp_path, "t::ok pass\n", "t::ok pass\nt::wobbly flaky\n")
    _, b = sink_it(repo, tmp_path, base)
    assert b.verdict.passed and statuses(b) == {"t::wobbly": "FLAKY"}
    assert b.verdict.inputs == ["local/o/x/r1/retry1"]


def test_no_base_comparison_when_policy_says_so(tmp_path, state):
    repo, base = repo_with(tmp_path, "t::p fail\n", "t::p fail\n")
    _, b = sink_it(repo, tmp_path, base, Policy(retry_failed=2, compare_with_base=False))
    assert not b.verdict.passed and "not compared with base" in b.verdict.tests[0].reason
    assert b.verdict.inputs == ["local/o/x/r1/retry1", "local/o/x/r1/retry2"]


def test_a_rerun_that_writes_nothing_never_exonerates(tmp_path):
    run = Run(id="r", repo="o/x", kind="presubmit", commit="c")
    results = [Result(run_id="r", test_id="t::a", status="FAIL", expected=False)]
    checked = retry.recheck(run, results, "true", tmp_path, Policy(compare_with_base=False))
    assert not checked.verdict.passed and checked.retries[0].run.results_found is False


def test_cli_fail_on_verdict(tmp_path, state, capsys):
    repo, base = repo_with(tmp_path, "t::ok pass\n", "t::ok pass\nt::broken fail\n")
    first_run(repo, tmp_path)
    args = ["sink", "--backend", "local", "--repo", "o/x", "--commit", "c", "--junit", "results/*.xml",
            "--root", str(repo), "--rerun", CMD, "--base", base, "--fail-on-verdict"]
    assert cli.main(args + ["--out", str(tmp_path / "o1")]) == 1
    assert "UNEXPECTED t::broken" in capsys.readouterr().out.replace("  ", " ").replace("  ", " ")
    assert cli.main(args[:-1] + ["--out", str(tmp_path / "o2")]) == 0


def test_policy_from_infra_config(tmp_path):
    from qqresults import policy
    root = tmp_path / "infra-config"
    (root / "tools").mkdir(parents=True)
    (root / "config").mkdir()
    (root / "tools" / "qqcfg.py").write_text(
        "import tomllib\nclass ConfigError(Exception): pass\n"
        "def load(root):\n"
        "    return {p.stem: tomllib.loads(p.read_text()) for p in (root / 'config').glob('*.toml')}\n")
    (root / "config" / "flakes.toml").write_text(
        "[verdict]\nretry_failed = 3\ncompare_with_base = false\nexonerate_known_flakes = true\n")
    assert policy.from_infra_config(root) == Policy(retry_failed=3, compare_with_base=False)
    (root / "config" / "flakes.toml").write_text(
        "[verdict]\nretry_failed = 1\ncompare_with_base = true\nmax_failures_to_retry = 5\n")
    assert policy.from_infra_config(root).max_failures_to_retry == 5
    with pytest.raises(policy.PolicyError, match="not an infra-config checkout"):
        policy.from_infra_config(tmp_path)


def test_a_base_that_cannot_be_checked_out_keeps_the_runs_and_blocks(tmp_path, state):
    repo, _ = repo_with(tmp_path, "t::p fail\n", "t::p fail\n")
    path, b = sink_it(repo, tmp_path, "0" * 40)          # an unreachable base
    assert not b.verdict.passed and "base comparison failed" in b.verdict.reason
    assert "could not run without the change" in b.verdict.tests[0].reason
    assert (tmp_path / "out" / "qq-results-local_o_x_r1_retry1").is_dir()   # retries still stored


def test_setup_runs_on_the_base_side_then_restores_the_change(tmp_path, state):
    repo, base = repo_with(tmp_path, "t::p fail\n", "t::p fail\n")
    log = tmp_path / "setup.log"
    run = first_run(repo, tmp_path)
    checked = retry.recheck(run, sink.junit.parse_file(repo / "results/junit.xml", run.id),
                            CMD, repo, Policy(), base,
                            setup=f'echo "$QQ_SIDE $(basename "$PWD")" >> "{log}"')
    assert log.read_text().splitlines() == ["base base", "change repo"]
    assert checked.verdict.passed


def test_a_skip_on_retry_is_not_a_pass(tmp_path):
    run = Run(id="r", repo="o/x", kind="presubmit", commit="c")
    results = [Result(run_id="r", test_id="t::a", status="FAIL", expected=False)]
    skip_xml = '<testsuite name="s"><testcase classname="t" name="a"><skipped/></testcase></testsuite>'
    cmd = f"printf '%s' '{skip_xml}' > \"$QQ_JUNIT_DIR/r.xml\""
    checked = retry.recheck(run, results, cmd, tmp_path, Policy(compare_with_base=False))
    assert not checked.verdict.passed and checked.verdict.tests[0].status == "UNEXPECTED"


def test_a_failed_base_setup_never_exonerates(tmp_path, state):
    repo, base = repo_with(tmp_path, "t::p fail\n", "t::p fail\n")   # would be exonerated
    run = first_run(repo, tmp_path)
    checked = retry.recheck(run, sink.junit.parse_file(repo / "results/junit.xml", run.id),
                            CMD, repo, Policy(), base,
                            setup='[ "$QQ_SIDE" = change ]')          # fails on the base side
    assert not checked.verdict.passed and not checked.bases
    assert "setup command failed on the base side" in checked.verdict.reason


def test_a_failed_restore_fails_the_step(tmp_path, state):
    repo, base = repo_with(tmp_path, "t::p fail\n", "t::p fail\n")
    run = first_run(repo, tmp_path)
    with pytest.raises(retry.RestoreError, match="change side"):
        retry.recheck(run, sink.junit.parse_file(repo / "results/junit.xml", run.id),
                      CMD, repo, Policy(), base, setup='[ "$QQ_SIDE" = base ]')
    assert len(git(repo, "worktree", "list").splitlines()) == 1   # the base worktree is gone


@pytest.mark.parametrize("n", [-1, 4])
def test_retries_are_bounded(n):
    from qqresults.policy import PolicyError
    with pytest.raises(PolicyError, match="retry_failed"):
        Policy(retry_failed=n)


def test_too_many_failures_are_not_retried(tmp_path):
    run = Run(id="r", repo="o/x", kind="presubmit", commit="c")
    results = [Result(run_id="r", test_id=f"t::{i}", status="FAIL", expected=False) for i in range(3)]
    checked = retry.recheck(run, results, "exit 1", tmp_path, Policy(max_failures_to_retry=2))
    assert not checked.verdict.passed and not checked.retries and "max_failures_to_retry" in checked.verdict.reason
    assert "base comparison" not in checked.verdict.reason
    assert all(c.reason.startswith("not retried: 3 failures") for c in checked.verdict.tests)


def _queue_run(repo, tmp_path, target):
    from qqresults.model import Change
    run = first_run(repo, tmp_path)
    return Run.from_dict({**run.to_dict(), "kind": "gate", "base_commit": f"{run.commit}^1",
                          "change": Change(repo="o/x", number=2, base_sha=target).to_dict()})


def queue_merge(repo, ahead, cases):
    """Check out what a merge-commit queue tests: merge(ahead, a PR commit) holding `cases`."""
    (repo / "cases.txt").write_text(cases)
    git(repo, "add", "-A")
    tree = git(repo, "write-tree")
    pr = git(repo, "commit-tree", tree, "-p", ahead, "-m", "the PR")
    git(repo, "reset", "-q", "--hard", git(repo, "commit-tree", tree, "-p", ahead, "-p", pr,
                                           "-m", "merge queue entry"))


def test_a_fix_queued_ahead_cannot_exonerate_a_change_that_breaks_the_test_again(tmp_path, state):
    # Main is red on t::add; fix A is queued ahead; entry B breaks t::add again. The queue tests
    # merge(A, B); its base_sha is main, where t::add also fails, and its first parent is A.
    repo, main = repo_with(tmp_path, "t::add fail\n", "t::add pass\n")    # HEAD is fix A
    queue_merge(repo, git(repo, "rev-parse", "HEAD"), "t::add fail\n")
    run = _queue_run(repo, tmp_path, main)
    assert retry.candidate_bases(run) == [f"{run.commit}^1", main]
    path, b = sink.sink(run, ["results/*.xml"], repo, tmp_path / "out", rerun_cmd=CMD)
    assert statuses(b) == {"t::add": "UNEXPECTED"} and not b.verdict.passed
    assert "passes without it" in b.verdict.tests[0].reason


def test_a_rebase_queue_cannot_exonerate_a_pr_whose_earlier_commit_broke_the_test(tmp_path, state):
    # Main is green; the PR's c1 breaks t::add and c2 is unrelated. A rebase queue tests c2,
    # whose first parent c1 also fails; main (base_sha) passes, so the failure is the PR's.
    repo, main = repo_with(tmp_path, "t::add pass\n", "t::add fail\n")    # HEAD is c1
    (repo / "other.txt").write_text("x")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "c2")
    path, b = sink.sink(_queue_run(repo, tmp_path, main), ["results/*.xml"], repo,
                        tmp_path / "out", rerun_cmd=CMD)
    assert statuses(b) == {"t::add": "UNEXPECTED"}
    assert retry.REBASE_QUEUE in b.verdict.tests[0].reason   # AUDIT-R4: refused before any base


def test_a_failure_at_every_base_is_still_exonerated(tmp_path, state):
    repo, main = repo_with(tmp_path, "t::add fail\n", "t::add fail\n")
    path, b = sink.sink(_queue_run(repo, tmp_path, main), ["results/*.xml"], repo,
                        tmp_path / "out", rerun_cmd=CMD)
    assert statuses(b) == {"t::add": "EXONERATED"}
    assert [i.rsplit("/", 1)[1] for i in b.verdict.inputs] == ["retry1", "base", "base-run2"]  # one base


def test_two_distinct_bases_both_run(tmp_path, state):
    repo, main = repo_with(tmp_path, "t::add fail\n", "t::add fail\n")    # HEAD is A, ahead
    queue_merge(repo, git(repo, "rev-parse", "HEAD"), "t::add fail\n")
    path, b = sink.sink(_queue_run(repo, tmp_path, main), ["results/*.xml"], repo,
                        tmp_path / "out", rerun_cmd=CMD)
    assert statuses(b) == {"t::add": "EXONERATED"}
    assert [i.rsplit("/", 1)[1] for i in b.verdict.inputs] == [
        "retry1", "base", "base-run2", "base2", "base2-run2"]
    assert (tmp_path / "out" / "qq-results-local_o_x_r1_base2").is_dir()


def test_a_second_base_that_cannot_be_checked_out_never_exonerates(tmp_path, state):
    repo, _ = repo_with(tmp_path, "t::add fail\n", "t::add fail\n")
    queue_merge(repo, git(repo, "rev-parse", "HEAD"), "t::add fail\n")
    path, b = sink.sink(_queue_run(repo, tmp_path, "0" * 40), ["results/*.xml"], repo,
                        tmp_path / "out", rerun_cmd=CMD)
    assert statuses(b) == {"t::add": "UNEXPECTED"} and "base comparison failed" in b.verdict.reason
    assert (tmp_path / "out" / "qq-results-local_o_x_r1_base").is_dir()   # the first base is kept


# --- a rebase-method queue never exonerates (AUDIT-R4) ----------------------------------------

def test_a_rebase_queue_cannot_exonerate_when_a_fix_ahead_is_undone_by_the_prs_earlier_commit(
        tmp_path, state):
    # The auditor's case: main is red on t::add; fix A is queued ahead; the PR's c1 breaks t::add
    # again and c2 is unrelated. A rebase queue tests c2: its first parent c1 fails and so does
    # main (base_sha), so comparing with both would exonerate. The tree to compare with is A.
    repo, main = repo_with(tmp_path, "t::add fail\n", "t::add pass\n")    # HEAD is fix A
    (repo / "cases.txt").write_text("t::add fail\n")
    git(repo, "commit", "-q", "-am", "c1")
    (repo / "other.txt").write_text("x")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "c2")
    path, b = sink.sink(_queue_run(repo, tmp_path, main), ["results/*.xml"], repo,
                        tmp_path / "out", rerun_cmd=CMD)
    assert statuses(b) == {"t::add": "UNEXPECTED"} and not b.verdict.passed
    assert b.verdict.tests[0].reason == f"still fails on retry; not compared with base: {retry.REBASE_QUEUE}"
    assert b.verdict.reason.endswith(f"not compared with base: {retry.REBASE_QUEUE}")
    assert b.verdict.inputs == ["local/o/x/r1/retry1"]                  # no base was run


def test_a_squash_queue_with_entries_ahead_looks_like_a_rebase_queue(tmp_path, state):
    # One parent (the entry ahead) that is not base_sha: the same shape as a rebase queue's
    # one-commit PR, and nothing in the commit tells them apart, so it is not compared either.
    repo, main = repo_with(tmp_path, "t::add fail\n", "t::add fail\n")    # HEAD is entry A
    (repo / "other.txt").write_text("x")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "squashed PR")
    path, b = sink.sink(_queue_run(repo, tmp_path, main), ["results/*.xml"], repo,
                        tmp_path / "out", rerun_cmd=CMD)
    assert statuses(b) == {"t::add": "UNEXPECTED"} and retry.REBASE_QUEUE in b.verdict.reason


def test_a_queue_entry_without_base_sha_is_not_compared(tmp_path, state):
    repo, _ = repo_with(tmp_path, "t::add fail\n", "t::add fail\n")
    path, b = sink.sink(_queue_run(repo, tmp_path, ""), ["results/*.xml"], repo,
                        tmp_path / "out", rerun_cmd=CMD)
    assert statuses(b) == {"t::add": "UNEXPECTED"} and retry.REBASE_QUEUE in b.verdict.reason


def test_an_explicit_base_does_not_skip_the_queue_check(tmp_path, state):
    # `base: ${{ github.event.merge_group.base_sha }}` must not reopen the rebase-queue case
    repo, main = repo_with(tmp_path, "t::add fail\n", "t::add fail\n")
    (repo / "other.txt").write_text("x")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "c2")
    path, b = sink.sink(_queue_run(repo, tmp_path, main), ["results/*.xml"], repo,
                        tmp_path / "out", rerun_cmd=CMD, base_commit=main)
    assert statuses(b) == {"t::add": "UNEXPECTED"} and retry.REBASE_QUEUE in b.verdict.reason


def test_an_explicit_base_adds_to_the_runs_own_bases(tmp_path, state):
    # The B3 trap through `base`: main is red, fix A is queued ahead, entry B breaks the test
    # again. Passing main as the base must not replace the first parent (A, where it passes).
    repo, main = repo_with(tmp_path, "t::add fail\n", "t::add pass\n")    # HEAD is fix A
    queue_merge(repo, git(repo, "rev-parse", "HEAD"), "t::add fail\n")
    path, b = sink.sink(_queue_run(repo, tmp_path, main), ["results/*.xml"], repo,
                        tmp_path / "out", rerun_cmd=CMD, base_commit=main)
    assert statuses(b) == {"t::add": "UNEXPECTED"} and "passes without it" in b.verdict.tests[0].reason


def test_an_explicit_base_must_fail_too(tmp_path, state):
    repo, main = repo_with(tmp_path, "t::add fail\n", "t::add fail\n")
    (repo / "cases.txt").write_text("t::add pass\n")      # a commit where the test passes
    git(repo, "add", "-A")
    green = git(repo, "commit-tree", git(repo, "write-tree"), "-p", main, "-m", "green")
    git(repo, "reset", "-q", "--hard")
    queue_merge(repo, git(repo, "rev-parse", "HEAD"), "t::add fail\n")
    path, b = sink.sink(_queue_run(repo, tmp_path, main), ["results/*.xml"], repo,
                        tmp_path / "out", rerun_cmd=CMD, base_commit=green)
    assert statuses(b) == {"t::add": "UNEXPECTED"} and "passes without it" in b.verdict.tests[0].reason
    assert [i.rsplit("/", 1)[1] for i in b.verdict.inputs][-2:] == ["base3", "base3-run2"]


def test_a_presubmit_kind_on_a_queue_run_still_gets_the_queue_check(tmp_path, state):
    # The auditor's R4 case with `kind: presubmit` overriding gate: the base is still <commit>^1
    repo, main = repo_with(tmp_path, "t::add fail\n", "t::add pass\n")    # HEAD is fix A
    (repo / "cases.txt").write_text("t::add fail\n")
    git(repo, "commit", "-q", "-am", "c1")
    (repo / "other.txt").write_text("x")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "c2")
    run = Run.from_dict({**_queue_run(repo, tmp_path, main).to_dict(), "kind": "presubmit"})
    path, b = sink.sink(run, ["results/*.xml"], repo, tmp_path / "out", rerun_cmd=CMD)
    assert statuses(b) == {"t::add": "UNEXPECTED"} and retry.REBASE_QUEUE in b.verdict.reason


def test_a_merge_commit_queue_in_a_shallow_checkout_fetches_the_parents(tmp_path, state):
    origin, main = repo_with(tmp_path, "t::add fail\n", "t::add fail\n")
    queue_merge(origin, git(origin, "rev-parse", "HEAD"), "t::add fail\n")
    repo = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", "--depth", "1", f"file://{origin}", str(repo)],
                   check=True)
    path, b = sink.sink(_queue_run(repo, tmp_path, main), ["results/*.xml"], repo,
                        tmp_path / "out", rerun_cmd=CMD)
    assert statuses(b) == {"t::add": "EXONERATED"}
    assert [i.rsplit("/", 1)[1] for i in b.verdict.inputs] == [
        "retry1", "base", "base-run2", "base2", "base2-run2"]


def test_a_tested_commit_whose_parents_cannot_be_read_never_exonerates(tmp_path, state):
    repo, main = repo_with(tmp_path, "t::add fail\n", "t::add fail\n")
    run = _queue_run(repo, tmp_path, main)
    run = Run.from_dict({**run.to_dict(), "commit": "0" * 40, "base_commit": "0" * 40 + "^1"})
    path, b = sink.sink(run, ["results/*.xml"], repo, tmp_path / "out", rerun_cmd=CMD)
    assert statuses(b) == {"t::add": "UNEXPECTED"}
    assert "cannot read the tested commit's parents" in b.verdict.reason


# --- the base side runs as often as the change side (AUDIT-S1) --------------------------------

def test_a_test_flaky_on_base_cannot_exonerate(tmp_path, state):
    # The test fails on the first base run and passes on the second; one base run would have
    # exonerated the change's real failure.
    repo, base = repo_with(tmp_path, "t::ok pass\nt::wobbly flaky\n", "t::ok pass\nt::wobbly fail\n")
    _, b = sink_it(repo, tmp_path, base)
    assert statuses(b) == {"t::wobbly": "UNEXPECTED"} and not b.verdict.passed
    assert "passes without it" in b.verdict.tests[0].reason


def test_a_crash_on_base_is_no_signal_for_a_failure_with_the_change(tmp_path, state):
    # e.g. the base worktree lacks a generated file, so the test cannot even start there
    repo, base = repo_with(tmp_path, "t::p crash\n", "t::p fail\n")
    _, b = sink_it(repo, tmp_path, base)
    assert statuses(b) == {"t::p": "UNEXPECTED"} and not b.verdict.passed
    assert b.verdict.tests[0].reason.startswith("no signal: the base side crashed (CRASH without")


def test_a_crash_on_every_run_is_never_exonerated(tmp_path, state):
    # AUDIT-R5: a crash on base is no evidence, even when the change crashes the same way
    repo, base = repo_with(tmp_path, "t::p crash\n", "t::p crash\n")
    _, b = sink_it(repo, tmp_path, base, Policy(retry_failed=2))
    assert statuses(b) == {"t::p": "UNEXPECTED"} and not b.verdict.passed
    assert b.verdict.tests[0].reason.startswith("no signal: the base side crashed (CRASH without")
    assert b.verdict.inputs[2:] == ["local/o/x/r1/base", "local/o/x/r1/base-run2",
                                    "local/o/x/r1/base-run3"]


def test_a_crash_with_the_change_is_no_signal_against_a_failure_on_base(tmp_path, state):
    repo, base = repo_with(tmp_path, "t::p fail\n", "t::p crash\n")
    _, b = sink_it(repo, tmp_path, base)
    assert statuses(b) == {"t::p": "UNEXPECTED"} and not b.verdict.passed
    assert b.verdict.tests[0].reason.startswith("no signal: FAIL without the change but CRASH with")


TABLE_IN_FIXTURE = (
    "import pathlib\n\nimport pytest\n\nimport calc\n\n\n@pytest.fixture\n"
    "def table():\n    return calc.load(pathlib.Path('gen/table.txt').read_text())\n\n\n"
    "def test_lookup(table):\n    assert table['a'] == 1\n")
TABLE_IN_BODY = (
    "import pathlib\n\nimport calc\n\n\n"
    "def test_lookup():\n    table = calc.load(pathlib.Path('gen/table.txt').read_text())\n"
    "    assert table['a'] == 1\n")


@pytest.mark.parametrize("test_file, status, reason", [
    # The fixture reads the file: an error, so a CRASH on both sides (closed by AUDIT-R5).
    (TABLE_IN_FIXTURE, "CRASH", "no signal: the base side crashed"),
    # The test body reads it: pytest reports a <failure>, so a FAIL on both sides, but
    # FileNotFoundError on base and ValueError with the change (the R5 review's repro).
    (TABLE_IN_BODY, "FAIL", "no signal: fails differently without the change "
                            "(FileNotFoundError: vs ValueError:)"),
], ids=["in-fixture", "in-test-body"])
def test_a_missing_generated_file_on_base_cannot_hide_a_regression(tmp_path, test_file, status,
                                                                    reason):
    # AUDIT-R5 reproduction, with pytest itself: the test reads gen/table.txt, a gitignored
    # build output that only the change's checkout has, so on base it fails with
    # FileNotFoundError. The change breaks calc.load, so the test fails with it too. Before
    # AUDIT-R5 the two failures matched by status and the regression was exonerated.
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "t")
    (repo / ".gitignore").write_text("gen/\nresults/\n")
    (repo / "calc.py").write_text(
        "def load(text):\n    return {k: int(v) for k, v in (l.split('=') for l in text.split())}\n")
    (repo / "tests" / "test_table.py").write_text(test_file)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    base = git(repo, "rev-parse", "HEAD")
    (repo / "calc.py").write_text(   # the regression: splits on ',' and unpacks one value
        "def load(text):\n    return {k: int(v) for k, v in (l.split(',') for l in text.split())}\n")
    git(repo, "commit", "-q", "-am", "change")
    (repo / "gen").mkdir()
    (repo / "gen" / "table.txt").write_text("a=1\n")   # generated by the build, not committed
    pytest_cmd = f'"{sys.executable}" -m pytest -q -p no:cacheprovider'
    subprocess.run(f"{pytest_cmd} --junitxml=results/junit.xml", shell=True, cwd=repo,
                   capture_output=True)
    run = local.run_from_args("o/x", git(repo, "rev-parse", "HEAD"), kind="presubmit", run_id="r1")
    _, b = sink.sink(run, ["results/*.xml"], repo, tmp_path / "out",
                     rerun_cmd=f'{pytest_cmd} --junitxml="$QQ_JUNIT_DIR/rerun.xml"',
                     base_commit=base)
    assert [r.status for r in b.results] == [status]
    base_run = bundle.read(tmp_path / "out" / "qq-results-local_o_x_r1_base")
    assert [r.status for r in base_run.results] == [status]
    assert "FileNotFoundError" in base_run.results[0].message
    assert statuses(b) == {"tests.test_table::test_lookup": "UNEXPECTED"} and not b.verdict.passed
    assert b.verdict.tests[0].reason.startswith(reason)


def test_without_retries_base_runs_once(tmp_path, state):
    repo, base = repo_with(tmp_path, "t::p fail\n", "t::p fail\n")
    _, b = sink_it(repo, tmp_path, base, Policy(retry_failed=0))
    assert statuses(b) == {"t::p": "EXONERATED"} and b.verdict.inputs == ["local/o/x/r1/base"]


def test_any_base_run_that_disagrees_blocks():
    run = Run(id="r", repo="o/x", kind="presubmit", commit="c")
    fail = [Result(run_id="r", test_id="t::a", status="FAIL", expected=False,
                   message="AssertionError: planted")]

    def child(role, suffix, status, message="AssertionError: planted"):
        c = retry.child_run(run, role, commit="b" * 40 if role == "base" else "")
        c = Run.from_dict({**c.to_dict(), "id": f"{c.id}{suffix}"})
        rs = [Result(run_id=c.id, test_id="t::a", status=status, expected=False, message=message)]
        return bundle.Bundle(c, rs, retry.verdict.compute(c, rs))

    def base(suffix, status, message="AssertionError: planted"):
        return child("base", suffix, status, message)

    retries = [child("retry", "1", "FAIL")]
    same = retry.decide(run, fail, retries, [base("", "FAIL"), base("-run2", "FAIL")])
    assert same.passed and same.tests[0].status == "EXONERATED"
    mixed = retry.decide(run, fail, retries, [base("", "FAIL"), base("-run2", "CRASH")])
    assert not mixed.passed and mixed.tests[0].reason.startswith("no signal: the base side crashed")
    crash = [Result(run_id="r", test_id="t::a", status="CRASH", expected=False)]
    both = retry.decide(run, crash, [child("retry", "1", "CRASH")],
                        [base("", "CRASH"), base("-run2", "CRASH")])
    assert not both.passed and both.tests[0].status == "UNEXPECTED"
    assert both.tests[0].reason.startswith("no signal: the base side crashed (CRASH without")
    # AUDIT-R5 review: a FAIL of another kind on base, or one with no message, is no evidence
    other = retry.decide(run, fail, retries, [base("", "FAIL"),
                                              base("-run2", "FAIL", "FileNotFoundError: gen/x")])
    assert not other.passed and other.tests[0].reason == (
        "no signal: fails differently without the change "
        "(AssertionError:/FileNotFoundError: vs AssertionError:)")
    bare = retry.decide(run, fail, retries, [base("", "FAIL", ""), base("-run2", "FAIL", " \n")])
    assert not bare.passed and bare.tests[0].reason.endswith("((no message) vs AssertionError:)")
    # only the first word of the first line counts
    alike = retry.decide(run, fail, retries, [base("", "FAIL", "  AssertionError: other text\nE  x"),
                                              base("-run2", "FAIL")])
    assert alike.passed and alike.tests[0].status == "EXONERATED"
    missing = retry.decide(run, fail, retries,
                           [base("", "FAIL"), bundle.Bundle(base("-run2", "FAIL").run, [], same)])
    assert not missing.passed and "no result without the change" in missing.tests[0].reason
