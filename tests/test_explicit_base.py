"""An explicit base (`--base`, the sink's `base` input) must be a full commit id (AUDIT N6).

A name is fetched by name, and a tag can shadow a branch. A bad base refuses only the
comparison: the run's bundle is still written, its failures stay UNEXPECTED, and the sink
fails the verdict as for any unexonerated failure."""
import pytest

from qqresults import bundle, cli, retry
from qqresults.model import Result, Run
from qqresults.policy import Policy
from test_retry import CMD, first_run, repo_with, statuses

NAMES = ["main", "results", "v1.0", "abc1234", "A" * 40, "a" * 40 + "\n", "a" * 39 + "g",
         "HEAD^1", "-" + "a" * 39]


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_STATE", str(tmp_path / "state"))


@pytest.mark.parametrize("base", NAMES)
def test_recheck_refuses_the_comparison_but_keeps_the_run(tmp_path, base):
    run = Run(id="r", repo="o/x", kind="presubmit", commit="c")
    results = [Result(run_id="r", test_id="t::a", status="FAIL", expected=False)]
    checked = retry.recheck(run, results, "true", tmp_path, Policy(), base_commit=base)
    assert not checked.verdict.passed and checked.bases == []
    assert checked.verdict.tests[0].status == "UNEXPECTED"
    assert "not a full 40-hex commit id; not compared" in checked.verdict.tests[0].reason


def test_a_bad_base_is_ignored_when_nothing_is_compared(tmp_path):
    run = Run(id="r", repo="o/x", kind="presubmit", commit="c")
    passing = [Result(run_id="r", test_id="t::a", status="PASS", expected=True)]
    assert retry.recheck(run, passing, "true", tmp_path, Policy(), base_commit="main").verdict.passed


def sink_args(repo, tmp_path, out, *extra):
    return ["sink", "--backend", "local", "--repo", "o/x", "--commit", "c", "--junit",
            "results/*.xml", "--root", str(repo), "--out", str(tmp_path / out), *extra]


def main_bundle(out):
    return bundle.read(min(out.iterdir(), key=lambda p: len(p.name)))   # retries add a suffix


def test_the_sink_writes_the_bundle_and_fails_the_verdict(tmp_path, state, capsys):
    # The planted failure also fails at base, so a full base id would exonerate it.
    repo, base = repo_with(tmp_path, "t::planted fail\n", "t::planted fail\n")
    first_run(repo, tmp_path)
    assert cli.main(sink_args(repo, tmp_path, "full", "--rerun", CMD, "--base", base,
                              "--fail-on-verdict")) == 0
    assert cli.main(sink_args(repo, tmp_path, "named", "--rerun", CMD, "--base", "main",
                              "--fail-on-verdict")) == 1
    b = main_bundle(tmp_path / "named")
    assert statuses(b) == {"t::planted": "UNEXPECTED"} and "not compared" in b.verdict.tests[0].reason
    # the retry is still kept next to it
    assert any(p.name.endswith("_retry1") for p in (tmp_path / "named").iterdir())
    # without --fail-on-verdict the sink exits 0, as for any other failed verdict
    assert cli.main(sink_args(repo, tmp_path, "soft", "--rerun", CMD, "--base", "main")) == 0
    assert "not a full 40-hex" in capsys.readouterr().out


def test_without_rerun_a_bad_base_changes_nothing(tmp_path, state):
    repo, _ = repo_with(tmp_path, "t::ok pass\n", "t::ok pass\n")
    first_run(repo, tmp_path)
    assert cli.main(sink_args(repo, tmp_path, "o", "--base", "main", "--fail-on-verdict")) == 0
    assert main_bundle(tmp_path / "o").verdict.passed
