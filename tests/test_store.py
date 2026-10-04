import io
import json
import zipfile

import pytest

from qqresults import bundle, cli, scorecard, verdict
from qqresults.backends import github
from qqresults.model import CaseVerdict, Change, Failure, Result, Run, Verdict
from qqresults.store import FileStore, RunFilter, StoreError


def make(run_id, kind="postsubmit", commit="c1", finished="2026-10-04T10:00:00Z", fail=False,
         repo="quirq-ai/xo-space", change=None, flaky=False, queued=""):
    run = Run(id=run_id, repo=repo, kind=kind, commit=commit, finished_at=finished, branch="main",
              change=Change(repo=repo, number=change) if change else None, queued_at=queued)
    results = [Result(run_id=run_id, test_id="t::a", status="PASS", expected=True)]
    if fail:
        results.append(Result(run_id=run_id, test_id="t::b", status="FAIL", expected=False))
    v = verdict.compute(run, results)
    if flaky:  # as V0-TST-03's retries record it
        v = Verdict(run_id=run_id, passed=True, counts={"EXPECTED": 1, "FLAKY": 1},
                    tests=[CaseVerdict(test_id="t::c", status="FLAKY")])
    return bundle.Bundle(run, results, v)


def test_put_is_write_once_and_idempotent(tmp_path):
    st = FileStore(tmp_path)
    b = make("r1")
    assert st.put(b) is True
    assert st.put(b) is False
    changed = bundle.Bundle(b.run, b.results[:0], b.verdict)
    with pytest.raises(StoreError, match="write-once"):
        st.put(changed)
    assert st.bundle("r1") == b


def test_import_keeps_the_sink_bytes(tmp_path):
    src = bundle.write(make("r1"), tmp_path / "sink")
    run_json = json.loads((src / "run.json").read_text())
    run_json["field_from_a_newer_sink"] = 1
    (src / "run.json").chmod(0o644)
    (src / "run.json").write_text(json.dumps(run_json))
    st = FileStore(tmp_path / "store")
    assert st.import_dir(src)
    stored = st.runs_dir / src.name / "run.json"
    assert json.loads(stored.read_text())["field_from_a_newer_sink"] == 1


def test_a_bundle_with_raw_from_an_older_sink_still_imports(tmp_path):
    b = make("r1", fail=True)
    raw = '<testcase name="b"><failure message="x"/><system-out>out</system-out></testcase>'
    old = Result.from_dict({**b.results[1].to_dict(), "raw": raw})
    b = bundle.Bundle(b.run, [b.results[0], old], b.verdict)
    src = bundle.write(b, tmp_path / "sink")
    st = FileStore(tmp_path / "store")
    assert st.import_dir(src)
    assert st.bundle("r1").results[1].raw == raw
    assert cli.main(["import", "--store", str(tmp_path / "store"), str(src)]) == 0   # a no-op


def test_a_bundle_without_failure_type_from_an_older_sink_still_imports(tmp_path):
    b = make("r1", fail=True)
    src = bundle.write(b, tmp_path / "sink")
    lines = [json.loads(line) for line in (src / bundle.RESULTS).read_text().splitlines()]
    for line in lines:
        del line["failure_type"]
    (src / bundle.RESULTS).chmod(0o644)
    (src / bundle.RESULTS).write_text("".join(json.dumps(line) + "\n" for line in lines))
    st = FileStore(tmp_path / "store")
    assert st.import_dir(src)
    assert [r.failure_type for r in st.results("r1")] == ["", ""]
    assert "failure_type" not in (st.runs_dir / src.name / bundle.RESULTS).read_text()


def test_import_refuses_a_failure_type_that_is_not_a_string(tmp_path):
    b = make("r1", fail=True)
    src = bundle.write(b, tmp_path / "sink")
    line = {**json.loads((src / bundle.RESULTS).read_text().splitlines()[1]), "failure_type": 3}
    (src / bundle.RESULTS).chmod(0o644)
    (src / bundle.RESULTS).write_text(json.dumps(line) + "\n")
    with pytest.raises(bundle.BundleError, match="Result.failure_type must be a string"):
        FileStore(tmp_path / "store").import_dir(src)


@pytest.mark.parametrize("metric", [
    {"value": True, "unit": "s"},                 # a bool is not a number
    {"value": "1.5", "unit": "s"},
    {"value": float("nan"), "unit": "s"},
    {"value": float("inf"), "unit": "s"},
    {"value": None, "unit": "s"},
    {"unit": "s"},
    {"value": 1.5},
    {"value": 1.5, "unit": ""},
    {"value": 1.5, "unit": "  "},
    {"value": 1.5, "unit": 3},
    1.5,
])
def test_import_refuses_a_malformed_metric(tmp_path, metric):
    b = make("r1")
    r = Result.from_dict({**b.results[0].to_dict(), "metrics": {}})
    src = bundle.write(bundle.Bundle(b.run, [r], b.verdict), tmp_path / "sink")
    line = {**json.loads((src / bundle.RESULTS).read_text()), "metrics": {"size": metric}}
    (src / bundle.RESULTS).chmod(0o644)
    (src / bundle.RESULTS).write_text(json.dumps(line) + "\n")
    with pytest.raises(bundle.BundleError, match="Result.metrics must be an object of {value: finite"):
        FileStore(tmp_path / "store").import_dir(src)
    assert not (tmp_path / "store" / "runs" / src.name).exists()


def test_import_accepts_a_well_formed_metric(tmp_path):
    b = make("r1")
    r = Result.from_dict({**b.results[0].to_dict(),
                          "metrics": {"size": {"value": 1024, "unit": "bytes"},
                                      "time": {"value": 0.5, "unit": "s", "note": "p50"}}})
    src = bundle.write(bundle.Bundle(b.run, [r], b.verdict), tmp_path / "sink")
    st = FileStore(tmp_path / "store")
    assert st.import_dir(src)
    assert st.results("r1")[0].metrics["size"] == {"value": 1024, "unit": "bytes"}


