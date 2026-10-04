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
    name1, zip1 = zipped(make("github/o/x/1/1/presubmit"))
    name2, zip2 = zipped(make("github/o/x/2/1/presubmit", fail=True))
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
