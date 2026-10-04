"""The GitHub backend: a Run described from a GitHub Actions job's environment.

Variables used (all set by Actions): GITHUB_REPOSITORY, GITHUB_RUN_ID, GITHUB_RUN_ATTEMPT,
GITHUB_JOB, GITHUB_WORKFLOW, GITHUB_SHA, GITHUB_REF_NAME, GITHUB_EVENT_NAME, GITHUB_EVENT_PATH,
GITHUB_SERVER_URL. No token is needed to describe a run.

`collect` reads the result bundles that the sink kept as workflow artifacts, through the REST API.
"""
from __future__ import annotations

import datetime as dt
import fnmatch
import io
import json
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import zlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from qqresults import bundle
from qqresults.errors import Error
from qqresults.model import Change, Run, RunKind, is_time


class GitHubEnvError(Error):
    pass


def _event(env: Mapping[str, str]) -> dict:
    path = env.get("GITHUB_EVENT_PATH")
    if not path:
        return {}
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise GitHubEnvError(f"GITHUB_EVENT_PATH={path}: cannot read the event payload: {e}") from None


def kind_for(event_name: str, branch: str, default_branch: str) -> RunKind:
    if event_name == "merge_group":
        return RunKind.GATE
    # pull_request_target is presubmit when a job describes itself, but collect never takes it
    # (Origin.kinds): it runs the base branch's workflow, not the change's.
    if event_name in ("pull_request", "pull_request_target"):
        return RunKind.PRESUBMIT
    if event_name == "push" and default_branch and branch == default_branch:
        return RunKind.POSTSUBMIT
    return RunKind.OTHER


def run_from_env(env: Mapping[str, str], kind: str = "", name: str = "") -> Run:
    missing = [v for v in ("GITHUB_REPOSITORY", "GITHUB_RUN_ID", "GITHUB_SHA") if not env.get(v)]
    if missing:
        raise GitHubEnvError(f"not inside a GitHub Actions job: {', '.join(missing)} unset "
                             "(use --backend local outside Actions)")
    repo = env["GITHUB_REPOSITORY"]
    event_name = env.get("GITHUB_EVENT_NAME", "")
    event = _event(env)
    branch = env.get("GITHUB_REF_NAME", "")
    default_branch = (event.get("repository") or {}).get("default_branch", "")
    commit = env["GITHUB_SHA"]
    base_commit = ""
    change = None

    if pr := event.get("pull_request"):
        change = Change(repo=repo, number=pr.get("number"),
                        head_sha=(pr.get("head") or {}).get("sha", ""),
                        base_sha=(pr.get("base") or {}).get("sha", ""))
        # On pull_request GitHub tests a merge of the PR onto its branch: the first parent is the
        # tree without the change. pull_request_target tests the branch itself, not a merge.
        base_commit = f"{commit}^1" if event_name == "pull_request" else change.base_sha
        branch = (pr.get("base") or {}).get("ref", branch)
    elif mg := event.get("merge_group"):
        # The queue tests head_sha, which is base_sha plus the queued changes ahead of it.
        m = re.search(r"/pr-(\d+)-", mg.get("head_ref", ""))
        # The PR's own head commit is not in the payload, so head_sha stays empty.
        change = Change(repo=repo, number=int(m.group(1)) if m else None,
                        base_sha=mg.get("base_sha", ""))
        base_commit = f"{commit}^1"   # the entries ahead, without this one (retry.py)
        branch = mg.get("base_ref", "").removeprefix("refs/heads/") or branch
    elif event_name == "push":
        base_commit = event.get("before", "")

    attempt = int(env.get("GITHUB_RUN_ATTEMPT") or 1)
    job = env.get("GITHUB_JOB", "")
    run_id = f"github/{repo}/{env['GITHUB_RUN_ID']}/{attempt}/{job}" + (f"/{name}" if name else "")
    server = env.get("GITHUB_SERVER_URL", "https://github.com")
    return Run(
        id=run_id,
        repo=repo,
        kind=kind or kind_for(event_name, branch, default_branch).value,
        commit=commit,
        backend="github",
        base_commit=base_commit,
        branch=branch,
        change=change,
        workflow=env.get("GITHUB_WORKFLOW", ""),
        job=job,
        attempt=attempt,
        url=f"{server}/{repo}/actions/runs/{env['GITHUB_RUN_ID']}/attempts/{attempt}",
        finished_at=dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        executor="github-actions",
        job_status=env.get("QQ_JOB_STATUS", ""),   # the sink action passes ${{ job.status }}
        queued_at=_rfc3339_utc(env.get("QQ_QUEUED_AT", "")),   # set by quirq-ai/gate/timing
    )


