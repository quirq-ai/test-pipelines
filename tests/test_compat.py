import datetime as dt
import io
import json
import subprocess
import zipfile

import pytest

from qqresults import bundle, cli, compat, scorecard, verdict
from qqresults.backends import github
from qqresults.model import Result, Run
from qqresults.store import FileStore

from test_store import _collect_zips, workflow_run, zipped

QQ, CFG = "a" * 40, "b" * 40
API = github.API
REPO = "o/test-pipelines"
ORIGIN = {"repo": REPO, "run_id": 9, "attempt": 1, "at": "2026-10-20T07:00:00Z"}


# --- the plan ------------------------------------------------------------------------------

def test_read_skip(tmp_path):
    p = tmp_path / "skip.toml"
    p.write_text('[skip]\n".github" = "script only"\n')
    assert compat.read_skip(p) == {".github": "script only"}
    for bad in ('[skip]\nx = ""\n', '[skip]\n"a b" = "why"\n', 'other = 1\n', "[skip\n"):
        p.write_text(bad)
        with pytest.raises(compat.CompatError):
            compat.read_skip(p)


def _repo(name, **kw):
    return {"name": name, "default_branch": "main", "private": False, "archived": False, **kw}


def test_plan_checks_every_public_repo_but_the_skipped_and_archived():
    heads = {"b": "c" * 40, "a": "d" * 40}
    listing = [_repo("b"), _repo("a"), _repo("secret", private=True), _repo("old", archived=True),
               _repo("empty", default_branch="trunk"), _repo("fork")]
    p = compat.plan("o", listing, {"fork": "not ours"}, lambda r, b: heads.get(r, ""), QQ, CFG)
    assert [r["repo"] for r in p["repos"]] == ["a", "b", "empty"]
    assert p["repos"][2] == {"repo": "empty", "branch": "trunk", "sha": ""}
    assert p["skipped"] == [{"repo": "fork", "reason": "not ours"},
                            {"repo": "old", "reason": "archived"}]
    assert compat.read_plan(json.loads(json.dumps(p))) == p


@pytest.mark.parametrize("listing,head,qq,error", [
    ([_repo("a b")], "", QQ, "unexpected name"),
    ([_repo("a", default_branch="")], "", QQ, "default branch"),
    ([_repo("a")], "main", QQ, "not a full commit"),
    ([_repo("a")], "", "main", "not a full commit"),
    ([_repo("a", private=True)], "", QQ, "no public repo"),
])
def test_plan_refuses_what_it_cannot_check(listing, head, qq, error):
    with pytest.raises(compat.CompatError, match=error):
        compat.plan("o", listing, {}, lambda r, b: head, qq, CFG)


@pytest.mark.parametrize("change", [
    {"schema": "x"}, {"qq": "main"}, {"repos": []},
    {"repos": [{"repo": "a", "branch": "main", "sha": "c1"}]},
    {"repos": [{"repo": "a", "branch": "main", "sha": ""}] * 2},
    {"skipped": [{"repo": "a"}]},
])
def test_read_plan_checks_every_field(change):
    good = {"schema": compat.PLAN_SCHEMA, "org": "o", "qq": QQ, "infra_config": CFG,
            "repos": [{"repo": "a", "branch": "main", "sha": ""}], "skipped": []}
    compat.read_plan(good)
    with pytest.raises(compat.CompatError):
        compat.read_plan({**good, **change})


def _proc(rc, out="", err=""):
    return subprocess.CompletedProcess([], rc, out, err)


def test_ls_remote():
    sha = "c" * 40
    assert compat.ls_remote("u", "refs/heads/main",
                            lambda *a, **k: _proc(0, f"{sha}\trefs/heads/main\n")) == sha
    assert compat.ls_remote("u", "refs/heads/main", lambda *a, **k: _proc(2)) == ""
    with pytest.raises(compat.CompatError, match="exit 128"):
        compat.ls_remote("u", "refs/heads/main", lambda *a, **k: _proc(128, err="denied"))


