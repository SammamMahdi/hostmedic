"""Summarize ops-toolkit JSON-lines logs.

Reads the events written by the bin/ops-* commands and prints counts by level and
by check plus the most recent problems with their hints. The exit code follows the
same Nagios convention as the shell tools: the worst status seen wins
(CRIT=2 beats WARN=1 beats UNKNOWN=3 beats OK=0).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

EXIT_CODES = {"OK": 0, "WARN": 1, "CRIT": 2, "UNKNOWN": 3}
# Every log level maps onto a Nagios status. ERROR comes from backup/restore/logclean.
LEVEL_TO_STATUS = {
    "OK": "OK",
    "INFO": "OK",
    "WARN": "WARN",
    "CRIT": "CRIT",
    "ERROR": "CRIT",
    "UNKNOWN": "UNKNOWN",
}
SEVERITY = {"OK": 0, "UNKNOWN": 1, "WARN": 2, "CRIT": 3}
PROBLEM_LEVELS = {"WARN", "CRIT", "ERROR", "UNKNOWN"}
_RELATIVE = re.compile(r"^(\d+)([smhd])$")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_timestamp(value: str) -> datetime:
    """Parse an ISO-8601 timestamp; a trailing 'Z' or no offset means UTC."""
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def parse_since(value: str, now: datetime | None = None) -> datetime:
    """Accept a relative age ('45s', '30m', '2h', '1d') or an ISO-8601 timestamp."""
    match = _RELATIVE.match(value)
    if match:
        now = now or datetime.now(timezone.utc)
        return now - timedelta(seconds=int(match.group(1)) * _UNIT_SECONDS[match.group(2)])
    return parse_timestamp(value)


def worst_status(levels: Iterable[str]) -> str:
    """Return the most severe Nagios status among log levels (unknown levels count as UNKNOWN)."""
    worst = "OK"
    for level in levels:
        status = LEVEL_TO_STATUS.get(level, "UNKNOWN")
        if SEVERITY[status] > SEVERITY[worst]:
            worst = status
    return worst


@dataclass
class Summary:
    events: int = 0
    runs: int = 0
    malformed: int = 0
    older_than_since: int = 0
    by_level: Counter = field(default_factory=Counter)
    by_check: dict = field(default_factory=lambda: defaultdict(Counter))
    problems: list = field(default_factory=list)
    worst: str = "OK"

    @property
    def exit_code(self) -> int:
        return EXIT_CODES[self.worst]


def summarize(lines: Iterable[str], since: datetime | None = None) -> Summary:
    summary = Summary()
    for line in lines:
        raw = line.strip()
        if not raw:
            continue
        try:
            event = json.loads(raw)
            ts = parse_timestamp(event["ts"])
            level = str(event["level"])
            check = str(event["check"])
        except (ValueError, KeyError, TypeError, AttributeError):
            summary.malformed += 1
            continue
        if since is not None and ts < since:
            summary.older_than_since += 1
            continue
        if check == "summary":
            # One per run of ops-health/ops-services/ops-netdiag; it repeats the worst status.
            summary.runs += 1
            continue
        summary.events += 1
        summary.by_level[level] += 1
        summary.by_check[check][level] += 1
        if level in PROBLEM_LEVELS:
            summary.problems.append((ts, len(summary.problems), event))
    summary.worst = worst_status(summary.by_level)
    # Newest first; within the same second, later lines in the log are newer.
    summary.problems.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return summary


def _counts(counter: Counter) -> str:
    return "  ".join(f"{name}={count}" for name, count in sorted(counter.items()))


def format_text(summary: Summary, limit: int) -> str:
    out = [
        f"events: {summary.events}  runs: {summary.runs}  malformed lines: {summary.malformed}"
        + (f"  older than --since: {summary.older_than_since}" if summary.older_than_since else ""),
        f"by level: {_counts(summary.by_level) or '-'}",
        "by check:",
    ]
    width = max((len(check) for check in summary.by_check), default=0)
    for check in sorted(summary.by_check):
        out.append(f"  {check:<{width}}  {_counts(summary.by_check[check])}")
    out.append(f"latest problems (newest first, max {limit}):")
    shown = summary.problems[:limit]
    for _, _, event in shown:
        out.append(
            f"  {event['ts']} {event['level']:<7} {event.get('host', '?')} {event['check']}: {event.get('msg', '')}"
        )
        if event.get("hint"):
            out.append(f"      hint: {event['hint']}")
    if not shown:
        out.append("  none")
    out.append(f"overall: {summary.worst} (exit {summary.exit_code})")
    return "\n".join(out)


def to_json(summary: Summary, limit: int) -> str:
    return json.dumps(
        {
            "events": summary.events,
            "runs": summary.runs,
            "malformed": summary.malformed,
            "older_than_since": summary.older_than_since,
            "by_level": dict(summary.by_level),
            "by_check": {check: dict(counts) for check, counts in summary.by_check.items()},
            "problems": [event for _, _, event in summary.problems[:limit]],
            "worst": summary.worst,
            "exit_code": summary.exit_code,
        },
        indent=2,
        sort_keys=True,
    )


def default_log_file() -> str:
    """Same default location as lib/common.sh."""
    log_dir = os.environ.get("OPS_LOG_DIR")
    if not log_dir:
        if os.geteuid() == 0:
            log_dir = "/var/log/ops-toolkit"
        else:
            state = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
            log_dir = os.path.join(state, "ops-toolkit")
    return os.path.join(log_dir, "ops-toolkit.jsonl")


def _read_lines(paths: list[str]) -> Iterator[str]:
    for path in paths:
        if path == "-":
            yield from sys.stdin
        else:
            with open(path, encoding="utf-8", errors="replace") as handle:
                yield from handle


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        # argparse exits 2, which would read as CRITICAL; use EX_USAGE like the shell tools.
        self.print_usage(sys.stderr)
        self.exit(64, f"{self.prog}: error: {message}\n")


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(
        prog="ops-report",
        description="Summarize ops-toolkit JSON-lines logs. Exit code = worst status seen "
        "(0 OK, 1 WARNING, 2 CRITICAL, 3 UNKNOWN).",
    )
    parser.add_argument(
        "logfiles",
        nargs="*",
        metavar="LOGFILE",
        help="log file(s), '-' for stdin (default: $OPS_LOG_DIR/ops-toolkit.jsonl)",
    )
    parser.add_argument("--since", help="only events newer than this: 30m, 2h, 1d or an ISO-8601 time")
    parser.add_argument("--limit", type=int, default=10, help="latest problems to show (default 10)")
    parser.add_argument("--json", action="store_true", help="print the summary as JSON")
    args = parser.parse_args(argv)

    since = None
    if args.since:
        try:
            since = parse_since(args.since)
        except ValueError:
            parser.error(f"invalid --since value: {args.since!r}")
    if args.limit < 0:
        parser.error("--limit must be >= 0")

    paths = args.logfiles or [default_log_file()]
    try:
        summary = summarize(_read_lines(paths), since)
    except OSError as exc:
        print(f"ops-report: error: cannot read {exc.filename}: {exc.strerror}", file=sys.stderr)
        return EXIT_CODES["UNKNOWN"]

    print(to_json(summary, args.limit) if args.json else format_text(summary, args.limit))
    return summary.exit_code


if __name__ == "__main__":
    sys.exit(main())
