import hashlib
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
    digest = "sha256:" + hashlib.sha256(b"sha256:abc").hexdigest()[:16]   # not a full digest
    assert issue["title"] == f"[qq canary-held] quirq-ai/xo-space: probe health {digest}"
    assert "Canary held" not in issue["body"]      # free text is never public by default
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
    failures.add_link(b.path, "culprit", "c0ffee1")
    assert st.import_failure(a.path) is True
    assert st.import_failure(b.path) is True         # adds the link only
    assert st.import_failure(b.path) is False
    (only,) = st.failures()
    assert only.record.opened_at == a.record.opened_at and only.links == {"culprit": "c0ffee1"}
    assert st.failure(a.record.id).record.id == a.record.id


def test_collect_imports_failure_artifacts(tmp_path):
    repo = "quirq-ai/xo-space"
    state, _ = held(tmp_path / "scratch", run_id=f"github/{repo}/991/1/canary")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for p in state.path.rglob("*"):
            if p.is_file():
                z.write(p, p.relative_to(tmp_path / "scratch").as_posix())
    name = f"{state.path.name}-991-1"
    run = {"id": 991, "event": "schedule", "path": ".github/workflows/qq-canary.yml",
           "run_attempt": 1, "head_branch": "main", "head_sha": "c1",
           "head_repository": {"full_name": repo}}

    def get(url, token):
        if "/actions/artifacts" in url:
            return json.dumps({"artifacts": [{"name": name, "archive_download_url": "u",
                                              "size_in_bytes": 1, "workflow_run": {"id": 991}}]}).encode()
        if url.endswith("/actions/runs/991"):
            return json.dumps(run).encode()
        if url == f"{github.API}/repos/{repo}":
            return json.dumps({"default_branch": "main"}).encode()
        if "/compare/refs/heads/main...c1" in url:
            return json.dumps({"status": "identical"}).encode()
        return buf.getvalue()

    st = FileStore(tmp_path / "store")
    assert github.collect(repo, st, "", get=get) == (1, 0, [])
    assert github.collect(repo, st, "", get=get) == (0, 1, [])
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


def test_a_record_that_turns_security_hides_its_issue_and_asks_for_deletion(tmp_path):
    gh = FakeGitHub()
    state, _ = held(tmp_path)
    github.mirror_issue(state, "o/x", "tok", call=gh)
    failures.add_link(state.path, "failure_class", "credential leak in logs")
    with pytest.raises(github.NeedsDeletion, match="must delete"):   # editing is not redacting
        github.mirror_issue(failures.read(state.path), "o/x", "tok", call=gh)
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


def test_racing_runners_behind_a_lagging_list_end_with_one_open_issue(tmp_path):
    # Audit S3: the issue list lags behind creates, so two racing runners each see only the
    # issue they opened and keep it. The next report closes every duplicate but the lowest.
    gh = FakeGitHub()
    lagging = {"a": set(), "b": set()}   # the issue numbers each runner's list can see

    def runner(name):
        def call(method, url, token, body=None):
            status, data = gh(method, url, token, body)
            if method == "POST" and url.endswith("/issues"):
                lagging[name].add(data["number"])
            if method == "GET" and "/issues?" in url and name in lagging:
                data = [i for i in data if i["number"] in lagging[name]]
            return status, data
        return call

    a, _ = held(tmp_path / "a")
    b, _ = held(tmp_path / "b")
    assert github.mirror_issue(a, "o/x", "tok", call=runner("a"))[1]
    assert github.mirror_issue(b, "o/x", "tok", call=runner("b"))[1]
    assert [i["state"] for i in gh.issues] == ["open", "open"]      # each kept its own
    lagging.clear()                                                  # the list has caught up
    url, made = github.mirror_issue(b, "o/x", "tok", call=gh)
    assert url == "https://github.com/o/x/issues/1" and not made
    assert [i["state"] for i in gh.issues] == ["open", "closed"]
    assert gh.issues[1]["state_reason"] == "duplicate"
    # once the record closes, the kept issue closes as completed; the duplicate stays a duplicate
    for field, value in (("culprit", "c0ffee1"), ("fix", "c0ffee2"), ("covering_test", "t")):
        failures.add_link(b.path, field, value)
    github.mirror_issue(failures.read(b.path), "o/x", "tok", call=gh)
    assert [i["state"] for i in gh.issues] == ["closed", "closed"]
    assert gh.issues[1]["state_reason"] == "duplicate" and "state_reason" not in gh.issues[0]