def _rfc3339_utc(text: str) -> str:
    """text as RFC 3339 UTC ("...Z"), or "" when it is missing or not a timestamp with a zone."""
    try:
        t = dt.datetime.fromisoformat(text.strip().upper().replace("Z", "+00:00"))
    except ValueError:
        return ""
    if t.tzinfo is None:
        return ""
    return t.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- collecting bundles into the store ---------------------------------------------------------

API = "https://api.github.com"
ARTIFACT_PREFIX = "qq-results-"
FAILURE_PREFIX = "qq-failure-"


class GitHubAPIError(Error):
    pass


class NeedsDeletion(Error):
    """A public issue holds a record that now looks security-related. Editing it does not
    remove the text (edit history, timeline, notification emails), so a person must delete it."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _request(url: str, token: str, accept: str = "application/vnd.github+json") -> tuple[int, dict, bytes]:
    """GET url with the token, without following redirects. Returns (status, headers, body)."""
    headers = {"Accept": accept, "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "qqresults"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(urllib.request.Request(url, headers=headers), timeout=60) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307, 308):
            return e.code, dict(e.headers), b""
        raise GitHubAPIError(f"GET {url}: HTTP {e.code} {e.reason}") from None
    except urllib.error.URLError as e:
        raise GitHubAPIError(f"GET {url}: {e.reason}") from None


def http_get(url: str, token: str) -> bytes:
    """GET a GitHub URL, following a redirect to storage without sending the token there."""
    status, headers, body = _request(url, token)
    if status in (301, 302, 303, 307, 308):
        location = headers.get("Location") or headers.get("location")
        if not location:
            raise GitHubAPIError(f"GET {url}: redirect without a Location")
        status, _, body = _request(location, "", accept="*/*")
    return body


def list_result_artifacts(repo: str, token: str, get=http_get, max_pages: int = 20) -> list[dict]:
    """The repo's unexpired qq-results-* and qq-failure-* artifacts, oldest first.

    Oldest first, so a later upload can never take a run's place by being listed before it, a
    failure record reported again keeps the opened_at and run_id of its first report, and a link
    bundle comes after the record it links to.
    """
    found = []
    for page in range(1, max_pages + 1):
        data = json.loads(get(f"{API}/repos/{repo}/actions/artifacts?per_page=100&page={page}", token))
        artifacts = data.get("artifacts", [])
        found.extend(a for a in artifacts
                     if a.get("name", "").startswith((ARTIFACT_PREFIX, FAILURE_PREFIX))
                     and not a.get("expired"))
        if len(artifacts) < 100:
            break
    return sorted(found, key=lambda a: (str(a.get("created_at") or ""), _int(a.get("id"))))


# What collect trusts (README, "What collect trusts"). Who uploaded an artifact comes from GitHub
# (its workflow run), never from the artifact's contents; the contents must then agree with it.
DEFAULT_WORKFLOWS = (".github/workflows/qq-*.yml", ".github/workflows/presubmit.yml")
MAX_ARTIFACT_BYTES = 20 * 1024 * 1024    # zipped and unzipped; the store is a git branch
MAX_ARTIFACT_FILES = 1000
MAX_RESULTS = 50_000                     # per bundle
FAILURE_EVENTS = ("push", "schedule", "workflow_dispatch")
# A failure record's public copy replaces a free-text subject with this digest of it, so its id
# (from the raw subject) cannot be recomputed; the run-origin checks still apply.
PUBLIC_SUBJECT = re.compile(r"sha256:[0-9a-f]{16}")
# The only events a --cross-repo source's runs may have. workflow_run lets its trusted uploader be
# a workflow of its own, started when the measuring workflow finishes (perf-publish.yml).
CROSS_REPO_EVENTS = (*FAILURE_EVENTS, "workflow_run")


@dataclass(frozen=True)
class Trust:
    # Globs of the workflow files that may write; empty means DEFAULT_WORKFLOWS, except for a
    # cross_repo source, which must name its own (it has no default).
    workflows: tuple[str, ...] = ()
    cross_repo: frozenset[str] = frozenset()         # collected repos whose runs may name another

    def globs(self, repo: str) -> tuple[str, ...]:
        if self.workflows or repo not in self.cross_repo:
            return self.workflows or DEFAULT_WORKFLOWS
        raise GitHubAPIError(f"{repo} is a --cross-repo source, so --workflow must name its "
                             "trusted uploader workflow (there is no default)")


@dataclass(frozen=True)
class Origin:
    """The workflow run that uploaded an artifact, as GitHub reports it."""
    repo: str
    run_id: int
    attempts: int         # the run's latest attempt; an artifact may come from any up to it
    event: str
    path: str
    head_branch: str
    head_sha: str
    on_default: bool      # head_branch is the default branch and head_sha is in its history
    pull_requests: tuple[int, ...] = ()   # the PRs GitHub links to the run (same-repo PRs only)

    def kinds(self) -> set[str]:
        """The run kinds a bundle from this run may claim."""
        if self.event == "merge_group":
            return {RunKind.GATE.value}
        if self.event == "pull_request":
            return {RunKind.PRESUBMIT.value}
        if self.event == "push":
            return {RunKind.POSTSUBMIT.value} if self.on_default else {RunKind.OTHER.value}
        if self.event in ("workflow_dispatch", "schedule"):
            return ({RunKind.POSTSUBMIT.value, RunKind.CANARY.value, RunKind.OTHER.value}
                    if self.on_default else {RunKind.OTHER.value})
        return set()

    def attempt_of(self, what: str, run_id: object) -> int:
        """The attempt a run id names; it must be github/<repo>/<this run>/<attempt>/<job>[/...]."""
        prefix = f"github/{self.repo}/{self.run_id}/"
        rest = run_id.removeprefix(prefix).split("/") if isinstance(run_id, str) else []
        if not (isinstance(run_id, str) and run_id.startswith(prefix) and len(rest) >= 2
                and rest[0].isdigit() and 1 <= int(rest[0]) <= self.attempts and rest[1]):
            raise GitHubAPIError(f"{what} {_short(run_id)} does not name workflow run "
                                 f"{self.run_id} of {self.repo}, which uploaded it")
        return int(rest[0])


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else -1


def _short(value: object) -> str:
    text = repr(value)
    return text if len(text) <= 80 else text[:77] + "..."


def _get_json(url: str, token: str, get) -> dict:
    try:
        data = json.loads(get(url, token))
    except ValueError:
        raise GitHubAPIError(f"GET {url}: not JSON") from None
    if not isinstance(data, dict):
        raise GitHubAPIError(f"GET {url}: not a JSON object")
    return data


def _origin(repo: str, art: dict, trust: Trust, token: str, get, runs: dict,
            default_branch, in_default) -> Origin:
    """The artifact's workflow run, checked against what collect trusts (one fetch per run)."""
    run_id = _int((art.get("workflow_run") or {}).get("id"))
    if run_id < 1:
        raise GitHubAPIError("the artifact names no workflow run")
    if run_id not in runs:
        runs[run_id] = _get_json(f"{API}/repos/{repo}/actions/runs/{run_id}", token, get)
    run = runs[run_id]
    head_repo = (run.get("head_repository") or {}).get("full_name")
    if head_repo != repo:
        raise GitHubAPIError(f"workflow run {run_id} ran code from {_short(head_repo)}, not "
                             f"{repo} (a fork's pull request?); only {repo}'s own runs are stored")
    path = str(run.get("path") or "").split("@", 1)[0]
    # fnmatch's * also matches /, so a glob covers subdirectories too.
    if not any(fnmatch.fnmatchcase(path, g) for g in trust.globs(repo)):
        raise GitHubAPIError(f"workflow run {run_id} is from {_short(path)}, which is not an "
                             "allowed workflow (--workflow)")
    attempts = _int(run.get("run_attempt"))
    if attempts < 1:
        raise GitHubAPIError(f"workflow run {run_id}: no run_attempt")
    branch = str(run.get("head_branch") or "")
    event = str(run.get("event") or "")
    sha = str(run.get("head_sha") or "")
    # head_branch is only a ref's short name: a tag named like the default branch has it too, so
    # the commit must also be in the default branch's history. Only CROSS_REPO_EVENTS use it.
    on_default = (event in CROSS_REPO_EVENTS and bool(branch) and branch == default_branch()
                  and bool(sha) and in_default(sha))
    prs = run.get("pull_requests")
    numbers = tuple(_int(p.get("number")) for p in prs if isinstance(p, dict)) \
        if isinstance(prs, list) else ()
    if repo in trust.cross_repo and (event not in CROSS_REPO_EVENTS or not on_default):
        # A cross-repo source may name any repo, so it is held to its trusted uploader runs only.
        raise GitHubAPIError(f"{repo} is a --cross-repo source: only its "
                             f"{', '.join(CROSS_REPO_EVENTS)} runs of a commit on the default "
                             f"branch are stored, not workflow run {run_id} ({event or 'unknown'} "
                             f"of {_short(sha)} on {_short(branch)})")
    return Origin(repo=repo, run_id=run_id, attempts=attempts, event=event, path=path,
                  head_branch=branch, head_sha=sha, on_default=on_default, pull_requests=numbers)


