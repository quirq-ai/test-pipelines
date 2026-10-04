"""qqresults: test results, verdicts and failure records for quirq infra (qq).

    qqresults sink --junit GLOB [--junit GLOB ...] --out DIR [--backend github|local] [--kind K]
                   [--rerun CMD [--base SHA] [--infra-config PATH]] [--fail-on-verdict]
        Normalize this job's JUnit reports into one write-once run bundle under DIR; with
        --rerun, retry failed tests and compare them with base first (V0-TST-03).
    qqresults show BUNDLE_DIR [--json]
        Print a bundle's verdict.
    qqresults import --store DIR BUNDLE_DIR...
        Add sink bundles to the results store (write-once).
    qqresults collect --store DIR --repo OWNER/NAME...
        Add every bundle the repos' GitHub workflow runs kept (token from GITHUB_TOKEN).
    qqresults query runs|results|history --store DIR [filters] [--json]
        Read the store.
    qqresults scorecard --store DIR [--days N] [--json]
        Plan §8's metrics from the store.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

from qqresults import __version__, backends, bundle, policy, scorecard, sink, store
from qqresults.errors import Error
from qqresults.model import RunKind


def _run(args):
    backend = backends.load(args.backend)
    if args.backend == "github":
        return backend.run_from_env(os.environ, kind=args.kind or "", name=args.name)
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
                        rerun_cmd=args.rerun or "", policy=pol, base_commit=args.base or "")
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
    failed = False
    for repo in args.repo:
        try:
            new, old, errors = gh.collect(repo, st, token)
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
    card = scorecard.compute(store.open_store(args.store), until - dt.timedelta(days=args.days),
                             until, repos=args.repo)
    if args.json:
        print(json.dumps(card.to_dict(), indent=2, sort_keys=True))
    else:
        print(scorecard.to_markdown(card), end="")
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
    s.add_argument("--commit", help="local backend: the commit tested")
    s.add_argument("--rerun", metavar="CMD",
                   help="retry failed tests with this shell command, then compare with base "
                        "(it writes JUnit to $QQ_JUNIT_DIR; $QQ_RETRY_TESTS lists the failed ids)")
    s.add_argument("--base", help="the base commit for --rerun (default: the run's base)")
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
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except Error as e:
        print(f"qqresults: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
