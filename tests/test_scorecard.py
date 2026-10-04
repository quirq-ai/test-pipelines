import datetime as dt

from qqresults import cli, scorecard
from qqresults.store import FileStore
from test_store import make

SINCE = dt.datetime(2026, 10, 1, tzinfo=dt.UTC)
UNTIL = dt.datetime(2026, 10, 8, tzinfo=dt.UTC)


def metric(card, repo, name):
    return next(m for m in card.repos[repo] if m.name == name)


def seeded(tmp_path):
    st = FileStore(tmp_path)
    # main goes red at 10:00 and green again at 10:45: 45 minutes red this week.
    st.put(make("p1", commit="c1", finished="2026-10-04T09:00:00Z"))
    st.put(make("p2", commit="c2", finished="2026-10-04T10:00:00Z", fail=True))
    st.put(make("p2b", commit="c2", finished="2026-10-04T10:05:00Z"))   # another job, same commit
    st.put(make("p3", commit="c3", finished="2026-10-04T10:45:00Z"))
    st.put(make("g1", kind="gate", commit="m1", finished="2026-10-04T08:20:00Z",
                queued="2026-10-04T08:00:00Z", change=1))
    st.put(make("g2", kind="gate", commit="m2", finished="2026-10-04T08:40:00Z",
                queued="2026-10-04T08:30:00Z", change=2, flaky=True))
    st.put(make("pr1", kind="presubmit", commit="h1", finished="2026-10-04T07:00:00Z", fail=True))
    st.put(make("old", commit="c0", finished="2026-09-01T00:00:00Z", fail=True))  # outside window
    return st


def test_scorecard_reads_gate_and_postsubmit_results(tmp_path):
    card = scorecard.compute(seeded(tmp_path), SINCE, UNTIL)
    repo = "quirq-ai/xo-space"
    assert metric(card, repo, "Main-red time").value == 45.0
    assert metric(card, repo, "Gate time-to-green").value == 15.0     # p50 of 20 and 10 minutes
    assert "p90 19.0" in metric(card, repo, "Gate time-to-green").detail
    assert metric(card, repo, "Gate runs passed").value == 100.0
    assert metric(card, repo, "Post-submit runs passed").value == 75.0
    assert metric(card, repo, "Presubmit runs passed").value == 0.0
    assert metric(card, repo, "Flake rate").value == round(100 / 7, 2)
    assert metric(card, repo, "Runs with no test results").value == 0


def test_unmeasured_metrics_name_what_they_wait_on(tmp_path):
    card = scorecard.compute(FileStore(tmp_path), SINCE, UNTIL, repos=["quirq-ai/innernet"])
    gate = metric(card, "quirq-ai/innernet", "Gate time-to-green")
    assert not gate.measured and "V0-GAT-04" in gate.waiting_on
    assert not metric(card, "quirq-ai/innernet", "Main-red time").measured
    md = scorecard.to_markdown(card)
    assert "| Gate time-to-green | not measured |" in md
    assert "| Cache hit rate | at least 90% (P3+) | V0-RBE-01" in md


def test_red_main_at_the_end_counts_until_now(tmp_path):
    st = FileStore(tmp_path)
    st.put(make("p1", commit="c1", finished="2026-10-07T23:00:00Z", fail=True))
    m = metric(scorecard.compute(st, SINCE, UNTIL), "quirq-ai/xo-space", "Main-red time")
    assert m.value == 60.0 and "red now" in m.detail


def test_cli_scorecard(tmp_path, capsys):
    seeded(tmp_path)
    assert cli.main(["scorecard", "--store", str(tmp_path), "--days", "36500"]) == 0
    assert "## quirq-ai/xo-space" in capsys.readouterr().out
    assert cli.main(["scorecard", "--store", str(tmp_path), "--json"]) == 0
    assert '"not_measured"' in capsys.readouterr().out