def _check_bundle(origin: Origin, trust: Trust, b: bundle.Bundle) -> None:
    """A bundle must be one its workflow run could have produced."""
    run = b.run
    if origin.attempt_of("run", run.id) != run.attempt:
        raise GitHubAPIError(f"run {run.id}: attempt {run.attempt} does not match its id")
    cross = run.repo != origin.repo
    if cross and (origin.repo not in trust.cross_repo or run.kind != RunKind.OTHER.value
                  or not origin.on_default):
        raise GitHubAPIError(f"run {run.id} is for {run.repo} but was found in {origin.repo}; "
                             "only kind 'other' from a default-branch run of a --cross-repo "
                             "repo may name another repo")
    # _origin held a cross-repo source to push, schedule, dispatch and workflow_run runs on the
    # default branch, each of which may produce kind other.
    if not cross and run.kind not in origin.kinds():
        where = "" if origin.on_default else " off the default branch"
        raise GitHubAPIError(f"run {run.id} claims kind {_short(run.kind)}, which a "
                             f"{origin.event or 'unknown'} run{where} cannot produce")
    if cross:
        pass   # perf names the measured repo's commit, which this run's head cannot vouch for
    elif origin.event == "pull_request":
        # The run tests GitHub's merge of the PR, which the API does not name; the PR's head
        # must be the run's head, and the PR one GitHub links to the run when it lists any
        # (it does for same-repo PRs). run.commit, the merge, is not checked.
        if run.change is None or run.change.head_sha != origin.head_sha:
            raise GitHubAPIError(f"run {run.id}: its change head is not {origin.head_sha}, the "
                                 "commit its workflow run tested")
        if origin.pull_requests and run.change.number not in origin.pull_requests:
            raise GitHubAPIError(f"run {run.id}: change {_short(run.change.number)} is not a pull "
                                 f"request of its workflow run {origin.run_id}")
    elif run.role == "base" and run.parent and run.id.startswith(run.parent + "/"):
        pass   # V0-TST-03's base run tests the base commit; its parent is checked against the head
    elif run.role == "backfill" and origin.event == "workflow_dispatch":
        pass   # a dispatched backfill names the commit it checked out; the scorecard leaves it out
    elif run.commit != origin.head_sha:
        raise GitHubAPIError(f"run {run.id}: commit {_short(run.commit)} is not "
                             f"{origin.head_sha}, the commit its workflow run tested")
    if len(b.results) > MAX_RESULTS:
        raise GitHubAPIError(f"run {run.id}: {len(b.results)} results, more than {MAX_RESULTS}")
    if b.verdict.run_id != run.id or any(r.run_id != run.id for r in b.results):
        raise GitHubAPIError(f"run {run.id}: its results or verdict name another run")


