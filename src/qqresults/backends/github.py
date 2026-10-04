"""The GitHub backend: a Run described from a GitHub Actions job's environment.

Variables used (all set by Actions): GITHUB_REPOSITORY, GITHUB_RUN_ID, GITHUB_RUN_ATTEMPT,
GITHUB_JOB, GITHUB_WORKFLOW, GITHUB_SHA, GITHUB_REF_NAME, GITHUB_EVENT_NAME, GITHUB_EVENT_PATH,
GITHUB_SERVER_URL. No token is needed to describe a run.

`collect` reads the result bundles that the sink kept as workflow artifacts, through the REST API.
"""
from __future__ import annotations

import datetime as dt
import io
import json
import re
import tempfile
import urllib.error
import urllib.request
import zipfile
from collections.abc import Mapping
from pathlib import Path

from qqresults import bundle
from qqresults.errors import Error
from qqresults.model import Change, Run, RunKind


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


class GitHubAPIError(Error):
    pass


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
    """The repo's unexpired qq-results-* artifacts, newest first."""
    found = []
    for page in range(1, max_pages + 1):
        data = json.loads(get(f"{API}/repos/{repo}/actions/artifacts?per_page=100&page={page}", token))
        artifacts = data.get("artifacts", [])
        found.extend(a for a in artifacts
                     if a.get("name", "").startswith(ARTIFACT_PREFIX) and not a.get("expired"))
        if len(artifacts) < 100:
            break
    return found


def _import_artifact(repo: str, art: dict, store, token: str, get) -> bool:
    data = get(art["archive_download_url"], token)
    with tempfile.TemporaryDirectory() as tmp:
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                if any(n.startswith("/") or ".." in n.split("/") for n in z.namelist()):
                    raise GitHubAPIError("unsafe path in zip")
                z.extractall(tmp)
        except zipfile.BadZipFile:
            raise GitHubAPIError("not a zip archive") from None
        # One bundle at the root, or (with retries, V0-TST-03) one bundle per directory.
        root = Path(tmp)
        dirs = [root] if (root / "run.json").is_file() else sorted(
            d for d in root.iterdir() if (d / "run.json").is_file())
        if not dirs:
            raise GitHubAPIError("no results bundle inside")
        for d in dirs:            # all of them, before importing any
            _check_origin(repo, d)
        return any([store.import_dir(d) for d in dirs])


def _check_origin(repo: str, path: Path) -> None:
    """One repo's workflows must not add or hide runs the scorecard counts for another.

    The run id must be one this repo's jobs produce (run_from_env), so it cannot take another
    repo's id first and make the write-once store skip the real run. A run may name another
    repo's code only as kind "other" (perf runs do). Repo names compare exactly, as GitHub
    reports them, so one repo's metrics are never split across spellings.
    """
    run = bundle.read(path).run
    if not (isinstance(run.id, str) and isinstance(run.repo, str) and isinstance(run.kind, str)):
        raise GitHubAPIError("run.json: id, repo and kind must be strings")
    if not run.id.startswith(f"github/{repo}/"):
        raise GitHubAPIError(f"run {run.id} was found in {repo} but is not one of its runs")
    if run.repo != repo and run.kind != RunKind.OTHER.value:
        raise GitHubAPIError(f"run {run.id} is for {run.repo} but was found in {repo}; "
                             "only kind 'other' may name another repo")


def collect(repo: str, store, token: str, get=http_get) -> tuple[int, int, list[str]]:
    """Import every result bundle the repo's workflow runs kept.

    Returns (new, already stored, errors). One bad artifact is reported and skipped, so it does
    not hide the others. A public repo's artifacts can be read with any token, including another
    repo's GITHUB_TOKEN.
    """
    new = old = 0
    errors = []
    for art in list_result_artifacts(repo, token, get):
        if store.has(art["name"]):
            old += 1
            continue
        try:
            if _import_artifact(repo, art, store, token, get):
                new += 1
            else:
                old += 1
        except Error as e:
            errors.append(f"{repo} artifact {art.get('name')}: {e}")
    return new, old, errors
