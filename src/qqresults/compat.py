"""The compatibility check: does every repo of the org work with qq, without being onboarded?

Two workflows in this repo run it (README, "The compatibility check"):

- compat.yml checks each public repo of the org at its default branch, in a job of its own named
  `compat (<repo>)`. Its `plan` job runs no repo code: it lists the repos, resolves the commits
  every job will use and uploads them as the plan artifact. The other jobs run repo code, so
  nothing they write is trusted: each job's verdict is its conclusion and its steps' conclusions,
  as GitHub reports them.
- compat-publish.yml runs no repo code. It reads those conclusions and the plan through the
  Actions API and writes one results bundle per repo (role `compat`, kind other), plus one for
  qq's own path where it ran (role `compat-qq`), for collect to store.

Kind names and their commands are data read from infra-config through qqcfg; nothing here names
one.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from qqresults import bundle, verdict
from qqresults.errors import Error
from qqresults.model import Result, Run, RunKind, Status

PLAN_SCHEMA = "qq-compat-plan/1"
PLAN_JOB = "plan"
PLAN_FILE = "plan.json"
ARTIFACT_PREFIX = "qq-compat-"   # collect lists these only when asked (scorecard.yml's compat pass)
ROLE = "compat"                  # a repo's verdict: steps 1 to 3 of README's definition
QQ_ROLE = "compat-qq"            # qq's own path in that repo: `qq build`, then `qq test`
ROLES = (ROLE, QQ_ROLE)
WORKFLOW = ".github/workflows/compat.yml"

# compat.yml's steps that decide whether a repo is compatible, in the order they run. A test keeps
# these names in step with the workflow.
STEPS = ("check out", "manifest present", "manifest valid", "qq sync", "kind commands")
# compat.yml's marker steps for qq's own path: exactly one runs when the path ran, and its name
# says how it ended, as (build, test) statuses.
QQ_MARKERS = {
    "qq: passed": (Status.PASS, Status.PASS),
    "qq: build failed": (Status.FAIL, Status.SKIP),
    "qq: test failed": (Status.PASS, Status.FAIL),
}

NAME = re.compile(r"[A-Za-z0-9._-]+")
SHA = re.compile(r"[0-9a-f]{40}")


class CompatError(Error):
    pass


def job_name(repo: str) -> str:
    """The name of a repo's job in compat.yml. monitoring leaves exactly these runs out of this
    repo's CI rollup (its config/checks.ts), and a test keeps every other job from matching."""
    return f"compat ({repo})"


def plan_artifact(attempt: int) -> str:
    """The plan's artifact name. One per attempt, since a re-run of every job uploads it again."""
    return f"compat-plan-{attempt}"


# --- the plan (compat.yml's plan job) ----------------------------------------------------------