def test_a_create_closes_duplicates_its_list_shows(tmp_path):
    # The post-create check also closes an older duplicate that a lagging list hid before.
    gh = FakeGitHub()
    state, _ = held(tmp_path)
    marker_body = failures.marker(state.record.id) + "\nolder"
    for _ in range(2):
        gh("POST", f"{github.API}/repos/o/x/issues", "tok",
           {"title": "t", "body": marker_body, "labels": ["qq-failure"]})
    hidden = {1, 2}

    def lagging(method, url, token, body=None):
        status, data = gh(method, url, token, body)
        if method == "GET" and "/issues?" in url and hidden:
            data = [i for i in data if i["number"] not in hidden]
            hidden.clear()                       # only the first listing lags
        return status, data

    url, made = github.mirror_issue(state, "o/x", "tok", call=lagging)
    assert url == "https://github.com/o/x/issues/1" and not made
    assert [i["state"] for i in gh.issues] == ["open", "closed", "closed"]
    assert all(i["state_reason"] == "duplicate" for i in gh.issues[1:])


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
    failures.mark(state.path, "public_summary")
    body = failures.issue_body(failures.read(state.path))
    assert body.startswith(failures.marker(state.record.id))
    assert "```text\n" + other + " @someone ''' done\n```" in body


@pytest.mark.parametrize("text", ["SQLi in search", "api key exposed", "ssh key committed",
                                  "authz check skipped", "passwd in logs", "attacker-controlled",
                                  "PII in crash dump", "GHSA-xxxx", "double free",
                                  "TLS certificate validation off", "open redirect",
                                  "malicious package", "broken access control",
                                  "sensitive data in logs"])
def test_common_security_phrasing_is_withheld(text):
    f = failures.new("canary-held", "o/x", "s", summary=text)
    assert failures.looks_security_related(f, {})


@pytest.mark.parametrize("text", [
    "Canary held: SIGSEGV in tls handshake", "segfault in decoder", "SIGABRT in libssl",
    "UAF in renderer", "OOB read in parser", "buffer over-read", "stack smashing detected",
    "XXE in importer", "prototype pollution", "JWT signature not checked",
    "CORS allows any origin", "admin page reachable without login"])
def test_audit_phrasing_is_withheld(text):
    assert failures.new("canary-held", "o/x", "s", summary=text).security


def test_free_text_is_public_only_with_the_opt_in(tmp_path):
    state, _ = held(tmp_path / "rec")
    failures.add_link(state.path, "culprit", "quirq-ai/xo-space@c0ffee0")
    failures.add_link(state.path, "covering_test", "regression check for the new handler")
    state = failures.read(state.path)
    body = failures.issue_body(state)
    assert "Canary held" not in body + failures.issue_title(state)
    assert "regression check" not in body and "`withheld`" in body and "c0ffee0" in body
    copy = failures.public_copy(state, tmp_path / "public")
    text = "".join(p.read_text() for p in copy.rglob("*.json"))
    assert "Canary held" not in text and "regression check" not in text
    assert failures.read(copy).closed is False and failures.read(copy).current.culprit
    assert "Canary held" in (state.path / "failure.json").read_text()   # the record keeps it
    f = failures.new("red-run", "o/x", "nightly run of the probe suite")
    assert failures.public_view(f).subject.startswith("sha256:")
    failures.mark(state.path, "public_summary")
    state = failures.read(state.path)
    assert "Canary held" in failures.issue_title(state) and "Canary held" in failures.issue_body(state)
    copy = failures.public_copy(state, tmp_path / "public")
    assert "Canary held" in (copy / "failure.json").read_text()


@pytest.mark.parametrize("again", [{"security": True}, {"summary": "segfault in decoder"}])
def test_reporting_again_as_security_withdraws_the_issue(tmp_path, again):
    gh = FakeGitHub()
    state, _ = held(tmp_path)
    github.mirror_issue(state, "o/x", "tok", call=gh)
    state, created = held(tmp_path, **again)
    assert not created and state.security and not state.record.security
    with pytest.raises(github.NeedsDeletion, match="must delete"):
        github.mirror_issue(state, "o/x", "tok", call=gh)
    assert gh.issues[0]["state"] == "closed" and "probe" not in gh.issues[0]["body"]


def test_cli_upload_is_the_public_copy(tmp_path, monkeypatch):
    gh = FakeGitHub()
    monkeypatch.setattr(github, "api", gh)
    out, pub = tmp_path / "out", tmp_path / "pub"
    base = ["failure", "open", "--dir", str(tmp_path / "f"), "--kind", "canary-held",
            "--repo", "o/x", "--subject", "sha256:9", "--summary", "probe failed",
            "--mirror", "o/x", "--public-copy", str(pub), "--github-output", str(out)]
    assert cli.main(base) == 0
    upload = dict(l.split("=", 1) for l in out.read_text().splitlines())["upload"]
    assert Path(upload).parent == pub and "probe failed" not in (Path(upload) / "failure.json").read_text()
    assert "probe failed" not in gh.issues[0]["body"]
    assert cli.main(base + ["--public-summary"]) == 0
    assert "probe failed" in gh.issues[0]["body"]
    assert cli.main(base + ["--security"]) == 1          # NeedsDeletion: the issue is withdrawn
    assert gh.issues[0]["state"] == "closed" and "probe failed" not in gh.issues[0]["body"]
    out.write_text("")
    assert cli.main(["failure", "link", failures.failure_id("canary-held", "o/x", "sha256:9"),
                     "--dir", str(tmp_path / "f"), "--public-copy", str(pub),
                     "--github-output", str(out)]) == 0
    upload = dict(l.split("=", 1) for l in out.read_text().splitlines())["upload"]
    marks = failures.read(Path(upload))      # the security record goes up as its mark only
    assert marks.security and marks.record.security and "probe" not in marks.record.to_json()
    assert set(marks.links) == {"security"} and [p.name for p in pub.iterdir()] == [Path(upload).name]