def test_make_plan_reads_the_org_and_the_heads(tmp_path):
    skip = tmp_path / "skip.toml"
    skip.write_text('[skip]\nb = "why"\n')
    asked = []

    def get(url, token):
        asked.append(url)
        return json.dumps([_repo("a"), _repo("b")]).encode()

    def run(argv, **kw):
        return _proc(0, f"{'e' * 40}\t{argv[-1]}\n")
    p = compat.make_plan(API, "https://github.com", "o", skip, "tok", get, run)
    assert asked == [f"{API}/orgs/o/repos?type=public&per_page=100&page=1"]
    assert p["qq"] == p["infra_config"] == "e" * 40
    assert p["repos"] == [{"repo": "a", "branch": "main", "sha": "e" * 40}]


# --- the kind commands ---------------------------------------------------------------------

KINDS = {"kind": [
    {"name": "k1", "capabilities": ["fetch", "build", "run"], "interim": {"fetch": "f1", "build": "b1"}},
    {"name": "k2", "capabilities": ["fetch", "test"], "interim": {"fetch": "f2", "test": "t2"}},
    {"name": "k3", "capabilities": ["build", "deploy"]},
]}


def test_kind_commands_run_in_capability_order_as_generated_ci_does():
    manifest = {"targets": [{"name": "a", "kind": "k2"}, {"name": "b", "kind": "k1"},
                            {"name": "c", "kind": "k2"}, {"name": "d", "kind": "k3"}]}
    assert compat.kind_commands(manifest, KINDS) == [
        ("fetch", "k2", "f2"), ("fetch", "k1", "f1"), ("test", "k2", "t2"), ("build", "k1", "b1")]
    assert compat.kind_commands({"targets": [{"name": "d", "kind": "k3"}]}, KINDS) == []
    with pytest.raises(compat.CompatError, match="not in infra-config"):
        compat.kind_commands({"targets": [{"name": "x", "kind": "nope"}]}, KINDS)


def test_run_kind_commands_stops_at_the_first_failure(tmp_path, capsys):
    ran = []

    def run(argv, cwd):
        ran.append(argv[-1])
        assert cwd == tmp_path
        return _proc(3 if argv[-1] == "b1" else 0)
    steps = [("fetch", "k1", "f1"), ("build", "k1", "b1"), ("test", "k2", "t2")]
    assert compat.run_kind_commands(steps, tmp_path, run) == 3
    assert ran == ["f1", "b1"]
    assert "::error::build (k1) failed with exit code 3: b1" in capsys.readouterr().out


def test_run_kind_commands_runs_a_real_shell(tmp_path):
    assert compat.run_kind_commands([("build", "k", "false; true")], tmp_path) == 1
    assert compat.run_kind_commands([("build", "k", "touch made")], tmp_path) == 0
    assert (tmp_path / "made").is_file()


def test_cli_prints_the_commands(tmp_path, capsys):
    (tmp_path / "k.json").write_text(json.dumps(KINDS))
    (tmp_path / "m.json").write_text(json.dumps({"targets": [{"name": "a", "kind": "k1"}]}))
    assert cli.main(["compat", "commands", "--kinds", str(tmp_path / "k.json"),
                     "--manifest", str(tmp_path / "m.json")]) == 0
    assert capsys.readouterr().out == "fetch (k1): f1\nbuild (k1): b1\n"


# --- publishing ----------------------------------------------------------------------------

def _plan(*repos):
    return {"schema": compat.PLAN_SCHEMA, "org": "o", "qq": QQ, "infra_config": CFG,
            "repos": [{"repo": r, "branch": "main", "sha": "c" * 40} for r in repos],
            "skipped": [{"repo": "z", "reason": "why"}]}


def _job(repo, conclusion="success", steps=None, **kw):
    steps = steps if steps is not None else {s: "success" for s in compat.STEPS}
    return {"name": compat.job_name(repo), "conclusion": conclusion, "run_attempt": 1,
            "html_url": f"https://github.com/{REPO}/actions/runs/5/job/{repo}",
            "started_at": "2026-10-20T05:50:00Z", "completed_at": "2026-10-20T05:55:00Z",
            "steps": [{"name": "set up job", "conclusion": "success"}]
            + [{"name": n, "conclusion": c} for n, c in steps.items()], **kw}


