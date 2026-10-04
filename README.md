# test-pipelines

Part of **quirq infra** ("qq"), quirq-ai's CI/CD system for repos in any language. This repo turns
test output into stored results and mechanical verdicts.

**Chromium counterpart:** ResultDB (the results store and its ResultSink) and LUCI Analysis
(verdicts, flake handling and failure clustering).

## What it holds (plan §5.4, §5.10)

- **Result**: one test or action outcome, write-once, raw plus normalized. JUnit XML is the input,
  which every test adapter in `quirq-ai/recipes` emits.
- **Run**: one gate or post-submit attempt that produced Results.
- **Verdict**: computed mechanically from Results. Failed tests are retried, then run without the
  change; only failures that pass without the change fail it.
- **Failure**: one record per held canary, canary rollback or auto-revert, mirrored to a labelled
  GitHub issue, linking culprit, operation and fix.
- **Scorecard v0**: a script that computes plan §8's metrics from the store.

v0 has no cloud, so storage sits behind a `backend` interface. Retry policy comes from
infra-config's `flakes.toml`.

Plan and every v0 item: [quirq-ai/infra-config](https://github.com/quirq-ai/infra-config),
`docs/plan.md` and `docs/v0.md`.

## Storing a job's results (V0-TST-01)

Add the sink after the test steps of any builder, pinned by commit:

```yaml
- uses: quirq-ai/test-pipelines/sink@<commit>
  if: always()
  with:
    junit: .qq/out/junit/*.xml     # one glob per line
    name: ${{ strategy.job-index }} # only under a matrix, so each leg's run is distinct
```

A dispatched run that checks out another commit than `GITHUB_SHA` (a backfill) passes
`commit: ${{ inputs.commit || github.sha }}` and `kind: postsubmit`; the commit must be 40 hex.
A run whose commit differs from `GITHUB_SHA` is stored with role `backfill`: queryable, but left
out of the scorecard, since it finished long after its commit landed. One job that backfills
several commits needs a different `name` per commit (results are write-once per run id).

It normalizes every JUnit report into `Result` records, computes the run's `Verdict`, and keeps the
bundle (`run.json`, `results.jsonl`, `verdict.json`, in a directory) as a workflow artifact named
`qq-results-<run id>`. The run kind comes from the event: `merge_group` is `gate`,
`pull_request` is `presubmit`, a push to the default branch is `postsubmit`. A job with no reports
still stores a run, marked as having no results and failing, because a missing signal is not a
pass, and so does a report with no test cases. Failing tests never fail the sink step; a report
that is not JUnit XML does. Within one run a test with any unexpected result is UNEXPECTED: a
repeated id is not a retry, so only V0-TST-03's explicit retries make a test FLAKY.

The same thing from a shell:

```sh
qqresults sink --backend local --repo quirq-ai/xo-space --commit HEAD --junit '.qq/out/junit/*.xml' --out .qq/results
qqresults show .qq/results/qq-results-...
```

Records live in `src/qqresults/model.py` (schema `quirq-results/1`): `Change`, `Run`, `Result`
(write-once, normalized plus the raw `<testcase>`), `Verdict`, and `Failure` (plan §5.10). Test
ids are `<classname>::<name>`, which keeps pytest, vitest, jest-junit and gotestsum ids stable
across runs. Everything that knows GitHub is in `backends/github.py`.

## The results store and scorecard v0 (V0-TST-02)

The store is a write-once tree of run bundles (`runs/<bundle>/`), kept on this repo's `results`
branch. The `scorecard` workflow collects every `qq-results-*` artifact from the onboarded repos'
runs into it, commits only new files, and writes `scorecard.md` and `scorecard.json` next to them
(and to the run's summary). It also collects perf's runs (V0-PRF-01): their Run names the
measured repo with kind `other`, so they are stored and queryable but never counted as that
repo's presubmit, gate or post-submit runs. Importing a run that is already stored is a no-op;
importing different bytes for the same run is an error.

```sh
git fetch origin results && git worktree add .qq/store FETCH_HEAD
qqresults query runs --store .qq/store --repo quirq-ai/xo-space --kind postsubmit --failed
qqresults query results --store .qq/store --run <run id> --unexpected
qqresults query history --store .qq/store --test 'tests.test_greet::test_hello'
qqresults scorecard --store .qq/store --days 7          # --json for machines
qqresults collect --store .qq/store --repo quirq-ai/xo-space   # needs GITHUB_TOKEN
```

Scorecard v0 measures, per repo: gate time-to-green p50/p90 (gate runs carry their queue-entry
time once `quirq-ai/gate/timing` exports `QQ_QUEUED_AT` before the sink, V0-GAT-04; one sample per green gate workflow run, from its first queue entry to the last
finish of the latest attempt of each job, so a re-run counts its whole wait and red runs are
not counted), main-red minutes per week, flake rate, pass rates of gate,
post-submit and presubmit runs, and runs that stored no results. A run with test results is red
when its verdict failed; one without (a repo whose only check is a typecheck, or a job that
broke before its tests) is red only when the job itself failed, and a cancelled job, such as one
superseded by a newer push, is not counted. The sink records the job's status for this. Every other plan §8 metric is
listed as not measured, with the item that will measure it; nothing unmeasured shows as zero.

### What collect trusts

Any workflow run in a collected repo can upload an artifact, so `collect` takes who produced one
from GitHub, never from the artifact's contents, and then requires the contents to agree. Each
artifact's workflow run (`GET /repos/{repo}/actions/runs/{id}`, fetched once per run) must:

- have run the repo's own code: its head repository is the repo, so a fork's pull request
  never writes;
- come from an allowed workflow file: `--workflow GLOB`, repeatable, by default
  `.github/workflows/qq-*.yml` (infra-config's generated builders) and
  `.github/workflows/presubmit.yml`. The `scorecard` workflow adds `failure-demo.yml` for this
  repo and `perf.yml` for perf.

Every bundle in it must then name that run and one of its attempts in its id
(`github/<repo>/<run id>/<attempt>/<job>`), test the run's head commit (for a pull request, the
change's head; a dispatched backfill and V0-TST-03's base run are the exceptions), and claim the
kind its event gives: `merge_group` is `gate`, `pull_request` is `presubmit`, a push to the
default branch is `postsubmit`, a schedule or dispatch on the default branch is `postsubmit`,
`canary` or `other`, and a push, schedule or dispatch off it only `other`. A run may name
another repo only as kind `other` from a repo given with `--cross-repo` (the `scorecard`
workflow passes `quirq-ai/perf`). Failure records are taken only from push, schedule or dispatch
runs on the default branch; the record must be for the repo, its id must be the one its kind,
repo and subject give, and its `run_id` must name the run. Records are type-checked (strings,
finite non-negative numbers, booleans, RFC 3339 UTC times like `2026-10-04T10:00:00Z`);
artifacts over 20 MB, zipped or not, and bundles over 50,000 results are refused. Artifacts are
read oldest first. A refused artifact is a warning (`--strict` makes it fail the step). A stored
record that does not read is left out of queries and the scorecard, with a warning and a count
in the card, so it cannot break them.

The `results` branch is only ever added to, by the `scorecard` workflow.
TODO(suraj): add a ruleset on the `results` branch that blocks force-pushes and deletion (only
a repo admin can), so nothing can rewrite the stored history.

## Retry, then compare with base (V0-TST-03)

Give the sink a rerun command and let its verdict decide the check:

```yaml
- run: python -m pytest --junitxml=results/junit.xml   # the adapter's own test command
  continue-on-error: true
- uses: quirq-ai/test-pipelines/sink@<commit>
  if: always()
  with:
    junit: results/*.xml
    rerun: python -m pytest --junitxml="$QQ_JUNIT_DIR/rerun.xml"
    setup: python -m pip install -e .     # only if tests import installed code (see below)
    infra-config: .qq/infra-config    # retry_failed and compare_with_base from flakes.toml
    fail-on-verdict: "true"
```

Failed tests are rerun with the change (`retry_failed` times, default 1). Those that still fail
are rerun at the base commit, in a git worktree. A test that passes on a retry is FLAKY, one
that also fails on base is EXONERATED, and only a failure that passes without the change (or
that has no base result, such as a new test) is UNEXPECTED and fails the change. The retry and
base runs are stored too, linked to the run by `parent` and listed in the verdict's `inputs`.
The base side runs in the same job, so the rerun command must test the code in its working
directory. If the tests import installed code (an editable install, a build outside the tree),
pass `setup`: it runs in the base worktree before the base tests and in the change's checkout
afterwards, or the base side would test the change's code and wrongly exonerate it. If the base
cannot be checked out or `setup` fails there, the retries are still stored and nothing is
exonerated; if `setup` fails to restore the change, the step fails. The base is the tested
commit without this change: for a pull request or a merge-queue entry, the tested merge commit's
first parent; for a push, the commit before it. When the target branch's commit (base_sha)
differs, the test must also fail there: a failure is exonerated only if it fails at every base.
So neither a fix queued ahead nor a rebase queue (whose first parent is the PR's own earlier
commit) can exonerate a regression. The rerun command comes from the builder, so the core never names a runner;
`$QQ_RETRY_TESTS` lists the failed test ids for a command that can select them. CI proves the
done-when with `tools/planted_demo.sh`: the planted failure is exonerated, and changes that break
a test or the code under it still block. "Through the gate" waits for the merge queue (V0-ORG-03).

## Failure records (V0-TST-04)

Every held canary, canary rollback and auto-revert opens one write-once `Failure` record (plan
§5.10), mirrored to one GitHub issue labelled `qq-failure` and `qq-failure:<kind>`:

```yaml
- uses: quirq-ai/test-pipelines/failure@<commit>     # needs issues: write
  with:
    kind: canary-held                                 # canary-held | canary-rollback | auto-revert | red-run | fuzz
    subject: ${{ steps.build.outputs.digest }}        # same kind, repo and subject: same record
    summary: "Canary held: /health probe failed"
    stage: probe
    signal: health
```

The record id is derived from kind, repo and subject, and the issue carries the id in a hidden
marker, so reporting the same event twice (a retried pipeline, a second runner) still gives one
record and one issue. What is learned later is added as link records, never by rewriting:

```sh
qqresults failure link <id> --dir <store>/failures --culprit <change> --fix <change> \
  --covering-test <test id> --mirror quirq-ai/xo-space    # updates the issue; closes it when complete
```

A record closes only when culprit, fix and covering test are linked (infra-config
`postmortem.toml` `record_needs`), and the scorecard reports the share that are. Security-looking
records (flagged, or matching words such as "overflow" or "credential") are kept but never
mirrored to a public issue, and their failure artifact is not uploaded. If a record only looks
that way after its issue was opened, the issue's text is hidden and it is closed, and the step
fails asking a repo admin to delete it: editing an issue does not remove the old text from its
history or from emails already sent. The record itself may already be in a public artifact by
then. TODO(suraj): where those go instead. The `failure-demo` workflow
proves the done-when against the real API with a planted held canary.

## v0 status

| Item | What | PR | State |
|---|---|---|---|
| V0-TST-01 | Result schema and JUnit sink | #2 | merged |
| V0-TST-02 | Results store v0 and scorecard v0 | #3 | merged |
| V0-TST-03 | Verdict: retry, then compare with base | #4 | merged |
| V0-TST-04 | Failure records with issue mirror | #6 | merged |

## Working here

See [AGENTS.md](AGENTS.md). Run the checks with `python -m pytest`.
