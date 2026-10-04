# test-pipelines

Part of **quirq infra** ("qq"), quirq-ai's CI/CD system for repos in any language. This repo turns
test output into stored results and mechanical verdicts.

**Chromium counterpart:** ResultDB (the results store and its ResultSink) and LUCI Analysis
(verdicts, flake handling and failure clustering).

## What it holds (plan §5.4, §5.10)

- **Result**: one test or action outcome, write-once, normalized. JUnit XML is the input,
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

What the sink publishes is permanent: the artifact is public on a public repo, and `collect`
copies it into the write-once `results` branch, which keeps it after the job log is deleted. So
each `Result` holds only structured fields (test id, status, expected, duration, file, report
path, metrics) and the head of its failure or skip message: the first 20 lines and 1,000
characters (plus a short truncation marker), with any `<system-out>`/`<system-err>` markup cut out. That is enough for the
assertion and the first frames, which tell failures apart; the full text stays in the job log.
`raw` is empty, and `<system-out>`/`<system-err>` are never stored (audit R3). To also keep each
`<testcase>` element as it was, captured output included and capped at 16,000 characters, pass
`keep-raw-junit: "true"` (`--keep-raw-junit`); only do that when everything the tests print may be
public forever. Bundles written before this change keep their `raw` and still import.

The same thing from a shell:

```sh
qqresults sink --backend local --repo quirq-ai/xo-space --commit HEAD --junit '.qq/out/junit/*.xml' --out .qq/results
qqresults show .qq/results/qq-results-...
```

Records live in `src/qqresults/model.py` (schema `quirq-results/1`): `Change`, `Run`, `Result`
(write-once, normalized; the raw `<testcase>` only with `keep-raw-junit`), `Verdict`, and
`Failure` (plan §5.10). Test ids are `<classname>::<name>`, which keeps pytest, vitest,
jest-junit and gotestsum ids stable across runs. Everything that knows GitHub is in
`backends/github.py`.

## The results store and scorecard v0 (V0-TST-02)