def _verdicts(jobs, plan=None):
    out = {}
    for b in compat.bundles(plan or _plan(*{j["name"][8:-1] for j in jobs}), jobs, ORIGIN):
        out[b.run.id.removeprefix(f"github/{REPO}/9/1/compat/")] = b
    return out


def test_a_repo_passes_only_when_every_step_did():
    ok = {s: "success" for s in compat.STEPS}
    b = _verdicts([_job("a", steps={**ok, "qq: build failed": "success", "qq: passed": "skipped"})])
    assert b["a"].verdict.passed and b["a"].run.role == compat.ROLE
    assert b["a"].run.repo == "o/a" and b["a"].run.kind == "other"
    assert b["a"].run.commit == "c" * 40 and b["a"].run.config_revision == CFG
    assert b["a"].run.executor == f"qq@{QQ}" and b["a"].run.job == "compat (a)"
    assert not b["a/qq"].verdict.passed and b["a/qq"].run.role == compat.QQ_ROLE
    assert {r.test_id: r.status for r in b["a/qq"].results} == {"qq::build": "FAIL",
                                                                "qq::test": "SKIP"}


@pytest.mark.parametrize("job,why", [
    (None, "no job for this repo"),
    (_job("a", "failure", {"check out": "success", "manifest present": "failure",
                           "manifest valid": "skipped"}), ""),
    (_job("a", "failure", {"set up node": "failure"}), "failed at 'set up node'"),
    (_job("a", "cancelled", {s: "success" for s in compat.STEPS[:2]}), "job cancelled"),
    (_job("a", None, {}), "job not finished"),
    (_job("a", "success", {s: "success" for s in compat.STEPS[:4]}), "a step did not run"),
])
def test_a_missing_or_broken_job_never_reads_as_a_pass(job, why):
    b = _verdicts([job] if job else [], _plan("a"))
    assert not b["a"].verdict.passed and "a/qq" not in b
    crash = [r.message for r in b["a"].results if r.test_id == "compat::job"]
    assert crash == ([why] if why else [])
    if not job:
        assert b["a"].run.finished_at == ORIGIN["at"]


def test_qq_timing_out_is_qqs_gap_not_the_repos():
    ok = {s: "success" for s in compat.STEPS}
    b = _verdicts([_job("a", steps={**ok, "qq: test timed out": "success"}),
                   _job("b", "failure", {**ok, "kind commands": "failure",
                                         "qq: build timed out": "success"})])
    assert b["a"].verdict.passed and not b["a/qq"].verdict.passed
    assert {r.test_id: r.status for r in b["b/qq"].results} == {"qq::build": "CRASH",
                                                                "qq::test": "SKIP"}
    assert not b["b"].verdict.passed


def test_two_jobs_with_one_name_are_not_a_verdict():
    b = _verdicts([_job("a"), _job("a")], _plan("a"))
    assert not b["a"].verdict.passed


def _zip(name, data):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(name, data)
    return buf.getvalue()


def _api(jobs, plan, artifacts=None):
    """A fake Actions API for compat run 5: its jobs and its plan artifact."""
    plan_job = {"name": "plan", "conclusion": "success", "run_attempt": 1,
                "started_at": "2026-10-20T05:47:30Z", "completed_at": "2026-10-20T05:49:00Z",
                "steps": []}
    arts = artifacts if artifacts is not None else [
        {"name": "compat-plan-1", "created_at": "2026-10-20T05:48:50Z", "expired": False,
         "archive_download_url": f"{API}/repos/{REPO}/actions/artifacts/1/zip"}]

    def get(url, token):
        if "/actions/runs/5/jobs?" in url:
            return json.dumps({"jobs": [plan_job, *jobs]}).encode()
        if "/actions/runs/5/artifacts?" in url:
            assert url.endswith("&name=compat-plan-1")
            return json.dumps({"artifacts": arts}).encode()
        if url.endswith("/artifacts/1/zip"):
            return _zip(compat.PLAN_FILE, json.dumps(plan))
        raise AssertionError(url)
    return get