PROSE = "admin page open to anonymous users"
DIGEST = "sha256:" + "9" * 64


@pytest.mark.parametrize("field", ["stage", "signal", "channel", "last_good", "first_bad",
                                   "build_digest", "run_id", "operation"])
def test_every_free_text_field_is_withheld_publicly(tmp_path, field):
    state, _ = held(tmp_path / "rec", **{field: PROSE})
    copy = failures.public_copy(state, tmp_path / "pub")
    text = (failures.issue_title(state) + failures.issue_body(state)
            + "".join(p.read_text() for p in copy.rglob("*.json")))
    assert "anonymous" not in text and "`withheld`" in failures.issue_body(state)


def test_short_names_and_structured_values_still_show(tmp_path):
    sha = "c0ffee0" + "0" * 33
    state, _ = held(tmp_path, channel="stable", last_good=sha, first_bad="c0ffee1",
                    build_digest="sha256:" + "a" * 64,
                    run_id="github/quirq-ai/xo-space/991/1/canary")
    body = failures.issue_body(state)
    for value in ("probe", "health", "stable", sha, "c0ffee1", "sha256:" + "a" * 64,
                  "github/quirq-ai/xo-space/991/1/canary"):
        assert f"`{value}`" in body
    assert failures.issue_title(state).startswith("[qq canary-held] quirq-ai/xo-space: probe health ")


@pytest.mark.parametrize("value", [
    "admin_page_open_to_anonymous_users:0", "admin-panel/open-to-everyone#1",
    "https://x.example/admin-panel-open-to-everyone", "quirq-ai/admin page@c0ffee0",
    "https://github.com/quirq-ai/xo-space/issues/admin-panel-open", "sha256:0", "sha1:abc",
    "https://github.com/other-org/xo-space/pull/3", "other-org/repo@c0ffee0",
    "github/o/x/1/1/admin page open", "g" * 12])
def test_prose_shaped_like_a_reference_is_withheld(tmp_path, value):
    assert failures.public_value(value, "quirq-ai/xo-space") == failures.WITHHELD
    f = failures.new("canary-held", "quirq-ai/xo-space", value)
    assert failures.public_view(f).subject != value          # replaced by its digest
    assert value not in failures.issue_title(failures.State(f, {}, tmp_path))
    state, _ = held(tmp_path)
    failures.add_link(state.path, "culprit", value)
    failures.add_link(state.path, "issue", value)
    state = failures.read(state.path)
    assert value not in failures.issue_body(state) + failures.issue_title(state)
    copy = failures.public_copy(state, tmp_path / "pub")
    assert value not in "".join(p.read_text() for p in copy.rglob("*.json"))


@pytest.mark.parametrize("value", [
    "c0ffee0", "c0ffee0" + "0" * 33, "sha256:" + "a" * 64, "sha512:" + "b" * 128,
    "sha1:" + "c" * 40, "quirq-ai/xo-space@c0ffee0", "quirq-ai/infra-config#12",
    "https://github.com/quirq-ai/xo-space/pull/12",
    "https://github.com/quirq-ai/xo-space/commit/c0ffee0",
    "https://github.com/quirq-ai/xo-space/actions/runs/991/job/7"])
def test_structured_references_are_shown(value):
    assert failures.public_value(value, "quirq-ai/xo-space") == value


