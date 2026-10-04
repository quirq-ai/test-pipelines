#!/usr/bin/env bash
# V0-TST-03 done-when, end to end with a real test runner: a planted failure that also fails on
# base does not block, and a failure the change introduced still does. Builds a throwaway git
# repo, runs its tests, then lets `qqresults sink --rerun ... --fail-on-verdict` decide.
# Usage: tools/planted_demo.sh WORKDIR      (needs git, python3 with pytest, qqresults)
set -euo pipefail
work=$(realpath -m "${1:?usage: planted_demo.sh WORKDIR}")
rerun='python3 -m pytest -q -p no:cacheprovider --junitxml="$QQ_JUNIT_DIR/rerun.xml"'

make_repo() {  # make_repo DIR CHANGE_TEST_BODY
  rm -rf "$1"; mkdir -p "$1/tests"; cd "$1"
  git init -q -b main; git config user.email demo@example.com; git config user.name demo
  printf 'def test_ok():\n    assert True\n\ndef test_planted():\n    assert False, "planted: broken on base too"\n' > tests/test_demo.py
  git add -A; git commit -q -m base
  base=$(git rev-parse HEAD)
  printf '%s\n' "$2" >> tests/test_demo.py
  git commit -q -am change
}

verdict() {  # verdict NAME -> exit code of the sink deciding the check
  python3 -m pytest -q -p no:cacheprovider --junitxml=results/junit.xml >/dev/null || true
  qqresults sink --backend local --repo demo/"$1" --commit "$(git rev-parse HEAD)" \
    --junit 'results/*.xml' --root . --out "$work/out-$1" --rerun "$rerun" --base "$base" \
    --fail-on-verdict
}

make_repo "$work/exonerated" $'\ndef test_new():\n    assert True'
if verdict exonerated; then echo "OK: the planted failure that also fails on base did not block"
else echo "FAIL: a failure that also fails on base blocked the change"; exit 1; fi

make_repo "$work/introduced" $'\ndef test_ok():\n    assert False, "this change broke it"'
if verdict introduced; then echo "FAIL: a failure the change introduced did not block"; exit 1
else echo "OK: a failure the change introduced blocked"; fi
