# quirq infra scorecard v0

Window 2026-09-28T05:53:05Z to 2026-10-05T05:53:05Z, generated 2026-10-05T05:53:05Z from the results store.

## Collect incomplete

The collect before this card did not read everything, so the runs it missed are not counted below and this card is partial. A skip from an API error, such as a rate limit, is retried by the next collect.

- quirq-ai/test-pipelines: 37 artifact(s) skipped: `quirq-ai/test-pipelines artifact qq-failure-canary-held-aefebec4c11f668b-37206450387-1: failure records are taken only from push, schedule, workflow_dispatch runs of a commit on the default branch, not a pull_request run of '1cbbf5ea9efe329b135b7d00940e1460e03e3585' on 'fix-b1-origin'`; `quirq-ai/test-pipelines artifact qq-failure-canary-held-aefebec4c11f668b-37206496558-1: failure records are taken only from push, schedule, workflow_dispatch runs of a commit on the default branch, not a pull_request run of '24bd915e336de0b960ba5458d029b0246899cf7e' on 'fix-b1-origin'`; `quirq-ai/test-pipelines artifact qq-failure-canary-held-aefebec4c11f668b-37206870507-1: failure records are taken only from push, schedule, workflow_dispatch runs of a commit on the default branch, not a pull_request run of '05124c14695406fdd07afa1736a1a23ab2192305' on 'fix-b1-origin'`; `quirq-ai/test-pipelines artifact qq-failure-canary-held-aefebec4c11f668b-37206915339-1: failure records are taken only from push, schedule, workflow_dispatch runs of a commit on the default branch, not a pull_request run of 'e5fd7c22ed6691913c78a5391bff54acc6392d86' on 'fix-b1-origin'`; `quirq-ai/test-pipelines artifact qq-failure-canary-held-aefebec4c11f668b-37206957041-1-ff7c9462: failure records are taken only from push, schedule, workflow_dispatch runs of a commit on the default branch, not a pull_request run of '24710656d757dacb0075a6e0c5999e278f6f1d95' on 'fix-b2-security'`; and 32 more

## quirq-ai/innernet

| Metric | Value | Target | Detail |
|---|---|---|---|
| Gate time-to-green | 0.9 min | P1: p50 under 15 min, p90 under 30 min | p50 0.9 / p90 0.9 over 2 green gate runs |
| Main-red time | 0 min/week | under 60 min/week | 5 post-submit commits |
| Flake rate | 0 % | under 1% | 0 of 14 runs passed only on retry |
| Gate runs passed | 100 % | measured | 2 of 2 gate runs passed |
| Post-submit runs passed | 100 % | measured | 5 of 5 postsubmit runs passed (1 cancelled or unknown not counted) |
| Presubmit runs passed | 100 % | measured | 7 of 7 presubmit runs passed (5 cancelled or unknown not counted) |
| Runs with no test results | 6 runs | 0 | of 20 presubmit, gate and post-submit runs; a repo with no test reports (only a typecheck, say) shows up here, not as red |
| Failures fully recorded | not measured | 100% | waiting on held canary, rollback, auto-revert or fuzz records in the window (none opened) |

## quirq-ai/test-pipelines

| Metric | Value | Target | Detail |
|---|---|---|---|
| Gate time-to-green | not measured | P1: p50 under 15 min, p90 under 30 min | 1 green run(s) without a queue time; waiting on V0-GAT-04 records queue-entry time on gate runs |
| Main-red time | 0 min/week | under 60 min/week | 28 post-submit commits |
| Flake rate | 0 % | under 1% | 0 of 105 runs passed only on retry |
| Gate runs passed | 100 % | measured | 1 of 1 gate runs passed |
| Post-submit runs passed | 100 % | measured | 28 of 28 postsubmit runs passed |
| Presubmit runs passed | 100 % | measured | 77 of 77 presubmit runs passed |
| Runs with no test results | 0 runs | 0 | of 106 presubmit, gate and post-submit runs; a repo with no test reports (only a typecheck, say) shows up here, not as red |
| Failures fully recorded | not measured | 100% | waiting on held canary, rollback, auto-revert or fuzz records in the window (none opened) (1 demo record(s) not counted) |

## quirq-ai/xo-space

| Metric | Value | Target | Detail |
|---|---|---|---|
| Gate time-to-green | not measured | P1: p50 under 15 min, p90 under 30 min | waiting on V0-GAT-04 records queue-entry time on gate runs |
| Main-red time | 0 min/week | under 60 min/week | 9 post-submit commits |
| Flake rate | 0 % | under 1% | 0 of 25 runs passed only on retry |
| Gate runs passed | not measured | measured | waiting on gate runs in the store |
| Post-submit runs passed | 100 % | measured | 9 of 9 postsubmit runs passed |
| Presubmit runs passed | 100 % | measured | 16 of 16 presubmit runs passed (1 cancelled or unknown not counted) |
| Runs with no test results | 1 runs | 0 | of 26 presubmit, gate and post-submit runs; a repo with no test reports (only a typecheck, say) shows up here, not as red |
| Failures fully recorded | not measured | 100% | waiting on held canary, rollback, auto-revert or fuzz records in the window (none opened) |

## Not measured yet

| Metric | Target | Waiting on |
|---|---|---|
| Repos behind the gate | 100% by end of P1 | V0-ORG-03 merge queue and V0-ONB-01/02 manifests |
| Landed on a green merge result | 100% (enforced) | V0-ORG-03 merge queue: gate runs on merge-group SHAs |
| Time to revert a culprit | mean under 30 min | V0-GAR-03 auto-revert |
| Expired quarantines | 0 | V1-TST-01 flake quarantine with expiry |
| Cache hit rate | at least 90% (P3+) | V0-RBE-02 action cache with hit counters (V1-RBE-01 shared cache) |
| Reproducibility | 100% of deterministic targets | V1-TCH-01 reproducibility check |
| Pinned and mirrored deps | 100% | V0-SYN-03 pin check and V1-SYN-01 mirroring policy |
| Release cadence | canary daily | V0-REL-03 daily canary |
| Rollback time | under 10 min | V0-REL-02 channel rollback and its drill |
| Unattended canary days | 14 in a row by P5 | V0-REL-03 daily canary |
| Canary hold or rollback time | under 15 min | V0-REL-03 daily canary and V1-REL-01 soak with health signals |
| Postmortem action items closed | at least 90% | TODO(suraj): no item yet |
| Open recurring failure classes | 0 | V1-TST-04 failure classes and recurrence |
| Fuzz finding turnaround | under 24 h | V1-REC-03 and V1-REC-04 fuzzing |
| Intervention rate | falling every month | TODO(suraj): no item yet |
| Revert precision | at least 90% | V1-GAR-02 revert precision tracking |
| CI cost per landed change | measured against the V0-ORG-04 ceiling | V0-ORG-04 compute ceiling and billing data |
