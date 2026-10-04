"""qqresults: test results, verdicts and failure records for quirq infra (qq).

    qqresults sink --junit GLOB [--junit GLOB ...] --out DIR [--backend github|local] [--kind K]
        Normalize this job's JUnit reports into one write-once run bundle under DIR.
    qqresults show BUNDLE_DIR [--json]
        Print a bundle's verdict.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from qqresults import __version__, backends, bundle, sink
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
    path, b = sink.sink(run, args.junit, Path(args.root).resolve(), Path(args.out))
    v = b.verdict
    print(f"run {run.id} ({run.kind}): {len(b.results)} result(s) -> {path}")
    print(f"verdict: {'PASS' if v.passed else 'FAIL'} {v.counts} {v.reason}".rstrip())
    if args.github_output:
        with open(args.github_output, "a", encoding="utf-8") as f:
            f.write(f"bundle={path}\nname={path.name}\npassed={str(v.passed).lower()}\n")
    return 0


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
    s.add_argument("--github-output", default=os.environ.get("GITHUB_OUTPUT"),
                   help=argparse.SUPPRESS)
    s.set_defaults(func=cmd_sink)

    sh = sub.add_parser("show", help="print a bundle's verdict")
    sh.add_argument("bundle")
    sh.add_argument("--json", action="store_true")
    sh.set_defaults(func=cmd_show)
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