def read_skip(path: Path) -> dict[str, str]:
    """The skip list: `[skip]` maps a repo name to the reason it is not checked."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise CompatError(f"{path}: {e}") from None
    skip = data.get("skip", {})
    if not isinstance(skip, dict) or set(data) - {"skip"}:
        raise CompatError(f"{path}: only a [skip] table of repo = \"reason\" lines")
    for repo, reason in skip.items():
        if not NAME.fullmatch(repo) or not isinstance(reason, str) or not reason.strip():
            raise CompatError(f"{path}: skip.{repo} needs a repo name and a reason")
    return skip


def plan(org: str, listing: list[dict], skip: dict[str, str], head: Callable[[str, str], str],
         qq: str, infra_config: str) -> dict:
    """What compat.yml checks: every public repo in the listing (GitHub's org repo list) at its
    default branch's head, except the skip list and archived repos.

    head(repo, branch) is that branch's head commit, or "" when it has none (an empty repo,
    whose check then fails at "check out"). qq and infra_config are the commits of their main
    branches that every job uses, so one run checks every repo against the same qq.
    """
    if not NAME.fullmatch(org):
        raise CompatError(f"org {org!r} is not a GitHub name")
    for name, sha in (("qq", qq), ("infra-config", infra_config)):
        if not SHA.fullmatch(sha):
            raise CompatError(f"{name} commit {sha!r} is not a full commit id")
    repos, skipped = [], [{"repo": r, "reason": why} for r, why in sorted(skip.items())]
    for item in sorted(listing, key=lambda i: str(i.get("name"))):
        name, branch = item.get("name"), item.get("default_branch")
        if not (isinstance(name, str) and NAME.fullmatch(name)):
            raise CompatError(f"the repo list holds an unexpected name {name!r}")
        if item.get("private") is not False or name in skip:
            continue
        if item.get("archived") is True:
            skipped.append({"repo": name, "reason": "archived"})
            continue
        if not (isinstance(branch, str) and branch and NAME.fullmatch(branch.replace("/", "-"))):
            raise CompatError(f"{name}: unexpected default branch {branch!r}")
        sha = head(name, branch)
        if sha and not SHA.fullmatch(sha):
            raise CompatError(f"{name}: {branch} head {sha!r} is not a full commit id")
        repos.append({"repo": name, "branch": branch, "sha": sha})
    if not repos:
        raise CompatError(f"no public repo of {org} to check")
    return {"schema": PLAN_SCHEMA, "org": org, "qq": qq, "infra_config": infra_config,
            "repos": repos, "skipped": skipped}


def read_plan(data: Any) -> dict:
    """A plan as plan() writes it, checked field by field (it comes from an artifact)."""
    if not (isinstance(data, dict) and data.get("schema") == PLAN_SCHEMA):
        raise CompatError(f"the plan is not {PLAN_SCHEMA}")
    if not (isinstance(data.get("org"), str) and NAME.fullmatch(data["org"])
            and all(isinstance(data.get(k), str) and SHA.fullmatch(data[k])
                    for k in ("qq", "infra_config"))):
        raise CompatError("the plan's org, qq or infra_config is malformed")
    repos, skipped = data.get("repos"), data.get("skipped")
    if not (isinstance(repos, list) and repos and isinstance(skipped, list)):
        raise CompatError("the plan has no repos")
    for r in repos:
        if not (isinstance(r, dict) and isinstance(r.get("repo"), str) and NAME.fullmatch(r["repo"])
                and isinstance(r.get("branch"), str) and r["branch"]
                and isinstance(r.get("sha"), str) and (r["sha"] == "" or SHA.fullmatch(r["sha"]))):
            raise CompatError(f"the plan holds a malformed repo entry {str(r)[:80]}")
    if len({r["repo"] for r in repos}) != len(repos):
        raise CompatError("the plan names a repo twice")
    for s in skipped:
        if not (isinstance(s, dict) and isinstance(s.get("repo"), str)
                and isinstance(s.get("reason"), str)):
            raise CompatError(f"the plan holds a malformed skip entry {str(s)[:80]}")
    return data


# --- the kind commands (each repo's job) -------------------------------------------------------

def kind_commands(manifest: dict, kinds: dict) -> list[tuple[str, str, str]]:
    """(capability, kind, command) for the manifest's targets, as qq's generated CI runs them:
    each capability in turn, in the order the kinds list them, then every kind that has an interim
    command for it. kinds is `qqcfg get kinds`; manifest is `qqsync show`."""
    known = {k.get("name"): k for k in kinds.get("kind", []) if isinstance(k, dict)}
    used: list[str] = []
    for t in manifest.get("targets", []):
        kind = t.get("kind") if isinstance(t, dict) else None
        if kind not in known:
            raise CompatError(f"target {t!r}: kind {kind!r} is not in infra-config's kinds")
        if kind not in used:
            used.append(kind)
    caps: list[str] = []
    for kind in used:
        caps += [c for c in known[kind].get("capabilities", []) if c not in caps]
    steps = []
    for cap in caps:
        for kind in used:
            interim = known[kind].get("interim", {})
            if cap in known[kind].get("capabilities", []) and isinstance(interim.get(cap), str):
                steps.append((cap, kind, interim[cap]))
    return steps


def run_kind_commands(steps: list[tuple[str, str, str]], cwd: Path,
                      run: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> int:
    """Run each command in a shell that stops at the first error, as a workflow `run:` step does;
    stop at the first that fails and return its exit code."""
    if not steps:
        print("no kind of this repo has an interim command: nothing to run")
    for cap, kind, command in steps:
        print(f"::group::{cap} ({kind}): {command}", flush=True)
        rc = run(["bash", "-e", "-c", command], cwd=cwd).returncode
        print("::endgroup::", flush=True)
        if rc:
            print(f"::error::{cap} ({kind}) failed with exit code {rc}: {command}")
            return rc
    return 0


# --- publishing verdicts (compat-publish.yml) --------------------------------------------------

def _time(value: Any) -> str:
    """GitHub's time as RFC 3339 UTC to the second, or "" when it gave none."""
    if not isinstance(value, str) or not value:
        return ""
    try:
        t = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if t.tzinfo is None:
        return ""
    return t.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _json(get: Callable[[str, str], bytes], url: str, token: str) -> dict:
    try:
        data = json.loads(get(url, token))
    except ValueError:
        raise CompatError(f"GET {url.split('?', 1)[0]}: not JSON") from None
    if not isinstance(data, dict):
        raise CompatError(f"GET {url.split('?', 1)[0]}: not a JSON object")
    return data


