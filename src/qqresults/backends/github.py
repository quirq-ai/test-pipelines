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
import urllib.request
import zipfile
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
        base_commit = change.base_sha
        branch = (pr.get("base") or {}).get("ref", branch)
    elif mg := event.get("merge_group"):
        # The queue tests head_sha, which is base_sha plus the queued changes ahead of it.
        m = re.search(r"/pr-(\d+)-", mg.get("head_ref", ""))
        base_commit = mg.get("base_sha", "")
        # The PR's own head commit is not in the payload, so head_sha stays empty.
        change = Change(repo=repo, number=int(m.group(1)) if m else None, base_sha=base_commit)
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

    Oldest first, so a later upload can never take a run's place by being listed before it.
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


@dataclass(frozen=True)
class Trust:
    workflows: tuple[str, ...] = DEFAULT_WORKFLOWS   # globs of the workflow files that may write
    cross_repo: frozenset[str] = frozenset()         # collected repos whose runs may name another


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
    on_default: bool      # head_branch is the repo's default branch

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
            default_branch) -> Origin:
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
    if not any(fnmatch.fnmatchcase(path, g) for g in trust.workflows):
        raise GitHubAPIError(f"workflow run {run_id} is from {_short(path)}, which is not an "
                             "allowed workflow (--workflow)")
    attempts = _int(run.get("run_attempt"))
    if attempts < 1:
        raise GitHubAPIError(f"workflow run {run_id}: no run_attempt")
    branch = str(run.get("head_branch") or "")
    return Origin(repo=repo, run_id=run_id, attempts=attempts, event=str(run.get("event") or ""),
                  path=path, head_branch=branch, head_sha=str(run.get("head_sha") or ""),
                  on_default=bool(branch) and branch == default_branch())


def _check_bundle(origin: Origin, trust: Trust, b: bundle.Bundle) -> None:
    """A bundle must be one its workflow run could have produced."""
    run = b.run
    if origin.attempt_of("run", run.id) != run.attempt:
        raise GitHubAPIError(f"run {run.id}: attempt {run.attempt} does not match its id")
    if run.kind not in origin.kinds():
        where = "" if origin.on_default else " off the default branch"
        raise GitHubAPIError(f"run {run.id} claims kind {_short(run.kind)}, which a "
                             f"{origin.event or 'unknown'} run{where} cannot produce")
    if run.repo != origin.repo:
        if origin.repo not in trust.cross_repo or run.kind != RunKind.OTHER.value:
            raise GitHubAPIError(f"run {run.id} is for {run.repo} but was found in {origin.repo}; "
                                 "only kind 'other' from a --cross-repo repo may name another repo")
        # It names the other repo's commit (perf measures it), which this run's head cannot vouch for.
    elif origin.event == "pull_request":
        # The run tests GitHub's merge of the PR, which the API does not name; the PR's head
        # must be the run's head.
        if run.change is None or run.change.head_sha != origin.head_sha:
            raise GitHubAPIError(f"run {run.id}: its change head is not {origin.head_sha}, the "
                                 "commit its workflow run tested")
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


def _check_failure(origin: Origin, path: Path) -> None:
    """A failure record must come from a default-branch run of its repo and name that run."""
    from qqresults import failures

    if origin.event not in FAILURE_EVENTS or not origin.on_default:
        raise GitHubAPIError(f"failure records are taken only from {', '.join(FAILURE_EVENTS)} "
                             f"runs on the default branch, not a {origin.event or 'unknown'} run "
                             f"on {_short(origin.head_branch)}")
    f = failures.read(path).record
    if f.repo != origin.repo:
        raise GitHubAPIError(f"failure {_short(f.id)} is for {_short(f.repo)} but was found in "
                             f"{origin.repo}")
    if f.id != failures.failure_id(f.kind, f.repo, f.subject):
        raise GitHubAPIError(f"failure {_short(f.id)}: the id does not match its kind, repo and "
                             "subject")
    origin.attempt_of(f"failure {f.id}: run", f.run_id)
    links = path / failures.LINKS
    for p in sorted(links.glob("*.json")) if links.is_dir() else []:
        try:
            link = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise GitHubAPIError(f"failure {f.id}: link {p.name}: {e}") from None
        if not (isinstance(link, dict) and link.get("field") in failures.LINK_FIELDS
                and isinstance(link.get("value"), str) and link["value"]
                and is_time(link.get("at"))):
            raise GitHubAPIError(f"failure {f.id}: link {p.name} is not a "
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


def _import_artifact(art: dict, store, token: str, get, origin: Origin, trust: Trust) -> bool:
    data = get(art["archive_download_url"], token)
    if len(data) > MAX_ARTIFACT_BYTES:
        raise GitHubAPIError(f"larger than {MAX_ARTIFACT_BYTES} bytes")
    with tempfile.TemporaryDirectory() as tmp:
        _unzip(data, tmp)
        root = Path(tmp)
        if art["name"].startswith(FAILURE_PREFIX):
            recs = [d for d in [root, *sorted(root.iterdir())] if (d / "failure.json").is_file()]
            if not recs:
                raise GitHubAPIError("no failure record inside")
            for d in recs:        # all of them, before importing any
                _check_failure(origin, d)
            return any([store.import_failure(d) for d in recs])
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

    def default_branch() -> str:
        if not default:
            default.append(str(_get_json(f"{API}/repos/{repo}", token, get)
                               .get("default_branch") or ""))
        return default[0]

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
            origin = _origin(repo, art, trust, token, get, runs, default_branch)
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


def mirror_issue(state, repo: str, token: str, call=None) -> tuple[str, bool]:
    """Create or update the one labelled issue that mirrors a failure record.

    Returns (issue URL, created). The issue is found again by the marker its body starts with,
    so a second call never opens a second issue; if two runners race and both open one, the
    higher-numbered duplicate is closed. A security-looking record is never mirrored: it returns
    ("", False). An issue opened before the record looked that way has its title and body
    replaced and is closed, then NeedsDeletion is raised: the old text stays in its edit history
    and in emails already sent, so only deleting the issue (a repo admin) removes it.
    """
    from qqresults import failures  # core module; imported here to keep backends import-light

    call = call or api
    f = state.current
    existing = _find_issues(repo, f.id, token, call)
    if state.security:
        for issue in existing:
            _patch(repo, issue, {"title": f"[qq failure] withheld ({f.id})",
                                 "body": failures.marker(f.id) + "\n" + failures.WITHHELD_BODY,
                                 "state": "closed"}, token, call)
        if existing:
            urls = ", ".join(i["html_url"] for i in existing)
            raise NeedsDeletion(f"{f.id} now looks security-related but was mirrored to {urls}; "
                                "its text is hidden and closed but stays in the edit history. "
                                "A repo admin must delete the issue.")
        return "", False
    title, body = failures.issue_title(f), failures.issue_body(state)
    if existing:
        _patch(repo, existing[0], {"title": title, "body": body,
                                   "state": "closed" if state.closed else "open"}, token, call)
        return existing[0]["html_url"], False
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
    # Another runner may have opened one at the same moment: keep the lowest number only.
    now_there = _find_issues(repo, f.id, token, call)
    if now_there and now_there[0]["number"] != created["number"]:
        _patch(repo, created, {"state": "closed", "state_reason": "duplicate"}, token, call)
        return now_there[0]["html_url"], False
    return created["html_url"], True
