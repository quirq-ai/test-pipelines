"""Scorecard v0: plan §8's metrics, computed from the results store, never typed by hand.

v0 measures what the store already holds and names, for every other metric, the item that will
make it measurable. A metric is never shown as zero when it was not measured.
"""
from __future__ import annotations

import datetime as dt
import statistics
from collections import defaultdict
from dataclasses import asdict, dataclass, field

from qqresults.model import Run, RunKind, Verdict, VerdictStatus
from qqresults.store import FileStore, RunFilter

VERIFYING = (RunKind.PRESUBMIT.value, RunKind.GATE.value, RunKind.POSTSUBMIT.value)


def red(run: Run, v: Verdict) -> bool | None:
    """Whether a run counts as red on the scorecard; None when it says nothing.

    A run with test results is red when its verdict failed. A run without any (a repo whose only
    check is a typecheck, or a job that broke before its tests) is red only when the job itself
    failed. A cancelled job (superseded by a newer push) says nothing.
    """
    if run.job_status == "cancelled":
        return None
    if run.results_found and v.counts:
        return not v.passed
    if run.job_status == "failure":
        return True
    if run.job_status == "success":
        return False
    return None   # no results and no job status: unknown, not counted


@dataclass
class Metric:
    name: str
    target: str
    value: float | None = None
    unit: str = ""
    detail: str = ""
    waiting_on: str = ""       # why it is not measured yet, naming the item that will measure it

    @property
    def measured(self) -> bool:
        return self.value is not None


@dataclass
class Scorecard:
    generated_at: str
    since: str
    until: str
    repos: dict[str, list[Metric]] = field(default_factory=dict)
    not_measured: list[Metric] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def parse_time(text: str) -> dt.datetime:
    return dt.datetime.fromisoformat(text.replace("Z", "+00:00"))


def fmt_time(t: dt.datetime) -> str:
    return t.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _percentile(values: list[float], pct: int) -> float:
    if len(values) == 1:
        return values[0]
    return statistics.quantiles(values, n=100, method="inclusive")[pct - 1]


def gate_time(runs: list[tuple[Run, Verdict]]) -> Metric:
    m = Metric("Gate time-to-green", "P1: p50 under 15 min, p90 under 30 min", unit="min")
    minutes = [(parse_time(r.finished_at) - parse_time(r.queued_at)).total_seconds() / 60
               for r, _ in runs if r.kind == RunKind.GATE and r.queued_at and r.finished_at]
    if not minutes:
        m.waiting_on = "V0-GAT-04 records queue-entry time on gate runs"
        return m
    m.value = round(_percentile(sorted(minutes), 50), 1)
    m.detail = f"p50 {m.value} / p90 {round(_percentile(sorted(minutes), 90), 1)} over {len(minutes)} gate runs"
    return m


def main_red(runs: list[tuple[Run, Verdict]], since: dt.datetime, until: dt.datetime) -> Metric:
    """Minutes main spent red: from the first red post-submit commit to the next green one."""
    m = Metric("Main-red time", "under 60 min/week", unit="min/week")
    commits: dict[str, list[tuple[Run, Verdict]]] = defaultdict(list)
    for r, v in runs:
        if r.kind == RunKind.POSTSUBMIT and r.finished_at and red(r, v) is not None:
            commits[r.commit].append((r, v))
    if not commits:
        m.waiting_on = "post-submit runs that store results (the sink on each repo's main)"
        return m
    # A commit is red as soon as its first job fails, and green once its last job has passed.
    ordered = []
    for jobs in commits.values():
        reds = [parse_time(r.finished_at) for r, v in jobs if red(r, v)]
        ordered.append((min(reds), False) if reds
                       else (max(parse_time(r.finished_at) for r, _ in jobs), True))
    ordered.sort()
    red_since = None
    total = 0.0
    for at, green in ordered:
        if not green and red_since is None:
            red_since = at
        elif green and red_since is not None:
            total += (min(at, until) - max(red_since, since)).total_seconds()
            red_since = None
    if red_since is not None:
        total += (until - max(red_since, since)).total_seconds()
    weeks = max((until - since).total_seconds() / (7 * 86400), 1 / 7)
    m.value = round(max(total, 0) / 60 / weeks, 1)
    m.detail = f"{len(ordered)} post-submit commits" + ("; main is red now" if red_since else "")
    return m


def flake_rate(runs: list[tuple[Run, Verdict]]) -> Metric:
    m = Metric("Flake rate", "under 1%", unit="%")
    verifying = [(r, v) for r, v in runs if r.kind in VERIFYING and r.job_status != "cancelled"]
    if not verifying:
        m.waiting_on = "presubmit, gate or post-submit runs in the store"
        return m
    flaky = sum(1 for _, v in verifying if v.counts.get(VerdictStatus.FLAKY.value))
    m.value = round(100 * flaky / len(verifying), 2)
    m.detail = f"{flaky} of {len(verifying)} runs passed only on retry"
    return m


