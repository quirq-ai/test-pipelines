import datetime as dt

import pytest

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


def _gate_job(job, finished, fail=False, attempt=1, queued="2026-10-04T09:00:00Z", run_no=77,
              **run):
    from qqresults import bundle, verdict
    from qqresults.model import Run
    b = make(f"github/quirq-ai/xo-space/{run_no}/{attempt}/{job}", kind="gate", commit="m7",
             finished=finished, queued=queued, fail=fail)
    r = Run.from_dict({**b.run.to_dict(), "backend": "github", "attempt": attempt, **run})
    results = b.results if r.results_found else []
    return bundle.Bundle(r, results, verdict.compute(r, results))


def gate(st):
    return metric(scorecard.compute(st, SINCE, UNTIL), "quirq-ai/xo-space", "Gate time-to-green")


def test_a_gate_with_several_jobs_is_one_sample_at_its_last_finish(tmp_path):
    st = FileStore(tmp_path)
    st.put(_gate_job("lint", "2026-10-04T09:05:00Z"))
    st.put(_gate_job("test", "2026-10-04T09:30:00Z"))
    m = gate(st)
    assert m.value == 30.0 and "over 1 green gate run" in m.detail


def test_a_red_gate_run_is_not_time_to_green(tmp_path):
    st = FileStore(tmp_path)
    st.put(_gate_job("lint", "2026-10-04T09:05:00Z"))
    st.put(_gate_job("test", "2026-10-04T09:30:00Z", fail=True))
    m = gate(st)
    assert not m.measured and m.detail == "1 red gate run(s) not counted"
    assert "| Gate time-to-green | not measured |" in scorecard.to_markdown(
        scorecard.compute(st, SINCE, UNTIL))
    assert "1 red gate run(s) not counted |" in scorecard.to_markdown(scorecard.compute(st, SINCE, UNTIL))


def test_a_red_job_without_a_queue_time_still_makes_the_run_red(tmp_path):
    st = FileStore(tmp_path)
    st.put(_gate_job("lint", "2026-10-04T09:05:00Z"))
    st.put(_gate_job("test", "2026-10-04T09:30:00Z", fail=True, queued=""))
    assert not gate(st).measured and "1 red" in gate(st).detail


def test_a_gate_with_only_a_typecheck_is_judged_by_its_job_status(tmp_path):
    st = FileStore(tmp_path)
    st.put(_gate_job("typecheck", "2026-10-04T09:12:00Z", results_found=False, job_status="success"))
    st.put(_gate_job("old", "2026-10-04T09:20:00Z", results_found=False, job_status="cancelled"))
    assert gate(st).value == 12.0


def test_a_rerun_counts_from_the_first_queue_entry_to_green(tmp_path):
    st = FileStore(tmp_path)
    st.put(_gate_job("lint", "2026-10-04T09:05:00Z"))
    st.put(_gate_job("test", "2026-10-04T09:30:00Z", fail=True))
    st.put(_gate_job("test", "2026-10-04T10:30:00Z", attempt=2, queued="2026-10-04T10:00:00Z"))
    m = gate(st)
    assert m.value == 90.0 and "red" not in m.detail


def test_a_cancelled_retry_does_not_hide_a_red_attempt(tmp_path):
    st = FileStore(tmp_path)
    st.put(_gate_job("lint", "2026-10-04T09:05:00Z"))
    st.put(_gate_job("test", "2026-10-04T09:30:00Z", fail=True))
    st.put(_gate_job("test", "2026-10-04T10:00:00Z", attempt=2, results_found=False,
                     job_status="cancelled"))
    assert not gate(st).measured and "1 red" in gate(st).detail


def test_green_runs_without_a_queue_time_are_noted(tmp_path):
    st = FileStore(tmp_path)
    st.put(_gate_job("lint", "2026-10-04T09:05:00Z"))
    st.put(_gate_job("lint", "2026-10-04T09:05:00Z", queued="", run_no=78))
    m = gate(st)
    assert m.value == 5.0 and "1 green run(s) without a queue time" in m.detail


def test_one_jobs_queue_time_after_its_own_finish_does_not_set_the_runs_wait(tmp_path):
    # lint's clock is off: its queue time is after it finished, so test's queue time is used.
    st = FileStore(tmp_path)
    st.put(_gate_job("lint", "2026-10-04T09:05:00Z", queued="2026-10-04T09:20:00Z"))
    st.put(_gate_job("test", "2026-10-04T09:30:00Z", queued="2026-10-04T09:10:00Z"))
    m = gate(st)
    assert m.value == 20.0 and "skipped" not in m.detail


