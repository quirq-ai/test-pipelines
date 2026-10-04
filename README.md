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

It normalizes every JUnit report into `Result` records, computes the run's `Verdict`, and keeps the
bundle (`run.json`, `results.jsonl`, `verdict.json`) as a workflow artifact named
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

## v0 status

| Item | What | PR | State |
|---|---|---|---|
| V0-TST-01 | Result schema and JUnit sink | #2 | in review |
| V0-TST-02 | Results store v0 and scorecard v0 | | not started |
| V0-TST-03 | Verdict: retry, then compare with base | | not started |
| V0-TST-04 | Failure records with issue mirror | | not started |

## Working here

See [AGENTS.md](AGENTS.md). Run the checks with `python -m pytest`.