def _check_failure_origin(origin: Origin) -> None:
    if origin.event not in FAILURE_EVENTS or not origin.on_default:
        raise GitHubAPIError(f"failure records are taken only from {', '.join(FAILURE_EVENTS)} "
                             "runs of a commit on the default branch, not a "
                             f"{origin.event or 'unknown'} run of {_short(origin.head_sha)} on "
                             f"{_short(origin.head_branch)}")


def _check_failure(origin: Origin, path: Path) -> None:
    """A failure record must come from a default-branch run of its repo and name that run."""
    from qqresults import failures

    _check_failure_origin(origin)
    f = failures.read(path).record
    if f.repo != origin.repo:
        raise GitHubAPIError(f"failure {_short(f.id)} is for {_short(f.repo)} but was found in "
                             f"{origin.repo}")
    digested = isinstance(f.subject, str) and PUBLIC_SUBJECT.fullmatch(f.subject)
    if (f.id != failures.failure_id(f.kind, f.repo, f.subject) if not digested
            else not re.fullmatch(rf"{re.escape(str(f.kind))}-[0-9a-f]{{16}}", str(f.id))):
        raise GitHubAPIError(f"failure {_short(f.id)}: the id does not match its kind, repo and "
                             "subject")
    origin.attempt_of(f"failure {f.id}: run", f.run_id)
    _check_links(f.id, path)


