"""opsreport: unit tests for parsing/summarizing plus one end-to-end run on a real ops-health log."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from opsreport import report


def line(level, check="disk", ts="2026-10-10T10:00:00Z", **fields):
    return json.dumps(
        {"ts": ts, "level": level, "check": check, "host": "web1", "msg": f"{check} {level}", **fields}
    )


def test_counts_by_level_and_check():
    s = report.summarize([line("OK"), line("WARN"), line("OK", check="cpu"), line("CRIT", check="memory")])
    assert s.events == 4
    assert s.by_level == {"OK": 2, "WARN": 1, "CRIT": 1}
    assert s.by_check["disk"] == {"OK": 1, "WARN": 1}
    assert s.worst == "CRIT" and s.exit_code == 2


def test_summary_events_count_as_runs_not_events():
    s = report.summarize([line("OK"), line("CRIT", check="summary"), line("OK", check="summary")])
    assert (s.events, s.runs) == (1, 2)
    assert s.worst == "OK"


@pytest.mark.parametrize(
    "levels, expected",
    [
        ([], "OK"),
        (["OK", "INFO"], "OK"),
        (["OK", "WARN"], "WARN"),
        (["UNKNOWN"], "UNKNOWN"),
        (["UNKNOWN", "WARN"], "WARN"),
        (["WARN", "CRIT", "UNKNOWN"], "CRIT"),
        (["ERROR"], "CRIT"),
        (["SOMETHING-NEW"], "UNKNOWN"),
    ],
)
def test_worst_status_ordering(levels, expected):
    assert report.worst_status(levels) == expected


def test_parse_since_relative_and_absolute():
    now = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
    assert report.parse_since("2h", now) == now - timedelta(hours=2)
    assert report.parse_since("30m", now) == now - timedelta(minutes=30)
    assert report.parse_since("1d", now) == now - timedelta(days=1)
    assert report.parse_since("2026-10-10T08:00:00Z") == datetime(2026, 10, 10, 8, 0, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        report.parse_since("yesterday")


def test_since_filters_older_events():
    lines = [line("CRIT", ts="2026-10-09T23:59:59Z"), line("WARN", ts="2026-10-10T00:00:01Z")]
    s = report.summarize(lines, since=report.parse_since("2026-10-10T00:00:00Z"))
    assert (s.events, s.older_than_since, s.worst) == (1, 1, "WARN")


def test_malformed_lines_are_counted_not_fatal():
    lines = [
        "not json",
        '{"level": "OK"}',
        "[]",
        '{"ts": "bad", "level": "OK", "check": "x"}',
        "",
        line("WARN"),
    ]
    s = report.summarize(lines)
    assert (s.malformed, s.events, s.worst) == (4, 1, "WARN")


def test_text_report_lists_newest_problems_with_hints():
    lines = [
        line("WARN", ts="2026-10-10T10:00:00Z", hint="old hint"),
        line("OK", ts="2026-10-10T10:01:00Z"),
        line("CRIT", ts="2026-10-10T10:02:00Z", hint="du -xh --max-depth=1 / | sort -h"),
    ]
    text = report.format_text(report.summarize(lines), limit=10)
    assert text.index("du -xh") < text.index("old hint")
    assert "hint: du -xh --max-depth=1 / | sort -h" in text
    assert text.endswith("overall: CRIT (exit 2)")
    limited = report.format_text(report.summarize(lines), limit=1)
    assert "old hint" not in limited


def test_main_exit_code_text_and_json(tmp_path, capsys):
    log = tmp_path / "ops.jsonl"
    log.write_text("\n".join([line("OK"), line("WARN", hint="check it")]) + "\n")
    assert report.main([str(log)]) == 1
    assert "overall: WARN (exit 1)" in capsys.readouterr().out
    assert report.main(["--json", str(log)]) == 1
    data = json.loads(capsys.readouterr().out)
    assert (data["worst"], data["exit_code"], data["by_level"]) == ("WARN", 1, {"OK": 1, "WARN": 1})
    assert data["problems"][0]["hint"] == "check it"


def test_main_reads_default_log_from_ops_log_dir(tmp_path, monkeypatch, capsys):
    (tmp_path / "ops-toolkit.jsonl").write_text(line("CRIT") + "\n")
    monkeypatch.setenv("OPS_LOG_DIR", str(tmp_path))
    assert report.main([]) == 2


def test_main_missing_file_is_unknown(tmp_path, capsys):
    assert report.main([str(tmp_path / "nope.jsonl")]) == 3
    assert "cannot read" in capsys.readouterr().err


@pytest.mark.parametrize("args", [["--since", "yesterday"], ["--limit", "-1"], ["--bogus"]])
def test_main_usage_errors_exit_64(args):
    with pytest.raises(SystemExit) as exc:
        report.main(args)
    assert exc.value.code == 64


def test_end_to_end_report_on_real_ops_health_log(tk):
    tk.fake_proc()
    tk.fake_df([("/dev/sda1", "ext4", "92%", "10%", "/")])
    assert tk.run("ops-health").returncode == 2
    p = tk.run("ops-report", str(tk.log_file))
    assert p.returncode == 2
    assert "disk: / is 92% full (>= crit threshold 90%)" in p.stdout
    assert "hint: find what grew: du -xh --max-depth=1 / | sort -h" in p.stdout
    p = tk.run("ops-report", "--json", "--since", "1h", str(tk.log_file))
    assert json.loads(p.stdout)["runs"] == 1


def test_ops_report_does_not_import_from_current_directory(tk):
    """`cd /tmp && sudo ops-report` must not import a planted /tmp/json.py."""
    marker = tk.tmp / "pwned"
    (tk.tmp / "json.py").write_text(f"open({str(marker)!r}, 'w').close()\n")
    (tk.tmp / "empty.jsonl").write_text("")
    p = tk.run("ops-report", str(tk.tmp / "empty.jsonl"))  # tk.run uses cwd=tk.tmp
    assert p.returncode == 0, p.stderr
    assert not marker.exists()
