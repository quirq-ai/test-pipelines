# quirq infra scorecard v0

Window 2026-09-27T16:40:15Z to 2026-10-04T16:40:15Z, generated 2026-10-04T16:40:15Z from the results store.

## quirq-ai/innernet

| Metric | Value | Target | Detail |
|---|---|---|---|
| Gate time-to-green | not measured | P1: p50 under 15 min, p90 under 30 min | waiting on V0-GAT-04 records queue-entry time on gate runs |
| Main-red time | not measured | under 60 min/week | waiting on post-submit runs that store results (the sink on each repo's main) |
| Flake rate | 0 % | under 1% | 0 of 2 runs passed only on retry |
| Gate runs passed | not measured | measured | waiting on gate runs in the store |
| Post-submit runs passed | not measured | measured | waiting on postsubmit runs in the store |
| Presubmit runs passed | 100 % | measured | 2 of 2 presubmit runs passed (5 cancelled or unknown not counted) |
| Runs with no test results | 6 runs | 0 | of 8 presubmit, gate and post-submit runs; a repo with no test reports (only a typecheck, say) shows up here, not as red |
| Failures fully recorded | not measured | 100% | waiting on failure records in the window (none opened) |

## quirq-ai/test-pipelines

| Metric | Value | Target | Detail |
|---|---|---|---|
| Gate time-to-green | not measured | P1: p50 under 15 min, p90 under 30 min | waiting on V0-GAT-04 records queue-entry time on gate runs |
| Main-red time | 0 min/week | under 60 min/week | 13 post-submit commits |
| Flake rate | 0 % | under 1% | 0 of 59 runs passed only on retry |
| Gate runs passed | not measured | measured | waiting on gate runs in the store |
| Post-submit runs passed | 100 % | measured | 13 of 13 postsubmit runs passed |
| Presubmit runs passed | 100 % | measured | 46 of 46 presubmit runs passed |
| Runs with no test results | 0 runs | 0 | of 59 presubmit, gate and post-submit runs; a repo with no test reports (only a typecheck, say) shows up here, not as red |
| Failures fully recorded | 0 % | 100% | 0 of 1 records link culprit, fix and covering test |

## quirq-ai/xo-space

| Metric | Value | Target | Detail |
|---|---|---|---|
| Gate time-to-green | not measured | P1: p50 under 15 min, p90 under 30 min | waiting on V0-GAT-04 records queue-entry time on gate runs |
| Main-red time | 0 min/week | under 60 min/week | 2 post-submit commits |
| Flake rate | 0 % | under 1% | 0 of 10 runs passed only on retry |
| Gate runs passed | not measured | measured | waiting on gate runs in the store |
| Post-submit runs passed | 100 % | measured | 2 of 2 postsubmit runs passed |
| Presubmit runs passed | 100 % | measured | 8 of 8 presubmit runs passed (1 cancelled or unknown not counted) |
| Runs with no test results | 1 runs | 0 | of 11 presubmit, gate and post-submit runs; a repo with no test reports (only a typecheck, say) shows up here, not as red |
| Failures fully recorded | not measured | 100% | waiting on failure records in the window (none opened) |

## Not measured yet

| Metric | Target | Waiting on |
|---|---|---|
| Repos behind the gate | 100% by end of P1 | V0-ORG-03 merge queue and V0-ONB-01/02 manifests |
| Landed on a green merge result | 100% (enforced) | V0-ORG-03 merge queue: gate runs on merge-group SHAs |
| Time to revert a culprit | mean under 30 min | V0-GAR-03 auto-revert |
| Expired quarantines | 0 | v1 quarantine with expiry |
| Cache hit rate | at least 90% (P3+) | V0-RBE-01 executor reporting reused actions |
| Reproducibility | 100% of deterministic targets | remote-build digest comparison |
| Pinned and mirrored deps | 100% | quirq-ai/sync |
| Release cadence | canary daily | V0-REL-03 daily canary |
| Rollback time | under 10 min | release rollback drill |
| Unattended canary days | 14 in a row by P5 | V0-REL-03 daily canary |
| Canary hold or rollback time | under 15 min | V0-REL-03 and health signals |
| Postmortem action items closed | at least 90% | postmortem tracking (v1) |
| Open recurring failure classes | 0 | v1 failure classes |
| Fuzz finding turnaround | under 24 h | v1 fuzzers |
| Intervention rate | falling every month | GitHub PR data (scorecard v1) |
| Revert precision | at least 90% | V0-GAR-03 auto-revert |
| CI cost per landed change | measured against the V0-ORG-04 ceiling | V0-ORG-04 compute ceiling and billing data |