The store is a write-once tree of run bundles (`runs/<bundle>/`), kept on this repo's `results`
branch. The `scorecard` workflow collects every `qq-results-*` artifact from the onboarded repos'
runs into it, commits only new files, and writes `scorecard.md` and `scorecard.json` next to them
(and to the run's summary). It also collects perf's runs (V0-PRF-01): their Run names the
measured repo with kind `other`, so they are stored and queryable but never counted as that
repo's presubmit, gate or post-submit runs. Importing a run that is already stored is a no-op;
importing different bytes for the same run is an error.

```sh
git fetch origin +refs/heads/results:refs/remotes/origin/results && git worktree add .qq/store refs/remotes/origin/results
qqresults query runs --store .qq/store --repo quirq-ai/xo-space --kind postsubmit --failed
qqresults query results --store .qq/store --run <run id> --unexpected
qqresults query history --store .qq/store --test 'tests.test_greet::test_hello'
qqresults scorecard --store .qq/store --days 7          # --json for machines
qqresults collect --store .qq/store --repo quirq-ai/xo-space   # needs GITHUB_TOKEN
qqresults collect --store .qq/store --repo quirq-ai/xo-space --report r.jsonl \
  && qqresults scorecard --store .qq/store --collect-report r.jsonl   # "Collect incomplete" on skips
```

Scorecard v0 measures, per repo: gate time-to-green p50/p90 (gate runs carry their queue-entry
time once `quirq-ai/gate/timing` exports `QQ_QUEUED_AT`, a strict RFC 3339 time, before the sink, V0-GAT-04; one sample per green gate workflow run, from its first queue entry to the last
finish of the latest attempt of each job, so a re-run counts its whole wait and red runs are
not counted), main-red minutes per week (commits in main's push order, each push's `before` to
its `after`; when the stored runs do not link them into one chain, by job finish time, and the
row says so), flake rate, pass rates of gate,
post-submit and presubmit runs, and runs that stored no results. A run with test results is red
when its verdict failed; one without (a repo whose only check is a typecheck, or a job that
broke before its tests) is red only when the job itself failed, and a cancelled job, such as one
superseded by a newer push, is not counted. The sink records the job's status for this. Every other plan §8 metric is
listed as not measured, with the work item (quirq-infra v0 or v1) that will measure it, or
`TODO(suraj): no item yet`; nothing unmeasured shows as zero. A metric whose runs are stored but
say nothing (cancelled, or no results and no job status) is not measured and says "runs stored,
status unknown" rather than waiting on runs.

### A partial collect says so

`collect` skips an artifact it cannot read (an API error such as a rate-limited 403, a bad
archive, a bundle that fails its checks) with a warning, so one bad artifact cannot block the
others, and a repo whose artifacts cannot be listed does not stop the next repo. An artifact
from a run collect does not trust (a fork's pull request, a workflow not allowed, a
`--cross-repo` run off the default branch) is refused by policy at every collect; it is
reported apart as "refused (not trusted)" and is not a skip, so `--strict` ignores it. The card
must not present what is left as complete, so the scorecard workflow joins the two through one
report:

- `collect --report FILE` appends JSON lines to FILE: `{"repo", "finished": false}` for every
  repo before any is read, then, per repo, `{"repo", "finished": true, "listed", "new",
  "stored", "skipped": [reason, ...], "refused": [reason, ...]}`. Several collect calls share
  one FILE, and one repo's passes are merged: it is incomplete if any pass was. It lives in the
  runner's temp directory, never in the store, so no stale report carries over to a later card.
- `scorecard --collect-report FILE` reads it, and the card (and `scorecard.json`'s `collect`)
  then says either "Collect complete" or "Collect incomplete", with each repo that skipped
  artifacts (how many, and the first five reasons), could not be listed, or did not finish.
  Trust refusals are listed apart under either and never make it incomplete.
  A missing or unreadable report, or one that names no repo, is incomplete too; when the
  collect step fails, the workflow appends a `(collect step)` line that did not finish.

Error messages carry no URL query, so a signed storage URL never reaches the card. The runs
collect missed are left out of that card only: a skip from an API error is retried by
the next collect. Without `--collect-report` the card says nothing about collect, as before.

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
(it does for a same-repo PR). A gate run's `queued_at` must lie between 24 hours before GitHub
created its workflow run and five minutes after the run's finish (the finish is the runner's
clock), so one bad runner clock cannot dominate p90; without the run's creation time it is refused. A run may name another repo only as kind `other`, from a repo given
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
then still be `<kind>-<16 hex>`), its
`run_id` must name the run, and its opening time must lie between 24 hours before GitHub created
that run and 5 minutes after the run's last update or now, whichever is later (without the run's
creation time it is refused). A link bundle (from the `link` action) is held to the same: its
`target.json` must name a record of the repo by a `<kind>-<16 hex>` id and name the run in its
`run_id`. Its links are added only to a record already stored, and every bundle in an artifact
is checked against its record before any is imported. A bundle whose record is not stored yet
is tried again after the rest of the listing (its record may be in a newer artifact); if the
record is still missing, it is retried at the next collect, and refused for good (and not read
again) once it is 7 days old, since a record still missing after 28 scheduled collects was refused or has expired. A
link bundle may carry no mark but `security`; one with any other mark is refused whole. Every
link, in a record's artifact or a link bundle, must be dated between its record's opening and
5 minutes from now (later links win, so a link dated far ahead would outrank every later one); a
link bundle's links may also be dated up to 5 minutes before the opening, for the clock of the
runner that wrote them.
Records are type-checked (strings,
finite non-negative numbers, booleans, RFC 3339 UTC times like `2026-10-04T10:00:00Z`);
artifacts over 20 MB, zipped or not, and bundles over 50,000 results are refused. Artifacts are
read oldest first, so a record reported again keeps the opening time and run of its first report.
A refused artifact is a warning (`--strict` makes it fail the step). A stored
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
are rerun at the base commit, in a git worktree, `retry_failed + 1` times. A test that passes on
a retry is FLAKY, one that fails an assertion (FAIL) on every retry and on every base run, with
messages of the same kind, is EXONERATED, and every other failure is UNEXPECTED and fails the
change: one that passes on any base run, one that crashes on any base run (a JUnit error, such
as a fixture that reads a generated file the base worktree lacks; a crash on base is no signal,
even when the change crashes too), one that crashes with the change but fails an assertion on
base, one that fails differently there, one whose failure has no kind (below), and one with no
base result, such as a new test. When every failure on both sides has a JUnit `type` (an
exception class, as Surefire writes it), the types decide: different types fail differently,
however little each says (a `TypeError` with the change is not an `Error` on base), and one
type shared by every failure is the kind if it carries one. Only when a type is missing, or
every failure has the same type and it says nothing (Rust libtest's `assert`), do the messages
decide: the kind is the first word of the message's first line, after removing ANSI escape
codes and a leading pytest `E` marker, and only if it looks like an exception class: an
identifier or namespaced identifier (`a.b.C`, `a::C`) whose last part is CamelCase and ends in
`Error`, `Exception`, `Failure`, `Fault` or `Panic` after at least one more letter, such as
`FileNotFoundError:` (not `Error`, `Terror` or `parse_error`). **A failure with no
class-like kind never exonerates**: an empty message (libtest writes none), a file path
(`src/lib.rs:5:9:`), a test name, prose (`expected`, MSTest's `Test method X threw
exception:`), a quoted word, `assert`, `thread` (`thread 'x' panicked at`), `Traceback`, jest's
`thrown:` or `Timeout`. So a test body that reads a missing generated file on base (a FAIL in
pytest) does not exonerate a change that makes it raise something else. Generic words carry no
kind even when class-like: `Failed`, `Error`, and root classes such as `Exception` or
`Throwable`, under any namespace. A type carries no kind when it is one of those, a runner
category written for every failure whatever went wrong (audit N1: libtest's `assert` for an
`assert_eq!` and for an unrelated `unwrap()` panic alike, and `timeout`; cargo-nextest's `test
failure`, `test timeout` and `test abort`; `panicked`, `traceback`, `thrown:`, `abort`,
`signal`), or contains whitespace (including joined types such as `A / B`). A `<failure>` that
reports a timeout, an abort or a signal is a CRASH, like an `<error>`, so it never exonerates:
one whose `type` has the word `timeout`, `abort`, `aborted`, `signal` or a signal name
(libtest's `timeout`, nextest's `test timeout`), or one with no type, or a type that carries no
kind, whose message's first line begins with such a report (`Timeout of 2000ms exceeded`,
pytest-timeout's `Failed: Timeout >1.0s`, jest's `thrown: "Exceeded timeout`, `timed out`,
`Aborted`, `killed by signal 9`, `signal: 11, SIGSEGV`, `SIGSEGV`). A failure whose type is an
exception class stays a FAIL whatever its message says (`timeout: expected 3 to equal 5` with
`AssertionError`), as do `assert timeout == 5`, `timeout is None` and `signal 5 != 3`; a
timeout named later in the line or as an exception class (`TimeoutError`) stays a FAIL too, and
its kind is compared as usual. This is a heuristic and
cannot tell two failures of one kind apart. The retry and
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
That stops a fix queued ahead from exonerating a regression in a merge-commit queue, and a
rebase queue's PR whose earlier commit broke the test while main passes. It does not cover a
rebase queue where an entry ahead fixes the test and the PR's own earlier commit breaks it
again: both bases fail, and the real base (the entries ahead without any of the PR's commits)
is not derivable from the event. So a run whose base is its tested commit's first parent (a
pull request or merge-queue entry, whatever its `kind`) is compared with base only when that
commit is a merge (a pull request's merge ref, or a merge-commit queue) or has one parent that
is base_sha (a squash, or a one-commit rebase, with nothing queued ahead). Any other queue
entry, one parent that is not base_sha, which includes a squash queue with entries ahead, is
not compared, and its still-failing tests stay UNEXPECTED ("rebase-method queue: base not
derivable"). An explicit `base` is one more base the failure must also fail at: it never
replaces the run's own bases or skips this check. It must be a full 40-hex commit id, since a
tag can shadow a branch of the same name: with a branch or tag name, the run's bundle and
retries are still written, but nothing is compared ("not a full 40-hex commit id; not
compared"), so its still-failing tests stay UNEXPECTED and `fail-on-verdict` fails. Without
`rerun`, `base` is not used. TODO(expert): derive the base from the PR's
commits, or read the queue's merge method from the branch rules, once the org's merge queue
and merge method are decided (ORG-03). The rerun command comes from the builder, so the core never names a runner;
`$QQ_RETRY_TESTS` lists the failed test ids for a command that can select them. CI proves the
done-when with `tools/planted_demo.sh`: the planted failure is exonerated, and changes that break
a test or the code under it still block, including a Rust crate (real `cargo test` JUnit, through
`RUSTC_BOOTSTRAP=1 ... -Z unstable-options --format junit`) whose test panics on base and fails
an `assert_eq!` with the change. "Through the gate" waits for the merge queue (V0-ORG-03).

## Failure records (V0-TST-04)

Every held canary, canary rollback and auto-revert opens one write-once `Failure` record (plan
§5.10), mirrored to one GitHub issue labelled `qq-failure` and `qq-failure:<kind>`:

```yaml
- run: ./probe --report > "$RUNNER_TEMP/qq-summary.txt"   # written on the runner, never echoed
- uses: quirq-ai/test-pipelines/failure@<commit>     # needs issues: write
  with:
    kind: canary-held                                 # canary-held | canary-rollback | auto-revert | red-run | fuzz
    subject: ${{ steps.build.outputs.digest }}        # same kind, repo and subject: same record
    summary-file: ${{ runner.temp }}/qq-summary.txt   # one line on what happened; not logged
    stage: probe
    signal: health
```

GitHub prints every input of an action (and its steps' environment) in the run log, and a public
repo's run logs are public. So the `summary` input is published in the run log whatever the
classifier decides, and it exists only for text you would publish anyway. Pass the summary as
`summary-file: <path>`, a file on the runner (the file's contents are not logged), and never put
security detail in any other input on a public repo. Write that file without echoing the text through the log: a step that
runs `echo "${{ ... }}" > summary.txt` has the expression expanded into its script, and the script
is printed in the log, so the text is public anyway. TODO(suraj): where private details live.

The record id is derived from kind, repo and subject, and the issue carries the id in a hidden
marker, so reporting the same event twice (a retried pipeline, a second runner) still gives one
record and one issue. GitHub's issue list can lag behind a create, so two racing runners may
each open one; every report closes each open issue with the marker but the lowest-numbered as a
duplicate. What is learned later is added as link records, never by rewriting, with the
`link` action:

```yaml
- uses: quirq-ai/test-pipelines/link@<commit>        # needs issues: write
  with:
    id: ${{ steps.failure.outputs.id }}               # the failure action's id output
    culprit: quirq-ai/xo-space@<commit>
    fix: https://github.com/quirq-ai/xo-space/pull/12
    covering-test: <commit or URL>
    dir: .qq/store/failures   # only when the record is not the failure action's in this job
```

It adds the links to the record in `dir` (by default the directory the failure action keeps in
this job; otherwise a checkout of the `results` branch's `failures/`), updates the issue (closing
it when complete), and uploads the links this call added as a `qq-failure-*` link bundle:
`target.json` (the record id, its repo and this run) and `links/`, filtered as the failure
artifact is. `collect` adds them to the stored record, so the store closes when the issue does.
On a security record the bundle carries no values: each link goes up as `withheld`, next to the
security mark, so the record can still close. Linking the same value again (the issue, on every
report) adds nothing to the store. From a shell, `--link-copy DIR --run-id RUN` writes the bundle:

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
`postmortem.toml` `record_needs`), and the scorecard reports the share that are (TODO(suraj):
plan §8 also lists the operation). The scorecard counts held canaries, canary rollbacks,
auto-reverts and fuzz findings opened in the window; red-run records and the one planted record
that `failure-demo` reports (fixed by its id in `scorecard.DEMO_RECORDS`, so no pipeline can
take its own records out) are left out, each noted in the card. A fuzz
finding, like any security record, is stored only as its id and marks, so it is counted by id
and closes on its `withheld` links. A link stored as `withheld` counts as linked, so a record can
close on values nobody can read publicly.
TODO(expert): whether withheld links should count towards closing (audit S4).

Free text is never public without the opt-in. The issue title and body, the failure artifact and
the store carry only:

- kind, id, opening time and schema, which must have exactly the shape this version writes (an
  artifact whose record does not, or whose fields are not strings, is refused), and repo when it
  is a plain `owner/name` (org-chosen, so shown even when it reads like a security word);
- stage, signal and channel only when each is one of a small fixed list of known values
  (`failures.PUBLIC_LABELS`): stage `select`, `build`, `verify`, `fuzz-smoke`, `deploy`, `probe`,
  `soak`, `declare` (the canary pipeline's stages, plan §5.8), `presubmit`, `gate` or
  `postsubmit`; signal `health`, `canary-probe`, `canary-deploy`, `main-red-minutes`,
  `error-rate` or `session-drop` (infra-config `health.toml`); channel `canary`, `dev` or
  `stable`. Any other label shows as `withheld`, however harmless it looks. TODO(suraj): read
  the list from infra-config instead of keeping a copy;
- the subject, build digest, last good, first bad, run and every link (culprit, fix, covering
  test, operation, failure class, postmortem, issue) only when it is a commit (7 to 40 hex), a
  digest (`sha1:`, `sha256:` or `sha512:` with its full hex length), `owner/repo@<commit>` or
  `owner/repo#<number>`, a `https://github.com/owner/repo/` pull, issue, commit or Actions run
  URL, or the action's run id; references and URLs only when the owner is the record's own and
  the job name, and a repo name other than the record's own, do not read as security.

Those name slots are checked against a list of security words, not an allowlist, so a repo or
job name the list misses still shows. That is the accepted residual: it is a name chosen by the
org, not a description. Labels are not: a label outside the list above is withheld, because
labels such as `wild-pointer` or `timing-attack` slipped past the word list (audit R1).

Anything else, including any other URL, shows as `withheld` (a subject is replaced by its digest
instead). That fails closed on purpose: natural values such as a covering test id
(`tests/test_x.py::test_y`), an operation key or a failure class always show as `withheld`. The
summary stays in the local record unless you pass `public-summary: "true"` to the action (or
`--public-summary` to `qqresults failure open`/`link`). Since the record is write-once, a repeat
report publishes the summary only if it carries the same summary as the report that wrote it;
`failure link --public-summary` publishes whatever the record holds. The artifact is a public
copy written by `--public-copy`, never the record itself.

`--public-summary` (and `public-summary: "true"`) trusts the security classifier below, which is a
word list and can miss: a summary that describes a vulnerability in words the list does not know
is published. So keep it off for any failure that touches input handling (parsing, decoding,
authentication, crypto, file or network input), and turn it on only for summaries you would
publish yourself, such as a probe's status line.

The security classifier errs towards withholding: records flagged `security`, of kind `fuzz`, or
whose free text (summary, subject, labels, link values) matches security terms such as
"buffer overflow", "credential", "use-after-free", "remote code execution" or "without login" are
kept but never mirrored to a public issue, even with the opt-in. Words common in ordinary failures
(crash, panic, heap, leak, certificate, escalated, "not verified", overflow, injection, token,
auth, JWT, sandbox, privileged, access control, CORS) count only in a security phrase ("heap
buffer overflow", "SQL injection", "credential leak", "auth bypass", "missing authorization
check", "sandbox escape", "container breakout", "CORS any origin", "certificate verification
disabled", "JWT signature not verified"). Memory-safety findings (use-after-free, double free,
heap or stack buffer overflow, out-of-bounds read or write, invalid free, wild pointer, sanitizer
and KASAN reports) always count, as do a timing attack or side channel, a padding oracle, a TLS
downgrade, a skipped login, a hardcoded key and a zip bomb; a bare segfault, SIGSEGV, SIGABRT,
SIGBUS, SIGFPE, general protection fault, kernel oops, overflow, null dereference or out-of-bounds
index counts only when the record's free text anywhere (subject, summary, labels, links) also
names untrusted input (malformed, crafted, attacker, untrusted, remote or user input, a large
request, fuzzing) or an attack surface (TLS, a certificate, a decoder or parser, a codec, an image
or compression library, a packet, http2 or grpc, a font, a protocol, malloc, an allocation or
buffer size), so "SIGSEGV in tls handshake" is withheld and "segfault in worker" gets an issue.
"Remote exec" alone is not a security term (build systems run remote execution). Names the
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
with a planted held canary, then links and closes it with the `link` action.

## v0 status

| Item | What | PR | State |
|---|---|---|---|
| V0-TST-01 | Result schema and JUnit sink | #2 | merged |
| V0-TST-02 | Results store v0 and scorecard v0 | #3 | merged |
| V0-TST-03 | Verdict: retry, then compare with base | #4 | merged |
| V0-TST-04 | Failure records with issue mirror | #6 | merged |

## Working here

See [AGENTS.md](AGENTS.md). Run the checks with `python -m pytest`.