def _zip(path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for p in path.rglob("*"):
            if p.is_file():
                z.write(p, (Path(path.name) / p.relative_to(path)).as_posix())
    return buf.getvalue()


def _collect(st, artifacts):
    """collect, against a fake API listing the given {artifact name: zip bytes}, all uploaded by
    workflow run 1 of o/x (a scheduled run on main)."""
    run = {"id": 1, "event": "schedule", "path": ".github/workflows/qq-canary.yml",
           "run_attempt": 1, "head_branch": "main", "head_sha": "c1",
           "head_repository": {"full_name": "o/x"}}

    def get(url, token):
        if "/actions/artifacts" in url:
            return json.dumps({"artifacts": [
                {"name": n, "id": i, "archive_download_url": n, "size_in_bytes": len(z),
                 "workflow_run": {"id": 1}} for i, (n, z) in enumerate(artifacts.items())]}).encode()
        if url.endswith("/actions/runs/1"):
            return json.dumps(run).encode()
        if url == f"{github.API}/repos/o/x":
            return json.dumps({"default_branch": "main"}).encode()
        if "/compare/" in url and "main...c1?" in url:     # the run's head is on main
            return json.dumps({"status": "identical"}).encode()
        return artifacts[url]
    return github.collect("o/x", st, "", get=get)


@pytest.mark.parametrize("same_job", [True, False])
def test_a_withdrawn_issue_is_never_republished_from_the_store(tmp_path, monkeypatch, same_job):
    # open (public copy uploaded) -> open --security (issue withdrawn) -> collect -> link --mirror
    gh = FakeGitHub()
    monkeypatch.setattr(github, "api", gh)
    out = tmp_path / "out"
    fid = failures.failure_id("canary-held", "o/x", DIGEST)

    def open_(job, *extra):
        out.write_text("")
        code = cli.main(["failure", "open", "--dir", str(tmp_path / job / "f"), "--kind",
                         "canary-held", "--repo", "o/x", "--subject", DIGEST, "--stage",
                         "probe", "--summary", "probe failed", "--mirror", "o/x",
                         "--run-id", f"github/o/x/1/1/{job}",
                         "--public-copy", str(tmp_path / job / "pub"),
                         "--github-output", str(out), *extra])
        upload = dict(l.split("=", 1) for l in out.read_text().splitlines())["upload"]
        return code, _zip(Path(upload))

    code, first = open_("job1")
    assert code == 0 and gh.issues[0]["state"] == "open"
    code, second = open_("job1" if same_job else "job2", "--security")
    assert code == 1 and gh.issues[0]["state"] == "closed"     # NeedsDeletion, upload still made
    st = FileStore(tmp_path / "store")
    assert _collect(st, {f"{failures.dirname(fid)}-1-1-a": first,
                         f"{failures.dirname(fid)}-1-1-b": second})[2] == []
    (stored,) = st.failures()
    assert stored.security and "probe failed" not in stored.record.to_json()
    assert cli.main(["failure", "link", fid, "--dir", str(tmp_path / "store" / "failures"),
                     "--culprit", "c0ffee0", "--mirror", "o/x"]) == 1   # not republished
    assert len(gh.issues) == 1 and gh.issues[0]["state"] == "closed"
    assert gh.issues[0]["title"].startswith("[qq failure] withheld")
    assert "probe" not in gh.issues[0]["body"]


def test_a_marks_only_bundle_alone_makes_a_security_record(tmp_path):
    st = FileStore(tmp_path / "store")
    state, _ = held(tmp_path / "f", security=True, run_id="github/quirq-ai/xo-space/991/1/canary")
    copy = failures.public_copy(state, tmp_path / "pub")
    text = "".join(p.read_text() for p in copy.rglob("*.json"))
    assert "probe" not in text and "Canary held" not in text and "health" not in text
    marks = failures.read(copy)
    assert marks.record.id == state.record.id and marks.record.run_id == state.record.run_id
    assert set(marks.links) == {"security"} and marks.links["security"] == "true"
    assert st.import_failure(copy) and st.failure(state.record.id).security
    assert github.mirror_issue(st.failure(state.record.id), "o/x", "tok",
                               call=FakeGitHub()) == ("", False)


def test_cli_link_stores_only_public_values(tmp_path, capsys):
    state, _ = held(tmp_path)
    d = str(tmp_path)
    assert cli.main(["failure", "link", state.record.id, "--dir", d, "--culprit", "c0ffee0",
                     "--covering-test", "regression check for the handler"]) == 0
    st = failures.read(state.path)
    assert st.links == {"culprit": "c0ffee0", "covering_test": "withheld"} and not st.security
    assert "stored as 'withheld'" in capsys.readouterr().out
    assert cli.main(["failure", "link", state.record.id, "--dir", d,
                     "--fix", "patch for CVE-2026-1"]) == 0
    st = failures.read(state.path)
    assert st.security and "CVE" not in json.dumps(st.links)


@pytest.mark.parametrize("signal", [
    "test_jwt_signature_not_checked", "test_cors_any_origin", "test_xxe_importer",
    "test_uaf_renderer", "test_oob_read", "test_sqli_search", "stack-smashing-detected",
    "prototype-pollution", "admin-page-reachable-without-login", "denial-of-service",
    "private-key-in-logs", "open-redirect", "double-free-in-decoder", "openRedirectOnLogin",
    "JwtNotChecked"])
def test_snake_and_kebab_case_security_names_are_withheld(signal):
    f = failures.new("canary-held", "quirq-ai/innernet", "sha256:" + "a" * 64, signal=signal)
    assert f.security
    assert failures.public_value(signal, "quirq-ai/innernet", "signal") == failures.WITHHELD
    assert signal not in failures.issue_title(failures.State(f, {}, Path(".")))


@pytest.mark.parametrize("value", [
    "quirq-ai/admin_panel_open_to_everyone#1",
    "https://github.com/quirq-ai/login-not-required-on-admin/pull/1",
    "github/quirq-ai/x/1/1/admin_page_open_to_anyone"])
def test_name_slots_of_references_are_checked_for_security(tmp_path, value):
    assert failures.public_value(value, "quirq-ai/innernet") == failures.WITHHELD
    assert failures.reads_as_security(value)
    f = failures.new("canary-held", "quirq-ai/innernet", "sha256:" + "a" * 64, run_id=value)
    assert value not in failures.issue_body(failures.State(f, {}, tmp_path))   # run id: org-chosen, not classified, but withheld


@pytest.mark.parametrize("label,shown", [
    ("probe", True), ("health", True), ("http-5xx", True), ("p99.latency", True),
    ("test_probe", False), ("tests.test_health::test_probe", False), ("health_check", False),
    ("smoke-test", False), ("Health", False), ("one-two-three-four", False),
    ("a" * 33, False)])
def test_labels_are_a_small_fixed_shape(label, shown):
    assert (failures.public_value(label, "o/x", "signal") == label) is shown


def test_a_withdrawn_issue_is_never_reopened_by_a_later_report(tmp_path, monkeypatch):
    # first report public, then withdrawn as security, then a fresh runner (a re-run attempt,
    # new RUNNER_TEMP) reports it again without anything security-looking
    gh = FakeGitHub()
    state, _ = held(tmp_path / "run1")
    github.mirror_issue(state, "o/x", "tok", call=gh)
    state, _ = held(tmp_path / "run1", security=True)
    with pytest.raises(github.NeedsDeletion):
        github.mirror_issue(state, "o/x", "tok", call=gh)
    withdrawn = dict(gh.issues[0])
    fresh, _ = held(tmp_path / "run2")
    assert not fresh.security
    with pytest.raises(github.NeedsDeletion):
        github.mirror_issue(fresh, "o/x", "tok", call=gh)
    assert gh.issues[0] == withdrawn and len(gh.issues) == 1     # not patched, not reopened
    # through the CLI, the fresh runner's record turns security and uploads only its mark
    monkeypatch.setattr(github, "api", gh)
    out, pub = tmp_path / "out", tmp_path / "pub"
    assert cli.main(["failure", "open", "--dir", str(tmp_path / "run3"), "--kind", "canary-held",
                     "--repo", "quirq-ai/xo-space", "--subject", "sha256:abc", "--stage", "probe",
                     "--mirror", "o/x", "--public-copy", str(pub),
                     "--github-output", str(out)]) == 1
    outputs = dict(l.split("=", 1) for l in out.read_text().splitlines())
    assert outputs["security"] == "true" and "dir" not in outputs
    marks = failures.read(Path(outputs["upload"]))
    assert marks.record.security and set(marks.links) == {"security"}
    assert "probe" not in (Path(outputs["upload"]) / "failure.json").read_text()
    assert gh.issues[0] == withdrawn


def test_the_store_only_ever_holds_public_values(tmp_path):
    # an older action uploaded the full record; import keeps only its public bundle
    st = FileStore(tmp_path / "store")
    state, _ = held(tmp_path / "f")
    failures.add_link(state.path, "covering_test", "regression check for the handler")
    failures.add_link(state.path, "culprit", "c0ffee0")
    assert st.import_failure(state.path)
    stored = st.failure(state.record.id)
    text = "".join(p.read_text() for p in stored.path.rglob("*.json"))
    assert "Canary held" not in text and "regression check" not in text and "c0ffee0" in text
    assert stored.links == {"covering_test": "withheld", "culprit": "c0ffee0"}
    private = {p.name for p in (state.path / "links").glob("*.json")}
    public = {p.name for p in (stored.path / "links").glob("*.json")}
    assert len(public) == 2 and len(private & public) == 1   # renamed from the public body
    assert not st.import_failure(state.path)                  # the same bundle adds nothing
    copy = failures.public_copy(state, tmp_path / "pub")
    assert {p.name for p in (copy / "links").glob("*.json")} == public
    assert st.import_failure(held(tmp_path / "h", security=True)[0].path)   # adds the mark
    assert st.failure(state.record.id).security
    sec, _ = failures.open_record(failures.new("canary-held", "o/x", "c0ffee2", stage="probe",
                                               security=True), tmp_path / "g")
    st.import_failure(sec.path)
    assert st.failure(sec.record.id).security
    assert "probe" not in (st.failure(sec.record.id).path / "failure.json").read_text()


def test_a_repeat_report_cannot_publish_another_reports_summary(tmp_path, monkeypatch):
    gh = FakeGitHub()
    monkeypatch.setattr(github, "api", gh)
    base = ["failure", "open", "--dir", str(tmp_path), "--kind", "canary-held", "--repo", "o/x",
            "--subject", "sha256:9", "--mirror", "o/x"]
    assert cli.main(base + ["--summary", "first report text"]) == 0
    assert cli.main(base + ["--summary", "second report text", "--public-summary"]) == 0
    assert "report text" not in gh.issues[0]["body"]
    assert cli.main(base + ["--summary", "first report text", "--public-summary"]) == 0
    assert "first report text" in gh.issues[0]["body"]


def _crafted(tmp_path, **overrides):
    f = failures.new("canary-held", "o/x", "c0ffee0", stage="probe")
    record = {**f.to_dict(), **overrides}
    d = tmp_path / "crafted"
    (d / "links").mkdir(parents=True)
    (d / "failure.json").write_text(json.dumps(record))
    return d


@pytest.mark.parametrize("overrides", [
    {"opened_at": "Heap overflow in parser lets attacker run code"},
    {"schema": "unsigned updates accepted from mirror"},
    {"id": "token leak in prod: AKIAEXAMPLE"},
    {"kind": "whatever prose"},
    {"subject": ["x"]}, {"stage": 3}, {"security": "no"}, {"repo": {"a": 1}},
])
def test_a_crafted_record_never_reaches_the_store(tmp_path, overrides):
    st = FileStore(tmp_path / "store")
    with pytest.raises(failures.FailureError):
        st.import_failure(_crafted(tmp_path, **overrides))
    assert not (tmp_path / "store" / "failures").exists() or not list(
        (tmp_path / "store" / "failures").rglob("failure.json"))
    assert st.import_failure(_crafted(tmp_path / "ok"))      # the same record, well-formed


@pytest.mark.parametrize("text", [
    "CrashLoopBackOff in canary pod", "panic: runtime error in handler",
    "java.lang.OutOfMemoryError: Java heap space", "goroutine leak in worker",
    "memory leak in cache", "TLS certificate expired", "escalated to on-call",
    "rollout not verified within 10m", "config key not required", "kernel panic on boot"])
def test_ordinary_failures_still_get_an_issue(text):
    assert not failures.new("canary-held", "o/x", "c0ffee0", summary=text).security


@pytest.mark.parametrize("text", [
    "remote code execution in uploader", "zip slip in extractor", "unsigned update accepted",
    "SIGILL in decoder", "KASAN: slab-out-of-bounds", "XML external entity in importer",
    "requests.get(url, verify=False)",
    "privilege escalation via setuid", "credential leak in logs", "leaked the API key",
    "certificate verification disabled", "signature not verified", "auth not required on /admin",
    "heap buffer overflow", "o​pen redirect", "ｏpen redirect"])
def test_security_phrases_and_disguised_text_are_caught(text):
    assert failures.new("canary-held", "o/x", "c0ffee0", summary=text).security


def test_an_org_chosen_repo_name_is_neither_classified_nor_withheld(tmp_path):
    f = failures.new("canary-held", "quirq-ai/auth-gateway", "c0ffee0",
                     run_id="github/quirq-ai/auth-gateway/1/1/canary")
    assert not f.security
    pub = failures.public_view(f)
    assert pub.repo == "quirq-ai/auth-gateway" and pub.run_id == f.run_id
    assert failures.public_value("quirq-ai/auth-gateway@c0ffee0", f.repo) == "quirq-ai/auth-gateway@c0ffee0"
    assert failures.public_value("quirq-ai/exploit-store@c0ffee0", f.repo) == failures.WITHHELD


@pytest.mark.parametrize("repo", ["acme/auth-gateway", "acme/secret-store"])
@pytest.mark.parametrize("where", [
    {"subject": "github/{repo}/1/1/build"},
    {"links": {"fix": "https://github.com/{repo}/pull/5"}},
    {"links": {"culprit": "{repo}@c0ffee0"}},
    {"links": {"covering_test": "https://github.com/{REPO}/commit/c0ffee0"}},
    {"subject": "sha256:" + "a" * 64, "links": {"culprit": "deadbeefcafe0123"}}])
def test_the_own_repo_digests_and_hex_are_not_classified(repo, where):
    def fill(v):
        return v.replace("{repo}", repo).replace("{REPO}", repo.upper())
    f = failures.new("red-run", repo, fill(where.get("subject", "c0ffee0")))
    assert not f.security
    links = {k: fill(v) for k, v in where.get("links", {}).items()}
    assert not failures.looks_security_related(f, links)


@pytest.mark.parametrize("links", [
    {"fix": "https://github.com/acme/auth-gateway-exploit/pull/5"},
    {"culprit": "acme/auth-gateway leaked the API key"},
    {"fix": "acme/other-auth-gateway: sandbox escape"}])
def test_text_around_the_own_repo_is_still_classified(links):
    f = failures.new("red-run", "acme/auth-gateway", "c0ffee0")
    assert failures.new("red-run", "acme/auth-gateway", "github/other/secret-store/1/1/b").security
    assert failures.looks_security_related(f, links)


@pytest.mark.parametrize("text", [
    "cross-site scripting in comments", "Cross Site Scripting", "cross-site request forgery",
    "signature verification skipped", "certificate validation disabled", "TLS check bypassed",
    "token verification bypass", "host validation skipped", "hostname verification disabled",
    "leaked env vars", "leaked environment variables", "debug endpoint exposed",
    "exposed credentials", "secret key exposed", "key leaked in build output",
    "env vars leaked in logs", "account takeover via reset link", "session fixation on login",
    "data exfiltration", "backdoor in dependency", "malware in package",
    "MITM on update channel", "man-in-the-middle", "CVE-2026-12345", "cve 2026 1",
    "access key committed", "AWS key in repo", "alg=none accepted", "log4shell",
    "Log4Shell probe", "heartbleed", "clickjacking on settings"])
def test_round_four_security_phrases_are_caught(text):
    assert failures.new("canary-held", "o/x", "c0ffee0", summary=text).security


@pytest.mark.parametrize("text", [
    "stack overflow in recursion test", "integer overflow in counter", "segfault in worker",
    "SIGABRT in test runner", "out of bounds index in table view",
    "null pointer dereference in handler", "dependency injection container failed",
    "token bucket rate limiter flaky", "tokenizer test failed", "auth service timeout",
    "OAuth callback 502", "sandbox image pull failed", "privileged container required",
    "access control list sync failed", "cors preflight returns 404", "build overflowed disk",
    "segfault in remote cache worker", "fuzzy match test failed", "JWT expired",
    "jwt test flaky", "no author field in changelog", "authorization header test timed out"])
def test_ordinary_crashes_and_names_are_not_security(text):
    assert not failures.new("canary-held", "o/x", "c0ffee0", summary=text).security


@pytest.mark.parametrize("text", [
    "buffer overflow in parser", "heap overflow in decoder", "stack-buffer-overflow in codec",
    "heap-buffer-overflow", "AddressSanitizer: SEGV", "KASAN: use-after-free", "UAF",
    "double free", "use after free", "out-of-bounds write", "OOB write in encoder",
    "OOB read", "heap out-of-bounds read", "out of bounds read in table view",
    "SQL injection", "command injection", "code injection", "template injection",
    "LDAP injection", "XPath injection", "header injection", "log injection",
    "prompt injection", "auth bypass", "sandbox escape", "CORS any origin",
    "CORS misconfiguration", "broken access control", "privilege escalation"])
def test_memory_safety_and_security_phrases_still_match(text):
    assert failures.new("canary-held", "o/x", "c0ffee0", summary=text).security


@pytest.mark.parametrize("text", [
    "segfault in worker on malformed input", "SIGSEGV parsing crafted file",
    "SIGABRT on untrusted payload", "out of bounds index from attacker input",
    "null pointer dereference on remote request", "integer overflow found by fuzz run",
    "SIGBUS while fuzzing", "segfault on packet from the network",
    # or next to an attack surface
    "SIGSEGV in tls handshake", "segfault in decoder", "SIGABRT in libssl",
    "integer overflow in allocation size", "null pointer dereference in x509 parser",
    "stack overflow parsing nested json", "SIGSEGV in image codec", "segfault in font loader"])
def test_ordinary_crashes_are_security_only_on_untrusted_input(text):
    assert failures.new("canary-held", "o/x", "c0ffee0", summary=text).security


def test_a_security_copy_digests_its_subject_and_collect_accepts_it(tmp_path):
    f = failures.new("canary-held", "o/x", "c0ffee0", security=True,
                     run_id="github/o/x/1/1/canary")
    state, _ = failures.open_record(f, tmp_path / "f")
    copy = failures.public_copy(state, tmp_path / "pub")
    pub = failures.read(copy).record
    assert "c0ffee0" not in "".join(p.read_text() for p in copy.rglob("*.json"))
    assert pub.subject == failures.subject_digest("c0ffee0") and pub.id == f.id
    assert github.PUBLIC_SUBJECT.fullmatch(pub.subject)
    again = failures.public_copy(failures.read(copy), tmp_path / "pub2")   # a copy of a copy
    assert failures.read(again).record.subject == pub.subject
    st = FileStore(tmp_path / "store")
    assert _collect(st, {f"{failures.dirname(f.id)}-1-1-a": _zip(copy)})[2] == []
    (stored,) = st.failures()
    assert stored.security and stored.record.subject == pub.subject


@pytest.mark.parametrize("text", [
    "authorization missing on admin route", "missing authorization check", "authz missing on /admin",
    "no authentication on admin api", "admin API has no auth", "broken authentication",
    "access control missing", "privileged container escape", "container breakout",
    "auth token sent over http", "password sent in plaintext", "JWT signature not verified",
    "forged JWT accepted", "jwt alg none"])
def test_missing_auth_escapes_and_weak_jwt_are_caught(text):
    assert failures.new("canary-held", "o/x", "c0ffee0", summary=text).security


def test_cli_link_classifies_without_the_own_repo(tmp_path):
    f = failures.new("canary-held", "acme/secret-store", "c0ffee0")
    assert not f.security
    state, _ = failures.open_record(f, tmp_path)
    assert cli.main(["failure", "link", f.id, "--dir", str(tmp_path),
                     "--fix", "https://github.com/acme/secret-store/pull/5"]) == 0
    st = failures.read(state.path)
    assert not st.security and st.links["fix"] == "https://github.com/acme/secret-store/pull/5"
    assert cli.main(["failure", "link", f.id, "--dir", str(tmp_path),
                     "--culprit", "https://github.com/acme/secret-leak/pull/5"]) == 0
    assert failures.read(state.path).security


@pytest.mark.parametrize("fields", [
    {"stage": "decoder", "signal": "segfault"},
    {"summary": "SIGSEGV | tls handshake"},
    {"summary": "signal=SIGSEGV | component=decoder"},
    {"subject": "tests/tls/test_handshake.py::test_client_hello", "summary": "SIGSEGV"},
    {"subject": "fuzz_x509_parse", "summary": "segfault"},
    {"summary": "segfault in worker", "links": {"culprit": "crafted input"}}])
def test_a_crash_word_counts_with_an_attack_surface_anywhere_in_the_record(fields):
    fields = dict(fields)
    subject, links = fields.pop("subject", "c0ffee0"), fields.pop("links", {})
    f = failures.new("canary-held", "o/x", subject, **fields)
    assert failures.looks_security_related(f, links)


@pytest.mark.parametrize("fields", [
    {"summary": "segfault in worker"}, {"summary": "SIGABRT in test runner"},
    {"stage": "worker", "signal": "health", "summary": "segfault in worker"},
    {"summary": "image pull failed | segfault in worker"}])
def test_a_crash_word_without_an_attack_surface_stays_public(fields):
    assert not failures.new("canary-held", "o/x", "c0ffee0", **fields).security


@pytest.mark.parametrize("label", ["segfault", "sigsegv", "sigabrt", "sigbus", "overflow",
                                   "stack-overflow", "oob", "null-deref"])
def test_a_crash_word_label_is_withheld(label):
    assert failures.public_value(label, "o/x", "signal") == failures.WITHHELD
    pub = failures.public_view(failures.new("canary-held", "o/x", "c0ffee0", stage="decoder",
                                            signal=label))
    assert pub.signal == failures.WITHHELD


@pytest.mark.parametrize("text", [
    "null deref in tls parser", "null pointer deref in decoder", "stack-use-after-return",
    "stack use after scope", "type confusion in JIT", "dangling pointer in cache",
    "arbitrary file read via upload", "arbitrary file write", "pickle.loads of untrusted data",
    "yaml load of untrusted input", "marshal loads untrusted", "untrusted pickle load",
    "untrusted yaml load in importer", "self-signed certificate accepted",
    "self signed cert accepted", "hostname mismatch ignored", "hostname verification skipped",
    "hostname verification disabled", "integer overflow in length check",
    "session token reused after logout", "session still valid after logout",
    "race condition in auth", "race condition in the authorization middleware"])
def test_round_five_security_phrases_are_caught(text):
    assert failures.new("canary-held", "o/x", "c0ffee0", summary=text).security


@pytest.mark.parametrize("text", [
    "overflow in parser on crafted input", "overflow on malformed packet",
    "overflow in tls record length", "length overflow in decoder", "size_t overflow in png decoder",
    "integer overflow in malloc", "integer overflow in buffer size", "segfault in libpng",
    "segfault in zlib inflate", "segfault in json unmarshal", "SIGSEGV in http2 frame handler",
    "segfault in grpc server on large request", "yaml.load on user input"])
def test_overflow_and_codec_crashes_stay_private(text):
    assert failures.new("canary-held", "o/x", "c0ffee0", summary=text).security


@pytest.mark.parametrize("text", [
    "build overflowed disk", "stack overflow in recursion test", "integer overflow in counter",
    "segfault in worker", "SIGABRT in test runner"])
def test_ordinary_overflows_and_crashes_stay_public(text):
    assert not failures.new("canary-held", "o/x", "c0ffee0", summary=text).security


def test_an_overflow_next_to_a_parser_anywhere_fails_closed():
    # Bare "overflow" counts with an attack surface anywhere in the record, even an unrelated one.
    assert failures.new("canary-held", "o/x", "c0ffee0",
                        summary="build overflowed disk | parser tests pass").security
