#!/usr/bin/env bash
# V0-TST-03 done-when, end to end with a real test runner: a planted failure that also fails on
# base does not block, while a change that breaks a test (through the test or through the code
# under test) does. Builds throwaway git repos, runs their tests, then lets
# `qqresults sink --rerun ... --fail-on-verdict` decide.
# Usage: tools/planted_demo.sh WORKDIR      (needs git, python3 with pytest, qqresults)
set -euo pipefail
work=$(realpath -m "${1:?usage: planted_demo.sh WORKDIR}")
# `python3 -m pytest` puts $PWD on sys.path, so each side tests the code in its own checkout.
rerun='python3 -m pytest -q -p no:cacheprovider --junitxml="$QQ_JUNIT_DIR/rerun.xml"'

make_repo() {  # make_repo DIR CHANGE_FILE CHANGE_TEXT
  rm -rf "$1"; mkdir -p "$1/tests"; cd "$1"
  git init -q -b main; git config user.email demo@example.com; git config user.name demo
  printf 'def add(a, b):\n    return a + b\n' > calc.py
  printf 'import calc\n\ndef test_add():\n    assert calc.add(2, 2) == 4\n\ndef test_planted():\n    assert False, "planted: broken on base too"\n' > tests/test_demo.py
  git add -A; git commit -q -m base
  base=$(git rev-parse HEAD)
  printf '%s\n' "$3" >> "$2"
  git commit -q -am change
}

verdict() {  # verdict NAME -> sets $code and $out from the sink deciding the check
  python3 -m pytest -q -p no:cacheprovider --junitxml=results/junit.xml >/dev/null || true
  code=0
  out=$(qqresults sink --backend local --repo demo/"$1" --commit "$(git rev-parse HEAD)" \
    --junit 'results/*.xml' --root . --out "$work/out-$1" --rerun "$rerun" --base "$base" \
    --fail-on-verdict 2>&1) || code=$?
  printf '%s\n' "$out" | grep -E '^(verdict|  [A-Z])' || true
}

make_repo "$work/exonerated" tests/test_demo.py $'\ndef test_new():\n    assert True'
verdict exonerated
if [ "$code" = 0 ] && grep -q "EXONERATED tests.test_demo::test_planted" <<< "$out"; then
  echo "OK: the planted failure that also fails on base did not block"
else echo "FAIL: a failure that also fails on base blocked the change (exit $code)"; exit 1; fi

introduced() {  # introduced NAME FILE TEXT: a change that breaks test_add must block
  make_repo "$work/introduced-$1" "$2" "$3"
  verdict "introduced-$1"
  if [ "$code" = 1 ] && grep -q "UNEXPECTED tests.test_demo::test_add  fails with the change and passes without it" <<< "$out"; then
    echo "OK: a change that broke the $1 blocked"
  else echo "FAIL: a change that broke the $1 did not block as expected (exit $code)"; exit 1; fi
}
introduced test tests/test_demo.py $'\ndef test_add():\n    assert False, "this change broke the test"'
introduced code calc.py $'\ndef add(a, b):\n    return a - b'
