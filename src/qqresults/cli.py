"""qqresults: test results, verdicts and failure records for quirq infra (qq).

    qqresults sink --junit GLOB [--junit GLOB ...] --out DIR [--backend github|local] [--kind K]
                   [--rerun CMD [--base SHA] [--infra-config PATH]] [--fail-on-verdict]
        Normalize this job's JUnit reports into one write-once run bundle under DIR; with
        --rerun, retry failed tests and compare them with base first (V0-TST-03).
    qqresults show BUNDLE_DIR [--json]
        Print a bundle's verdict.
    qqresults import --store DIR BUNDLE_DIR...
        Add sink bundles to the results store (write-once).
    qqresults collect --store DIR --repo OWNER/NAME... [--workflow GLOB...] [--cross-repo OWNER/NAME...]
        Add every bundle the repos' own runs of trusted workflows kept (token from GITHUB_TOKEN).
    qqresults query runs|results|history --store DIR [filters] [--json]
        Read the store.
    qqresults scorecard --store DIR [--days N] [--json]
        Plan §8's metrics from the store.
    qqresults failure open --dir DIR --kind K --repo R --subject S [--summary ...] [--mirror REPO]
    qqresults failure link --dir DIR ID --culprit C --fix F --covering-test T [--mirror REPO]
    qqresults failure list --dir DIR
        Failure records (V0-TST-04): one per held canary, rollback or auto-revert, mirrored
        to one labelled GitHub issue (token from GITHUB_TOKEN). DIR is a store's failures/
        directory or a scratch directory that the backend keeps.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

from qqresults import __version__, backends, bundle, failures, policy, scorecard, sink, store
from qqresults.errors import Error
from qqresults.model import FailureKind, Run, RunKind


def _run(args):
    backend = backends.load(args.backend)
    if args.backend == "github":
        run = backend.run_from_env(os.environ, kind=args.kind or "", name=args.name)
        if not args.commit or args.commit == run.commit:
            return run
        if not re.fullmatch(r"[0-9a-f]{40}", args.commit):
            raise SystemExit("qqresults sink: --commit must be a full 40-hex lowercase commit, "
                             f"not {args.commit!r}")
        # A dispatched backfill tests another commit than GITHUB_SHA: the event's base and
        # change describe GITHUB_SHA, so they are dropped rather than recorded wrongly, and the
        # run is marked as a backfill, which the scorecard leaves out (it finished long after the
        # commit landed, so it would misplace main-red time). It stays queryable.
        return Run.from_dict({**run.to_dict(), "commit": args.commit, "base_commit": "",
                              "change": None, "role": "backfill"})
    if not (args.repo and args.commit):
        raise SystemExit("qqresults sink: --backend local needs --repo and --commit")
    return backend.run_from_args(args.repo, args.commit, kind=args.kind or RunKind.LOCAL.value,
                                 name=args.name)


def cmd_sink(args) -> int:
    run = _run(args)
    pol = policy.from_infra_config(Path(args.infra_config)) if args.infra_config else policy.Policy()
    if args.retries is not None:
        pol = policy.Policy(retry_failed=args.retries, compare_with_base=pol.compare_with_base)
    if args.no_base:
        pol = policy.Policy(retry_failed=pol.retry_failed, compare_with_base=False)
    path, b = sink.sink(run, args.junit, Path(args.root).resolve(), Path(args.out),
                        rerun_cmd=args.rerun or "", policy=pol, base_commit=args.base or "",
                        setup=args.setup or "")
    v = b.verdict
    print(f"run {run.id} ({run.kind}): {len(b.results)} result(s) -> {path}")
    print(f"verdict: {'PASS' if v.passed else 'FAIL'} {v.counts} {v.reason}".rstrip())
    for t in v.tests:
        print(f"  {t.status:<10} {t.test_id}  {t.reason}".rstrip())
    if args.github_output:
        with open(args.github_output, "a", encoding="utf-8") as f:
            f.write(f"bundle={path}\nname={path.name}\npassed={str(v.passed).lower()}\n")
    return 1 if args.fail_on_verdict and not v.passed else 0


def cmd_show(args) -> int:
    b = bundle.read(Path(args.bundle))
    if args.json:
        print(b.verdict.to_json())
        return 0
    v = b.verdict
    print(f"run {b.run.id} ({b.run.kind}) at {b.run.commit}: {'PASS' if v.passed else 'FAIL'}")
    for t in v.tests:
        print(f"  {t.status:<10} {t.test_id}")
    return 0


def cmd_import(args) -> int:
    st = store.open_store(args.store)
    for path in args.bundles:
        new = st.import_dir(Path(path))
        print(f"{'stored' if new else 'already stored'}: {Path(path).name}")
    return 0


def cmd_collect(args) -> int:
    gh = backends.load("github")
    st = store.open_store(args.store)
    token = os.environ.get("GITHUB_TOKEN", "")
    trust = gh.Trust(workflows=tuple(args.workflow or ()),
                     cross_repo=frozenset(args.cross_repo or ()))
    failed = False
    for repo in args.repo:
        try:
            new, old, errors = gh.collect(repo, st, token, trust=trust)
        except Error as e:   # one repo failing to list must not hide the others
            new, old, errors = 0, 0, [f"{repo}: {e}"]
        print(f"{repo}: {new} new, {old} already stored, {len(errors)} skipped")
        for e in errors:
            print(f"qqresults: warning: {e}", file=sys.stderr)
        failed |= bool(errors)
    # A bad artifact is skipped with a warning so it cannot block every later collection.
    return 1 if failed and args.strict else 0


def _utc(text: str) -> str:
    try:
        return scorecard.fmt_time(scorecard.parse_time(text))
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an RFC 3339 time: {text!r}") from None


def _filter(args) -> store.RunFilter:
    return store.RunFilter(repo=args.repo or "", kind=args.kind or "", commit=args.commit or "",
                           branch=args.branch or "", change=args.change, since=args.since or "",
                           failed=True if args.failed else None)


def cmd_query(args) -> int:
    st = store.open_store(args.store)
    if args.what == "runs":
        for r, v in st.runs(_filter(args)):
            if args.json:
                print(json.dumps({"run": r.to_dict(), "verdict": v.to_dict()}, sort_keys=True))
            else:
                print(f"{r.finished_at}  {'PASS' if v.passed else 'FAIL'}  {r.kind:<10} {r.repo} "
                      f"{r.commit[:12]}  {r.id}")
    elif args.what == "results":
        if not args.run:
            raise SystemExit("qqresults query results: --run is required")
        for r in st.results(args.run):
            if args.unexpected and r.expected:
                continue
            print(r.to_json() if args.json else f"{r.status:<6} {r.test_id}")
    else:
        if not args.test:
            raise SystemExit("qqresults query history: --test is required")
        for run, r in st.history(args.test, _filter(args)):
            print(json.dumps({"run": run.to_dict(), "result": r.to_dict()}, sort_keys=True)
                  if args.json
                  else f"{run.finished_at}  {r.status:<6} {run.kind:<10} {run.commit[:12]}  {run.id}")
    return 0


def cmd_scorecard(args) -> int:
    until = dt.datetime.now(dt.UTC)
    st = store.open_store(args.store)
    card = scorecard.compute(st, until - dt.timedelta(days=args.days), until, repos=args.repo)
    for message in st.skipped.values():
        print(f"qqresults: warning: {message}", file=sys.stderr)
    if args.json:
        print(json.dumps(card.to_dict(), indent=2, sort_keys=True))
    else:
        print(scorecard.to_markdown(card), end="")
    return 0


def _mirror(state: failures.State, repo: str) -> failures.State:
    gh = backends.load("github")
    url, created = gh.mirror_issue(state, repo, os.environ.get("GITHUB_TOKEN", ""))
    if not url and state.security:
        print(f"issue: withheld, the record looks security-related (never mirrored publicly)")
        return state
    print(f"issue: {url} ({'opened' if created else 'up to date'})")
    if state.links.get("issue") != url:
        failures.add_link(state.path, "issue", url)
    return failures.read(state.path)


def _report(state: failures.State, created: bool | None, gh_output: str | None) -> None:
    f = state.current
    verb = "" if created is None else ("opened " if created else "already open: ")
    print(f"{verb}{f.id} ({f.kind}, {f.repo}) at {state.path}")
    print("closed" if state.closed else "open; missing " + ", ".join(state.missing))
    if gh_output:
        with open(gh_output, "a", encoding="utf-8") as out:
            out.write(f"id={f.id}\ndir={state.path}\nname={state.path.name}\n"
                      f"issue={state.links.get('issue', '')}\ncreated={str(bool(created)).lower()}\n"
                      f"security={str(state.security).lower()}\n")


FAILURE_FIELDS = ("channel", "build_digest", "last_good", "first_bad", "stage", "signal",
                  "run_id", "operation", "summary")


def cmd_failure(args) -> int:
    parent = Path(args.dir)
    if args.action == "open":
        fields = {k: getattr(args, k) for k in FAILURE_FIELDS if getattr(args, k)}
        f = failures.new(args.kind, args.repo, args.subject, security=args.security, **fields)
        state, created = failures.open_record(f, parent)
        if args.mirror:
            state = _mirror(state, args.mirror)
        _report(state, created, args.github_output)
    elif args.action == "link":
        path = parent / failures.dirname(args.id)
        failures.read(path)  # fails clearly if the record is not here
        for field in failures.LINK_FIELDS:
            value = getattr(args, field, None)
            if value:
                failures.add_link(path, field, value)
        state = failures.read(path)
        if args.mirror:
            state = _mirror(state, args.mirror)
        _report(state, None, args.github_output)
    else:
        for d in sorted(parent.iterdir()) if parent.is_dir() else []:
            if (d / failures.RECORD).is_file():
                st = failures.read(d)
                print(f"{st.record.opened_at}  {'closed' if st.closed else 'open  '}  "
                      f"{st.record.kind:<16} {st.record.repo}  {st.record.id}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="qqresults", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"qqresults {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("sink", help="store this job's JUnit reports as a run bundle")
    s.add_argument("--junit", action="append", required=True, metavar="GLOB",
                   help="report files, relative to --root; repeat for more")
    s.add_argument("--out", required=True, help="directory the bundle is written under")
    s.add_argument("--root", default=".", help="where the globs are resolved (default: .)")
    s.add_argument("--backend", default="github", choices=backends.KNOWN)
    s.add_argument("--kind", choices=[k.value for k in RunKind],
                   help="override the run kind (default: from the backend)")
    s.add_argument("--name", default="", help="tells apart several sinks in one job")
    s.add_argument("--repo", help="local backend: owner/name")
    s.add_argument("--commit", help="the commit tested (local backend: required; github: "
                   "overrides GITHUB_SHA, 40 hex)")
    s.add_argument("--rerun", metavar="CMD",
                   help="retry failed tests with this shell command, then compare with base "
                        "(it writes JUnit to $QQ_JUNIT_DIR; $QQ_RETRY_TESTS lists the failed ids)")
    s.add_argument("--base", help="the base commit for --rerun (default: the run's base)")
    s.add_argument("--setup", metavar="CMD",
                   help="prepares a checkout for --rerun: runs in the base worktree before its "
                        "tests and in the change's checkout after ($QQ_SIDE says which)")
    s.add_argument("--infra-config", metavar="PATH",
                   help="read retry policy from this infra-config checkout's flakes.toml")
    s.add_argument("--retries", type=int, help="override flakes.toml retry_failed")
    s.add_argument("--no-base", action="store_true", help="do not compare with base")
    s.add_argument("--fail-on-verdict", action="store_true",
                   help="exit 1 when the verdict fails (use when the sink decides the check)")
    s.add_argument("--github-output", default=os.environ.get("GITHUB_OUTPUT"),
                   help=argparse.SUPPRESS)
    s.set_defaults(func=cmd_sink)

    sh = sub.add_parser("show", help="print a bundle's verdict")
    sh.add_argument("bundle")
    sh.add_argument("--json", action="store_true")
    sh.set_defaults(func=cmd_show)

    im = sub.add_parser("import", help="add sink bundles to the results store")
    im.add_argument("--store", required=True)
    im.add_argument("bundles", nargs="+")
    im.set_defaults(func=cmd_import)

    co = sub.add_parser("collect", help="add the bundles GitHub kept as workflow artifacts")
    co.add_argument("--store", required=True)
    co.add_argument("--repo", action="append", required=True, metavar="OWNER/NAME")
    co.add_argument("--workflow", action="append", metavar="GLOB",
                    help="only runs of workflow files matching this glob may write; repeat for "
                         "more (default: .github/workflows/qq-*.yml and "
                         ".github/workflows/presubmit.yml; none for a --cross-repo repo)")
    co.add_argument("--cross-repo", action="append", metavar="OWNER/NAME",
                    help="this collected repo's runs may store kind 'other' runs naming "
                         "another repo (perf); needs --workflow, and only its push, schedule "
                         "and dispatch runs on the default branch are stored")
    co.add_argument("--strict", action="store_true", help="exit 1 if any artifact was skipped")
    co.set_defaults(func=cmd_collect)

    q = sub.add_parser("query", help="read the results store")
    q.add_argument("what", choices=["runs", "results", "history"])
    q.add_argument("--store", required=True)
    q.add_argument("--repo")
    q.add_argument("--kind", choices=[k.value for k in RunKind])
    q.add_argument("--commit", help="a commit or its prefix")
    q.add_argument("--branch")
    q.add_argument("--change", type=int, help="a PR number")
    q.add_argument("--since", type=_utc, help="RFC 3339 time, e.g. 2026-10-01T00:00:00Z")
    q.add_argument("--failed", action="store_true", help="runs: only failed ones")
    q.add_argument("--run", help="results: the run id")
    q.add_argument("--unexpected", action="store_true", help="results: only unexpected ones")
    q.add_argument("--test", help="history: the test id")
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=cmd_query)

    sc = sub.add_parser("scorecard", help="plan §8's metrics from the store")
    sc.add_argument("--store", required=True)
    sc.add_argument("--days", type=int, default=7)
    sc.add_argument("--repo", action="append", help="also list a repo with no runs yet")
    sc.add_argument("--json", action="store_true")
    sc.set_defaults(func=cmd_scorecard)

    fa = sub.add_parser("failure", help="failure records with an issue mirror")
    fa.add_argument("action", choices=["open", "link", "list"])
    fa.add_argument("id", nargs="?", help="link: the record id")
    fa.add_argument("--dir", required=True, help="where records live (a store's failures/)")
    fa.add_argument("--kind", choices=[k.value for k in FailureKind])
    fa.add_argument("--repo", help="open: owner/name of the repo that failed")
    fa.add_argument("--subject", help="open: what failed (build digest, commit or run id)")
    for name in FAILURE_FIELDS:
        fa.add_argument("--" + name.replace("_", "-"), dest=name)
    fa.add_argument("--security", action="store_true",
                    help="open: security-looking; kept but never mirrored to a public issue")
    for name in failures.LINK_FIELDS:
        if name != "operation":
            fa.add_argument("--" + name.replace("_", "-"), dest=name, help="link")
    fa.add_argument("--mirror", metavar="OWNER/NAME",
                    help="mirror to one labelled issue in this repo (GITHUB_TOKEN, issues: write)")
    fa.add_argument("--github-output", default=os.environ.get("GITHUB_OUTPUT"),
                    help=argparse.SUPPRESS)
    fa.set_defaults(func=cmd_failure)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "failure":
        need = {"open": ("kind", "repo", "subject"), "link": ("id",)}.get(args.action, ())
        missing = [n for n in need if not getattr(args, n)]
        if missing:
            parser.error(f"failure {args.action} needs " + ", ".join("--" + m for m in missing))
    try:
        return args.func(args)
    except Error as e:
        print(f"qqresults: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
