"""What the workflows must keep true for the compatibility check (compat.yml, compat-publish.yml)."""
import re
from pathlib import Path

from qqresults import compat

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
COMPAT = WORKFLOWS / "compat.yml"
PUBLISH = WORKFLOWS / "compat-publish.yml"
JOB_NAME = re.compile(r"^compat \(.+\)$")   # monitoring's config/checks.ts drops these runs


def _names(text: str) -> list[str]:
    return [m.group(1).strip().strip("\"'") for m in re.finditer(r"^\s*-?\s*name:\s*(.+?)\s*$", text, re.M)]


def test_only_compat_yml_check_job_is_named_like_a_compat_repo_job():
    # monitoring leaves runs named `compat (<repo>)` out of this repo's CI rollup, so a job of any
    # other workflow named like that would vanish from it; compat-publish finds jobs by the name.
    found = [(p.name, n) for p in sorted(WORKFLOWS.glob("*.y*ml")) for n in _names(p.read_text())
             if JOB_NAME.match(n)]
    assert found == [("compat.yml", "compat (${{ matrix.repo }})")]
    assert compat.job_name("x") == "compat (x)"
    # A matrix job with no name is named "<id> (<values>)": no job may have the id `compat`.
    for p in WORKFLOWS.glob("*.y*ml"):
        assert not re.search(r"^  compat:\s*$", p.read_text(), re.M), p.name


def test_compat_yml_has_the_steps_compat_publish_reads():
    names = _names(COMPAT.read_text())
    for step in (*compat.STEPS, *compat.QQ_MARKERS):
        assert names.count(step) == 1, step
    order = [names.index(s) for s in compat.STEPS]
    assert order == sorted(order)
    assert compat.WORKFLOW == ".github/workflows/compat.yml"


def test_no_workflow_restores_a_cache():
    # compat.yml runs other repos' code on this repo's default branch, which can save cache
    # entries that any workflow here could restore. So none restores one.
    for p in WORKFLOWS.glob("*.y*ml"):
        text = p.read_text()
        assert "actions/cache" not in text, p.name
        assert not re.search(r"^\s+cache(-dependency-path)?:", text, re.M), p.name
        # setup-node v5+ restores an npm cache without a cache input when the workspace's
        # package.json names npm, unless told not to.
        for step in re.findall(r"uses: actions/setup-node@.*?(?=\n      - |\Z)", text, re.S):
            assert "package-manager-cache: false" in step, p.name


def test_the_reset_keeps_only_what_qq_sync_made():
    # qq's path leaves .qq/venv, .qq/out and recipes' own files; a kind command that walks the
    # tree would read them, so only qq sync's links and record survive the reset.
    text = COMPAT.read_text()
    assert "git clean -ffdxq -e /.qq/toolchains -e /.qq/deps -e /.qq/sync.json\n" in text
    assert "-e /.qq/\n" not in text


def test_qqs_path_runs_after_the_kind_commands_and_only_on_a_clean_checkout():
    text = COMPAT.read_text()
    names = _names(text)
    order = [names.index(s) for s in ("kind commands", "reset the checkout", "qq build and test",
                                      *compat.QQ_MARKERS)]
    assert order == sorted(order)

    def step(n):
        return text.split(f"- name: {n}\n", 1)[1].split("\n      - ", 1)[0]
    assert "id: reset\n" in step("reset the checkout")
    qq = step("qq build and test")
    assert "if: ${{ !cancelled() && steps.reset.outcome == 'success' }}\n" in qq


def test_compat_workflows_pin_actions_and_keep_no_credentials():
    for p in (COMPAT, PUBLISH):
        text = p.read_text()
        uses = re.findall(r"uses:\s*(\S+)", text)
        assert uses and all(re.fullmatch(r"actions/[a-z-]+@[0-9a-f]{40}", u) for u in uses), p.name
        checkouts = sum(u.startswith("actions/checkout@") for u in uses)
        assert text.count("persist-credentials: false") == checkouts, p.name
        assert "secrets." not in text, p.name


def test_compat_yml_gives_the_token_only_to_its_plan_job():
    text = COMPAT.read_text()
    assert text.count("github.token") == 1
    plan, check = text.split("\n  check:\n")
    assert "github.token" in plan and "github.token" not in check
    assert re.search(r"^permissions:\n  contents: read\n", text, re.M)
    assert "write" not in re.sub(r"#.*", "", text).replace("GITHUB_OUTPUT", "").replace(
        "GITHUB_ENV", "").replace("GITHUB_PATH", "")


def test_compat_publish_runs_only_for_main_runs_of_compat_yml():
    text = PUBLISH.read_text()
    for guard in ("github.event.workflow_run.head_branch == 'main'",
                  "github.event.workflow_run.head_repository.full_name == github.repository",
                  "github.event.workflow_run.path == '.github/workflows/compat.yml'",
                  """contains(fromJSON('["schedule", "workflow_dispatch"]'), github.event.workflow_run.event)""",
                  "git merge-base --is-ancestor"):
        assert guard in text, guard
    assert "pull_request" not in text


def test_scorecard_collects_compat_verdicts_only_from_compat_publish():
    text = (WORKFLOWS / "scorecard.yml").read_text()
    assert ("--repo quirq-ai/test-pipelines --cross-repo quirq-ai/test-pipelines \\\n"
            "            --workflow .github/workflows/compat-publish.yml --artifact-prefix qq-compat-"
            in text)
    assert text.count("--cross-repo quirq-ai/test-pipelines") == 1
    assert text.count("--compat-skip .github/compat-skip.toml") == 2


def test_the_skip_list_reads():
    skip = compat.read_skip(WORKFLOWS.parent / "compat-skip.toml")
    assert skip and all(reason.strip() for reason in skip.values())
