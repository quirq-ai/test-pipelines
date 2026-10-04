"""A partial collect must not publish a scorecard that looks complete (AUDIT N7)."""
import json
import urllib.error

import pytest

from qqresults import cli, scorecard
from qqresults.backends import github

RATE = "GET https://api.github.com/x: HTTP 403 rate limit exceeded (rate limited; retry after 60 s)"


def fake_collect(outcomes):
    def collect(repo, store, token, trust, refused=None):
        out = outcomes[repo]
        if isinstance(out, Exception):
            raise out
        if len(out) == 4:          # (new, old, errors, refused)
            refused.extend(out[3])
            out = out[:3]
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
                                "reasons": ["o/x artifact a: HTTP 403"], "refused": 0,
                                "refused_reasons": []}]


def test_a_crash_leaves_the_repos_it_did_not_finish(tmp_path, monkeypatch, capsys):
    out = run(tmp_path, monkeypatch, capsys,
              {"o/x": (1, 0, []), "o/y": RuntimeError("boom"), "o/z": (1, 0, [])})
    assert "## Collect incomplete" in out
    assert "- o/y: collect did not finish" in out and "- o/z: collect did not finish" in out


def test_a_full_collect_says_complete(tmp_path, monkeypatch, capsys):
    out = run(tmp_path, monkeypatch, capsys, {"o/x": (1, 2, []), "o/y": (0, 0, [])})
    assert "Collect complete: every trusted artifact of 2 repo(s) was read." in out
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


FORK = "o/x artifact qq-results-f: workflow run 9 ran code from mallory/x, not o/x (a fork's pull request?)"


def test_a_trust_refusal_is_listed_apart_and_keeps_the_card_complete(tmp_path, monkeypatch, capsys):
    out = run(tmp_path, monkeypatch, capsys, {"o/x": (1, 0, [], [FORK])})
    assert "Collect complete" in out and "incomplete" not in out
    assert "- o/x: 1 artifact(s) refused (not trusted): `" + FORK + "`" in out


def test_a_refusal_next_to_a_real_skip_is_still_incomplete(tmp_path, monkeypatch, capsys):
    out = run(tmp_path, monkeypatch, capsys, {"o/x": (1, 0, ["o/x artifact a: HTTP 403"], [FORK])},
              "--json")
    repo = json.loads(out)["collect"]["repos"][0]
    assert json.loads(out)["collect"]["complete"] is False
    assert (repo["skipped"], repo["refused"]) == (1, 1)


def test_collect_sorts_trust_refusals_from_errors(tmp_path):
    from qqresults.store import FileStore
    arts = {"artifacts": [
        {"id": 1, "name": "qq-results-a", "size_in_bytes": 10, "workflow_run": {"id": 7},
         "archive_download_url": "https://api.example/a"},
        {"id": 2, "name": "qq-results-b", "size_in_bytes": 10, "workflow_run": {"id": 8},
         "archive_download_url": "https://api.example/b"}]}
    runs = {7: {"head_repository": {"full_name": "mallory/x"}, "path": ".github/workflows/qq-x.yml",
                "run_attempt": 1},
            8: {"head_repository": {"full_name": "o/x"}, "path": ".github/workflows/evil.yml",
                "run_attempt": 1}}

    def get(url, token):
        if "/actions/artifacts?" in url:
            return json.dumps(arts if "page=1" in url else {"artifacts": []}).encode()
        if "/actions/runs/" in url:
            return json.dumps(runs[int(url.rsplit("/", 1)[1])]).encode()
        raise AssertionError(url)
    refused = []
    new, old, errors = github.collect("o/x", FileStore(tmp_path), "", get=get, refused=refused)
    assert (new, old, errors) == (0, 0, [])
    assert "a fork's pull request" in refused[0] and "not an allowed workflow" in refused[1]
    # without a refused list they stay errors, as before
    assert len(github.collect("o/x", FileStore(tmp_path), "", get=get)[2]) == 2


