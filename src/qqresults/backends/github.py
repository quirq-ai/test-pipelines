"""The GitHub backend: a Run described from a GitHub Actions job's environment.

Variables used (all set by Actions): GITHUB_REPOSITORY, GITHUB_RUN_ID, GITHUB_RUN_ATTEMPT,
GITHUB_JOB, GITHUB_WORKFLOW, GITHUB_SHA, GITHUB_REF_NAME, GITHUB_EVENT_NAME, GITHUB_EVENT_PATH,
GITHUB_SERVER_URL. No token is needed to describe a run.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from collections.abc import Mapping
from pathlib import Path

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
    )
