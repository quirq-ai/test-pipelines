"""A run bundle: one run's records as files, written once.

    <dir>/run.json        the Run
    <dir>/results.jsonl   one Result per line, in report order
    <dir>/verdict.json    the Verdict computed from them

The sink writes a bundle where the tests ran; the backend then keeps it (on GitHub, as a workflow
artifact named after the directory), and the results store (V0-TST-02) imports it. Files are
created exclusively, so a bundle is never overwritten.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from qqresults.errors import Error
from qqresults.model import Result, Run, Verdict

RUN = "run.json"
RESULTS = "results.jsonl"
VERDICT = "verdict.json"


class BundleError(Error):
    pass


@dataclass(frozen=True)
class Bundle:
    run: Run
    results: list[Result]
    verdict: Verdict


def dirname(run: Run) -> str:
    """A file and artifact name for the run: readable, and safe on every filesystem."""
    return "qq-results-" + re.sub(r"[^A-Za-z0-9._-]+", "_", run.id).strip("_")


def write(bundle: Bundle, parent: Path) -> Path:
    path = parent / dirname(bundle.run)
    try:
        path.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        raise BundleError(f"{path}: a bundle for run {bundle.run.id} already exists; "
                          "results are write-once") from None
    lines = "".join(r.to_json() + "\n" for r in bundle.results)
    for name, text in ((RUN, bundle.run.to_json() + "\n"), (RESULTS, lines),
                       (VERDICT, bundle.verdict.to_json() + "\n")):
        with open(path / name, "x", encoding="utf-8") as f:
            f.write(text)
    return path


def read(path: Path) -> Bundle:
    try:
        run = Run.from_dict(json.loads((path / RUN).read_text(encoding="utf-8")))
        results = [Result.from_dict(json.loads(line))
                   for line in (path / RESULTS).read_text(encoding="utf-8").splitlines() if line]
        verdict = Verdict.from_dict(json.loads((path / VERDICT).read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError) as e:
        raise BundleError(f"{path}: not a readable results bundle: {e}") from None
    return Bundle(run, results, verdict)