def test_publish_writes_one_artifact_of_bundles(tmp_path):
    get = _api([_job("a"), _job("b", "failure", {"check out": "failure"})], _plan("a", "b"))
    path = compat.publish(API, REPO, 5, ORIGIN, "tok", tmp_path, get, github._unzip)
    assert path.name == "qq-compat-5-9-1"
    read = [bundle.read(d) for d in sorted(path.iterdir())]
    assert [(b.run.repo, b.verdict.passed) for b in read] == [("o/a", True), ("o/b", False)]


@pytest.mark.parametrize("artifacts,error", [
    ([], "0 artifacts named compat-plan-1"),
    ([{"name": "compat-plan-1", "created_at": "2026-10-20T05:48:00Z",
       "archive_download_url": f"{API}/x"}] * 2, "2 artifacts"),
    # Replaced by a later job (upload-artifact's overwrite deletes and re-creates it).
    ([{"name": "compat-plan-1", "created_at": "2026-10-20T05:52:00Z",
       "archive_download_url": f"{API}/x"}], "was not uploaded by the plan job"),
    ([{"name": "compat-plan-1", "archive_download_url": f"{API}/x"}], "was not uploaded"),
    # Uploaded under this attempt's name by a job of an earlier attempt.
    ([{"name": "compat-plan-1", "created_at": "2026-10-20T05:40:00Z",
       "archive_download_url": f"{API}/x"}], "was not uploaded by the plan job"),
    ([{"name": "compat-plan-1", "created_at": "2026-10-20T05:48:00Z",
       "archive_download_url": "https://elsewhere/x"}], "no download URL"),
])
def test_publish_takes_only_the_plan_jobs_own_artifact(tmp_path, artifacts, error):
    get = _api([_job("a")], _plan("a"), artifacts)
    with pytest.raises(compat.CompatError, match=error):
        compat.publish(API, REPO, 5, ORIGIN, "tok", tmp_path, get, github._unzip)


def test_publish_needs_a_successful_plan_job(tmp_path):
    def get(url, token):
        return json.dumps({"jobs": [{"name": "plan", "conclusion": "failure"}]}).encode()
    with pytest.raises(compat.CompatError, match="no successful plan job"):
        compat.publish(API, REPO, 5, ORIGIN, "tok", tmp_path, get, github._unzip)


# --- collect and the scorecard -------------------------------------------------------------

TP = "o/test-pipelines"
TRUST = github.Trust(workflows=(".github/workflows/compat-publish.yml",), cross_repo=frozenset({TP}))
PUBLISH_RUN = workflow_run(9, repo=TP, event="workflow_run", sha="t1",
                           path=".github/workflows/compat-publish.yml")


def _artifact(tmp_path, plan, jobs):
    path = compat.publish(API, REPO, 5, ORIGIN, "tok", tmp_path / "pub", _api(jobs, plan),
                          github._unzip)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for d in sorted(path.iterdir()):
            for f in bundle.FILES:
                z.writestr(f"{d.name}/{f}", (d / f).read_text())
    return path.name, buf.getvalue()


def test_collect_stores_compat_verdicts_once_including_its_own_repo(tmp_path):
    art = _artifact(tmp_path, {**_plan("test-pipelines", "a")},
                    [_job("test-pipelines"), _job("a", steps={**{s: "success" for s in compat.STEPS},
                                                              "qq: test failed": "success"})])
    st = FileStore(tmp_path / "st")
    fetched = []
    assert _collect_zips(st, TP, [(*art, 9)], [PUBLISH_RUN], TRUST, fetched=fetched,
                         prefixes=(compat.ARTIFACT_PREFIX,))[::2] == (1, [])
    assert {r.repo for r, _ in st.runs()} == {"o/test-pipelines", "o/a"}
    assert len(st.runs()) == 3      # a, its qq path, and test-pipelines itself
    fetched.clear()
    # Marked as read: not downloaded again.
    assert _collect_zips(st, TP, [(*art, 9)], [PUBLISH_RUN], TRUST, fetched=fetched,
                         prefixes=(compat.ARTIFACT_PREFIX,))[:2] == (0, 1)
    assert not any(u.startswith("https://dl/") for u in fetched)


