# Agent guide

How an agent changes this repo safely. Read `README.md` first.

- Every change is a pull request against `main`, titled with its work item id (for example
  `V0-TST-01: ...`). It lands only with the `presubmit` check green.
- Results are write-once. Never add code that edits or deletes a stored record; corrections are new
  records that point at the old one.
- The core stays agnostic: no module under `src/qqresults` names a language, build tool or test
  runner. JUnit XML is the input; the adapters in `quirq-ai/recipes` produce it.
- GitHub-specific code stays behind a `backend` field (`github` now, `launchpad` later).
- Failures that look like security issues never go to a public issue.
- Leave `.github/CODEOWNERS` and any `owners` list empty; suraj assigns people.
- Mark a decision you cannot make with a one-line `TODO(suraj):` or `TODO(expert):`.
- This repo is public: no secrets, tokens or internal hostnames.
- Use other qq repos by pinned commit, never by copying their code.