def test_a_run_whose_every_queue_time_is_late_is_skipped_not_untimed(tmp_path):
    st = FileStore(tmp_path)
    st.put(_gate_job("lint", "2026-10-04T09:05:00Z", queued="2026-10-04T09:20:00Z"))
    m = gate(st)
    assert not m.measured and m.detail == "1 run(s) queued after they finished, skipped"


def _push(st, rid, commit, before, finished, fail=False, **run):
    from qqresults import bundle, verdict
    from qqresults.model import Run
    b = make(rid, commit=commit, finished=finished, fail=fail)
    data = {**b.run.to_dict(), "base_commit": before, **run}
    if before is None:
        del data["base_commit"]              # a record written without the field
    r = Run.from_dict(data)
    results = b.results if r.results_found else []
    st.put(bundle.Bundle(r, results, verdict.compute(r, results)))


def _slow_green_on_an_older_commit(st, before=lambda b: b):
    # c1's job is slow and passes at 10:30; c2 (pushed on top of c1) is red at 10:00; c3 fixes
    # it at 10:45. Main was red from 10:00 to 10:45, even though c1 went green in between.
    _push(st, "p1", "c1", before("c0"), "2026-10-04T10:30:00Z")
    _push(st, "p2", "c2", before("c1"), "2026-10-04T10:00:00Z", fail=True)
    _push(st, "p3", "c3", before("c2"), "2026-10-04T10:45:00Z")


def test_main_red_follows_the_push_chain_not_finish_times(tmp_path):
    st = FileStore(tmp_path)
    _slow_green_on_an_older_commit(st)
    m = metric(scorecard.compute(st, SINCE, UNTIL), "quirq-ai/xo-space", "Main-red time")
    assert m.value == 45.0 and m.detail == "3 post-submit commits"


def test_without_a_push_chain_main_red_falls_back_to_finish_times_and_says_so(tmp_path):
    st = FileStore(tmp_path)
    _slow_green_on_an_older_commit(st, before=lambda b: None)   # older records: no base_commit
    m = metric(scorecard.compute(st, SINCE, UNTIL), "quirq-ai/xo-space", "Main-red time")
    assert m.value == 30.0 and "push chain unknown, ordered by job finish time" in m.detail


def test_a_cancelled_push_still_links_the_chain(tmp_path):
    st = FileStore(tmp_path)
    _push(st, "p1", "c1", "c0", "2026-10-04T10:30:00Z")
    _push(st, "p2", "c2", "c1", "2026-10-04T10:00:00Z", fail=True)
    _push(st, "p3", "c3", "c2", "2026-10-04T10:20:00Z", results_found=False, job_status="cancelled")
    _push(st, "p4", "c4", "c3", "2026-10-04T10:45:00Z")
    m = metric(scorecard.compute(st, SINCE, UNTIL), "quirq-ai/xo-space", "Main-red time")
    assert m.value == 45.0 and m.detail == "3 post-submit commits"


@pytest.mark.parametrize("pushes", [
    [("c1", "c0"), ("c2", "c1"), ("c3", "c1")],      # a force push: two pushes from c1
    [("c1", "c0"), ("c2", "c1"), ("c3", "")],        # a dispatched run: no before
    [("c1", "c0"), ("c3", "c2")],                     # a push the store has no run of
])
def test_a_broken_push_chain_is_not_trusted(tmp_path, pushes):
    st = FileStore(tmp_path)
    for i, (commit, before) in enumerate(pushes):
        _push(st, f"p{i}", commit, before, f"2026-10-04T1{i}:00:00Z", fail=i == 0)
    m = metric(scorecard.compute(st, SINCE, UNTIL), "quirq-ai/xo-space", "Main-red time")
    assert m.value == 60.0 and "push chain unknown" in m.detail


def test_a_newer_green_before_an_older_red_is_not_negative_red_time(tmp_path):
    st = FileStore(tmp_path)
    _push(st, "p1", "c1", "c0", "2026-10-04T10:30:00Z", fail=True)   # slow, red
    _push(st, "p2", "c2", "c1", "2026-10-04T10:10:00Z")              # already green
    m = metric(scorecard.compute(st, SINCE, UNTIL), "quirq-ai/xo-space", "Main-red time")
    assert m.value == 0.0 and "red now" not in m.detail