def _check_link_bundle(origin: Origin, path: Path) -> None:
    """A link bundle (links added after the record was opened) is held to the same origin rules
    as a record: a default-branch run of the record's repo, named as the run that linked."""
    from qqresults import failures

    _check_failure_origin(origin)
    target = failures.read_target(path)
    if target.repo != origin.repo:
        raise GitHubAPIError(f"links for {_short(target.id)} are for {_short(target.repo)} but "
                             f"were found in {origin.repo}")
    origin.attempt_of(f"links for {target.id}: run", target.run_id)
    _check_links(target.id, path)


def _check_links(fid: str, path: Path) -> None:
    from qqresults import failures

    links = path / failures.LINKS
    for p in sorted(links.glob("*.json")) if links.is_dir() else []:
        try:
            link = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError) as e:
            raise GitHubAPIError(f"failure {fid}: link {p.name}: {e}") from None
        if not (isinstance(link, dict) and link.get("field") in failures.LINK_FIELDS
                and isinstance(link.get("value"), str) and link["value"]
                and is_time(link.get("at"))):
            raise GitHubAPIError(f"failure {fid}: link {p.name} is not a "
                                 "{field, value, at} record")


def _unzip(data: bytes, dest: str) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            infos = z.infolist()
            if any(i.filename.startswith("/") or ".." in i.filename.split("/") for i in infos):
                raise GitHubAPIError("unsafe path in zip")
            if len(infos) > MAX_ARTIFACT_FILES:
                raise GitHubAPIError(f"more than {MAX_ARTIFACT_FILES} files")
            if sum(i.file_size for i in infos) > MAX_ARTIFACT_BYTES:
                raise GitHubAPIError(f"unzips to more than {MAX_ARTIFACT_BYTES} bytes")
            z.extractall(dest)
    except zipfile.BadZipFile:
        raise GitHubAPIError("not a zip archive") from None
    # An encrypted member (RuntimeError), corrupt or truncated data (zlib.error, EOFError), an
    # unknown compression method (NotImplementedError) or a bad name (OSError, ValueError).
    except (RuntimeError, zlib.error, EOFError, NotImplementedError, OSError, ValueError) as e:
        raise GitHubAPIError(f"cannot unzip: {type(e).__name__}: {e}") from None


def _import_artifact(art: dict, store, token: str, get, origin: Origin, trust: Trust) -> bool:
    url = art.get("archive_download_url")
    if not isinstance(url, str) or not url:
        raise GitHubAPIError("the artifact has no archive_download_url")
    data = get(url, token)
    if len(data) > MAX_ARTIFACT_BYTES:
        raise GitHubAPIError(f"larger than {MAX_ARTIFACT_BYTES} bytes")
    with tempfile.TemporaryDirectory() as tmp:
        _unzip(data, tmp)
        root = Path(tmp)
        if art["name"].startswith(FAILURE_PREFIX):
            from qqresults import failures

            dirs = [root, *sorted(root.iterdir())]
            recs = [d for d in dirs if (d / failures.RECORD).is_file()]
            links = [d for d in dirs if (d / failures.TARGET).is_file() and d not in recs]
            if not recs and not links:
                raise GitHubAPIError("no failure record or link bundle inside")
            for d in recs:        # all of them, before importing any
                _check_failure(origin, d)
            for d in links:
                _check_link_bundle(origin, d)
            # Records first, so a bundle's links can find a record in the same artifact.
            return any([store.import_failure(d) for d in recs]
                       + [store.import_links(d) for d in links])
        # One bundle at the root, or (with retries, V0-TST-03) one bundle per directory.
        dirs = [root] if (root / "run.json").is_file() else sorted(
            d for d in root.iterdir() if (d / "run.json").is_file())
        if not dirs:
            raise GitHubAPIError("no results bundle inside")
        for d in dirs:            # all of them, before importing any
            _check_bundle(origin, trust, bundle.read(d))
        return any([store.import_dir(d) for d in dirs])