@pytest.mark.parametrize("trust,run,error", [
    # The verdict on its own repo is taken only from a --cross-repo source's workflow_run.
    (github.Trust(workflows=(".github/workflows/compat-publish.yml",)), PUBLISH_RUN, "claims kind"),
    (TRUST, {**PUBLISH_RUN, "path": ".github/workflows/presubmit.yml"}, "not an allowed workflow"),
    (TRUST, {**PUBLISH_RUN, "head_branch": "side"}, "--cross-repo source"),
])
def test_collect_refuses_compat_verdicts_from_anything_else(tmp_path, trust, run, error):
    art = _artifact(tmp_path, _plan("test-pipelines"), [_job("test-pipelines")])
    refused = []
    st = FileStore(tmp_path / "st")
    by_id = {r["id"]: r for r in [run]}

    def get(url, token):
        if "/actions/artifacts" in url:
            return json.dumps({"artifacts": [{"id": 1, "name": art[0], "expired": False,
                                              "archive_download_url": "https://dl/0",
                                              "created_at": "2026-10-20T07:00:00Z",
                                              "size_in_bytes": len(art[1]),
                                              "workflow_run": {"id": 9}}]}).encode()
        if "/actions/runs/" in url:
            return json.dumps(by_id[int(url.rsplit("/", 1)[1])]).encode()
        if "/compare/" in url:
            return json.dumps({"status": "identical"}).encode()
        if url == f"{API}/repos/{TP}":
            return json.dumps({"default_branch": "main"}).encode()
        return art[1]
    new, _, errors = github.collect(TP, st, "tok", get=get, trust=trust, refused=refused,
                                    prefixes=(compat.ARTIFACT_PREFIX,))
    assert new == 0 and error in (errors + refused)[0]


def _role_bundle(run_id, repo, kind, role):
    run = Run(id=run_id, repo=repo, kind=kind, commit="c1", finished_at="2026-10-04T10:00:00Z",
              branch="main", role=role)
    results = [Result(run_id=run_id, test_id=f"compat::{s}", status="PASS", expected=True)
               for s in compat.STEPS]
    return bundle.Bundle(run, results, verdict.compute(run, results))


PERF = "o/perf"
PERF_TRUST = github.Trust(workflows=(".github/workflows/perf-publish.yml",),
                          cross_repo=frozenset({PERF}))
PERF_RUN = workflow_run(7, repo=PERF, event="workflow_run",
                        path=".github/workflows/perf-publish.yml")


@pytest.mark.parametrize("repo,b,runs,trust", [
    # A builder's own bundle claiming a compat role: its repo's Compatibility row.
    ("o/x", _role_bundle("github/o/x/1/1/postsubmit", "o/x", "postsubmit", compat.ROLE), (),
     github.Trust()),
    ("o/x", _role_bundle("github/o/x/1/1/postsubmit/qq", "o/x", "postsubmit", compat.QQ_ROLE), (),
     github.Trust()),
    # Another cross-repo uploader (perf's) naming any repo with a compat role.
    (PERF, _role_bundle("github/o/perf/7/1/publish/x", "o/a", "other", compat.ROLE), [PERF_RUN],
     PERF_TRUST),
    # compat-publish writing anything but a compat role would feed the per-repo metrics.
    (TP, _role_bundle("github/o/test-pipelines/9/1/compat/a", "o/a", "other", ""), [PUBLISH_RUN],
     TRUST),
])
def test_only_compat_publish_writes_compat_roles(tmp_path, repo, b, runs, trust):
    st = FileStore(tmp_path)
    new, _, errors = _collect_zips(st, repo, [(*zipped(b), runs[0]["id"] if runs else 1)], runs,
                                   trust)
    assert new == 0 and len(errors) == 1 and "compatibility roles" in errors[0]
    assert compat.table(st, "2026-10-01T00:00:00Z", "2026-10-21T00:00:00Z") is None


def test_the_default_listing_leaves_compat_artifacts_to_their_own_pass():
    listing = {"artifacts": [{"name": "qq-compat-5-9-1"}, {"name": "qq-results-x"}]}
    get = lambda url, token: json.dumps(listing).encode()
    assert [a["name"] for a in github.list_result_artifacts("o/x", "", get)] == ["qq-results-x"]
    assert [a["name"] for a in github.list_result_artifacts(
        "o/x", "", get, prefixes=(compat.ARTIFACT_PREFIX,))] == ["qq-compat-5-9-1"]


