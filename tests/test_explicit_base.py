"""An explicit base (`--base`, the sink's `base` input) must be a full commit id (AUDIT N6)."""
import pytest

from qqresults import cli, retry
from qqresults.model import Result, Run
from qqresults.policy import Policy

NAMES = ["main", "results", "v1.0", "abc1234", "A" * 40, "a" * 40 + "\n", "a" * 39 + "g",
         "HEAD^1", "-" + "a" * 39]


@pytest.mark.parametrize("base", NAMES)
def test_recheck_refuses_a_base_that_is_not_a_full_commit_id(tmp_path, base):
    # A name is fetched by name, and a tag of the same name would shadow the branch.
    run = Run(id="r", repo="o/x", kind="presubmit", commit="c")
    results = [Result(run_id="r", test_id="t::a", status="FAIL", expected=False)]
    with pytest.raises(retry.RetryError, match="full 40-hex"):
        retry.recheck(run, results, "true", tmp_path, Policy(), base_commit=base)


@pytest.mark.parametrize("base", NAMES)
def test_the_sink_cli_refuses_it_before_writing_anything(tmp_path, base):
    args = ["sink", "--backend", "local", "--repo", "o/x", "--commit", "c", "--junit", "x.xml",
            "--root", str(tmp_path), "--out", str(tmp_path / "o"), "--rerun", "true",
            f"--base={base}"]
    with pytest.raises(SystemExit, match="--base must be a full 40-hex"):
        cli.main(args)
    assert not (tmp_path / "o").exists()


def test_a_full_commit_id_passes_the_check(tmp_path):
    run = Run(id="r", repo="o/x", kind="presubmit", commit="c")
    results = [Result(run_id="r", test_id="t::a", status="PASS", expected=True)]
    assert retry.recheck(run, results, "true", tmp_path, Policy(), base_commit="a" * 40).verdict.passed
