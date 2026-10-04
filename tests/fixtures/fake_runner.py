"""A stand-in test runner for the retry tests: reads cases.txt in the current directory.

Each line is `<test id> <pass|fail|crash|flaky>`. A crash is a JUnit <error>. A flaky test fails
the first time it runs (per state file $FAKE_STATE) and passes after. Writes JUnit XML to $QQ_JUNIT_DIR, or to argv[1].
"""
import os
import sys
from pathlib import Path
from xml.sax.saxutils import quoteattr

state = Path(os.environ.get("FAKE_STATE", "/dev/null"))
seen = set(state.read_text().split()) if state.is_file() else set()
cases = []
for line in Path("cases.txt").read_text().splitlines():
    test, outcome = line.split()
    if outcome == "flaky":
        outcome = "pass" if test in seen else "fail"
        seen.add(test)
    cases.append((test, outcome))
if state.name != "null":
    state.write_text("\n".join(sorted(seen)))
out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(os.environ["QQ_JUNIT_DIR"]) / "fake.xml"
out.parent.mkdir(parents=True, exist_ok=True)
body = "".join(
    f"<testcase classname={quoteattr(t.split('::')[0])} name={quoteattr(t.split('::')[1])}>"
    + {"fail": '<failure message="AssertionError: planted"/>', "crash": '<error message="crashed"/>'}.get(o, "")
    + "</testcase>" for t, o in cases)
out.write_text(f'<testsuite name="fake">{body}</testsuite>')
sys.exit(1 if any(o in ("fail", "crash") for _, o in cases) else 0)
