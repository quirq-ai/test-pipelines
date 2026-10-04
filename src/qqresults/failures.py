"""Failure records (plan §5.10); V0-TST-04.

One record per held canary, canary rollback, auto-revert or red run. The id is derived from the
kind, repo and subject (the build digest, commit or run that failed), so a pipeline that retries
or reports the same event twice still opens exactly one record. A record is written once; what is
learned later (culprit, fix, covering test, postmortem, issue) is added as separate link records.
It closes only when culprit, fix and covering test are all linked (postmortem.toml record_needs).

    <dir>/failure.json                 the Failure, write-once
    <dir>/links/<time>-<field>.json    {"field", "value", "at"}, each write-once

The same layout is a failure bundle (kept by the backend, e.g. as a workflow artifact) and its
place in the store (`<store>/failures/<dir>`).
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from qqresults.errors import Error
from qqresults.model import Failure, FailureKind, canonical_json

RECORD = "failure.json"
LINKS = "links"
LINK_FIELDS = Failure.LINKS + ("issue",)

# Words that make a failure look like a security issue. Such records are kept, but never mirrored
# to a public issue. TODO(suraj): where security-looking failures go instead (a private advisory,
# a private repo, or a person).
SECURITY_WORDS = re.compile(
    r"secur|vulnerab|\bcve-|exploit|overflow|use-after-free|out-of-bounds|injection|"
    r"credential|secret|token|password|private key|sandbox escape|privilege", re.IGNORECASE)


class FailureError(Error):
    pass


def now() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def failure_id(kind: str, repo: str, subject: str) -> str:
    if kind not in {k.value for k in FailureKind}:
        raise FailureError(f"unknown failure kind {kind!r}; known: "
                           + ", ".join(k.value for k in FailureKind))
    if not subject:
        raise FailureError("a failure needs a subject: the build digest, commit or run that failed")
    h = hashlib.sha256(f"{kind}\n{repo}\n{subject}".encode()).hexdigest()[:16]
    return f"{kind}-{h}"


def dirname(fid: str) -> str:
    return "qq-failure-" + re.sub(r"[^A-Za-z0-9._-]+", "_", fid)


def looks_security_related(f: Failure) -> bool:
    return f.security or bool(SECURITY_WORDS.search(" ".join((f.summary, f.signal, f.stage))))


def new(kind: str, repo: str, subject: str, **fields) -> Failure:
    known = {f.name for f in dataclasses.fields(Failure)} - {"id", "kind", "repo", "subject",
                                                             "opened_at", "schema"}
    unknown = set(fields) - known
    if unknown:
        raise FailureError(f"unknown failure field(s): {', '.join(sorted(unknown))}")
    f = Failure(id=failure_id(kind, repo, subject), kind=kind, repo=repo, subject=subject,
                opened_at=now(), **fields)
    if looks_security_related(f) and not f.security:
        f = dataclasses.replace(f, security=True)
    return f


@dataclass
class State:
    """A record with its links folded in: what is known about the failure now."""
    record: Failure
    links: dict[str, str]
    path: Path

    @property
    def current(self) -> Failure:
        return dataclasses.replace(self.record, **{k: v for k, v in self.links.items()
                                                   if k in Failure.LINKS})

    @property
    def missing(self) -> list[str]:
        cur = self.current
        return [n for n in Failure.NEEDED_TO_CLOSE if not getattr(cur, n)]

    @property
    def closed(self) -> bool:
        return not self.missing


def _write_once(path: Path, text: str) -> None:
    with open(path, "x", encoding="utf-8") as f:
        f.write(text)


def open_record(f: Failure, parent: Path) -> tuple[State, bool]:
    """Write the record under parent unless it exists. Returns (state, created)."""
    path = parent / dirname(f.id)
    if (path / RECORD).is_file():
        return read(path), False
    path.mkdir(parents=True, exist_ok=True)
    (path / LINKS).mkdir(exist_ok=True)
    try:
        _write_once(path / RECORD, f.to_json() + "\n")
    except FileExistsError:
        return read(path), False
    return read(path), True


def add_link(path: Path, field: str, value: str) -> None:
    if field not in LINK_FIELDS:
        raise FailureError(f"cannot link {field!r}; links are {', '.join(LINK_FIELDS)}")
    if not value:
        raise FailureError(f"link {field}: empty value")
    at = now()
    body = canonical_json({"field": field, "value": value, "at": at})
    digest = hashlib.sha256(body.encode()).hexdigest()[:8]
    (path / LINKS).mkdir(exist_ok=True)
    _write_once(path / LINKS / f"{at.replace(':', '')}-{field}-{digest}.json", body + "\n")


def read(path: Path) -> State:
    try:
        record = Failure.from_dict(json.loads((path / RECORD).read_text(encoding="utf-8")))
        links: dict[str, str] = {}
        for p in sorted((path / LINKS).glob("*.json")) if (path / LINKS).is_dir() else []:
            link = json.loads(p.read_text(encoding="utf-8"))
            links[link["field"]] = link["value"]   # later links win
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as e:
        raise FailureError(f"{path}: not a readable failure record: {e}") from None
    return State(record, links, path)


def import_dir(src: Path, parent: Path) -> bool:
    """Copy a failure bundle into the store: the record if new, and any links not yet there.

    The first record with an id wins; a later one (the same event reported again) adds nothing.
    Returns True if anything was added.
    """
    state = read(src)
    dest = parent / dirname(state.record.id)
    added = False
    parent.mkdir(parents=True, exist_ok=True)
    if not (dest / RECORD).is_file():
        with tempfile.TemporaryDirectory(dir=parent) as tmp:
            staged = Path(tmp) / dest.name
            staged.mkdir()
            shutil.copyfile(src / RECORD, staged / RECORD)
            shutil.move(staged, dest)   # a rename: the record appears whole
        added = True
    (dest / LINKS).mkdir(exist_ok=True)
    for link in sorted((src / LINKS).glob("*.json")) if (src / LINKS).is_dir() else []:
        if not (dest / LINKS / link.name).exists():
            shutil.copyfile(link, dest / LINKS / link.name)
            added = True
    return added


def issue_body(state: State) -> str:
    f = state.current
    rows = [("Kind", f.kind), ("Repo", f.repo), ("Subject", f.subject), ("Opened", f.opened_at),
            ("Channel", f.channel), ("Build digest", f.build_digest), ("Last good", f.last_good),
            ("First bad", f.first_bad), ("Stage", f.stage), ("Signal", f.signal),
            ("Run", f.run_id), ("Operation", f.operation), ("Culprit", f.culprit),
            ("Fix", f.fix), ("Covering test", f.covering_test), ("Failure class", f.failure_class),
            ("Postmortem", f.postmortem)]
    table = "\n".join(f"| {k} | {v.replace('|', '/') if v else '_not yet_'} |" for k, v in rows)
    status = ("closed: culprit, fix and covering test are linked" if state.closed
              else "open until " + ", ".join(m.replace("_", " ") for m in state.missing)
              + " are linked")
    return (f"<!-- qq-failure: {f.id} -->\n"
            f"Failure record `{f.id}` from quirq infra (test-pipelines, plan §5.10). "
            f"This issue mirrors the record; the record is the source of truth.\n\n"
            f"{f.summary}\n\n| Field | Value |\n|---|---|\n{table}\n\nStatus: {status}.\n")


def issue_title(f: Failure) -> str:
    what = f.summary.splitlines()[0][:80] if f.summary else f.subject[:40]
    return f"[qq {f.kind}] {f.repo}: {what}"
