"""The local backend: runs described by flags, for a developer's machine and for tests."""
from __future__ import annotations

import datetime as dt
import uuid

from qqresults.model import Run, RunKind


def run_from_args(repo: str, commit: str, kind: str = RunKind.LOCAL.value, name: str = "",
                  run_id: str = "") -> Run:
    now = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    rid = run_id or uuid.uuid4().hex[:12]
    return Run(id=f"local/{repo}/{rid}" + (f"/{name}" if name else ""), repo=repo, kind=kind,
               commit=commit, backend="local", finished_at=now)