def collect(repo: str, store, token: str, get=http_get,
            trust: Trust = Trust()) -> tuple[int, int, list[str]]:
    """Import every result bundle and failure record that the repo's trusted runs kept.

    Returns (new, already stored, errors). One bad artifact is reported and skipped, so it does
    not hide the others. A public repo's artifacts can be read with any token, including another
    repo's GITHUB_TOKEN.
    """
    new = old = 0
    errors = []
    runs: dict[int, dict] = {}     # workflow run id -> the run, fetched once
    default: list[str] = []
    history: dict[tuple[str, str], bool] = {}   # (repo, sha) -> in the default branch's history
    trust.globs(repo)    # a --cross-repo source without --workflow is refused before any listing

    def default_branch() -> str:
        if not default:
            default.append(str(_get_json(f"{API}/repos/{repo}", token, get)
                               .get("default_branch") or ""))
        return default[0]

    def in_default(sha: str) -> bool:
        if (repo, sha) not in history:
            # refs/heads/: a bare name would resolve a tag of the same name first, as git does.
            base = urllib.parse.quote(f"refs/heads/{default_branch()}", safe="/")
            url = f"{API}/repos/{repo}/compare/{base}...{urllib.parse.quote(sha, safe='')}?per_page=1"
            # identical or behind: sha is the default branch's head or one of its ancestors.
            history[(repo, sha)] = _get_json(url, token, get).get("status") in ("identical", "behind")
        return history[(repo, sha)]

    for art in list_result_artifacts(repo, token, get):
        if (store.has(art["name"]) if art["name"].startswith(ARTIFACT_PREFIX)
                else store.seen_artifact(f"{art['name']}-{art.get('id', '')}")):
            old += 1
            continue
        try:
            size = _int(art.get("size_in_bytes"))
            if not 0 <= size <= MAX_ARTIFACT_BYTES:
                raise GitHubAPIError(f"size {_short(art.get('size_in_bytes'))} is not at most "
                                     f"{MAX_ARTIFACT_BYTES} bytes")
            origin = _origin(repo, art, trust, token, get, runs, default_branch, in_default)
            if _import_artifact(art, store, token, get, origin, trust):
                new += 1
            else:
                old += 1
            if art["name"].startswith(FAILURE_PREFIX):
                store.mark_artifact(f"{art['name']}-{art.get('id', '')}")
        except Error as e:
            errors.append(f"{repo} artifact {art.get('name')}: {e}")
    return new, old, errors


# --- mirroring failure records to issues (V0-TST-04) ------------------------------------------

FAILURE_LABEL = "qq-failure"


def api(method: str, url: str, token: str, body: dict | None = None) -> tuple[int, object]:
    """Call the REST API with a JSON body. Returns (status, parsed JSON or None)."""
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
               "User-Agent": "qqresults", "Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        return e.code, None
    except urllib.error.URLError as e:
        raise GitHubAPIError(f"{method} {url}: {e.reason}") from None


def _find_issues(repo: str, fid: str, token: str, call) -> list[dict]:
    """Every issue (not PR) whose body starts with the record's marker, lowest number first."""
    from qqresults import failures

    marker = failures.marker(fid)
    found = []
    for page in range(1, 51):
        status, issues = call("GET", f"{API}/repos/{repo}/issues?labels={FAILURE_LABEL}"
                              f"&state=all&per_page=100&page={page}", token)
        if status != 200:
            raise GitHubAPIError(f"{repo}: listing {FAILURE_LABEL} issues: HTTP {status}")
        found += [i for i in issues if "pull_request" not in i
                  and (i.get("body") or "").startswith(marker)]
        if len(issues) < 100:
            break
    return sorted(found, key=lambda i: i["number"])


