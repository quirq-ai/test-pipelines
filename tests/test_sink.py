import json
import shutil

import pytest

from qqresults import bundle, cli, sink
from qqresults.backends import github, local
from qqresults.model import Run, RunKind




def good_reports(tmp_path, junit_dir):
    root = tmp_path / "ws"
    (root / "results").mkdir(parents=True)
    for name in ("pytest.xml", "vitest.xml"):
        shutil.copy(junit_dir / name, root / "results" / name)
    return root


def test_bundle_round_trip(tmp_path, junit_dir):
    root = good_reports(tmp_path, junit_dir)
    run = local.run_from_args("quirq-ai/xo-space", "abc123", run_id="r1")
    path, b = sink.sink(run, ["results/**/*.xml"], root, tmp_path / "out")
    assert path.name == "qq-results-local_quirq-ai_xo-space_r1"
    assert len(b.results) == 6 and not b.verdict.passed
    assert b.verdict.counts == {"EXPECTED": 3, "UNEXPECTED": 3}
    assert {r.source for r in b.results} == {"results/pytest.xml", "results/vitest.xml"}
    again = bundle.read(path)
    assert again == b


def test_bundles_are_write_once(tmp_path, junit_dir):
    root = good_reports(tmp_path, junit_dir)
    run = local.run_from_args("o/x", "c", run_id="r1")
    sink.sink(run, ["results/*.xml"], root, tmp_path / "out")
    with pytest.raises(bundle.BundleError, match="write-once"):
        sink.sink(run, ["results/*.xml"], root, tmp_path / "out")


def test_no_reports_still_stores_a_failed_run(tmp_path):
    run = local.run_from_args("o/x", "c", run_id="r1")
    path, b = sink.sink(run, ["nothing/*.xml"], tmp_path, tmp_path / "out")
    assert not b.run.results_found and not b.verdict.passed
    assert bundle.read(path).run.results_found is False


def gh_env(tmp_path, event_name, payload, **extra):
    event = tmp_path / "event.json"
    event.write_text(json.dumps(payload))
    return {"GITHUB_REPOSITORY": "quirq-ai/xo-space", "GITHUB_RUN_ID": "991", "GITHUB_RUN_ATTEMPT": "2",
            "GITHUB_JOB": "presubmit", "GITHUB_WORKFLOW": "qq xo-space-presubmit", "GITHUB_SHA": "m1",
            "GITHUB_REF_NAME": "main", "GITHUB_EVENT_NAME": event_name,
            "GITHUB_EVENT_PATH": str(event), **extra}


def test_github_merge_group_is_a_gate_run(tmp_path):
    env = gh_env(tmp_path, "merge_group", {"merge_group": {
        "head_sha": "m1", "head_ref": "refs/heads/gh-readonly-queue/main/pr-42-b0", "base_sha": "b0",
        "base_ref": "refs/heads/main"}, "repository": {"default_branch": "main"}})
    run = github.run_from_env(env)
    assert run.kind == RunKind.GATE and run.commit == "m1" and run.base_commit == "b0"
    assert run.change.number == 42 and run.branch == "main"
    assert run.id == "github/quirq-ai/xo-space/991/2/presubmit"
    assert run.url == "https://github.com/quirq-ai/xo-space/actions/runs/991/attempts/2"


def test_github_pull_request_and_push(tmp_path):
    pr = github.run_from_env(gh_env(tmp_path, "pull_request", {"pull_request": {
        "number": 7, "head": {"sha": "h7"}, "base": {"sha": "b7", "ref": "main"}}}), name="unit")
    assert (pr.kind, pr.change.number, pr.change.head_sha, pr.base_commit) == ("presubmit", 7, "h7", "b7")
    assert pr.id.endswith("/presubmit/unit")
    push = github.run_from_env(gh_env(tmp_path, "push", {"before": "p0",
                                                         "repository": {"default_branch": "main"}}))
    assert (push.kind, push.base_commit, push.change) == ("postsubmit", "p0", None)
    side = github.run_from_env(gh_env(tmp_path, "push", {"repository": {"default_branch": "main"}},
                                      GITHUB_REF_NAME="feature"))
    assert side.kind == "other"


def test_github_backend_outside_actions_says_so():
    with pytest.raises(github.GitHubEnvError, match="--backend local"):
        github.run_from_env({})


def test_cli_sink_and_show(tmp_path, junit_dir, capsys):
    root = good_reports(tmp_path, junit_dir)
    out = tmp_path / "out"
    gh_out = tmp_path / "gh_output"
    code = cli.main(["sink", "--backend", "local", "--repo", "o/x", "--commit", "c", "--junit",
                     "results/*.xml", "--root", str(root), "--out", str(out),
                     "--github-output", str(gh_out)])
    assert code == 0
    assert "verdict: FAIL" in capsys.readouterr().out
    outputs = dict(line.split("=", 1) for line in gh_out.read_text().splitlines())
    assert outputs["passed"] == "false" and outputs["name"].startswith("qq-results-local_o_x_")
    assert cli.main(["show", outputs["bundle"]]) == 0
    shown = capsys.readouterr().out
    assert "UNEXPECTED tests.test_greet::test_goodbye" in shown.replace("  ", " ").replace("  ", " ")


def test_cli_reports_bad_xml_in_one_line(tmp_path, junit_dir, capsys):
    code = cli.main(["sink", "--backend", "local", "--repo", "o/x", "--commit", "c", "--junit",
                     "not-junit.xml", "--root", str(junit_dir), "--out", str(tmp_path)])
    assert code == 1
    assert capsys.readouterr().err.startswith("qqresults: not-junit.xml: root element")


def test_records_round_trip_and_ignore_unknown_fields():
    run = Run(id="r", repo="o/x", kind="gate", commit="c")
    data = {**run.to_dict(), "added_in_v2": 1}
    assert Run.from_dict(data) == run


@pytest.mark.parametrize("pattern", ["/etc/*.xml", "../*.xml", "results/../../x.xml"])
def test_globs_stay_inside_the_workspace(tmp_path, pattern):
    run = local.run_from_args("o/x", "c", run_id="r1")
    with pytest.raises(sink.SinkError, match="relative to the workspace"):
        sink.sink(run, [pattern], tmp_path, tmp_path / "out")


def test_unreadable_bundle_line_is_a_bundle_error(tmp_path, junit_dir):
    root = good_reports(tmp_path, junit_dir)
    path, _ = sink.sink(local.run_from_args("o/x", "c", run_id="r1"), ["results/*.xml"], root,
                        tmp_path / "out")
    (path / "results.jsonl").chmod(0o644)
    (path / "results.jsonl").write_text("[1, 2]\n")
    with pytest.raises(bundle.BundleError):
        bundle.read(path)


@pytest.mark.parametrize("value, expected", [
    ("2026-10-04T12:00:00Z", "2026-10-04T12:00:00Z"),
    ("2026-10-04T14:00:00+02:00", "2026-10-04T12:00:00Z"),
    ("2026-10-04T12:00:00", ""),          # no zone: ignored
    ("yesterday", ""), ("", "")])
def test_gate_timing_sets_queued_at(tmp_path, value, expected):
    env = gh_env(tmp_path, "merge_group", {"merge_group": {"head_ref": "refs/heads/gh-readonly-queue/main/pr-1-x"}},
                 QQ_QUEUED_AT=value)
    assert github.run_from_env(env).queued_at == expected
