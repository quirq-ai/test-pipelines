"""The results store v0: write-once run bundles in a directory tree, with simple queries.

    <root>/runs/<bundle name>/run.json, results.jsonl, verdict.json
    <root>/failures/<record dir>/failure.json, links/*.json      (failures.py, V0-TST-04)

v0 has no cloud, so the tree is plain files. On GitHub it lives on this repo's `results` branch
(see .github/workflows/scorecard.yml), which keeps every run past the 90 days GitHub keeps
artifacts. Queries scan run.json files, which is fine at v0's volume.
TODO(expert): a real database behind the same interface once volume or Launchpad calls for it.

Write-once: importing a run that is already stored is a no-op when the bytes match and an error
when they differ. Nothing here edits or deletes a stored record.
"""
from __future__ import annotations

import filecmp
import json
import shutil
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from qqresults import bundle, failures
from qqresults.errors import Error
from qqresults.model import Result, Run, Verdict


class StoreError(Error):
    pass


@dataclass(frozen=True)
class RunFilter:
    repo: str = ""
    kind: str = ""
    commit: str = ""
    branch: str = ""
    change: int | None = None
    since: str = ""            # RFC 3339 in UTC ("...Z"); compared with the run's finished_at
    failed: bool | None = None

    def matches(self, run: Run, verdict: Verdict | None) -> bool:
        if self.repo and run.repo != self.repo:
            return False
        if self.kind and run.kind != self.kind:
            return False
        if self.commit and not run.commit.startswith(self.commit):
            return False
        if self.branch and run.branch != self.branch:
            return False
        if self.change is not None and (run.change is None or run.change.number != self.change):
            return False
        if self.since and run.finished_at < self.since:
            return False
        if self.failed is not None and (verdict is None or verdict.passed == self.failed):
            return False
        return True


class FileStore:
    """The `files` store backend."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.runs_dir = self.root / "runs"
        self.failures_dir = self.root / "failures"
        self.skipped: dict[str, str] = {}   # stored records that did not read, by directory

    # --- writing ------------------------------------------------------------------------------

    def put(self, b: bundle.Bundle) -> bool:
        """Store a bundle. True if it was new, False if the same run was already stored."""
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=self.root) as tmp:
            staged = bundle.write(b, Path(tmp))
            return self._commit(staged, b.run.id)

    def import_dir(self, path: Path) -> bool:
        """Store a bundle directory as the sink wrote it, byte for byte, once it reads cleanly.

        Copying the files (rather than re-serializing) keeps fields a newer sink added.
        """
        b = bundle.read(path)
        expected = bundle.dirname(b.run)
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=self.root) as tmp:
            staged = Path(tmp) / expected
            staged.mkdir()
            for f in bundle.FILES:
                shutil.copyfile(path / f, staged / f)
            return self._commit(staged, b.run.id)

    def _commit(self, staged: Path, run_id: str) -> bool:
        final = self.runs_dir / staged.name
        if final.exists():
            if all((final / f).is_file() and filecmp.cmp(staged / f, final / f, shallow=False)
                   for f in bundle.FILES):
                return False
            raise StoreError(f"run {run_id} is already stored with different contents; "
                             "results are write-once")
        shutil.move(staged, final)  # a rename within one filesystem: the bundle appears whole
        return True

    # --- reading ------------------------------------------------------------------------------

    def _dirs(self) -> Iterator[Path]:
        if self.runs_dir.is_dir():
            yield from sorted(p for p in self.runs_dir.iterdir() if (p / bundle.RUN).is_file())

    def runs(self, flt: RunFilter = RunFilter()) -> list[tuple[Run, Verdict]]:
        """Matching runs with their verdicts, oldest first by finished_at.

        A stored run that does not read cleanly is skipped and named in `skipped`, so one bad
        record cannot stop every later query or scorecard (the store is write-once).
        """
        out = []
        for d in self._dirs():
            try:
                run = Run.from_dict(json.loads((d / bundle.RUN).read_text(encoding="utf-8")))
                v = Verdict.from_dict(json.loads((d / bundle.VERDICT).read_text(encoding="utf-8")))
            except (OSError, ValueError, TypeError) as e:
                self._skip(d, e)
                continue
            if flt.matches(run, v):
                out.append((run, v))
        return sorted(out, key=lambda rv: (rv[0].finished_at, rv[0].id))

    def _skip(self, path: Path, error: Exception) -> None:
        self.skipped[path.name] = f"{path}: not readable, skipped: {error}"

    def bundle(self, run_id: str) -> bundle.Bundle:
        path = self.runs_dir / bundle.dirname(Run(id=run_id, repo="", kind="", commit=""))
        if not (path / bundle.RUN).is_file():
            raise StoreError(f"run {run_id} is not in the store at {self.root}")
        b = bundle.read(path)
        if b.run.id != run_id:
            raise StoreError(f"run {run_id} is not in the store at {self.root} "
                             f"({path.name} holds run {b.run.id})")
        return b

    def results(self, run_id: str) -> list[Result]:
        return self.bundle(run_id).results

    def history(self, test_id: str, flt: RunFilter = RunFilter()) -> list[tuple[Run, Result]]:
        """Every stored result of one test, oldest run first.

        A stored run whose results do not read cleanly is skipped and named in `skipped`, as
        runs() does, so one bad bundle cannot stop the query.
        """
        out = []
        for run, _ in self.runs(flt):
            try:
                results = self.results(run.id)
            except Error as e:   # BundleError, or StoreError when the run is not where its id says
                self._skip(self.runs_dir / bundle.dirname(run), e)
                continue
            out.extend((run, r) for r in results if r.test_id == test_id)
        return out

    def has(self, bundle_name: str) -> bool:
        return (self.runs_dir / bundle_name / bundle.RUN).is_file()

    # --- failure records ----------------------------------------------------------------------

    def import_failure(self, path: Path) -> bool:
        return failures.import_dir(path, self.failures_dir)

    def import_links(self, path: Path, at_most=None) -> bool:
        return failures.import_links(path, self.failures_dir, at_most)

    def stored_failure(self, fid: str):
        """The stored record with this id (a model.Failure), or None when there is none."""
        path = self.failures_dir / failures.dirname(fid)
        return failures.read(path).record if (path / failures.RECORD).is_file() else None

    def seen_artifact(self, name: str) -> bool:
        """Whether a failure artifact was already imported (artifacts are immutable once uploaded)."""
        return (self.failures_dir / ".artifacts" / name).is_file()

    def mark_artifact(self, name: str) -> None:
        (self.failures_dir / ".artifacts").mkdir(parents=True, exist_ok=True)
        (self.failures_dir / ".artifacts" / name).touch()

    def failures(self) -> list[failures.State]:
        if not self.failures_dir.is_dir():
            return []
        out = []
        for d in sorted(self.failures_dir.iterdir()):
            if (d / failures.RECORD).is_file():
                try:
                    out.append(failures.read(d))
                except Error as e:
                    self._skip(d, e)
        return sorted(out, key=lambda st: (st.record.opened_at, st.record.id))

    def failure(self, fid: str) -> failures.State:
        path = self.failures_dir / failures.dirname(fid)
        if not (path / failures.RECORD).is_file():
            raise StoreError(f"failure {fid} is not in the store at {self.root}")
        return failures.read(path)


def open_store(location: str | Path, backend: str = "files") -> FileStore:
    if backend != "files":
        raise StoreError(f"unknown store backend {backend!r}; v0 has only 'files'")
    return FileStore(Path(location))
