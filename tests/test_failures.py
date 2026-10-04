import io
import json
import zipfile
from pathlib import Path

import pytest

from qqresults import cli, failures, scorecard
from qqresults.backends import github
from qqresults.store import FileStore


class FakeGitHub:
    """Just enough of the issues and labels API, in memory."""

    def __init__(self):
        self.issues = []
        self.labels = set()
        self.calls = []

    def __call__(self, method, url, token, body=None):
        self.calls.append((method, url))
        path = url.removeprefix(github.API)
        if method == "GET" and "/issues?" in path:
            page = int(path.rsplit("page=", 1)[1])
            label = path.split("labels=")[1].split("&")[0]
            hits = [i for i in self.issues if label in i["labels"]]
            return 200, hits[(page - 1) * 100: page * 100]
        if method == "POST" and path.endswith("/labels"):
            if body["name"] in self.labels:
                return 422, None
            self.labels.add(body["name"])
            return 201, {}
        if method == "POST" and path.endswith("/issues"):
            n = len(self.issues) + 1
            issue = {"number": n, "html_url": f"https://github.com/o/x/issues/{n}", "state": "open",
                     **body}
            self.issues.append(issue)
            return 201, issue
        if method == "PATCH":
            n = int(path.rsplit("/", 1)[1])
            self.issues[n - 1].update(body)
            return 200, self.issues[n - 1]
        raise AssertionError(f"unexpected call {method} {url}")


def held(tmp_path, **extra):
    fields = {"stage": "probe", "signal": "health", "summary": "Canary held: /health probe failed",
              **extra}
    f = failures.new("canary-held", "quirq-ai/xo-space", "sha256:abc", **fields)
    return failures.open_record(f, tmp_path)


def test_a_held_canary_creates_exactly_one_record_and_one_issue(tmp_path):
    # V0-TST-04 done-when, against a fake GitHub: report the same held canary twice.
    gh = FakeGitHub()
    state, created = held(tmp_path)
    url, made = github.mirror_issue(state, "o/x", "tok", call=gh)
    assert created and made and url == "https://github.com/o/x/issues/1"
    state2, created2 = held(tmp_path)               # the pipeline retries and reports it again
    url2, made2 = github.mirror_issue(state2, "o/x", "tok", call=gh)
    assert not created2 and not made2 and url2 == url
    assert len(list(tmp_path.iterdir())) == 1 and len(gh.issues) == 1
    issue = gh.issues[0]
    assert issue["labels"] == ["qq-failure", "qq-failure:canary-held"]
    assert f"<!-- qq-failure: {state.record.id} -->" in issue["body"]
    assert issue["title"] == "[qq canary-held] quirq-ai/xo-space: Canary held: /health probe failed"
    # a fresh runner (empty scratch dir) still finds the issue by its marker
    other = tmp_path / "other"
    state3, _ = held(other)
    assert github.mirror_issue(state3, "o/x", "tok", call=gh) == (url, False)
    assert len(gh.issues) == 1


def test_record_ids_are_stable_and_distinct():
    a = failures.failure_id("canary-held", "o/x", "sha256:1")
    assert a == failures.failure_id("canary-held", "o/x", "sha256:1")
    assert a != failures.failure_id("canary-held", "o/x", "sha256:2")
    assert a != failures.failure_id("canary-rollback", "o/x", "sha256:1")
    with pytest.raises(failures.FailureError, match="unknown failure kind"):
        failures.failure_id("oops", "o/x", "s")
    with pytest.raises(failures.FailureError, match="needs a subject"):
        failures.failure_id("canary-held", "o/x", "")


def test_links_close_the_record_and_the_issue(tmp_path):
    gh = FakeGitHub()
    state, _ = held(tmp_path)
    github.mirror_issue(state, "o/x", "tok", call=gh)
    assert state.missing == ["culprit", "fix", "covering_test"]
    failures.add_link(state.path, "culprit", "quirq-ai/xo-space@c0ffee")
    failures.add_link(state.path, "fix", "quirq-ai/xo-space#12")
    state = failures.read(state.path)
    github.mirror_issue(state, "o/x", "tok", call=gh)
    assert gh.issues[0]["state"] == "open" and "open until covering test" in gh.issues[0]["body"]
    failures.add_link(state.path, "covering_test", "tests.test_health::test_probe")
    state = failures.read(state.path)
    assert state.closed and state.current.culprit == "quirq-ai/xo-space@c0ffee"
    github.mirror_issue(state, "o/x", "tok", call=gh)
    assert gh.issues[0]["state"] == "closed"
    record = json.loads((state.path / "failure.json").read_text())
    assert record["culprit"] == ""      # the record itself is never rewritten
    with pytest.raises(failures.FailureError, match="cannot link"):
        failures.add_link(state.path, "kind", "x")


@pytest.mark.parametrize("extra", [
    {"security": True}, {"signal": "heap-buffer-overflow in parser"}, {"stage": "secret scan"},
    {"summary": "XSS in login form"}, {"summary": "RCE via deserialization"},
    {"summary": "SSRF to metadata"}, {"summary": "auth bypass"}, {"summary": "CSRF on settings"},
    {"summary": "leaked API key"}, {"summary": "unauthenticated access"},
    {"summary": "path traversal"}, {"summary": "ReDoS in parser"}])
def test_security_looking_failures_are_never_mirrored(tmp_path, extra):
    gh = FakeGitHub()
    state, _ = held(tmp_path, **extra)
    assert state.record.security
    assert github.mirror_issue(state, "o/x", "tok", call=gh) == ("", False)
    assert [m for m, _ in gh.calls] == ["GET"] and gh.issues == []


