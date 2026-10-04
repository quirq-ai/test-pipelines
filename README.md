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

## v0 status

| Item | What | PR | State |
|---|---|---|---|
| V0-TST-01 | Result schema and JUnit sink | | not started |
| V0-TST-02 | Results store v0 and scorecard v0 | | not started |
| V0-TST-03 | Verdict: retry, then compare with base | | not started |
| V0-TST-04 | Failure records with issue mirror | | not started |

## Working here

See [AGENTS.md](AGENTS.md). Run the checks with `python -m pytest`.