def test_queries(tmp_path):
    st = FileStore(tmp_path)
    st.put(make("r1", finished="2026-10-04T10:00:00Z"))
    st.put(make("r2", kind="gate", commit="c2", finished="2026-10-04T11:00:00Z", fail=True, change=42))
    st.put(make("r3", repo="quirq-ai/innernet", finished="2026-10-04T09:00:00Z"))
    assert [r.id for r, _ in st.runs()] == ["r3", "r1", "r2"]
    assert [r.id for r, _ in st.runs(RunFilter(repo="quirq-ai/xo-space", kind="gate"))] == ["r2"]
    assert [r.id for r, _ in st.runs(RunFilter(failed=True))] == ["r2"]
    assert [r.id for r, _ in st.runs(RunFilter(change=42))] == ["r2"]
    assert [r.id for r, _ in st.runs(RunFilter(since="2026-10-04T10:30:00Z"))] == ["r2"]
    assert [r.id for r, _ in st.runs(RunFilter(commit="c"))] == ["r3", "r1", "r2"]
    assert [(run.id, r.status) for run, r in st.history("t::a")] == [
        ("r3", "PASS"), ("r1", "PASS"), ("r2", "PASS")]
    with pytest.raises(StoreError, match="not in the store"):
        st.results("nope")


def test_cli_import_and_query(tmp_path, capsys):
    src = bundle.write(make("r2", kind="gate", fail=True), tmp_path / "sink")
    assert cli.main(["import", "--store", str(tmp_path / "st"), str(src)]) == 0
    assert cli.main(["import", "--store", str(tmp_path / "st"), str(src)]) == 0
    out = capsys.readouterr().out
    assert "stored:" in out and "already stored:" in out
    assert cli.main(["query", "runs", "--store", str(tmp_path / "st"), "--failed"]) == 0
    assert "FAIL  gate" in capsys.readouterr().out
    assert cli.main(["query", "results", "--store", str(tmp_path / "st"), "--run", "r2",
                     "--unexpected"]) == 0
    assert capsys.readouterr().out == "FAIL   t::b\n"


def zipped(*bundles, mutate=None):
    """An artifact holding the bundles (several: one directory each, as with retries).
    mutate(name, text) may rewrite each file's text, to forge what a sink would never write."""
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as tmp:
        paths = [bundle.write(b, Path(tmp)) for b in bundles]
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for path in paths:
                for f in bundle.FILES:
                    text = (path / f).read_text()
                    z.writestr(f if len(paths) == 1 else f"{path.name}/{f}",
                               mutate(f, text) if mutate else text)
        return paths[0].name, buf.getvalue()


def workflow_run(run_id, repo="o/x", event="push", branch="main", sha="c1", attempts=1,
                 path=".github/workflows/presubmit.yml", head_repo=None):
    return {"id": run_id, "event": event, "path": path, "run_attempt": attempts,
            "head_branch": branch, "head_sha": sha, "created_at": "2026-10-04T09:30:00Z",
            "head_repository": {"full_name": head_repo or repo}}


def _collect_zips(st, repo, zips, runs=(), trust=github.Trust(), default_branch="main",
                  fetched=None, compare=None, now=None):
    """Collect a fake listing: zips are (name, data[, workflow run id[, size]]), listed newest
    first as GitHub does; runs are workflow_run()s (default: a push to main at c1). compare maps
    a commit to its status against the default branch (default: identical, so on it)."""
    by_id = {r["id"]: r for r in runs}
    listing = {"artifacts": [
        {"id": 100 + i, "name": z[0], "expired": False, "archive_download_url": f"https://dl/{i}",
         "created_at": f"2026-10-04T10:00:{i:02d}Z", "size_in_bytes": z[3] if len(z) > 3 else len(z[1]),
         "workflow_run": {"id": z[2] if len(z) > 2 else 1}}
        for i, z in reversed(list(enumerate(zips)))]}

    def get(url, token):
        if fetched is not None:
            fetched.append(url)
        if "/actions/artifacts" in url:
            return json.dumps(listing).encode()
        if "/actions/runs/" in url:
            rid = int(url.rsplit("/", 1)[1])
            return json.dumps(by_id.get(rid) or workflow_run(rid, repo=repo)).encode()
        if url.startswith(f"{github.API}/repos/{repo}/compare/refs/heads/{default_branch}..."):
            sha = url.split("...", 1)[1].split("?", 1)[0]
            return json.dumps({"status": (compare or {}).get(sha, "identical")}).encode()
        if url == f"{github.API}/repos/{repo}":
            return json.dumps({"default_branch": default_branch}).encode()
        return zips[int(url.rsplit("/", 1)[1])][1]
    return github.collect(repo, st, "tok", get=get, trust=trust, now=now)


def test_collect_imports_each_artifact_once(tmp_path):
    a = zipped(make("github/o/x/1/1/postsubmit", repo="o/x"))
    b = zipped(make("github/o/x/2/1/postsubmit", fail=True, repo="o/x"))
    zips = [(*a, 1), (*b, 2), ("qq-results-broken", b"not a zip", 2)]
    fetched = []
    st = FileStore(tmp_path)
    new, old, errors = _collect_zips(st, "o/x", zips, fetched=fetched)
    assert (new, old) == (2, 0) and errors == ["o/x artifact qq-results-broken: not a zip archive"]
    assert sum("/actions/runs/" in u for u in fetched) == 2      # one fetch per workflow run
    fetched.clear()
    new, old, _ = _collect_zips(st, "o/x", zips, fetched=fetched)
    assert (new, old) == (0, 2) and "https://dl/0" not in fetched


def test_collect_skips_other_and_expired_artifacts(tmp_path):
    listing = {"artifacts": [
        {"name": "coverage", "expired": False, "archive_download_url": "https://dl/3"},
        {"name": "qq-results-old", "expired": True, "archive_download_url": "https://dl/4"}]}
    fetched = []

    def get(url, token):
        fetched.append(url)
        return json.dumps(listing).encode()

    assert github.collect("o/x", FileStore(tmp_path), "", get=get) == (0, 0, [])
    assert len(fetched) == 1