def test_an_oversized_artifact_from_a_fork_is_a_refusal(tmp_path):
    # AUDIT M2: origin is checked before size, so a fork's oversized artifact is refused, not an
    # error that marks the collect incomplete; an oversized one from a trusted run stays an error.
    from qqresults.store import FileStore
    big = github.MAX_ARTIFACT_BYTES + 1
    arts = {"artifacts": [
        {"id": 1, "name": "qq-results-a", "size_in_bytes": big, "workflow_run": {"id": 7},
         "archive_download_url": "https://api.example/a"},
        {"id": 2, "name": "qq-results-b", "size_in_bytes": big, "workflow_run": {"id": 8},
         "archive_download_url": "https://api.example/b"}]}
    runs = {7: {"head_repository": {"full_name": "mallory/x"}, "path": ".github/workflows/qq-x.yml",
                "run_attempt": 1},
            8: {"head_repository": {"full_name": "o/x"}, "path": ".github/workflows/presubmit.yml",
                "event": "push", "head_branch": "main", "head_sha": "a" * 40, "run_attempt": 1}}

    def get(url, token):
        if "/actions/artifacts?" in url:
            return json.dumps(arts if "page=1" in url else {"artifacts": []}).encode()
        if "/actions/runs/" in url:
            return json.dumps(runs[int(url.rsplit("/", 1)[1])]).encode()
        if "/compare/" in url or "/repos/o/x" == url.rsplit("api.github.com", 1)[-1]:
            return json.dumps({"status": "identical", "default_branch": "main"}).encode()
        raise AssertionError(url)
    refused = []
    _, _, errors = github.collect("o/x", FileStore(tmp_path), "", get=get, refused=refused)
    assert len(refused) == 1 and "a fork's pull request" in refused[0]
    assert len(errors) == 1 and "is not at most" in errors[0]


def test_passes_over_one_repo_are_merged(tmp_path):
    report = tmp_path / "r.jsonl"
    report.write_text("\n".join(json.dumps(e) for e in [
        {"repo": "o/x", "finished": False},
        {"repo": "o/x", "finished": True, "listed": True, "skipped": ["o/x artifact a: HTTP 403"]},
        {"repo": "o/x", "finished": False},
        {"repo": "o/x", "finished": True, "listed": True, "skipped": [], "refused": [FORK]},
    ]) + "\n")
    status = scorecard.collect_status(report)
    assert not status["complete"]
    assert status["repos"] == [{"repo": "o/x", "finished": True, "listed": True, "skipped": 1,
                                "reasons": ["o/x artifact a: HTTP 403"], "refused": 1,
                                "refused_reasons": [FORK]}]
    # a second pass that never finished leaves the repo unfinished, though the first did
    report.write_text("\n".join(json.dumps(e) for e in [
        {"repo": "o/x", "finished": False},
        {"repo": "o/x", "finished": True, "listed": True, "skipped": []},
        {"repo": "o/x", "finished": False}]) + "\n")
    assert scorecard.collect_status(report)["repos"][0]["finished"] is False
    # and a pass that could not list makes it unlisted though another pass listed
    report.write_text("\n".join(json.dumps(e) for e in [
        {"repo": "o/x", "finished": True, "listed": False, "skipped": ["o/x: HTTP 502"]},
        {"repo": "o/x", "finished": True, "listed": True, "skipped": []}]) + "\n")
    status = scorecard.collect_status(report)
    assert not status["complete"] and status["repos"][0]["listed"] is False


SIGNED = "https://storage.example/blob/a.zip?sv=2024&se=2026&sig=SECRETSIGNATURE%3D"


class _Opener:
    def __init__(self, exc):
        self.exc = exc

    def open(self, req, timeout):
        raise self.exc


@pytest.mark.parametrize("exc", [
    urllib.error.HTTPError(SIGNED, 403, "Forbidden", {}, None),
    urllib.error.URLError("connection reset"),
])
def test_error_messages_drop_the_query_of_a_signed_url(monkeypatch, exc):
    monkeypatch.setattr(github.urllib.request, "build_opener", lambda *a: _Opener(exc))
    with pytest.raises(github.GitHubAPIError) as e:
        github._request(SIGNED, "")
    assert "sig=" not in str(e.value) and "SECRET" not in str(e.value)
    assert "GET https://storage.example/blob/a.zip:" in str(e.value)


def test_a_failed_storage_get_after_the_redirect_leaks_no_signature(monkeypatch):
    # The API answers with a redirect to signed storage, whose GET then fails.
    monkeypatch.setattr(github.urllib.request, "build_opener",
                        lambda *a: _Opener(urllib.error.HTTPError(SIGNED, 403, "Forbidden", {}, None)))
    real = github._request
    monkeypatch.setattr(github, "_request",
                        lambda url, token, accept="application/vnd.github+json":
                        (302, {"Location": SIGNED}, b"") if url.startswith("https://api")
                        else real(url, token, accept))
    with pytest.raises(github.GitHubAPIError) as e:
        github.http_get("https://api.github.com/x/zip?per_page=1", "t")
    assert "sig=" not in str(e.value) and "storage.example/blob/a.zip" in str(e.value)
