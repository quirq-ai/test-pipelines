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
  repo and `perf-publish.yml` for perf.

A run is on the default branch only when its head branch is the default branch's name and its
head commit is in that branch's history (`GET /repos/{repo}/compare/refs/heads/{default}...{sha}`
is `identical` or `behind`, fetched once per commit), so a tag named like the default branch is not.
Every bundle in it must then name that run and one of its attempts in its id
(`github/<repo>/<run id>/<attempt>/<job>`) and claim the kind its event gives: `merge_group` is
`gate`, `pull_request` is `presubmit`, a push to the default branch is `postsubmit`, a schedule
or dispatch on the default branch is `postsubmit`, `canary` or `other`, and a push, schedule or
dispatch off it only `other`. Its `commit` must be the run's head commit (a dispatched backfill
and V0-TST-03's base run are the exceptions). For a pull request, whose run tests GitHub's merge
commit that the API does not name, `commit` is not checked; instead the change's `head_sha` must
be the run's head commit, and its `number` one of the run's `pull_requests` when GitHub lists any
(it does for a same-repo PR). A run may name another repo only as kind `other`, from a repo given
with `--cross-repo` (the `scorecard` workflow passes `quirq-ai/perf`). Such a source can name any
repo, so everything from it is held to more: `--workflow` must be given (its default globs never
apply to it, and collecting it without one is an error), and only its push, schedule, dispatch
and workflow_run runs on the default branch are read; every other artifact of it is refused. The
artifacts API does not say which job uploaded an artifact, only which workflow run, so the job is not checked:
a cross-repo source must upload from a workflow file of its own that holds only trusted jobs
(no pull request triggers, no job running code under test). An uploader started by workflow_run
vouches for the run that triggered it, which collect cannot see: it must filter on the default
branch, check that run's event and head repository, and never republish that run's artifacts
unchecked (perf-publish.yml reads perf's validated `perf-data` branch instead). Failure records are taken only from
push, schedule or dispatch runs on the default branch; the record must be for the repo, its id
must be the one its kind, repo and subject give (unless the subject is the public
`sha256:<16 hex>` digest of a free-text one, which cannot be checked against the id; the id must
then still be `<kind>-<16 hex>`), and its
`run_id` must name the run. Records are type-checked (strings,
finite non-negative numbers, booleans, RFC 3339 UTC times like `2026-10-04T10:00:00Z`);
artifacts over 20 MB, zipped or not, and bundles over 50,000 results are refused. Artifacts are
read oldest first. A refused artifact is a warning (`--strict` makes it fail the step). A stored
record that does not read is left out of queries and the scorecard, with a warning and a count
in the card, so it cannot break them.

What this still trusts: a run's own code shapes its record. Anyone whose change runs in the merge
queue (`merge_group`, including a fork's PR once it is approved into the queue) or as a same-repo
pull request can make that run's bundle say what they like, within its kind, run and commit. A
failure record's links are additive: a later default-branch run can add links (and so close the
record) to a record an earlier run opened, and a record with a digested subject may carry any id,
so a default-branch run of an allowed workflow can add to any record of its repo.

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
    infra-config: .qq/infra-config    # [verdict] from flakes.toml
    fail-on-verdict: "true"
```

Failed tests are rerun with the change (`retry_failed` times, default 1, at most 3); with more
than `max_failures_to_retry` failures (default 20) the change is treated as broken and nothing is
retried. Those that still fail
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
    summary: "Canary held: /health probe failed"     # not in the issue or artifact, but in the run log
    stage: probe
    signal: health
```

GitHub prints every input of an action (and its steps' environment) in the run log, and a public
repo's run logs are public. So never put security detail in `summary`, or in any other input, on a
public repo: write it to a file on the runner and pass `summary-file: <path>` instead (the file's
contents are not logged). Write that file without echoing the text through the log: a step that
runs `echo "${{ ... }}" > summary.txt` has the expression expanded into its script, and the script
is printed in the log, so the text is public anyway. TODO(suraj): where private details live.

The record id is derived from kind, repo and subject, and the issue carries the id in a hidden
marker, so reporting the same event twice (a retried pipeline, a second runner) still gives one
record and one issue. What is learned later is added as link records, never by rewriting:

```sh
qqresults failure link <id> --dir <store>/failures --culprit <owner/repo@sha> --fix <PR URL> \
  --covering-test <commit or URL> --mirror quirq-ai/xo-space   # updates the issue; closes it when complete
```

The store is the public results branch. Only `collect` and `failure link` write there, and both
write only what may be public (below): `collect` imports each artifact's public bundle, whatever
the artifact holds, and `failure link` stores any other value as `withheld` (it still marks the
record security if the value reads that way). Never run `failure open --dir` on a store path; it
writes the full record. Keep the detail where it belongs (the PR, the postmortem) and link to it.

A record closes only when culprit, fix and covering test are linked (infra-config
`postmortem.toml` `record_needs`), and the scorecard reports the share that are. A link stored
as `withheld` counts as linked, so a record can close on values nobody can read publicly.
TODO(expert): whether withheld links should count towards closing (audit S4).

Free text is never public without the opt-in. The issue title and body, the failure artifact and
the store carry only:

- kind, id, opening time and schema, which must have exactly the shape this version writes (an
  artifact whose record does not, or whose fields are not strings, is refused), and repo when it
  is a plain `owner/name` (org-chosen, so shown even when it reads like a security word);
- stage, signal and channel when each is a short label: lowercase, at most three words joined by
  `-` or `.`, at most 32 characters, no `_`, `/` or `::`, not containing `test`, and not reading
  as security (so `probe`, `health`, `stable` and `http-5xx` show; a test id does not);
- the subject, build digest, last good, first bad, run and every link (culprit, fix, covering
  test, operation, failure class, postmortem, issue) only when it is a commit (7 to 40 hex), a
  digest (`sha1:`, `sha256:` or `sha512:` with its full hex length), `owner/repo@<commit>` or
  `owner/repo#<number>`, a `https://github.com/owner/repo/` pull, issue, commit or Actions run
  URL, or the action's run id; references and URLs only when the owner is the record's own and
  the job name, and a repo name other than the record's own, do not read as security.

Labels and those name slots are checked against a list of security words, not an allowlist, so
author-chosen names the list misses (`remote-exec`, `login-skipped`) still
show. That is the accepted residual: they are at most three short words or a repo or job name
chosen by the org, not a description.

Anything else, including any other URL, shows as `withheld` (a subject is replaced by its digest
instead). That fails closed on purpose: natural values such as a covering test id
(`tests/test_x.py::test_y`), an operation key or a failure class always show as `withheld`. The
summary stays in the local record unless you pass `public-summary: "true"` to the action (or
`--public-summary` to `qqresults failure open`/`link`). Since the record is write-once, a repeat
report publishes the summary only if it carries the same summary as the report that wrote it;
`failure link --public-summary` publishes whatever the record holds. The artifact is a public
copy written by `--public-copy`, never the record itself.

The security classifier errs towards withholding: records flagged `security`, of kind `fuzz`, or
whose free text (summary, subject, labels, link values) matches security terms such as
"buffer overflow", "credential", "use-after-free", "remote code execution" or "without login" are
kept but never mirrored to a public issue, even with the opt-in. Words common in ordinary failures
(crash, panic, heap, leak, certificate, escalated, "not verified", overflow, injection, token,
auth, JWT, sandbox, privileged, access control, CORS) count only in a security phrase ("heap
buffer overflow", "SQL injection", "credential leak", "auth bypass", "missing authorization
check", "sandbox escape", "container breakout", "CORS any origin", "certificate verification
disabled", "JWT signature not verified"). Memory-safety findings (use-after-free, double free,
heap or stack buffer overflow, out-of-bounds read or write, sanitizer and KASAN reports) always
count; a bare segfault, SIGSEGV, SIGABRT, SIGBUS, overflow, null dereference or out-of-bounds
index counts only when the record's free text anywhere (subject, summary, labels, links) also
names untrusted input (malformed, crafted, attacker, untrusted, remote or user input, a large
request, fuzzing) or an attack surface (TLS, a certificate, a decoder or parser, a codec, an image
or compression library, a packet, http2 or grpc, a font, a protocol, malloc, an allocation or
buffer size), so "SIGSEGV in tls handshake" is withheld and "segfault in worker" gets an issue. A
label (stage, signal) that is itself a crash word is withheld. Names the
org chose (the repo, the run id and job, the issue URL, and the record's own `owner/name` wherever
it appears, as in a run-id subject or a pull request link) are not classified, nor are digests
and hex runs. Text is matched after NFKC normalisation, removing zero-width and other format
characters, splitting camelCase and turning `_`, `-`, `.`, `/`, `:`, `#` and `@` into spaces,
so `test_jwt_not_checked` and `open-redirect` count. A security record's artifact holds only its id,
kind, subject digest (`sha256:<16 hex>`, even for a commit), run and a `security` mark, so the store learns the mark and
`failure link --mirror` on the store record never republishes it. The digest hides a prose
subject, not a commit: anyone can hash the repo's public commits and find which one it is. Reporting an existing record
again with `security: "true"` or security-looking text marks it security too. If a record only
looks that way after its issue was opened, the issue's text is hidden and it is closed, and the
step fails asking a repo admin to delete it: editing an issue does not remove the old text from
its history or from emails already sent. A withdrawn issue counts as a security mark itself: a
later report (a re-run on a fresh runner) never patches or reopens it, and its record turns
security. The record's earlier public copy may already be in an artifact by then; this job's
artifact is replaced by the marks-only one. TODO(suraj): where security records go instead.

Limits: a withdrawn issue is the only mark the action can see, so it holds only until a repo
admin deletes that issue; after that a fresh report that does not look security-related opens a
public issue again. Likewise, when the first report is security-related, no issue is opened, so
nothing on GitHub remembers it, and a later such report from a fresh runner (without
`security: "true"`) opens a public issue. The store learns the mark only once `collect` has run,
and the action does not read the store. TODO(expert): a durable mark the action can check before
opening an issue. The `failure-demo` workflow proves the done-when against the real API
with a planted held canary.

## v0 status

| Item | What | PR | State |
|---|---|---|---|
| V0-TST-01 | Result schema and JUnit sink | #2 | merged |
| V0-TST-02 | Results store v0 and scorecard v0 | #3 | merged |
| V0-TST-03 | Verdict: retry, then compare with base | #4 | merged |
| V0-TST-04 | Failure records with issue mirror | #6 | merged |

## Working here

See [AGENTS.md](AGENTS.md). Run the checks with `python -m pytest`.
