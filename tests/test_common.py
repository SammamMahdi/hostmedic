"""lib/common.sh behaviour that the commands rely on, tested by sourcing the library directly."""

import json
import subprocess

from conftest import ROOT


def run_lib(tk, script, env=None):
    """Run a small bash program that sources lib/common.sh like the bin/ scripts do."""
    program = f"set -Eeuo pipefail\nsource {ROOT}/lib/common.sh\nops_init check\n{script}\n"
    proc = subprocess.run(
        ["bash", "-c", program],
        env={**tk.env, **(env or {})},
        cwd=tk.tmp,
        capture_output=True,
        text=True,
        check=False,
    )
    proc.events = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
    return proc


def test_unexpected_shell_error_is_unknown_not_warning(tk):
    """An unset variable bypasses the ERR trap and would exit 1 (= WARNING) without the EXIT trap."""
    p = run_lib(tk, 'ops_status OK demo "fine"\necho "$NOT_SET_ANYWHERE"')
    assert p.returncode == 3
    assert p.events[-1]["level"] == "UNKNOWN"
    assert "unexpected exit" in p.events[-1]["msg"]


def test_failure_after_critical_keeps_critical(tk):
    p = run_lib(tk, 'ops_status CRIT demo "broken"\nfalse')
    assert p.returncode == 2
    assert "unexpected failure" in p.events[-1]["msg"]


def test_json_escaping_round_trips(tk):
    nasty = 'quote " backslash \\ tab \t amp & bell \a newline \n end'
    p = run_lib(tk, 'ops_log INFO demo "$NASTY" "field=$NASTY" n=007 m=-1.5', env={"NASTY": nasty})
    assert p.returncode == 0, p.stderr
    event = p.events[0]
    expected = nasty.replace("\a", "")  # other control characters are dropped
    assert event["msg"] == expected and event["field"] == expected
    assert event["n"] == "007"  # not a valid JSON number, so kept as a string
    assert event["m"] == -1.5