def test_the_scorecard_shows_compat_in_its_own_table(tmp_path):
    st = FileStore(tmp_path)
    ok = {s: "success" for s in compat.STEPS}
    jobs = [_job("a", steps={**ok, "qq: passed": "success"}),
            _job("b", "failure", {"check out": "success", "manifest present": "failure"}),
            _job("c", steps={**ok, "qq: build failed": "success"})]
    for b in compat.bundles(_plan("a", "b", "c"), jobs, ORIGIN):
        st.put(b)
    since, until = dt.datetime(2026, 10, 14, tzinfo=dt.UTC), dt.datetime(2026, 10, 21, tzinfo=dt.UTC)
    card = scorecard.compute(st, since, until)
    assert card.repos == {}     # compat verdicts make no per-repo metric rows
    card.compat = compat.table(st, scorecard.fmt_time(since), scorecard.fmt_time(until),
                               {"z": "why"})
    rows = {r["repo"]: (r["compatible"], r["failed_at"], r["qq"]) for r in card.compat["rows"]}
    assert rows == {"o/a": (True, "", "passes"), "o/b": (False, "manifest present", "not reached"),
                    "o/c": (True, "", "build fails")}
    text = scorecard.to_markdown(card)
    assert "2 of 3 repos compatible; 1 pass qq build and test." in text
    assert "| o/b | ccccccc | no | manifest present | not reached | 2026-10-20T05:55:00Z |" in text
    assert "Not checked: z (why)." in text
    assert json.loads(json.dumps(card.to_dict()))["compat"]["compatible"] == 2


def test_the_table_takes_each_repos_latest_verdict(tmp_path):
    st = FileStore(tmp_path)
    old = compat.bundles(_plan("a"), [_job("a", "failure", {"check out": "failure"})],
                         {**ORIGIN, "run_id": 8})
    new = compat.bundles(_plan("a"), [_job("a", completed_at="2026-10-20T06:30:00Z")], ORIGIN)
    for b in old + new:
        st.put(b)
    t = compat.table(st, "2026-10-14T00:00:00Z", "2026-10-21T00:00:00Z")
    assert [(r["repo"], r["compatible"]) for r in t["rows"]] == [("o/a", True)]
    assert compat.table(FileStore(tmp_path / "empty"), "2026-10-14T00:00:00Z",
                        "2026-10-21T00:00:00Z") is None


def test_a_repo_the_latest_check_did_not_check_is_never_counted(tmp_path):
    st = FileStore(tmp_path)
    ok = {s: "success" for s in compat.STEPS}
    week_ago = [_job(r, steps=ok, completed_at="2026-10-14T05:55:00Z") for r in ("a", "b", "z")]
    for b in compat.bundles(_plan("a", "b", "z"), week_ago, {**ORIGIN, "run_id": 8}):
        st.put(b)
    # The latest check had only a: b is gone (archived, renamed or its plan entry lost), and z is
    # in the skip list since.
    for b in compat.bundles(_plan("a"), [_job("a", "failure", {"check out": "failure"})], ORIGIN):
        st.put(b)
    t = compat.table(st, "2026-10-14T00:00:00Z", "2026-10-21T00:00:00Z", {"z": "why"})
    assert [(r["repo"], r["compatible"]) for r in t["rows"]] == [("o/a", False)]
    assert t["compatible"] == 0 and t["checked_at"] == "2026-10-20T05:55:00Z"
    assert t["not_in_latest"] == [{"repo": "o/b", "last_checked": "2026-10-14T05:55:00Z"}]
    text = "\n".join(compat.markdown(t))
    assert "The latest check finished 2026-10-20T05:55:00Z. 0 of 1 repos compatible" in text
    assert ("Not in the latest check, so not counted: o/b (last checked 2026-10-14T05:55:00Z)."
            in text)
    assert "| o/z |" not in text and "Not checked: z (why)." in text