def test_collect_refuses_zip_slip(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("../evil", "x")
    _, _, errors = _collect_zips(FileStore(tmp_path), "o/x", [("qq-results-x", buf.getvalue())])
    assert "unsafe path" in errors[0]


def test_bundle_lookup_checks_the_run_id(tmp_path):
    st = FileStore(tmp_path)
    st.put(make("x y"))
    with pytest.raises(StoreError, match="holds run x y"):
        st.bundle("x_y")


def test_collect_cli_warns_and_keeps_going(tmp_path, capsys, monkeypatch):
    seen = []

    def broken(repo, store, token, trust):
        seen.append(trust)
        if repo == "o/bad":
            raise github.GitHubAPIError("HTTP 404")
        return 1, 0, ["o/x artifact z: not a zip archive"]
    monkeypatch.setattr(github, "collect", broken)
    args = ["collect", "--store", str(tmp_path), "--repo", "o/bad", "--repo", "o/x"]
    assert cli.main(args) == 0
    assert cli.main(args + ["--strict"]) == 1
    captured = capsys.readouterr()
    assert "o/x: 1 new, 0 already stored, 1 skipped" in captured.out
    assert "warning: o/bad: HTTP 404" in captured.err
    assert seen[0] == github.Trust()
    assert cli.main(args + ["--workflow", ".github/workflows/perf.yml",
                            "--cross-repo", "o/x"]) == 0
    assert seen[-1] == github.Trust(workflows=(".github/workflows/perf.yml",),
                                    cross_repo=frozenset({"o/x"}))


# --- what collect accepts ---------------------------------------------------------------------

def test_collect_accepts_each_event_with_its_kind(tmp_path):
    st = FileStore(tmp_path)
    gate = make("github/o/x/1/1/presubmit", kind="gate", repo="o/x", queued="2026-10-04T09:00:00Z")
    pre = make("github/o/x/2/1/presubmit", kind="presubmit", commit="merge", repo="o/x", change=7)
    pre = bundle.Bundle(Run.from_dict({**pre.run.to_dict(), "change": {
        "repo": "o/x", "number": 7, "head_sha": "pr-head"}}), pre.results, pre.verdict)
    post = make("github/o/x/3/1/qq-x-postsubmit", repo="o/x")
    canary = make("github/o/x/4/1/canary", kind="canary", repo="o/x")
    runs = [workflow_run(1, event="merge_group", branch="gh-readonly-queue/main/pr-7-abc",
                         path=".github/workflows/qq-x-presubmit.yml"),
            workflow_run(2, event="pull_request", branch="feature", sha="pr-head"),
            workflow_run(3, path=".github/workflows/qq-x-postsubmit.yml"),
            workflow_run(4, event="schedule")]
    new, _, errors = _collect_zips(st, "o/x", [(*zipped(gate), 1), (*zipped(pre), 2),
                                               (*zipped(post), 3), (*zipped(canary), 4)], runs)
    assert (new, errors) == (4, [])
    assert sorted(r.kind for r, _ in st.runs()) == ["canary", "gate", "postsubmit", "presubmit"]


def test_collect_accepts_an_earlier_attempt_retries_and_base_runs(tmp_path):
    main = make("github/o/x/9/1/presubmit", repo="o/x")
    retry = make("github/o/x/9/1/presubmit/retry1", repo="o/x")
    base = make("github/o/x/9/1/presubmit/base", commit="base-sha", repo="o/x")
    base = bundle.Bundle(Run.from_dict({**base.run.to_dict(), "parent": main.run.id,
                                        "role": "base"}), base.results, base.verdict)
    st = FileStore(tmp_path)
    new, _, errors = _collect_zips(st, "o/x", [(*zipped(main, retry, base), 9)],
                                   [workflow_run(9, attempts=2)])
    assert (new, errors) == (1, []) and len(st.runs()) == 3


def test_collect_accepts_a_dispatched_backfill(tmp_path):
    b = make("github/o/x/5/1/postsubmit", commit="old", repo="o/x")
    b = bundle.Bundle(Run.from_dict({**b.run.to_dict(), "role": "backfill"}), b.results, b.verdict)
    st = FileStore(tmp_path)
    assert _collect_zips(st, "o/x", [(*zipped(b), 5)],
                         [workflow_run(5, event="workflow_dispatch")])[::2] == (1, [])
    # Only a dispatched run may name another commit.
    st = FileStore(tmp_path / "2")
    _, _, errors = _collect_zips(st, "o/x", [(*zipped(b), 5)])
    assert "commit 'old' is not c1" in errors[0]


def test_perf_runs_name_another_repo_only_with_cross_repo(tmp_path):
    perf = zipped(make("github/o/perf/2/1/measure/xo", kind="other", commit="xo-sha"))
    runs = [workflow_run(2, repo="o/perf", event="schedule", path=".github/workflows/perf.yml")]
    trust = github.Trust(workflows=(".github/workflows/perf.yml",))
    _, _, errors = _collect_zips(FileStore(tmp_path / "a"), "o/perf", [(*perf, 2)], runs, trust)
    assert "--cross-repo" in errors[0]
    trust = github.Trust(workflows=(".github/workflows/perf.yml",), cross_repo=frozenset({"o/perf"}))
    st = FileStore(tmp_path / "b")
    assert _collect_zips(st, "o/perf", [(*perf, 2)], runs, trust) == (1, 0, [])
    assert [r.repo for r, _ in st.runs()] == ["quirq-ai/xo-space"]
    # Even then, a run naming another repo may only be kind other.
    forged = zipped(make("github/o/perf/3/1/measure/xo", kind="postsubmit", fail=True))
    runs.append(workflow_run(3, repo="o/perf", path=".github/workflows/perf.yml"))
    _, _, errors = _collect_zips(FileStore(tmp_path / "c"), "o/perf", [(*forged, 3)], runs, trust)
    assert "only kind 'other'" in errors[0]


# --- what collect refuses ---------------------------------------------------------------------

def _refused(tmp_path, b, run, trust=github.Trust(), **kw):
    st = FileStore(tmp_path)
    new, _, errors = _collect_zips(st, "o/x", [(*zipped(b, **kw), run["id"])], [run], trust)
    assert new == 0 and len(errors) == 1 and st.runs() == []
    return errors[0]


def test_collect_refuses_a_fork_pull_request(tmp_path):
    b = make("github/o/x/1/1/presubmit", kind="presubmit", repo="o/x", change=7)
    run = workflow_run(1, event="pull_request", head_repo="mallory/x")
    assert "a fork's pull request" in _refused(tmp_path, b, run)


def test_collect_refuses_workflows_not_allowed(tmp_path):
    b = make("github/o/x/1/1/evil", repo="o/x")
    assert "not an allowed workflow" in _refused(
        tmp_path, b, workflow_run(1, path=".github/workflows/evil.yml"))


def test_collect_refuses_forged_gate_timing(tmp_path):
    # A gate run with a made-up queue time, from a run that was not in the merge queue.
    b = make("github/o/x/1/1/presubmit", kind="gate", repo="o/x", queued="2026-10-04T09:59:00Z")
    assert "claims kind 'gate'" in _refused(tmp_path, b, workflow_run(1, event="workflow_dispatch"))


@pytest.mark.parametrize("queued,ok", [
    ("2026-10-03T09:30:00Z", True),     # 24 h before the run was created
    ("2026-10-04T10:00:00Z", True),     # at its finish
    ("2026-10-03T09:29:59Z", False),    # earlier than that
    ("1970-01-01T00:00:00Z", False),    # a runner with no clock
    ("2026-10-04T10:05:00Z", True),     # within the runner's clock skew of its finish
    ("2026-10-04T10:05:01Z", False)])   # after it finished
def test_collect_bounds_a_gate_runs_queue_time(tmp_path, queued, ok):
    b = make("github/o/x/1/1/presubmit", kind="gate", repo="o/x", queued=queued)
    run = {**workflow_run(1, event="merge_group", branch="gh-readonly-queue/main/pr-7-abc"),
           "created_at": "2026-10-04T09:30:00Z"}
    if ok:
        assert _collect_zips(FileStore(tmp_path), "o/x", [(*zipped(b), 1)], [run])[::2] == (1, [])
    else:
        assert f"queued_at {queued} is not between 2026-10-03T09:30:00Z" in _refused(tmp_path, b, run)


def test_collect_refuses_a_queue_time_without_a_creation_time(tmp_path):
    for queued in ("2026-10-04T09:59:00Z", "1970-01-01T00:00:00Z"):
        b = make("github/o/x/1/1/presubmit", kind="gate", repo="o/x", queued=queued)
        run = workflow_run(1, event="merge_group", branch="gh-readonly-queue/main/pr-7-abc")
        run.pop("created_at", None)
        assert "has no creation time" in _refused(tmp_path / queued[:4], b, run)


def test_collect_refuses_forged_red_postsubmits(tmp_path):
    b = make("github/o/x/1/1/presubmit", kind="postsubmit", fail=True, repo="o/x", change=7)
    assert "claims kind 'postsubmit'" in _refused(tmp_path / "pr", b,
                                                  workflow_run(1, event="pull_request"))
    assert "off the default branch" in _refused(tmp_path / "side", b,
                                                workflow_run(1, branch="side"))


def test_collect_refuses_a_run_id_or_commit_its_workflow_run_did_not_produce(tmp_path):
    other_run = make("github/o/x/555/1/postsubmit", fail=True, repo="o/x")
    assert "does not name workflow run 1" in _refused(tmp_path / "a", other_run, workflow_run(1))
    later_attempt = make("github/o/x/1/2/postsubmit", repo="o/x")
    assert "does not name workflow run 1" in _refused(tmp_path / "b", later_attempt,
                                                      workflow_run(1, attempts=1))
    other_commit = make("github/o/x/1/1/postsubmit", commit="c0", fail=True, repo="o/x")
    assert "the commit its workflow run tested" in _refused(tmp_path / "c", other_commit,
                                                            workflow_run(1))
    pr = make("github/o/x/1/1/presubmit", kind="presubmit", repo="o/x", change=7)
    assert "change head" in _refused(tmp_path / "d", pr, workflow_run(1, event="pull_request"))


def test_postsubmit_needs_a_commit_in_the_default_branchs_history(tmp_path):
    b = make("github/o/x/1/1/postsubmit", commit="t1", fail=True, repo="o/x")
    # A tag named main: head_branch is "main", but the commit is not on main (unreviewed).
    for event in ("push", "workflow_dispatch"):
        st = FileStore(tmp_path / event)
        _, _, errors = _collect_zips(st, "o/x", [(*zipped(b), 1)],
                                     [workflow_run(1, event=event, sha="t1")],
                                     compare={"t1": "ahead"})
        assert "off the default branch" in errors[0] and st.runs() == []
    st = FileStore(tmp_path / "diverged")
    _, _, errors = _collect_zips(st, "o/x", [(*zipped(b), 1)], [workflow_run(1, sha="t1")],
                                 compare={"t1": "diverged"})
    assert "off the default branch" in errors[0] and st.runs() == []
    # An older commit of main (behind its head) is on it; the comparison is made once per commit.
    st, fetched = FileStore(tmp_path / "behind"), []
    c = make("github/o/x/2/1/postsubmit", commit="t1", repo="o/x")
    assert _collect_zips(st, "o/x", [(*zipped(b), 1), (*zipped(c), 2)],
                         [workflow_run(1, sha="t1"), workflow_run(2, sha="t1")],
                         compare={"t1": "behind"}, fetched=fetched) == (2, 0, [])
    assert sum("/compare/" in u for u in fetched) == 1


def test_collect_refuses_a_pull_request_the_run_is_not_for(tmp_path):
    pr = make("github/o/x/1/1/presubmit", kind="presubmit", repo="o/x", change=7)
    pr = bundle.Bundle(Run.from_dict({**pr.run.to_dict(), "change": {
        "repo": "o/x", "number": 8, "head_sha": "pr-head"}}), pr.results, pr.verdict)
    run = {**workflow_run(1, event="pull_request", branch="feature", sha="pr-head"),
           "pull_requests": [{"number": 7}]}
    assert "is not a pull request of its workflow run" in _refused(tmp_path / "a", pr, run)
    # GitHub lists no pull requests for some runs; then only the head is checked.
    st = FileStore(tmp_path / "b")
    assert _collect_zips(st, "o/x", [(*zipped(pr), 1)], [{**run, "pull_requests": []}])[::2] \
        == (1, [])


PERF = github.Trust(workflows=(".github/workflows/perf.yml",), cross_repo=frozenset({"o/perf"}))


def _perf(tmp_path, trust=PERF, compare=None, **run):
    """Collect one perf bundle naming another repo, from a run of perf.yml (overridden by run)."""
    b = zipped(make("github/o/perf/2/1/measure/xo", kind="other", commit="xo-sha"))
    wr = workflow_run(2, repo="o/perf", **{"path": ".github/workflows/perf.yml", **run})
    st = FileStore(tmp_path)
    new, _, errors = _collect_zips(st, "o/perf", [(*b, 2)], [wr], trust, compare=compare)
    assert len(st.runs()) == new
    return new, errors


def test_a_cross_repo_source_stores_its_trusted_default_branch_runs(tmp_path):
    for event in ("push", "schedule", "workflow_dispatch"):
        assert _perf(tmp_path / event, event=event) == (1, [])


def test_a_cross_repo_source_needs_its_own_workflow(tmp_path):
    # It has no default workflow globs: without --workflow, nothing of it is read.
    with pytest.raises(github.GitHubAPIError, match="--workflow must name"):
        _perf(tmp_path, github.Trust(cross_repo=frozenset({"o/perf"})))


@pytest.mark.parametrize("trust,run,compare,error", [
    # Only the workflow given for it, never the default globs.
    (PERF, {"path": ".github/workflows/qq-x.yml"}, None, "not an allowed workflow"),
    (PERF, {"path": ".github/workflows/presubmit.yml"}, None, "not an allowed workflow"),
    # Only push, schedule and dispatch runs (a fork's or same-repo PR, the merge queue).
    (PERF, {"event": "pull_request", "branch": "feature"}, None, "--cross-repo source"),
    (PERF, {"event": "pull_request", "branch": "main"}, None, "--cross-repo source"),
    (PERF, {"event": "merge_group"}, None, "--cross-repo source"),
    (PERF, {"event": "pull_request_target"}, None, "--cross-repo source"),
    # Only on the default branch, with a commit in its history.
    (PERF, {"branch": "side"}, None, "--cross-repo source"),
    (PERF, {"sha": "t1"}, {"t1": "ahead"}, "--cross-repo source"),
])
def test_a_cross_repo_source_refuses_everything_else(tmp_path, trust, run, compare, error):
    new, errors = _perf(tmp_path, trust, compare, **run)
    assert new == 0 and error in errors[0]


def test_collect_refuses_unreadable_artifacts_and_keeps_going(tmp_path):
    good = zipped(make("github/o/x/1/1/j", repo="o/x"))
    corrupt = io.BytesIO()
    with zipfile.ZipFile(corrupt, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("run.json", "x" * 1000)
    data = bytearray(corrupt.getvalue())
    start = data.index(b"run.json") + len("run.json")     # the local header's name, then the data
    data[start:start + 8] = b"\xff" * 8
    deep = zipped(make("github/o/x/2/1/j", repo="o/x"),
                  mutate=lambda f, text: "[" * 100_000 + "]" * 100_000 if f == bundle.RUN else text)
    st = FileStore(tmp_path)
    new, _, errors = _collect_zips(st, "o/x", [("qq-results-corrupt", bytes(data), 1),
                                               (*deep, 2), (*good, 1)])
    assert new == 1 and len(errors) == 2
    assert "cannot unzip" in errors[0] and "not a readable results bundle" in errors[1]


def test_a_forged_bundle_cannot_take_a_real_runs_id_first(tmp_path):
    st = FileStore(tmp_path)
    real_id = "github/o/x/555/1/postsubmit"
    squat = zipped(make(real_id, kind="postsubmit", repo="o/x"))
    real = zipped(make(real_id, fail=True, repo="o/x"))
    # The forged one is uploaded by another run (listed later, but read in upload order anyway).
    new, _, errors = _collect_zips(st, "o/x", [(*squat, 1), (*real, 555)])
    assert new == 1 and "does not name workflow run 1" in errors[0]
    assert not st.runs()[0][1].passed


def test_one_repo_cannot_hide_anothers_run_by_taking_its_id(tmp_path):
    st = FileStore(tmp_path)
    real_id = "github/quirq-ai/xo-space/555/1/postsubmit"
    squat = zipped(make(real_id, kind="other"))                 # uploaded by quirq-ai/perf
    trust = github.Trust(workflows=(".github/workflows/presubmit.yml",),
                         cross_repo=frozenset({"quirq-ai/perf"}))
    new, _, errors = _collect_zips(st, "quirq-ai/perf", [(*squat, 1)],
                                   [workflow_run(1, repo="quirq-ai/perf", event="schedule")], trust)
    assert new == 0 and "does not name workflow run" in errors[0]
    new, _, errors = _collect_zips(st, "quirq-ai/xo-space", [(*zipped(make(real_id, fail=True)), 555)])
    assert (new, errors) == (1, []) and not next(iter(st.runs()))[1].passed


def test_artifacts_are_read_oldest_first():
    listing = {"artifacts": [{"name": f"qq-results-{i}", "id": i, "created_at": t}
                             for i, t in [(3, "2026-10-04T10:00:02Z"), (1, "2026-10-04T10:00:00Z"),
                                          (2, "2026-10-04T10:00:01Z")]]}
    arts = github.list_result_artifacts("o/x", "", get=lambda url, token: json.dumps(listing).encode())
    assert [a["id"] for a in arts] == [1, 2, 3]


@pytest.mark.parametrize("field,value,error", [
    ("finished_at", 5, "Run.finished_at must be a string"),
    ("finished_at", "yesterday", "RFC 3339"),
    ("repo", None, "Run.repo must be a string"),
    ("attempt", "1", "Run.attempt must be a non-negative integer"),
    ("results_found", "yes", "Run.results_found must be true or false"),
    ("change", "o/x#1", "Run.change must be an object or null"),
])
def test_collect_refuses_wrongly_typed_runs(tmp_path, field, value, error):
    def mutate(name, text):
        if name != bundle.RUN:
            return text
        return json.dumps({**json.loads(text), field: value})
    assert error in _refused(tmp_path, make("github/o/x/1/1/j", repo="o/x"), workflow_run(1),
                             mutate=mutate)


@pytest.mark.parametrize("name,patch,error", [
    (bundle.RESULTS, {"duration_s": float("inf")}, "Result.duration_s must be a finite"),
    (bundle.RESULTS, {"duration_s": -1}, "Result.duration_s must be a finite"),
    (bundle.RESULTS, {"expected": 1}, "Result.expected must be true or false"),
    (bundle.RESULTS, {"metrics": {"size": {"value": "nan", "unit": 3, "x": None}}}, "metrics"),
    (bundle.VERDICT, {"passed": "false"}, "Verdict.passed must be true or false"),
    (bundle.VERDICT, {"counts": {"EXPECTED": -1}}, "Verdict.counts must be"),
    (bundle.VERDICT, {"tests": [{"test_id": 1, "status": "FLAKY"}]}, "CaseVerdict.test_id"),
])
def test_collect_refuses_wrongly_typed_results_and_verdicts(tmp_path, name, patch, error):
    def mutate(f, text):
        if f != name:
            return text
        return "".join(json.dumps({**json.loads(line), **patch}) + "\n" for line in text.splitlines())
    assert error in _refused(tmp_path, make("github/o/x/1/1/j", repo="o/x"), workflow_run(1),
                             mutate=mutate)


def test_collect_refuses_results_of_another_run(tmp_path):
    def mutate(name, text):
        return text.replace('"run_id":"github/o/x/1/1/j"', '"run_id":"github/o/x/2/1/j"') \
            if name == bundle.RESULTS else text
    assert "name another run" in _refused(tmp_path, make("github/o/x/1/1/j", repo="o/x"),
                                          workflow_run(1), mutate=mutate)


def test_collect_caps_artifact_size_and_results(tmp_path, monkeypatch):
    st = FileStore(tmp_path)
    name, data = zipped(make("github/o/x/1/1/j", repo="o/x"))
    _, _, errors = _collect_zips(st, "o/x", [(name, data, 1, 100 * 1024 * 1024)])
    assert "is not at most" in errors[0]
    bomb = io.BytesIO()
    with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("run.json", " " * (github.MAX_ARTIFACT_BYTES + 1))
    _, _, errors = _collect_zips(st, "o/x", [("qq-results-bomb", bomb.getvalue(), 1)])
    assert "unzips to more than" in errors[0]
    monkeypatch.setattr(github, "MAX_RESULTS", 1)
    b = make("github/o/x/1/1/j", fail=True, repo="o/x")
    assert "more than 1" in _refused(tmp_path / "r", b, workflow_run(1))


# --- failure records --------------------------------------------------------------------------

def _failure_zip(tmp_path, repo="o/x", run_id="github/o/x/7/1/held-canary", subject="planted",
                 fid=None, links=(), public_subject=None):
    from qqresults import failures
    f = failures.new("canary-held", repo, subject, run_id=run_id)
    if fid:
        f = Failure.from_dict({**f.to_dict(), "id": fid})
    if public_subject:     # the public copy, which keeps the id of the raw subject
        f = Failure.from_dict({**f.to_dict(), "subject": public_subject})
    state, _ = failures.open_record(f, tmp_path)
    for field, value in links:
        failures.add_link(state.path, field, value)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for p in state.path.rglob("*"):
            if p.is_file():
                z.write(p, p.relative_to(tmp_path).as_posix())
    return f"{state.path.name}-7-1", buf.getvalue()


DEMO = github.Trust(workflows=(".github/workflows/failure-demo.yml",))


def _demo_run(run_id=7, **kw):
    return workflow_run(run_id, **{"path": ".github/workflows/failure-demo.yml", **kw})


def test_collect_accepts_a_failure_from_a_default_branch_run(tmp_path):
    st = FileStore(tmp_path / "store")
    art = _failure_zip(tmp_path / "f", links=[("culprit", "c0ffee0")])
    for event in ("push", "schedule", "workflow_dispatch"):
        st = FileStore(tmp_path / event)
        assert _collect_zips(st, "o/x", [(*art, 7)], [_demo_run(event=event)], DEMO) == (1, 0, [])
        assert st.failures()[0].links == {"culprit": "c0ffee0"}


@pytest.mark.parametrize("run,kw,error", [
    ({"event": "pull_request", "branch": "feature"}, {}, "not a pull_request run"),
    ({"event": "merge_group"}, {}, "not a merge_group run"),
    ({"branch": "side"}, {}, "default branch"),
    ({"head_repo": "mallory/x"}, {}, "a fork's pull request"),
    ({}, {"repo": "quirq-ai/xo-space"}, "is for 'quirq-ai/xo-space'"),
    ({}, {"run_id": "github/o/x/8/1/held-canary"}, "does not name workflow run 7"),
    ({}, {"run_id": ""}, "does not name workflow run 7"),
    # A real record's id, with links that would close it.
    ({}, {"fid": "canary-held-0123456789abcdef",
          "links": [("culprit", "x"), ("fix", "y"), ("covering_test", "z")]},
     "the id does not match"),
])
def test_collect_refuses_forged_failures(tmp_path, run, kw, error):
    st = FileStore(tmp_path / "store")
    art = _failure_zip(tmp_path / "f", **kw)
    new, _, errors = _collect_zips(st, "o/x", [(*art, 7)], [_demo_run(**run)], DEMO)
    assert new == 0 and error in errors[0] and st.failures() == []
    _, _, errors = _collect_zips(st, "o/x", [(*art, 7)], [_demo_run(**run)])
    assert "not an allowed workflow" in errors[0] or "fork" in errors[0]


def test_collect_refuses_a_failure_from_a_tag_named_like_the_default_branch(tmp_path):
    art = _failure_zip(tmp_path / "f")
    st = FileStore(tmp_path / "store")
    new, _, errors = _collect_zips(st, "o/x", [(*art, 7)], [_demo_run(sha="t1")], DEMO,
                                   compare={"t1": "ahead"})
    assert new == 0 and "runs of a commit on the default branch" in errors[0]
    assert st.failures() == []


def test_a_digested_subject_skips_only_the_id_check(tmp_path):
    art = _failure_zip(tmp_path / "f", subject="free text", public_subject="sha256:0123456789abcdef")
    st = FileStore(tmp_path / "ok")
    assert _collect_zips(st, "o/x", [(*art, 7)], [_demo_run()], DEMO) == (1, 0, [])
    for name, run, kw, error in [
            ("pr", {"event": "pull_request", "branch": "feature"}, {}, "not a pull_request run"),
            ("tag", {"sha": "t1"}, {"compare": {"t1": "ahead"}}, "commit on the default branch"),
            ("workflow", {"path": ".github/workflows/evil.yml"}, {}, "not an allowed workflow")]:
        st = FileStore(tmp_path / name)
        new, _, errors = _collect_zips(st, "o/x", [(*art, 7)], [_demo_run(**run)], DEMO, **kw)
        assert new == 0 and error in errors[0] and st.failures() == []
    for name, kw, error in [
            ("run", {"run_id": "github/o/x/8/1/held-canary"}, "does not name workflow run 7"),
            ("repo", {"repo": "quirq-ai/xo-space"}, "is for 'quirq-ai/xo-space'"),
            # Not exactly the public digest form: the id must match.
            ("form", {"public_subject": "sha256:0123456789ABCDEF"}, "the id does not match")]:
        art = _failure_zip(tmp_path / f"f-{name}", subject="free text",
                           **{"public_subject": "sha256:0123456789abcdef", **kw})
        st = FileStore(tmp_path / name)
        new, _, errors = _collect_zips(st, "o/x", [(*art, 7)], [_demo_run()], DEMO)
        assert new == 0 and error in errors[0] and st.failures() == []


def test_collect_refuses_a_malformed_failure_link(tmp_path):
    import zipfile as zf
    name, data = _failure_zip(tmp_path / "f")
    buf = io.BytesIO(data)
    with zf.ZipFile(buf, "a") as z:
        z.writestr(f"{name.rsplit('-7-1', 1)[0]}/links/x-culprit.json",
                   json.dumps({"field": "culprit", "value": ["x"], "at": "2026-10-04T10:00:00Z"}))
    st = FileStore(tmp_path / "store")
    _, _, errors = _collect_zips(st, "o/x", [(name, buf.getvalue(), 7)], [_demo_run()], DEMO)
    assert "is not a {field, value, at} record" in errors[0]


def _now(minutes=0):
    import datetime as dt
    t = dt.datetime.now(dt.UTC) + dt.timedelta(minutes=minutes)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _link_zip(fid, repo="o/x", run_id="github/o/x/8/1/link", links=(("culprit", "c0ffee1"),),
              schema="quirq-results/1", at=None, bundles=None):
    """A link bundle as the link action uploads it (its contents at the zip's root), by default
    from workflow run 8 and dated now. bundles: several (fid, links) in one artifact instead."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for prefix, (bid, blinks) in ([("", (fid, links))] if bundles is None else
                                      [(f"qq-failure-{b}/", (b, l)) for b, l in bundles]):
            z.writestr(f"{prefix}target.json", json.dumps(
                {"id": bid, "repo": repo, "run_id": run_id, "schema": schema}))
            for i, (field, value) in enumerate(blinks):
                z.writestr(f"{prefix}links/{i}.json", json.dumps(
                    {"field": field, "value": value, "at": at or _now()}))
    return f"qq-failure-{fid}-link-8-1-abcd0123", buf.getvalue()


def test_collect_adds_a_link_bundle_to_its_stored_record(tmp_path):
    from qqresults import failures
    art = _failure_zip(tmp_path / "f")
    fid = failures.failure_id("canary-held", "o/x", "planted")
    st = FileStore(tmp_path / "store")
    assert _collect_zips(st, "o/x", [(*art, 7), (*_link_zip(fid), 8)],
                         [_demo_run(), _demo_run(8)], DEMO) == (2, 0, [])
    assert st.failure(fid).links == {"culprit": "c0ffee1"}


@pytest.mark.parametrize("run,kw,error", [
    ({"event": "pull_request", "branch": "feature"}, {}, "not a pull_request run"),
    ({"event": "merge_group"}, {}, "not a merge_group run"),
    ({"branch": "side"}, {}, "default branch"),
    ({"sha": "t1"}, {}, "commit on the default branch"),
    ({"head_repo": "mallory/x"}, {}, "a fork's pull request"),
    ({}, {"repo": "quirq-ai/xo-space"}, "are for 'quirq-ai/xo-space'"),
    ({}, {"run_id": "github/o/x/7/1/link"}, "does not name workflow run 8"),
    ({}, {"fid": "canary-held-xyz"}, "the id is not <kind>-<16 hex>"),
    ({}, {"schema": "other/1"}, "schema is not"),
    ({}, {"links": (("kind", "x"),)}, "is not a {field, value, at} record"),
    # Only the security mark may travel in a link bundle: refused whole, not stripped.
    ({}, {"links": (("culprit", "c0ffee1"), ("public_summary", "true"))},
     "may not carry the public_summary mark"),
    ({}, {"links": (("security", "false"),)}, "may not carry the security mark"),
    ({}, {"links": (("demo", "true"),)}, "is not a {field, value, at} record"),
    # A link dated far ahead would outrank every later link for good; one before the record
    # was opened cannot be about it.
    ({}, {"at": "9999-12-31T23:59:59Z"}, "is dated 9999-12-31T23:59:59Z, not between"),
    ({}, {"at": "2020-01-01T00:00:00Z"}, "is dated 2020-01-01T00:00:00Z, not between"),
])
def test_collect_refuses_forged_link_bundles(tmp_path, run, kw, error):
    from qqresults import failures
    art = _failure_zip(tmp_path / "f")
    fid = failures.failure_id("canary-held", "o/x", "planted")
    st = FileStore(tmp_path / "store")
    assert _collect_zips(st, "o/x", [(*art, 7)], [_demo_run()], DEMO)[2] == []
    new, _, errors = _collect_zips(st, "o/x", [(*_link_zip(**{"fid": fid, **kw}), 8)],
                                   [_demo_run(), _demo_run(8, **run)], DEMO,
                                   compare={"t1": "ahead"})
    assert new == 0 and error in errors[0] and st.failure(fid).links == {}


def test_a_link_dated_within_the_clock_skew_is_taken(tmp_path):
    from qqresults import failures
    art = _failure_zip(tmp_path / "f")
    fid = failures.failure_id("canary-held", "o/x", "planted")
    st = FileStore(tmp_path / "store")
    assert _collect_zips(st, "o/x", [(*art, 7), (*_link_zip(fid, at=_now(4)), 8)],
                         [_demo_run(), _demo_run(8)], DEMO) == (2, 0, [])


def test_a_failure_artifacts_links_must_be_dated_after_its_opening(tmp_path):
    from qqresults import failures
    name, data = _failure_zip(tmp_path / "f")
    buf = io.BytesIO(data)
    with zipfile.ZipFile(buf, "a") as z:
        z.writestr(f"{name.rsplit('-7-1', 1)[0]}/links/z-culprit.json", json.dumps(
            {"field": "culprit", "value": "c0ffee1", "at": "9999-12-31T23:59:59Z"}))
    st = FileStore(tmp_path / "store")
    new, _, errors = _collect_zips(st, "o/x", [(name, buf.getvalue(), 7)], [_demo_run()], DEMO)
    assert new == 0 and "is dated 9999-12-31T23:59:59Z" in errors[0] and st.failures() == []


def test_an_artifact_of_link_bundles_is_imported_whole_or_not_at_all(tmp_path):
    from qqresults import failures
    art = _failure_zip(tmp_path / "f")
    fid = failures.failure_id("canary-held", "o/x", "planted")
    missing = failures.failure_id("canary-held", "o/x", "never reported")
    st = FileStore(tmp_path / "store")
    assert _collect_zips(st, "o/x", [(*art, 7)], [_demo_run()], DEMO)[2] == []
    both = _link_zip(fid, bundles=[(fid, (("culprit", "c0ffee1"),)),
                                   (missing, (("culprit", "c0ffee2"),))])
    new, _, errors = _collect_zips(st, "o/x", [(*both, 8)], [_demo_run(8)], DEMO)
    assert new == 0 and "not in the store yet" in errors[0]
    assert st.failure(fid).links == {}          # the first bundle was not imported either


def test_a_link_bundle_whose_record_never_comes_is_refused_for_good(tmp_path):
    import datetime as dt
    from qqresults import failures
    fid = failures.failure_id("canary-held", "o/x", "never reported")
    created = dt.datetime(2026, 10, 4, 10, 0, 0, tzinfo=dt.UTC)   # _collect_zips' first artifact
    links = _link_zip(fid, at="2026-10-04T10:00:00Z")
    st = FileStore(tmp_path / "store")
    for days, seen in ((6, False), (8, True)):
        now = created + dt.timedelta(days=days)
        new, old, errors = _collect_zips(st, "o/x", [(*links, 8)], [_demo_run(8)], DEMO, now=now)
        assert new == 0 and old == 0
        assert ("refused for good" in errors[0]) is seen
        assert ("retried at the next collect" in errors[0]) is not seen
    # marked as read: the next collect does not fetch it again
    assert _collect_zips(st, "o/x", [(*links, 8)], [_demo_run(8)], DEMO,
                         now=created + dt.timedelta(days=9)) == (0, 1, [])


def test_a_record_reported_twice_keeps_its_first_report(tmp_path):
    # GitHub lists artifacts newest first; collect reads them oldest first, so the second
    # report (another run, a later opening time) does not replace the first.
    from qqresults import failures
    first = _failure_zip(tmp_path / "a")
    second = _failure_zip(tmp_path / "b", run_id="github/o/x/8/1/held-canary")
    first_record = failures.read(next((tmp_path / "a").iterdir())).record
    second_dir = next((tmp_path / "b").iterdir())
    second_record = failures.read(second_dir).record
    assert first_record.run_id != second_record.run_id
    st = FileStore(tmp_path / "store")
    new, _, errors = _collect_zips(st, "o/x", [(*first, 7), (*second, 8)],
                                   [_demo_run(), _demo_run(8)], DEMO)
    assert errors == [] and new == 1
    (stored,) = st.failures()
    assert stored.record.run_id == first_record.run_id
    assert stored.record.opened_at == first_record.opened_at


# --- a bad stored record never breaks the scorecard -------------------------------------------

def test_scorecard_skips_a_bad_stored_record(tmp_path, capsys):
    import datetime as dt
    st = FileStore(tmp_path)
    st.put(make("good", finished="2026-10-04T10:00:00Z"))
    path = bundle.write(make("bad", finished="2026-10-04T10:00:00Z"), st.runs_dir)
    (path / "run.json").chmod(0o644)
    (path / "run.json").write_text(json.dumps({**json.loads((path / "run.json").read_text()),
                                               "finished_at": 5}))
    (st.failures_dir / "qq-failure-x").mkdir(parents=True)
    (st.failures_dir / "qq-failure-x" / "failure.json").write_text('{"id": 5}')
    until = dt.datetime(2026, 10, 5, tzinfo=dt.UTC)
    card = scorecard.compute(st, until - dt.timedelta(days=7), until)
    assert card.skipped == 2 and list(card.repos) == ["quirq-ai/xo-space"]
    assert "2 stored record(s) could not be read" in scorecard.to_markdown(card)
    assert cli.main(["scorecard", "--store", str(tmp_path), "--days", "7"]) == 0
    assert "warning:" in capsys.readouterr().err


def test_the_history_check_compares_against_the_branch_not_a_tag_of_the_same_name(tmp_path):
    fetched = []
    b = make("github/o/x/1/1/postsubmit", kind="postsubmit")
    _collect_zips(FileStore(tmp_path), "o/x", [zipped(b)], fetched=fetched)
    assert any("/compare/refs/heads/main...c1" in u for u in fetched)


def test_a_cross_repo_uploader_may_be_a_workflow_run_of_its_trusted_workflow(tmp_path):
    trust = github.Trust(workflows=(".github/workflows/perf-publish.yml",),
                         cross_repo=frozenset({"o/perf"}))
    perf = zipped(make("github/o/perf/2/1/publish/xo", kind="other", commit="xo-sha"))
    ok = [workflow_run(2, repo="o/perf", event="workflow_run", path=".github/workflows/perf-publish.yml")]
    assert _collect_zips(FileStore(tmp_path / "a"), "o/perf", [(*perf, 2)], ok, trust) == (1, 0, [])
    for run, why in [
            (workflow_run(2, repo="o/perf", event="workflow_run", branch="side",
                          path=".github/workflows/perf-publish.yml"), "--cross-repo source"),
            (workflow_run(2, repo="o/perf", event="workflow_run",
                          path=".github/workflows/perf.yml"), "not an allowed workflow")]:
        _, _, errors = _collect_zips(FileStore(tmp_path / why[:3]), "o/perf", [(*perf, 2)], [run], trust)
        assert why in errors[0]
    # A same-repo source never takes workflow_run runs.
    _, _, errors = _collect_zips(FileStore(tmp_path / "d"), "o/x",
                                 [(*zipped(make("github/o/x/2/1/j", kind="postsubmit")), 2)],
                                 [workflow_run(2, event="workflow_run")])
    assert errors


@pytest.mark.parametrize("fid", ["canary-rollback-0123456789abcdef", "canary-held-not-hex"])
def test_a_digested_failure_record_must_still_have_an_id_of_its_kind(tmp_path, fid):
    art = _failure_zip(tmp_path / "f", subject="free text", fid=fid,
                       public_subject="sha256:0123456789abcdef")
    st = FileStore(tmp_path / "store")
    new, _, errors = _collect_zips(st, "o/x", [(*art, 7)], [_demo_run()], DEMO)
    assert new == 0 and "the id does not match" in errors[0] and st.failures() == []
