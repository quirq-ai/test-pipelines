"""The records of the results store (plan §5.4, §5.10).

    Change -> Run -> Result (write-once, raw plus normalized) -> Verdict
    Failure links a red run, held canary, rollback or auto-revert to its culprit, operation and fix.

Every record is a frozen dataclass that serializes to canonical JSON (sorted keys, no spaces), so
the same record always has the same bytes and the same digest. Readers ignore fields they do not
know, so a later schema can add fields without breaking v0 readers. `Artifact` and `Operation`
belong to `release` and `remote-build`; a Failure refers to them by key.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, ClassVar, Self

SCHEMA = "quirq-results/1"


class Status(StrEnum):
    """How one test or action ended. The names follow ResultDB's TestStatus."""
    PASS = "PASS"
    FAIL = "FAIL"      # an assertion failed
    CRASH = "CRASH"    # an error outside the assertion: setup, import, timeout, a dead process
    SKIP = "SKIP"


class RunKind(StrEnum):
    PRESUBMIT = "presubmit"    # a check on an open change, before it enters the queue
    GATE = "gate"              # the merge queue verifying the exact merge result
    POSTSUBMIT = "postsubmit"  # a commit that landed on the default branch
    CANARY = "canary"          # a stage of the daily canary pipeline
    OTHER = "other"            # anything else: a timer, a manual dispatch, a push to a side branch
    LOCAL = "local"            # a developer's machine; never counted on the scorecard


class VerdictStatus(StrEnum):
    """A test's verdict within a run. The names follow ResultDB's TestVariantStatus."""
    EXPECTED = "EXPECTED"        # every result passed or was skipped
    UNEXPECTED = "UNEXPECTED"    # every result failed: this fails the run
    FLAKY = "FLAKY"              # failed, then passed with the same inputs
    EXONERATED = "EXONERATED"    # failed with the change and also without it: not this change's fault


class _Record:
    _nested: ClassVar[dict[str, type[_Record]]] = {}       # field -> record type
    _nested_lists: ClassVar[dict[str, type[_Record]]] = {}  # field -> type of each list item

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)  # type: ignore[call-overload]

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """Build a record from its JSON form, ignoring fields this version does not know."""
        known = {f.name for f in dataclasses.fields(cls)}  # type: ignore[arg-type]
        kwargs = {}
        for key, value in data.items():
            if key not in known:
                continue
            if key in cls._nested and isinstance(value, dict):
                value = cls._nested[key].from_dict(value)
            elif key in cls._nested_lists:
                value = [cls._nested_lists[key].from_dict(v) for v in value or []]
            kwargs[key] = value
        return cls(**kwargs)


def canonical_json(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


@dataclass(frozen=True)
class Change(_Record):
    """A unit of intent: a pull request, or a commit pushed straight to a branch."""
    repo: str                       # "quirq-ai/xo-space"
    number: int | None = None       # the PR number, when there is one
    head_sha: str = ""
    base_sha: str = ""


@dataclass(frozen=True)
class Run(_Record):
    """One attempt at verifying a commit: a gate, post-submit or canary run of one job."""
    id: str                         # unique per backend; see backends/github.py
    repo: str
    kind: str                       # a RunKind
    commit: str                     # the commit that was tested (for the gate: the merge result)
    backend: str = ""               # which backend described the run; see backends/
    base_commit: str = ""           # the base it was merged onto, when known
    branch: str = ""
    change: Change | None = None
    workflow: str = ""
    job: str = ""
    attempt: int = 1
    url: str = ""
    queued_at: str = ""             # RFC 3339 UTC; gate timing (V0-GAT-04) reads these
    started_at: str = ""
    finished_at: str = ""
    config_revision: str = ""       # the infra-config commit the run used, when known
    adapters: dict[str, str] = field(default_factory=dict)   # kind -> recipes version
    executor: str = ""
    results_found: bool = True      # False when the run produced no test results at all
    schema: str = SCHEMA

    _nested: ClassVar[dict[str, type[_Record]]] = {"change": Change}


@dataclass(frozen=True)
class Result(_Record):
    """One outcome of one test (or of one non-test step reported as a test). Write-once."""
    run_id: str
    test_id: str                    # stable across runs: "<classname>::<name>", or the name alone
    status: str                     # a Status
    expected: bool                  # PASS and SKIP are expected; FAIL and CRASH are not
    duration_s: float | None = None
    message: str = ""               # the failure or skip message, truncated
    file: str = ""
    source: str = ""                # which report it came from, relative to the run's workspace
    raw: str = ""                   # the original report element, truncated
    metrics: dict[str, dict[str, Any]] = field(default_factory=dict)  # name -> {value, unit}; for bench
    schema: str = SCHEMA

    @property
    def id(self) -> str:
        """Content address: the same outcome recorded twice has the same id."""
        return digest(self.to_json())


@dataclass(frozen=True)
class CaseVerdict(_Record):
    test_id: str
    status: str                     # a VerdictStatus
    reason: str = ""


@dataclass(frozen=True)
class Verdict(_Record):
    """Computed mechanically from a run's Results; never typed by hand."""
    run_id: str
    passed: bool                    # False when any test is UNEXPECTED, or no results were found
    counts: dict[str, int] = field(default_factory=dict)      # VerdictStatus -> number of tests
    tests: list[CaseVerdict] = field(default_factory=list)    # only the tests that were not EXPECTED
    reason: str = ""
    schema: str = SCHEMA

    _nested_lists: ClassVar[dict[str, type[_Record]]] = {"tests": CaseVerdict}


class FailureKind(StrEnum):
    CANARY_HELD = "canary-held"
    CANARY_ROLLBACK = "canary-rollback"
    AUTO_REVERT = "auto-revert"
    RED_RUN = "red-run"
    FUZZ = "fuzz"


@dataclass(frozen=True)
class Failure(_Record):
    """A failure record (plan §5.10). Opened write-once; links are added as separate records.

    It closes only when culprit, fix and covering test are all linked (postmortem.toml
    `record_needs`).
    """
    id: str                         # derived from kind and subject, so one event opens one record
    kind: str                       # a FailureKind
    repo: str
    subject: str                    # what failed: a build digest, a commit, a run id
    opened_at: str
    channel: str = ""
    build_digest: str = ""
    last_good: str = ""
    first_bad: str = ""
    stage: str = ""
    signal: str = ""                # the health signal or test that fired
    run_id: str = ""
    operation: str = ""             # the key of the operation that held or rolled it back
    culprit: str = ""               # the culprit change, from bisection
    fix: str = ""
    covering_test: str = ""
    failure_class: str = ""
    postmortem: str = ""
    security: bool = False          # security-looking: never mirrored to a public issue
    summary: str = ""
    schema: str = SCHEMA

    LINKS: ClassVar[tuple[str, ...]] = (
        "culprit", "fix", "covering_test", "operation", "postmortem", "failure_class")
    NEEDED_TO_CLOSE: ClassVar[tuple[str, ...]] = ("culprit", "fix", "covering_test")