def run_jobs(api: str, repo: str, run_id: int, token: str, get) -> list[dict]:
    """The latest attempt of every job of the compat run."""
    jobs: list[dict] = []
    for page in range(1, 6):
        data = _json(get, f"{api}/repos/{repo}/actions/runs/{run_id}/jobs?filter=latest"
                          f"&per_page=100&page={page}", token)
        batch = data.get("jobs")
        if not isinstance(batch, list):
            raise CompatError(f"compat run {run_id}: no jobs list")
        jobs += [j for j in batch if isinstance(j, dict)]
        if len(batch) < 100:
            return jobs
    raise CompatError(f"compat run {run_id}: more than 500 jobs")


def fetch_plan(api: str, repo: str, run_id: int, jobs: list[dict], token: str, get, unzip) -> dict:
    """The plan the run's plan job uploaded. Any job of the run can upload an artifact, and the
    repo jobs run repo code, so the plan is taken only when it is the one artifact of its name and
    was created before the plan job finished, which was before any repo job started (needs:)."""
    found = [j for j in jobs if j.get("name") == PLAN_JOB]
    if len(found) != 1 or found[0].get("conclusion") != "success":
        raise CompatError(f"compat run {run_id}: no successful {PLAN_JOB} job")
    job = found[0]
    attempt, done = job.get("run_attempt"), _time(job.get("completed_at"))
    if not (isinstance(attempt, int) and attempt >= 1 and done):
        raise CompatError(f"compat run {run_id}: the {PLAN_JOB} job has no attempt or finish time")
    name = plan_artifact(attempt)
    listing = _json(get, f"{api}/repos/{repo}/actions/runs/{run_id}/artifacts"
                         f"?per_page=100&name={name}", token)
    arts = [a for a in listing.get("artifacts", []) if isinstance(a, dict) and a.get("name") == name]
    if len(arts) != 1:
        raise CompatError(f"compat run {run_id}: {len(arts)} artifacts named {name}, not one")
    art = arts[0]
    created = _time(art.get("created_at"))
    if art.get("expired") or not created or created > done:
        raise CompatError(f"compat run {run_id}: {name} was not uploaded by the {PLAN_JOB} job "
                          f"(created {created or 'at an unknown time'}, the job finished {done})")
    url = art.get("archive_download_url")
    if not isinstance(url, str) or not url.startswith(f"{api}/"):
        raise CompatError(f"compat run {run_id}: {name} has no download URL")
    with tempfile.TemporaryDirectory() as tmp:
        unzip(get(url, token), tmp)
        try:
            data = json.loads((Path(tmp) / PLAN_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise CompatError(f"compat run {run_id}: {name} holds no readable {PLAN_FILE}: "
                              f"{type(e).__name__}") from None
    return read_plan(data)


def _status(conclusion: Any) -> Status:
    if conclusion == "success":
        return Status.PASS
    if conclusion == "failure":
        return Status.FAIL
    if conclusion in (None, "skipped"):
        return Status.SKIP
    return Status.CRASH   # cancelled, timed out, or anything GitHub adds later


def bundles(plan_data: dict, jobs: list[dict], origin: dict) -> list[bundle.Bundle]:
    """One bundle per planned repo, and one more for qq's path where it ran.

    origin is the publishing run: {"repo", "run_id", "attempt", "at"} (compat-publish's own run,
    which collect checks the bundles against, and the time it publishes, for a job that never
    finished). A repo passes only when its job succeeded and each of
    STEPS did; a job that is missing, never ran or ended otherwise fails with a CRASH result that
    says why, so a missing signal is never a pass.
    """
    by_name: dict[str, list[dict]] = {}
    for j in jobs:
        by_name.setdefault(str(j.get("name")), []).append(j)
    owner, out = plan_data["org"], []
    for entry in plan_data["repos"]:
        repo, name = entry["repo"], job_name(entry["repo"])
        found = by_name.get(name, [])
        job = found[0] if len(found) == 1 else {}
        steps = {}
        for s in job.get("steps") or []:
            if isinstance(s, dict) and isinstance(s.get("name"), str):
                steps.setdefault(s["name"], s.get("conclusion"))
        run_id = (f"github/{origin['repo']}/{origin['run_id']}/{origin['attempt']}"
                  f"/compat/{repo}")
        conclusion = job.get("conclusion") if isinstance(job.get("conclusion"), str) else ""
        run = Run(id=run_id, repo=f"{owner}/{repo}", kind=RunKind.OTHER.value, commit=entry["sha"],
                  backend="github", branch=entry["branch"], workflow=WORKFLOW, job=name,
                  attempt=origin["attempt"], url=str(job.get("html_url") or ""),
                  started_at=_time(job.get("started_at")), finished_at=_time(job.get("completed_at")),
                  config_revision=plan_data["infra_config"], executor=f"qq@{plan_data['qq']}",
                  job_status=conclusion, role=ROLE)
        results = []
        for step in STEPS:
            status = _status(steps.get(step))
            results.append(_result(run_id, f"compat::{step}", status,
                                   "" if status is Status.PASS else f"step {steps.get(step) or 'not run'}"))
        others = [n for n, c in steps.items() if n not in STEPS and n not in QQ_MARKERS
                  and _status(c) in (Status.FAIL, Status.CRASH)]
        failed = any(r.status in (Status.FAIL, Status.CRASH) for r in results)
        if not found:
            why = "no job for this repo"
        elif len(found) > 1:
            why = f"{len(found)} jobs named {name}"
        elif others:
            why = f"failed at {others[0]!r}"        # a setup step: the steps after it never ran
        elif conclusion != "success" and not failed:
            why = f"job {conclusion or 'not finished'}"
        elif conclusion == "success" and any(r.status != Status.PASS for r in results):
            why = "a step did not run"
        else:
            why = ""
        if why:
            results.append(_result(run_id, "compat::job", Status.CRASH, why))
        if not run.finished_at:
            run = Run.from_dict({**run.to_dict(), "finished_at": origin["at"]})
        out.append(bundle.Bundle(run, results, verdict.compute(run, results)))
        ran = [m for m in QQ_MARKERS if steps.get(m) == "success"]
        if len(ran) == 1:
            qq_id = run_id + "/qq"
            qq_run = Run.from_dict({**run.to_dict(), "id": qq_id, "role": QQ_ROLE})
            build, test = QQ_MARKERS[ran[0]]
            qq_results = [_result(qq_id, "qq::build", build, ""), _result(qq_id, "qq::test", test, "")]
            out.append(bundle.Bundle(qq_run, qq_results, verdict.compute(qq_run, qq_results)))
    return out


def _result(run_id: str, test_id: str, status: Status, message: str) -> Result:
    return Result(run_id=run_id, test_id=test_id, status=status.value,
                  expected=status in (Status.PASS, Status.SKIP), message=message)


def publish(api: str, repo: str, compat_run: int, origin: dict, token: str, out: Path,
            get, unzip) -> Path:
    """Write the compat run's bundles under out/<artifact name> and return that directory."""
    jobs = run_jobs(api, repo, compat_run, token, get)
    plan_data = fetch_plan(api, repo, compat_run, jobs, token, get, unzip)
    path = out / f"{ARTIFACT_PREFIX}{compat_run}-{origin['run_id']}-{origin['attempt']}"
    path.mkdir(parents=True, exist_ok=False)
    for b in bundles(plan_data, jobs, origin):
        bundle.write(b, path)
    return path


# --- the scorecard's Compatibility table -------------------------------------------------------

QQ_STATES = {("PASS", "PASS"): "passes", ("FAIL", "SKIP"): "build fails", ("PASS", "FAIL"): "test fails"}


def table(store, since: str, until: str, skip: dict[str, str] | None = None) -> dict | None:
    """Each repo's latest compat verdict in the window, with qq's path in that run.

    None when there is nothing to show (no compat runs and no skip list)."""
    from qqresults.store import RunFilter

    latest: dict[str, tuple[Run, Any]] = {}
    qq: dict[str, Run] = {}
    for r, v in store.runs(RunFilter(since=since)):
        if r.finished_at > until:
            continue
        if r.role == ROLE and (r.repo not in latest or (r.finished_at, r.id) >= (
                latest[r.repo][0].finished_at, latest[r.repo][0].id)):
            latest[r.repo] = (r, v)
        elif r.role == QQ_ROLE:
            qq[r.id] = r
    if not latest and not skip:
        return None
    rows = []
    for repo, (r, v) in sorted(latest.items()):
        failed = [c.test_id.removeprefix("compat::") for c in v.tests]
        failed_at = next((s for s in STEPS if s in failed), failed[0] if failed else "")
        qq_state = "not reached"
        if r.id + "/qq" in qq:
            got = {x.test_id: x.status for x in store.results(r.id + "/qq")}
            qq_state = QQ_STATES.get((got.get("qq::build"), got.get("qq::test")), "unknown")
        reason = next((c.reason for c in v.tests if c.test_id == "compat::job"), "")
        rows.append({"repo": repo, "commit": r.commit, "branch": r.branch, "url": r.url,
                     "finished_at": r.finished_at, "compatible": v.passed,
                     "failed_at": failed_at, "qq": qq_state, "qq_commit": r.executor,
                     "infra_config": r.config_revision, "detail": reason})
    return {"rows": rows, "skipped": [{"repo": k, "reason": w} for k, w in sorted((skip or {}).items())],
            "compatible": sum(1 for x in rows if x["compatible"]),
            "qq_passes": sum(1 for x in rows if x["qq"] == "passes")}


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ").replace("`", "'")


def markdown(t: dict) -> list[str]:
    lines = ["## Compatibility (qq without onboarding)", "",
             "Each public repo's default branch, from the daily compat check (compat.yml). "
             "Compatible: its manifest validates against infra-config's kinds, `qq sync` works "
             "and its kinds' commands pass. qq build and test is qq's own path through recipes: "
             "a failure there is a gap in qq, not in the repo.", ""]
    if not t["rows"]:
        lines += ["No compat runs in this window.", ""]
    else:
        lines += [f"{t['compatible']} of {len(t['rows'])} repos compatible; "
                  f"{t['qq_passes']} pass qq build and test.", "",
                  "| Repo | Commit | Compatible | Failed at | qq build and test | Checked |",
                  "|---|---|---|---|---|---|"]
        for x in t["rows"]:
            failed = _cell(x["failed_at"] + (f" ({x['detail']})" if x["detail"] else ""))
            lines.append(f"| {_cell(x['repo'])} | {x['commit'][:7] or 'none'} | "
                         f"{'yes' if x['compatible'] else 'no'} | {failed} | {x['qq']} | "
                         f"{x['finished_at']} |")
        lines.append("")
    if t["skipped"]:
        lines += ["Not checked: " + "; ".join(f"{_cell(s['repo'])} ({_cell(s['reason'])})"
                                             for s in t["skipped"]) + ".", ""]
    return lines


def ls_remote(url: str, ref: str, run: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> str:
    """The commit a remote's full ref names, or "" when the remote has no such ref (an empty
    repo). Anonymous: the repos are public."""
    try:
        p = run(["git", "ls-remote", "--exit-code", url, ref], capture_output=True, text=True,
                timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise CompatError(f"git ls-remote {url} {ref}: {type(e).__name__}") from None
    if p.returncode == 2:
        return ""
    if p.returncode:
        raise CompatError(f"git ls-remote {url} {ref}: exit {p.returncode}: {p.stderr.strip()[:200]}")
    shas = [line.split("\t")[0] for line in p.stdout.splitlines() if line.endswith("\t" + ref)]
    if len(shas) != 1:
        raise CompatError(f"git ls-remote {url} {ref}: {len(shas)} matching refs")
    return shas[0]


def org_repos(api: str, org: str, token: str, get) -> list[dict]:
    """The org's public repos, as GitHub lists them."""
    repos: list[dict] = []
    for page in range(1, 11):
        try:
            batch = json.loads(get(f"{api}/orgs/{org}/repos?type=public&per_page=100&page={page}",
                                   token))
        except ValueError:
            raise CompatError(f"the repo list of {org} is not JSON") from None
        if not isinstance(batch, list):
            raise CompatError(f"the repo list of {org} is not a list")
        repos += [r for r in batch if isinstance(r, dict)]
        if len(batch) < 100:
            return repos
    raise CompatError(f"{org} has more than 1000 public repos")


def make_plan(api: str, host: str, org: str, skip_path: Path, token: str, get,
              run: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> dict:
    """compat.yml's plan step: the org's repos and the commits every job will use."""
    head = lambda repo, branch: ls_remote(f"{host}/{org}/{repo}", f"refs/heads/{branch}", run)
    qq, infra_config = head("qq", "main"), head("infra-config", "main")
    return plan(org, org_repos(api, org, token, get), read_skip(skip_path), head, qq, infra_config)


if __name__ == "__main__":
    sys.exit("run `qqresults compat`")
