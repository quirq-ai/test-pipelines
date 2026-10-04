"""Failure records (plan §5.10); V0-TST-04.

One record per held canary, canary rollback, auto-revert or red run. The id is derived from the
kind, repo and subject (the build digest, commit or run that failed), so a pipeline that retries
or reports the same event twice still opens exactly one record. A record is written once; what is
learned later (culprit, fix, covering test, postmortem, issue) is added as separate link records.
It closes only when culprit, fix and covering test are all linked (postmortem.toml record_needs).

    <dir>/failure.json                 the Failure, write-once
    <dir>/links/<time>-<field>.json    {"field", "value", "at"}, each write-once

Free text (the summary, and any value public_value() does not allow) stays in the record. What
goes public (the issue, and the copy uploaded as an artifact) is public_view(): structured fields
only, plus the summary when the caller opted in with a public_summary mark. A security record
goes public only as its id and security mark (public_copy), so the store learns the mark.

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
# Marks are links whose value is "true": security (re-reported or flagged later as security)
# and public_summary (the caller opted in to showing the summary publicly).
MARKS = ("security", "public_summary")
VALUE_LINKS = Failure.LINKS + ("issue",)   # links that carry a value
LINK_FIELDS = VALUE_LINKS + MARKS          # every field a link file may have

# Words that make a failure look like a security issue. Such records are kept, but never mirrored
# to a public issue. TODO(suraj): where security-looking failures go instead (a private advisory,
# a private repo, or a person).
SECURITY_WORDS = re.compile(
    r"secur|vulnerab|\bcve-|exploit|overflow|use-after-free|out-of-bounds|injection|"
    r"credential|secret|token|password|private key|sandbox|privilege|privesc|"
    r"\bxss\b|\brce\b|ssrf|csrf|bypass|unauthori|unauthenticated|\bauthn?\b|leak|"
    r"sanitizer|\basan\b|\bmsan\b|\bubsan\b|heap|traversal|\bdos\b|redos|denial of service|"
    r"deserializ|memory corruption|arbitrary code|\bsqli\b|api[ _-]?key|ssh[ _-]?key|\bauthz\b|"
    r"passwd|attacker|\bpii\b|\bghsa-|double free|certificate|open redirect|malicious|"
    r"access control|sensitive data|segv|segfault|sigabrt|sigbus|stack smash|\buaf\b|"
    r"use after free|\boob\b|out of bounds|over-?read|\bxxe\b|prototype pollution|\bjwt|"
    r"\bcors\b|toctou|crash|panic|authentication|authorization|\bidor\b|\bssti\b|\bcwe-|"
    r"spoof|impersonat|smuggl|without login|reachable without", re.IGNORECASE)
# Errs towards withholding: "memory leak" or "tokenizer" match too, and only cost a public issue.
# Fuzz findings are treated as security-looking by default (postmortem.toml fuzz-security-crash).
SECURITY_KINDS = {FailureKind.FUZZ.value}


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


def looks_security_related(f: Failure, links: dict[str, str] | None = None) -> bool:
    """True when the record or any link reads like a security issue. Errs towards True."""
    if f.security or f.kind in SECURITY_KINDS or (links or {}).get("security"):
        return True
    text = " ".join([v for v in f.to_dict().values() if isinstance(v, str)]
                    + list((links or {}).values()))
    return bool(SECURITY_WORDS.search(text))


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
    def security(self) -> bool:
        return looks_security_related(self.record, self.links)

    @property
    def public_summary(self) -> bool:
        return bool(self.links.get("public_summary"))

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
    """Write the record under parent unless it exists. Returns (state, created).

    The record is write-once, so a repeat report that is (or reads as) security-related is kept
    as a security mark; the State then turns security and the mirror withdraws any public issue.
    """
    path = parent / dirname(f.id)
    if not (path / RECORD).is_file():
        path.mkdir(parents=True, exist_ok=True)
        (path / LINKS).mkdir(exist_ok=True)
        try:
            _write_once(path / RECORD, f.to_json() + "\n")
            return read(path), True
        except FileExistsError:
            pass
    if looks_security_related(f):
        mark(path, "security")
    return read(path), False


def add_link(path: Path, field: str, value: str) -> None:
    if field not in LINK_FIELDS:
        raise FailureError(f"cannot link {field!r}; links are {', '.join(VALUE_LINKS)}")
    if not value:
        raise FailureError(f"link {field}: empty value")
    at = now()
    body = canonical_json({"field": field, "value": value, "at": at})
    digest = hashlib.sha256(body.encode()).hexdigest()[:8]
    (path / LINKS).mkdir(exist_ok=True)
    _write_once(path / LINKS / f"{at.replace(':', '')}-{field}-{digest}.json", body + "\n")


def mark(path: Path, name: str) -> None:
    """Set a mark (see MARKS) unless it is set already."""
    if not read(path).links.get(name):
        add_link(path, name, "true")


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


# What may be shown publicly. Anything else may be free text, which could describe a
# vulnerability, so it is withheld: when unsure, withhold.
#   - a commit (7 to 40 hex, or 64), or a digest: sha1:<40 hex>, sha256:<64 hex> (or the 16-hex
#     digest public_view itself writes for a free-text subject), sha512:<128 hex>;
#   - owner/repo@<commit> or owner/repo#<number>;
#   - https://github.com/owner/repo/(pull|issues)/<n>, .../commit/<hex>, .../actions/runs/<n>
#     (optionally /job/<n> or /attempts/<n>); no other URL;
#   - the action's run id, github/owner/repo/<run>/<attempt>/<job>.
# The owner must be the record's own owner (an org's repo names are made on purpose, not prose).
_OWNER = r"[A-Za-z0-9][A-Za-z0-9-]{0,38}"
_NAME_RE = r"[A-Za-z0-9._-]{1,100}"
_COMMIT = r"[0-9a-f]{7,40}"
_VALUE = re.compile(
    rf"{_COMMIT}|[0-9a-f]{{64}}|sha1:[0-9a-f]{{40}}|sha256:(?:[0-9a-f]{{16}}|[0-9a-f]{{64}})|"
    rf"sha512:[0-9a-f]{{128}}|"
    rf"(?P<o1>{_OWNER})/{_NAME_RE}(?:@{_COMMIT}|#[0-9]{{1,10}})|"
    rf"https://github\.com/(?P<o2>{_OWNER})/{_NAME_RE}/(?:(?:pull|issues)/[0-9]{{1,10}}|"
    rf"commit/{_COMMIT}|actions/runs/[0-9]{{1,20}}(?:/(?:job|attempts)/[0-9]{{1,20}})?)|"
    rf"github/(?P<o3>{_OWNER})/{_NAME_RE}/[0-9]{{1,20}}/[0-9]{{1,5}}/[A-Za-z0-9_.-]{{1,100}}")
_REPO = re.compile(rf"{_OWNER}/{_NAME_RE}")
# Stage, signal and channel are short names ("probe", "health", "stable"): no spaces.
_LABEL = re.compile(r"[A-Za-z0-9_.-]{1,40}")
LABEL_FIELDS = ("stage", "signal", "channel")
VALUE_FIELDS = ("build_digest", "last_good", "first_bad", "run_id") + Failure.LINKS
WITHHELD = "withheld"


def public_value(v: str, repo: str, field: str = "") -> str:
    """v as it may be shown publicly for a record of repo: as it is, or WITHHELD."""
    if not v:
        return v
    if field in LABEL_FIELDS:
        return v if _LABEL.fullmatch(v) else WITHHELD
    if field == "repo":
        return v if _REPO.fullmatch(v) else WITHHELD
    m = _VALUE.fullmatch(v)
    if not m:
        return WITHHELD
    owner = next((o for o in m.group("o1", "o2", "o3") if o), None)
    if owner is not None and not (_REPO.fullmatch(repo)
                                  and owner.lower() == repo.split("/")[0].lower()):
        return WITHHELD
    return v


def public_view(f: Failure, public_summary: bool = False) -> Failure:
    """The failure as it may be shown publicly: every free-text field is withheld unless it is a
    short name (stage, signal, channel) or a commit, digest, own-org reference or GitHub URL; a
    free-text subject is replaced by its digest, and the summary is dropped unless the caller
    opted in. Kind, id and opened_at are not free text (an enum, a hash, a time)."""
    subject = f.subject if public_value(f.subject, f.repo) == f.subject else (
        "sha256:" + hashlib.sha256(f.subject.encode()).hexdigest()[:16])
    return dataclasses.replace(
        f, repo=public_value(f.repo, f.repo, "repo"), subject=subject,
        summary=f.summary if public_summary else "",
        **{k: public_value(getattr(f, k), f.repo, k) for k in LABEL_FIELDS + VALUE_FIELDS})


def _public_link(link: dict, repo: str) -> dict:
    field = link["field"]
    value = "true" if field in MARKS else public_value(link["value"], repo, field)
    return {**link, "value": value}


def public_copy(state: State, parent: Path) -> Path:
    """Write what of a record may be uploaded publicly, as a bundle under parent.

    Rewritten on every call (it is derived, not a record). A record that is not security-related
    gets its public view, with its links passed through the same filter. A security record gets a
    marks-only bundle instead: its id, kind, structured subject and run, and the security mark,
    so the store learns the mark (and stops mirroring it) without learning anything else.
    """
    dest = parent / state.path.name
    shutil.rmtree(dest, ignore_errors=True)
    (dest / LINKS).mkdir(parents=True)
    if state.security:
        mark(state.path, "security")   # a link file, so the store gets it like any other link
        state = read(state.path)
        pub = public_view(state.record)
        record = Failure(id=pub.id, kind=pub.kind, repo=pub.repo, subject=pub.subject,
                         opened_at=pub.opened_at, run_id=pub.run_id, security=True)
        keep: tuple[str, ...] = ("security",)
    else:
        record = public_view(state.record, state.public_summary)
        keep = LINK_FIELDS
    (dest / RECORD).write_text(record.to_json() + "\n", encoding="utf-8")
    for p in sorted((state.path / LINKS).glob("*.json")):
        link = json.loads(p.read_text(encoding="utf-8"))
        if link["field"] in keep:
            (dest / LINKS / p.name).write_text(
                canonical_json(_public_link(link, state.record.repo)) + "\n", encoding="utf-8")
    return dest


def issue_body(state: State) -> str:
    f = public_view(state.current, state.public_summary)
    rows = [("Kind", f.kind), ("Repo", f.repo), ("Subject", f.subject), ("Opened", f.opened_at),
            ("Channel", f.channel), ("Build digest", f.build_digest), ("Last good", f.last_good),
            ("First bad", f.first_bad), ("Stage", f.stage), ("Signal", f.signal),
            ("Run", f.run_id), ("Operation", f.operation), ("Culprit", f.culprit),
            ("Fix", f.fix), ("Covering test", f.covering_test), ("Failure class", f.failure_class),
            ("Postmortem", f.postmortem)]
    def cell(v: str) -> str:
        return "`" + v.replace("`", "'").replace("|", "/").replace("\n", " ") + "`" if v else "_not yet_"
    table = "\n".join(f"| {k} | {cell(v)} |" for k, v in rows)
    status = ("closed: culprit, fix and covering test are linked" if state.closed
              else "open until " + ", ".join(m.replace("_", " ") for m in state.missing)
              + " are linked")
    summary = f.summary.replace("`", "'")
    summary = (f"```text\n{summary}\n```\n\n" if summary
               else "The summary and other free text stay in the record.\n\n")
    return (f"{marker(f.id)}\n"
            f"Failure record `{f.id}` from quirq infra (test-pipelines, plan §5.10). "
            f"This issue mirrors the record; the record is the source of truth.\n\n"
            f"{summary}| Field | Value |\n|---|---|\n{table}\n\n"
            f"Status: {status}.\n")


def marker(fid: str) -> str:
    """The first line of the mirrored issue's body, by which the issue is found again."""
    return f"<!-- qq-failure: {fid} -->"


WITHHELD_BODY = ("The failure record mirrored here was later found to look security-related, so "
                 "its details were hidden and this issue is waiting to be deleted by a repo "
                 "admin. TODO(suraj): where such records are tracked.\n")


def issue_title(state: State) -> str:
    f = public_view(state.current, state.public_summary)
    what = (f.summary.splitlines()[0][:80] if f.summary.strip()
            else " ".join(filter(None, (f.stage, f.signal, f.subject[:40]))))
    return f"[qq {f.kind}] {f.repo}: {what}"
