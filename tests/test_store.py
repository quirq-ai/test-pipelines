import io
import json
import zipfile

import pytest

from qqresults import bundle, cli, verdict
from qqresults.backends import github
from qqresults.model import CaseVerdict, Change, Result, Run, Verdict
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


def zipped(b):
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as tmp:
        path = bundle.write(b, Path(tmp))
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for f in bundle.FILES:
                z.write(path / f, f)
        return path.name, buf.getvalue()


def test_collect_imports_each_artifact_once(tmp_path):
    name1, zip1 = zipped(make("github/o/x/1/1/presubmit", repo="o/x"))
    name2, zip2 = zipped(make("github/o/x/2/1/presubmit", fail=True, repo="o/x"))
    listing = {"artifacts": [
        {"name": name1, "expired": False, "archive_download_url": "https://dl/1"},
        {"name": name2, "expired": False, "archive_download_url": "https://dl/2"},
        {"name": "coverage", "expired": False, "archive_download_url": "https://dl/3"},
        {"name": "qq-results-old", "expired": True, "archive_download_url": "https://dl/4"},
        {"name": "qq-results-broken", "expired": False, "archive_download_url": "https://dl/5"},
    ]}
    fetched = []

    def get(url, token):
        fetched.append(url)
        if "/actions/artifacts" in url:
            return json.dumps(listing).encode()
        return {"https://dl/1": zip1, "https://dl/2": zip2, "https://dl/5": b"not a zip"}[url]

    st = FileStore(tmp_path)
    new, old, errors = github.collect("o/x", st, "tok", get=get)
    assert (new, old) == (2, 0) and errors == ["o/x artifact qq-results-broken: not a zip archive"]
    assert "https://dl/3" not in fetched and "https://dl/4" not in fetched
    fetched.clear()
    new, old, errors = github.collect("o/x", st, "tok", get=get)
    assert (new, old) == (0, 2) and fetched.count("https://dl/1") == 0


def test_collect_refuses_zip_slip(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("../evil", "x")

    def get(url, token):
        if "/actions/artifacts" in url:
            return json.dumps({"artifacts": [{"name": "qq-results-x", "archive_download_url": "u"}]}).encode()
        return buf.getvalue()

    _, _, errors = github.collect("o/x", FileStore(tmp_path), "", get=get)
    assert "unsafe path" in errors[0]


def test_bundle_lookup_checks_the_run_id(tmp_path):
    st = FileStore(tmp_path)
    st.put(make("x y"))
    with pytest.raises(StoreError, match="holds run x y"):
        st.bundle("x_y")


def test_collect_cli_warns_and_keeps_going(tmp_path, capsys, monkeypatch):
    def broken(repo, store, token):
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


def test_collect_refuses_runs_one_repo_files_for_another(tmp_path):
    forged, forged_zip = zipped(make("github/o/perf/1/1/x", kind="postsubmit", fail=True))
    perf, perf_zip = zipped(make("github/o/perf/2/1/x", kind="other"))   # how perf names xo-space
    listing = {"artifacts": [
        {"name": forged, "expired": False, "archive_download_url": "https://dl/1"},
        {"name": perf, "expired": False, "archive_download_url": "https://dl/2"}]}

    def get(url, token):
        if "/actions/artifacts" in url:
            return json.dumps(listing).encode()
        return {"https://dl/1": forged_zip, "https://dl/2": perf_zip}[url]

    st = FileStore(tmp_path)
    new, old, errors = github.collect("o/perf", st, "tok", get=get)
    assert (new, old) == (1, 0) and len(errors) == 1 and "only kind 'other'" in errors[0]
    assert [r.kind for r, _ in st.runs()] == ["other"]


def _collect_zips(st, repo, zips):
    listing = {"artifacts": [{"name": n, "expired": False, "archive_download_url": f"https://dl/{i}"}
                             for i, (n, _) in enumerate(zips)]}

    def get(url, token):
        if "/actions/artifacts" in url:
            return json.dumps(listing).encode()
        return zips[int(url.rsplit("/", 1)[1])][1]
    return github.collect(repo, st, "tok", get=get)


def test_one_repo_cannot_hide_anothers_run_by_taking_its_id(tmp_path):
    st = FileStore(tmp_path)
    real_id = "github/quirq-ai/xo-space/555/1/postsubmit"
    squat = zipped(make(real_id, kind="other"))                 # uploaded by quirq-ai/perf
    new, _, errors = _collect_zips(st, "quirq-ai/perf", [squat])
    assert new == 0 and "not one of its runs" in errors[0]
    new, _, errors = _collect_zips(st, "quirq-ai/xo-space", [zipped(make(real_id, fail=True))])
    assert (new, errors) == (1, []) and not next(iter(st.runs()))[1].passed


def test_a_malformed_run_is_skipped_not_fatal(tmp_path):
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as tmp:
        path = bundle.write(make("github/o/x/1/1/j", repo="o/x"), Path(tmp))
        run = json.loads((path / "run.json").read_text())
        (path / "run.json").chmod(0o644)
        (path / "run.json").write_text(json.dumps({**run, "repo": None}))
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for f in bundle.FILES:
                z.write(path / f, f)
    ok = zipped(make("github/o/x/2/1/j", repo="o/x"))
    new, _, errors = _collect_zips(FileStore(tmp_path), "o/x", [("qq-results-bad", buf.getvalue()), ok])
    assert new == 1 and "must be strings" in errors[0]
