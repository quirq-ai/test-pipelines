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
    assert b.verdict.inputs == ["local/o/x/r1/retry1", "local/o/x/r1/base"]
    # the retry and base runs are stored next to the change's run, each write-once
    names = sorted(p.name for p in (tmp_path / "out").iterdir())
    assert names == ["qq-results-local_o_x_r1", "qq-results-local_o_x_r1_base",
                     "qq-results-local_o_x_r1_retry1"]
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
    with pytest.raises(policy.PolicyError, match="not an infra-config checkout"):
        policy.from_infra_config(tmp_path)
