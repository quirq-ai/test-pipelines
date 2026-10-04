"""A partial collect must not publish a scorecard that looks complete (AUDIT N7)."""
import json

import pytest

from qqresults import cli, scorecard
from qqresults.backends import github

RATE = "GET https://api.github.com/x: HTTP 403 rate limit exceeded (rate limited; retry after 60 s)"


def fake_collect(outcomes):
    def collect(repo, store, token, trust):
        out = outcomes[repo]
        if isinstance(out, Exception):
            raise out
        return out
    return collect


def run(tmp_path, monkeypatch, capsys, outcomes, *extra):
    monkeypatch.setattr(github, "collect", fake_collect(outcomes))
    report = tmp_path / "report.jsonl"
    store = tmp_path / "store"
    args = ["collect", "--store", str(store), "--report", str(report)]
    for repo in outcomes:
        args += ["--repo", repo]
    try:
        cli.main(args)
    except RuntimeError:     # a crash inside collect, not an artifact error
        pass
    capsys.readouterr()
    assert cli.main(["scorecard", "--store", str(store), "--collect-report", str(report), *extra]) == 0
    return capsys.readouterr().out


def test_skipped_artifacts_make_the_card_say_collect_incomplete(tmp_path, monkeypatch, capsys):
    out = run(tmp_path, monkeypatch, capsys, {
        "o/bad": github.GitHubAPIError(RATE),
        "o/x": (1, 3, [f"o/x artifact qq-results-a: {RATE}", "o/x artifact qq-results-b: HTTP 500"]),
        "o/ok": (2, 0, []),
    })
    assert "## Collect incomplete" in out and "this card is partial" in out
    assert "- o/x: 2 artifact(s) skipped: `o/x artifact qq-results-a: GET" in out
    assert "rate limited; retry after 60 s" in out and "HTTP 500" in out
    assert f"- o/bad: its artifacts could not be listed: `o/bad: {RATE}`" in out
    assert "o/ok" not in out.split("## Collect incomplete")[1].split("\n\n")[1]
    assert "Collect complete" not in out


def test_the_json_card_says_it_too(tmp_path, monkeypatch, capsys):
    out = run(tmp_path, monkeypatch, capsys, {"o/x": (0, 0, ["o/x artifact a: HTTP 403"])}, "--json")
    status = json.loads(out)["collect"]
    assert status["complete"] is False
    assert status["repos"] == [{"repo": "o/x", "finished": True, "listed": True, "skipped": 1,
                                "reasons": ["o/x artifact a: HTTP 403"]}]


def test_a_crash_leaves_the_repos_it_did_not_finish(tmp_path, monkeypatch, capsys):
    out = run(tmp_path, monkeypatch, capsys,
              {"o/x": (1, 0, []), "o/y": RuntimeError("boom"), "o/z": (1, 0, [])})
    assert "## Collect incomplete" in out
    assert "- o/y: collect did not finish" in out and "- o/z: collect did not finish" in out


def test_a_full_collect_says_complete(tmp_path, monkeypatch, capsys):
    out = run(tmp_path, monkeypatch, capsys, {"o/x": (1, 2, []), "o/y": (0, 0, [])})
    assert "Collect complete: every artifact of 2 repo(s) was read." in out
    assert "incomplete" not in out


def test_a_missing_or_broken_report_is_incomplete(tmp_path):
    assert not scorecard.collect_status(tmp_path / "none.jsonl")["complete"]
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"repo": "o/x", "finished": true, "listed": true, "skipped": []}\nnot json\n')
    status = scorecard.collect_status(bad)
    assert not status["complete"] and status["problems"] == ["1 line(s) of the collect report did not read"]
    empty = tmp_path / "empty.jsonl"
    empty.write_text("")
    assert scorecard.collect_status(empty)["problems"] == ["the collect report names no repo"]
    # what the workflow appends when its collect step failed
    step = tmp_path / "step.jsonl"
    step.write_text('{"repo": "(collect step)", "finished": false}\n')
    assert "- (collect step): collect did not finish" in scorecard.collect_markdown(
        scorecard.collect_status(step))


def test_the_card_lists_a_few_reasons_and_counts_the_rest(tmp_path, monkeypatch, capsys):
    errors = [f"o/x artifact a{i}: HTTP 403" for i in range(8)]
    out = run(tmp_path, monkeypatch, capsys, {"o/x": (0, 0, errors)})
    assert "- o/x: 8 artifact(s) skipped: " in out and "a4: HTTP 403`; and 3 more" in out
    assert "a5:" not in out


def test_without_a_report_the_card_is_unchanged(tmp_path, capsys):
    assert cli.main(["scorecard", "--store", str(tmp_path / "s")]) == 0
    out = capsys.readouterr().out
    assert "ollect" not in out
    assert cli.main(["scorecard", "--store", str(tmp_path / "s"), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["collect"] is None


def test_collect_appends_so_several_calls_share_one_report(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(github, "collect", fake_collect({"o/x": (1, 0, []), "o/y": (0, 0, ["e"])}))
    report = tmp_path / "r.jsonl"
    for repo in ("o/x", "o/y"):
        assert cli.main(["collect", "--store", str(tmp_path / "s"), "--report", str(report),
                         "--repo", repo]) == 0
    lines = [json.loads(line) for line in report.read_text().splitlines()]
    assert [(e["repo"], e["finished"]) for e in lines] == [
        ("o/x", False), ("o/x", True), ("o/y", False), ("o/y", True)]
    assert [r["skipped"] for r in scorecard.collect_status(report)["repos"]] == [0, 1]


def test_a_reason_cannot_add_markdown_to_the_card(tmp_path, monkeypatch, capsys):
    name = "o/x artifact qq-results-`![x](https://e.example/p.png)`" + "y" * 400
    out = run(tmp_path, monkeypatch, capsys, {"o/x": (0, 0, [name + "\nline two"])})
    line = next(l for l in out.splitlines() if l.startswith("- o/x"))
    assert line.count("`") == 2 and "'![x](https://e.example/p.png)'" in line
    assert line.endswith("...`") and "line two" not in line
