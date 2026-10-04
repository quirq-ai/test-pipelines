"""The records of the results store (plan §5.4, §5.10).

    Change -> Run -> Result (write-once, normalized; raw only on opt-in) -> Verdict
    Failure links a red run, held canary, rollback or auto-revert to its culprit, operation and fix.

Every record is a frozen dataclass that serializes to canonical JSON (sorted keys, no spaces), so
the same record always has the same bytes and the same digest. Readers ignore fields they do not
know, so a later schema can add fields without breaking v0 readers. `Artifact` and `Operation`
belong to `release` and `remote-build`; a Failure refers to them by key.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import math
import re
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
        """Build a record from its JSON form, ignoring fields this version does not know.

        Every known field is type-checked (ValueError), so a record read from an artifact or the
        store holds what its annotations say, and a stored record cannot break a reader later.
        """
        if not isinstance(data, dict):
            raise ValueError(f"{cls.__name__}: expected a JSON object, not {_short(data)}")
        types = {f.name: f.type for f in dataclasses.fields(cls)}  # type: ignore[arg-type]
        kwargs = {}
        for key, value in data.items():
            if key not in types:
                continue
            if key in cls._nested:
                if value is not None:
                    if not isinstance(value, dict):
                        raise ValueError(f"{cls.__name__}.{key} must be an object or null, "
                                         f"not {_short(value)}")
                    value = cls._nested[key].from_dict(value)
            elif key in cls._nested_lists:
                if not isinstance(value, list):
                    raise ValueError(f"{cls.__name__}.{key} must be a list, not {_short(value)}")
                value = [cls._nested_lists[key].from_dict(v) for v in value]
            else:
                _check(cls.__name__, key, types[key], value)
            kwargs[key] = value
        return cls(**kwargs)


# Times are RFC 3339 in UTC to the second, as every writer here makes them, so that comparing the
# strings orders them (store queries and the scorecard window do).
TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")


def is_time(value: Any) -> bool:
    if not (isinstance(value, str) and TIME.fullmatch(value)):
        return False
    try:
        dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _short(value: Any) -> str:
    text = repr(value)
    return text if len(text) <= 60 else text[:57] + "..."


def _count(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def _number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _metric(v: Any) -> bool:
    """{value, unit}: a finite number and a non-empty unit; other keys are strings or numbers."""
    return (isinstance(v, dict) and _number(v.get("value"))
            and isinstance(v.get("unit"), str) and v["unit"].strip() != ""
            and all(isinstance(k, str) and (isinstance(x, str) or _number(x)) for k, x in v.items()))


# Annotation (as written in the dataclasses below) -> (check, what it must be).
_CHECKS: dict[str, tuple[Any, str]] = {
    "str": (lambda v: isinstance(v, str), "a string"),
    "bool": (lambda v: isinstance(v, bool), "true or false"),
    "int": (_count, "a non-negative integer"),
    "int | None": (lambda v: v is None or _count(v), "a non-negative integer or null"),
    "float | None": (lambda v: v is None or (_number(v) and v >= 0),
                     "a finite non-negative number or null"),
    "dict[str, str]": (lambda v: isinstance(v, dict)
                       and all(isinstance(x, str) for x in v.values()), "an object of strings"),
    "dict[str, int]": (lambda v: isinstance(v, dict) and all(_count(x) for x in v.values()),
                       "an object of non-negative integers"),
    "list[str]": (lambda v: isinstance(v, list) and all(isinstance(x, str) for x in v),
                  "a list of strings"),
    "dict[str, dict[str, Any]]": (lambda v: isinstance(v, dict) and all(map(_metric, v.values())),
                                  "an object of {value: finite number, unit: non-empty "
                                  "string} objects"),
}


def _check(record: str, key: str, annotation: str, value: Any) -> None:
    check, want = _CHECKS[annotation]
    if not check(value):
        raise ValueError(f"{record}.{key} must be {want}, not {_short(value)}")
    if key.endswith("_at") and value and not is_time(value):
        raise ValueError(f"{record}.{key} must be an RFC 3339 UTC time like "
                         f"2026-10-04T10:00:00Z, not {_short(value)}")


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
    base_commit: str = ""           # the tree without the change (e.g. <commit>^1 in a queue)
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
    job_status: str = ""            # how the job itself ended: success, failure or cancelled
    parent: str = ""                # for a retry or base run: the run whose failures it rechecks
    role: str = ""                  # "", "retry" or "base"
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
    message: str = ""               # the head of the failure or skip message (junit.MAX_MESSAGE)
    file: str = ""
    source: str = ""                # which report it came from, relative to the run's workspace
    raw: str = ""                   # the original report element, truncated; "" unless the sink
                                    # was asked to keep it (--keep-raw-junit)
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
    inputs: list[str] = field(default_factory=list)          # retry and base runs it also read
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
    # TODO(suraj): plan §8 also lists "operation" as needed to close a record; infra-config
    # postmortem.toml record_needs agrees with this set, so the two disagree. Hardcoded here, not
    # read from postmortem.toml.
    NEEDED_TO_CLOSE: ClassVar[tuple[str, ...]] = ("culprit", "fix", "covering_test")