def _patch(repo: str, issue: dict, want: dict, token: str, call) -> None:
    if any(issue.get(k) != v for k, v in want.items()):
        status, _ = call("PATCH", f"{API}/repos/{repo}/issues/{issue['number']}", token, want)
        if status != 200:
            raise GitHubAPIError(f"{repo}#{issue['number']}: updating the issue: HTTP {status}")


def _close_duplicates(repo: str, issues: list[dict], token: str, call) -> dict:
    """Close every open issue but the lowest-numbered one as a duplicate; return that one.

    The issue list can lag behind a create, so two racing runners may each keep the issue it
    opened; whichever call next sees both closes the later one."""
    keep, *rest = sorted(issues, key=lambda i: i["number"])
    for issue in rest:
        if issue.get("state") != "closed":
            _patch(repo, issue, {"state": "closed", "state_reason": "duplicate"}, token, call)
    return keep


def _withdrawn(issue: dict) -> bool:
    """Whether mirror_issue withdrew this issue (its record looked security-related)."""
    from qqresults import failures

    return (str(issue.get("title") or "").startswith(failures.WITHHELD_TITLE)
            or failures.WITHHELD_BODY in str(issue.get("body") or ""))


def mirror_issue(state, repo: str, token: str, call=None) -> tuple[str, bool]:
    """Create or update the one labelled issue that mirrors a failure record.

    Returns (issue URL, created). The issue is found again by the marker its body starts with,
    so a second call never opens a second issue; if two runners race and both open one, every
    call closes each open marker issue but the lowest-numbered one as a duplicate (the issue list
    can lag a create, so one call may not see the other's issue yet). A security-looking record is never mirrored: it returns
    ("", False). An issue opened before the record looked that way has its title and body
    replaced and is closed, then NeedsDeletion is raised: the old text stays in its edit history
    and in emails already sent, so only deleting the issue (a repo admin) removes it.

    An issue already withdrawn that way is itself a security mark: a later report that does not
    look security-related (a fresh runner that never saw the mark) takes the same path, so the
    issue is never patched back or reopened. The caller should then mark the record security.
    """
    from qqresults import failures  # core module; imported here to keep backends import-light

    call = call or api
    f = state.current
    existing = _find_issues(repo, f.id, token, call)
    if state.security or any(_withdrawn(i) for i in existing):
        for issue in existing:
            _patch(repo, issue, {"title": f"{failures.WITHHELD_TITLE} ({f.id})",
                                 "body": failures.marker(f.id) + "\n" + failures.WITHHELD_BODY,
                                 "state": "closed"}, token, call)
        if existing:
            urls = ", ".join(i["html_url"] for i in existing)
            raise NeedsDeletion(f"{f.id} now looks security-related but was mirrored to {urls}; "
                                "its text is hidden and closed but stays in the edit history. "
                                "A repo admin must delete the issue.")
        return "", False
    title, body = failures.issue_title(state), failures.issue_body(state)
    if existing:
        keep = _close_duplicates(repo, existing, token, call)
        _patch(repo, keep, {"title": title, "body": body,
                            "state": "closed" if state.closed else "open"}, token, call)
        return keep["html_url"], False
    labels = [FAILURE_LABEL, f"{FAILURE_LABEL}:{f.kind}"]
    for name in labels:
        status, _ = call("POST", f"{API}/repos/{repo}/labels", token,
                         {"name": name, "color": "b60205",
                          "description": "quirq infra failure record (test-pipelines)"})
        if status not in (201, 422):   # 422: the label exists already
            raise GitHubAPIError(f"{repo}: creating label {name}: HTTP {status}")
    status, created = call("POST", f"{API}/repos/{repo}/issues", token,
                           {"title": title, "body": body, "labels": labels})
    if status != 201:
        raise GitHubAPIError(f"{repo}: opening the failure issue: HTTP {status} "
                             "(the token needs issues: write)")
    # Another runner may have opened one at the same moment: keep the lowest number only. The
    # list may not show the issue just created yet, so it is added from the create's response.
    now_there = {created["number"]: created,
                 **{i["number"]: i for i in _find_issues(repo, f.id, token, call)}}
    keep = _close_duplicates(repo, list(now_there.values()), token, call)
    return keep["html_url"], keep["number"] == created["number"]