def test_runs_without_results_are_not_red_unless_the_job_failed(tmp_path):
    from qqresults import bundle, verdict
    from qqresults.model import Run
    st = FileStore(tmp_path)

    def bare(rid, commit, finished, job_status):
        run = Run(id=rid, repo="quirq-ai/innernet", kind="postsubmit", commit=commit,
                  finished_at=finished, results_found=False, job_status=job_status)
        st.put(bundle.Bundle(run, [], verdict.compute(run, [])))

    bare("a", "c1", "2026-10-04T09:00:00Z", "success")      # typecheck only: no test reports
    bare("b", "c2", "2026-10-04T10:00:00Z", "failure")      # build broke before any test ran
    bare("c", "c3", "2026-10-04T10:30:00Z", "success")
    bare("d", "c4", "2026-10-04T11:00:00Z", "cancelled")    # superseded; says nothing
    card = scorecard.compute(st, SINCE, UNTIL)
    assert metric(card, "quirq-ai/innernet", "Main-red time").value == 30.0
    m = metric(card, "quirq-ai/innernet", "Post-submit runs passed")
    assert m.value == round(200 / 3, 1) and "1 cancelled or unknown" in m.detail
    assert metric(card, "quirq-ai/innernet", "Runs with no test results").value == 3


def test_a_passing_rerun_of_a_failed_job_ends_the_red(tmp_path):
    from qqresults import bundle, verdict
    from qqresults.model import Result, Run
    st = FileStore(tmp_path)
    for attempt, finished, ok in ((1, "2026-10-04T10:00:00Z", False), (2, "2026-10-04T10:05:00Z", True)):
        run = Run(id=f"github/quirq-ai/xo-space/77/{attempt}/presubmit", repo="quirq-ai/xo-space",
                  kind="postsubmit", commit="c1", backend="github", attempt=attempt,
                  finished_at=finished, job_status="success" if ok else "failure")
        rs = [Result(run_id=run.id, test_id="t::a", status="PASS" if ok else "FAIL", expected=ok)]
        st.put(bundle.Bundle(run, rs, verdict.compute(run, rs)))
    card = scorecard.compute(st, SINCE, UNTIL)
    m = metric(card, "quirq-ai/xo-space", "Main-red time")
    assert m.value == 0.0 and "red now" not in m.detail
    assert metric(card, "quirq-ai/xo-space", "Flake rate").value == 100.0


def test_gate_p90_is_a_field(tmp_path):
    card = scorecard.compute(seeded(tmp_path), SINCE, UNTIL)
    assert metric(card, "quirq-ai/xo-space", "Gate time-to-green").extra == {"p50": 15.0, "p90": 19.0}


def test_perf_runs_naming_a_product_repo_are_not_counted_as_its_ci(tmp_path):
    st = seeded(tmp_path)
    st.put(make("perf1", kind="other", commit="c9", finished="2026-10-04T12:00:00Z", fail=True))
    card = scorecard.compute(st, SINCE, UNTIL)
    assert metric(card, "quirq-ai/xo-space", "Main-red time").value == 45.0
    assert metric(card, "quirq-ai/xo-space", "Post-submit runs passed").value == 75.0
    assert metric(card, "quirq-ai/xo-space", "Runs with no test results").value == 0


def test_a_gate_run_queued_after_it_finished_is_not_a_negative_wait(tmp_path):
    st = seeded(tmp_path)
    st.put(make("g-bad", kind="gate", commit="c8", finished="2026-10-04T10:00:00Z",
                queued="2026-10-04T11:00:00Z"))
    card = scorecard.compute(st, SINCE, UNTIL)
    assert metric(card, "quirq-ai/xo-space", "Gate time-to-green").extra == {"p50": 15.0, "p90": 19.0}


def test_backfilled_runs_are_left_out_of_the_scorecard(tmp_path):
    from qqresults import bundle
    from qqresults.model import Run
    st = seeded(tmp_path)
    before = scorecard.compute(st, SINCE, UNTIL).to_dict()
    b = make("bf", commit="c7", finished="2026-10-04T12:00:00Z", fail=True)
    st.put(bundle.Bundle(Run.from_dict({**b.run.to_dict(), "role": "backfill"}), b.results, b.verdict))
    assert scorecard.compute(st, SINCE, UNTIL).to_dict() == before