def pass_rate(runs: list[tuple[Run, Verdict]], kind: str, name: str) -> Metric:
    m = Metric(name, "measured", unit="%")
    of_kind = [red(r, v) for r, v in runs if r.kind == kind]
    counted = [x for x in of_kind if x is not None]
    if not counted:
        m.waiting_on = f"{kind} runs in the store"
        return m
    passed = counted.count(False)
    m.value = round(100 * passed / len(counted), 1)
    m.detail = f"{passed} of {len(counted)} {kind} runs passed"
    if len(counted) < len(of_kind):
        m.detail += f" ({len(of_kind) - len(counted)} cancelled or unknown not counted)"
    return m


def missing_results(runs: list[tuple[Run, Verdict]]) -> Metric:
    m = Metric("Runs with no test results", "0", unit="runs")
    m.value = sum(1 for r, v in runs if not (r.results_found and v.counts)
                  and r.job_status != "cancelled")
    m.detail = (f"of {len(runs)} stored runs; a repo with no test reports (only a typecheck, say) "
                "shows up here, not as red")
    return m


# Plan §8 metrics this version cannot measure yet, and what will measure them.
NOT_MEASURED = [
    ("Repos behind the gate", "100% by end of P1", "V0-ORG-03 merge queue and V0-ONB-01/02 manifests"),
    ("Landed on a green merge result", "100% (enforced)", "V0-ORG-03 merge queue: gate runs on merge-group SHAs"),
    ("Time to revert a culprit", "mean under 30 min", "V0-GAR-03 auto-revert"),
    ("Expired quarantines", "0", "v1 quarantine with expiry"),
    ("Cache hit rate", "at least 90% (P3+)", "V0-RBE-01 executor reporting reused actions"),
    ("Reproducibility", "100% of deterministic targets", "remote-build digest comparison"),
    ("Pinned and mirrored deps", "100%", "quirq-ai/sync"),
    ("Release cadence", "canary daily", "V0-REL-03 daily canary"),
    ("Rollback time", "under 10 min", "release rollback drill"),
    ("Unattended canary days", "14 in a row by P5", "V0-REL-03 daily canary"),
    ("Canary hold or rollback time", "under 15 min", "V0-REL-03 and health signals"),
    ("Failures fully recorded", "100%", "V0-TST-04 failure records"),
    ("Postmortem action items closed", "at least 90%", "postmortem tracking (v1)"),
    ("Open recurring failure classes", "0", "v1 failure classes"),
    ("Fuzz finding turnaround", "under 24 h", "v1 fuzzers"),
    ("Intervention rate", "falling every month", "GitHub PR data (scorecard v1)"),
    ("Revert precision", "at least 90%", "V0-GAR-03 auto-revert"),
    ("CI cost per landed change", "measured against the V0-ORG-04 ceiling", "V0-ORG-04 compute ceiling and billing data"),
]


def compute(store: FileStore, since: dt.datetime, until: dt.datetime,
            repos: list[str] | None = None) -> Scorecard:
    runs = [(r, v) for r, v in store.runs(RunFilter(since=fmt_time(since)))
            if r.finished_at <= fmt_time(until) and r.kind != RunKind.LOCAL]
    by_repo: dict[str, list[tuple[Run, Verdict]]] = defaultdict(list)
    for r, v in runs:
        by_repo[r.repo].append((r, v))
    for repo in repos or []:
        by_repo.setdefault(repo, [])
    card = Scorecard(generated_at=fmt_time(dt.datetime.now(dt.UTC)), since=fmt_time(since),
                     until=fmt_time(until))
    for repo in sorted(by_repo):
        rr = by_repo[repo]
        card.repos[repo] = [
            gate_time(rr),
            main_red(rr, since, until),
            flake_rate(rr),
            pass_rate(rr, RunKind.GATE.value, "Gate runs passed"),
            pass_rate(rr, RunKind.POSTSUBMIT.value, "Post-submit runs passed"),
            pass_rate(rr, RunKind.PRESUBMIT.value, "Presubmit runs passed"),
            missing_results(rr),
        ]
    card.not_measured = [Metric(n, t, waiting_on=w) for n, t, w in NOT_MEASURED]
    return card


def to_markdown(card: Scorecard) -> str:
    lines = [f"# quirq infra scorecard v0", "",
             f"Window {card.since} to {card.until}, generated {card.generated_at} from the results store.",
             ""]
    if not card.repos:
        lines += ["No runs in the store for this window.", ""]
    for repo, metrics in card.repos.items():
        lines += [f"## {repo}", "", "| Metric | Value | Target | Detail |", "|---|---|---|---|"]
        for m in metrics:
            value = f"{m.value:g} {m.unit}".strip() if m.measured else "not measured"
            detail = m.detail if m.measured else f"waiting on {m.waiting_on}"
            lines.append(f"| {m.name} | {value} | {m.target} | {detail} |")
        lines.append("")
    lines += ["## Not measured yet", "", "| Metric | Target | Waiting on |", "|---|---|---|"]
    lines += [f"| {m.name} | {m.target} | {m.waiting_on} |" for m in card.not_measured]
    return "\n".join(lines) + "\n"
