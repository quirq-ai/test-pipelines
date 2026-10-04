"""The result sink: JUnit reports from one job become one run bundle."""
from __future__ import annotations

import glob
from pathlib import Path

from qqresults import bundle, junit, verdict
from qqresults.errors import Error
from qqresults.model import Result, Run


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


def sink(run: Run, patterns: list[str], root: Path, out: Path) -> tuple[Path, bundle.Bundle]:
    reports = find_reports(patterns, root)
    results: list[Result] = []
    for path in reports:
        rel = path.relative_to(root).as_posix() if path.is_relative_to(root) else str(path)
        results.extend(junit.parse_file(path, run.id, source=rel))
    if not reports:
        run = Run.from_dict({**run.to_dict(), "results_found": False})
    b = bundle.Bundle(run, results, verdict.compute(run, results))
    return bundle.write(b, out), b
