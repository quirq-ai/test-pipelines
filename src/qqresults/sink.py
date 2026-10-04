"""The result sink: JUnit reports from one job become one run bundle."""
from __future__ import annotations

import glob
from pathlib import Path

from qqresults import bundle, junit, retry, verdict
from qqresults.errors import Error
from qqresults.model import Result, Run
from qqresults.policy import Policy


class SinkError(Error):
    pass


def find_reports(patterns: list[str], root: Path) -> list[Path]:
    """Every file matching any pattern (relative to root, `**` allowed), sorted, without repeats."""
    found: set[Path] = set()
    for pattern in patterns:
        if Path(pattern).is_absolute() or ".." in Path(pattern).parts:
            raise SinkError(f"report glob {pattern!r} must be relative to the workspace, "
                            "without '..'")
        for match in glob.glob(pattern, root_dir=root, recursive=True):
            path = root / match
            if path.is_file():
                found.add(path)
    return sorted(found)


def sink(run: Run, patterns: list[str], root: Path, out: Path, rerun_cmd: str = "",
         policy: Policy = Policy(), base_commit: str = "",
         setup: str = "") -> tuple[Path, bundle.Bundle]:
    """Write the run's bundle under out. With rerun_cmd, failed tests are first retried and
    compared with base (retry.py), and those runs are written next to it."""
    reports = find_reports(patterns, root)
    results: list[Result] = []
    for path in reports:
        rel = path.relative_to(root).as_posix() if path.is_relative_to(root) else str(path)
        results.extend(junit.parse_file(path, run.id, source=rel))
    if not reports:
        run = Run.from_dict({**run.to_dict(), "results_found": False})
    if rerun_cmd:
        checked = retry.recheck(run, results, rerun_cmd, root, policy, base_commit, setup=setup)
        for extra in checked.retries + checked.bases:
            bundle.write(extra, out)
        v = checked.verdict
    else:
        v = verdict.compute(run, results)
    b = bundle.Bundle(run, results, v)
    return bundle.write(b, out), b