def test_store_imports_the_first_record_and_every_link(tmp_path):
    st = FileStore(tmp_path / "store")
    a, _ = held(tmp_path / "run1")
    b, _ = held(tmp_path / "run2")                   # same event, reported by a second run
    failures.add_link(b.path, "culprit", "c1")
    assert st.import_failure(a.path) is True
    assert st.import_failure(b.path) is True         # adds the link only
    assert st.import_failure(b.path) is False
    (only,) = st.failures()
    assert only.record.opened_at == a.record.opened_at and only.links == {"culprit": "c1"}
    assert st.failure(a.record.id).record.id == a.record.id


def test_collect_imports_failure_artifacts(tmp_path):
    state, _ = held(tmp_path / "scratch")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for p in state.path.rglob("*"):
            if p.is_file():
                z.write(p, p.relative_to(tmp_path / "scratch").as_posix())
    name = f"{state.path.name}-991-1"

    def get(url, token):
        if "/actions/artifacts" in url:
            return json.dumps({"artifacts": [{"name": name, "archive_download_url": "u"}]}).encode()
        return buf.getvalue()

    st = FileStore(tmp_path / "store")
    assert github.collect("o/x", st, "", get=get) == (1, 0, [])
    assert github.collect("o/x", st, "", get=get) == (0, 1, [])
    assert len(st.failures()) == 1


def test_scorecard_counts_fully_recorded_failures(tmp_path):
    import datetime as dt
    st = FileStore(tmp_path)
    state, _ = held(tmp_path / "scratch")
    for field, value in (("culprit", "c"), ("fix", "f"), ("covering_test", "t")):
        failures.add_link(state.path, field, value)
    st.import_failure(state.path)
    now = dt.datetime.now(dt.UTC)
    card = scorecard.compute(st, now - dt.timedelta(days=1), now + dt.timedelta(minutes=1))
    m = next(m for m in card.repos["quirq-ai/xo-space"] if m.name == "Failures fully recorded")
    assert m.value == 100.0


def test_cli_failure_open_link_list(tmp_path, capsys, monkeypatch):
    gh = FakeGitHub()
    monkeypatch.setattr(github, "api", gh)
    d = str(tmp_path / "f")
    base = ["failure", "open", "--dir", d, "--kind", "canary-rollback", "--repo", "o/x",
            "--subject", "sha256:9", "--summary", "rolled back", "--mirror", "o/x"]
    assert cli.main(base) == 0 and cli.main(base) == 0
    out = capsys.readouterr().out
    assert "opened canary-rollback-" in out and "already open: canary-rollback-" in out
    assert out.count("issue: https://github.com/o/x/issues/1") == 2 and len(gh.issues) == 1
    fid = failures.failure_id("canary-rollback", "o/x", "sha256:9")
    assert cli.main(["failure", "link", fid, "--dir", d, "--culprit", "c", "--fix", "f",
                     "--covering-test", "t", "--mirror", "o/x"]) == 0
    assert "closed" in capsys.readouterr().out and gh.issues[0]["state"] == "closed"
    assert cli.main(["failure", "list", "--dir", d]) == 0
    assert "closed  canary-rollback" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.main(["failure", "open", "--dir", d, "--kind", "canary-held"])


def test_subject_links_and_fuzz_kind_count_as_security(tmp_path):
    assert failures.new("canary-held", "o/x", "CVE-2026-1").security
    assert failures.new("fuzz", "o/x", "crash-1").security
    state, _ = held(tmp_path)
    assert not state.security
    failures.add_link(state.path, "fix", "patch for CVE-2026-1")
    assert failures.read(state.path).security


def test_a_record_that_turns_security_redacts_its_public_issue(tmp_path):
    gh = FakeGitHub()
    state, _ = held(tmp_path)
    github.mirror_issue(state, "o/x", "tok", call=gh)
    failures.add_link(state.path, "failure_class", "credential leak in logs")
    assert github.mirror_issue(failures.read(state.path), "o/x", "tok", call=gh) == ("", False)
    issue = gh.issues[0]
    assert issue["state"] == "closed" and "credential" not in issue["body"]
    assert issue["body"].startswith(failures.marker(state.record.id))


def test_racing_runners_keep_one_open_issue(tmp_path):
    gh = FakeGitHub()
    state, _ = held(tmp_path)

    def racing(method, url, token, body=None):
        if method == "POST" and url.endswith("/issues") and not gh.issues:
            gh("POST", url, token, dict(body))      # another runner got there first
        return gh(method, url, token, body)

    url, made = github.mirror_issue(state, "o/x", "tok", call=racing)
    assert url == "https://github.com/o/x/issues/1" and not made
    assert [i["state"] for i in gh.issues] == ["open", "closed"]
    assert gh.issues[1]["state_reason"] == "duplicate"


def test_only_issues_whose_body_starts_with_the_marker_match(tmp_path):
    gh = FakeGitHub()
    state, _ = held(tmp_path)
    marker = failures.marker(state.record.id)
    gh.issues = [
        {"number": 1, "labels": ["qq-failure"], "body": "quoting " + marker, "html_url": "u1",
         "state": "open"},
        {"number": 2, "labels": ["qq-failure"], "body": marker, "html_url": "u2", "state": "open",
         "pull_request": {}},
    ]
    url, made = github.mirror_issue(state, "o/x", "tok", call=gh)
    assert made and url == "https://github.com/o/x/issues/3"


def test_summary_cannot_inject_a_marker_or_close_its_fence(tmp_path):
    other = failures.marker("canary-held-0000")
    state, _ = held(tmp_path, summary=f"{other} @someone ``` done")
    body = failures.issue_body(state)
    assert body.startswith(failures.marker(state.record.id))
    assert "```text\n" + other + " @someone ''' done\n```" in body
