# Verification

What was run, where, and what it proved. Raw outputs live in this repository:

- `evidence/test-results/` - `pytest.txt`, `pytest-nonroot.txt`, `shellcheck.txt`, `ruff.txt`,
  `actionlint.txt`, `systemd-check.txt`, `ansible-docker.txt`, `mermaid-parse.txt`, and the
  collector scripts that produced them (`collect-*.sh`)
- `evidence/execution-logs/` - demo runs on the WSL host (`ops-health-wsl-host.txt`,
  `ops-services-demo.txt`, `ops-netdiag-github.txt`, `ops-netdiag-nonexistent.txt`,
  `backup-restore-demo.txt`, `logclean-demo.txt`, `ops-report-demo.txt`), the combined JSON log
  `demo-ops-toolkit.jsonl` and `collect-demos.sh`

Environment: Windows 11 host, WSL2 distro running Ubuntu 24.04.4 LTS (kernel
6.6.87.2-microsoft-standard-WSL2, systemd 255, bash 5.2.21, GNU tar 1.35, curl 8.5.0), commands run
as root. Docker Engine 29.8.1 inside WSL for the Ansible test. Date of all runs: 2026-10-10.

Status legend: **Executed successfully** / **Statically validated** / **Blocked by environment** /
**Not implemented**.

## Requirement matrix

| # | Requirement | Status | Evidence |
|---|---|---|---|
| 1 | `ops-health`: load vs cores + top CPU processes, MemAvailable, disk and inode per mount, top processes, WARN/CRIT thresholds, Nagios exit codes, worst wins | Executed successfully | `pytest.txt` (`test_health.py`, 21 tests), `ops-health-wsl-host.txt` |
| 2 | `ops-services`: `systemctl is-active`, TCP with timeout, HTTP expected status with curl timeouts, same exit codes | Executed successfully | `pytest.txt` (`test_services.py`, 20 tests), `ops-services-demo.txt` |
| 3 | `ops-netdiag HOST [PORT]`: getent, resolv.conf nameservers, TCP with timeout, optional HTTP, default route, hint per failure | Executed successfully | `pytest.txt` (`test_netdiag.py`), `ops-netdiag-github.txt`, `ops-netdiag-nonexistent.txt` |
| 4 | JSON-lines logs (ts, level, check, host, msg + fields) to stdout and `OPS_LOG_DIR`, actionable messages | Executed successfully | `test_events_are_also_appended_to_log_file`, `test_odd_mount_names_produce_valid_json`, `demo-ops-toolkit.jsonl` |
| 5 | Disk threshold alerts with `du -xh --max-depth=1` hint, inode hint with `df -i` | Executed successfully | `test_disk_thresholds[*]`, `test_inode_exhaustion_is_critical_with_df_i_hint`, real CRIT/WARN in `ops-health-wsl-host.txt` |
| 6 | `ops-backup`/`ops-restore`: tar.gz + SHA-256 manifest, retention, umask 077, flock, dangerous input refused, checksum verified, tampered refused, non-empty target needs `--force` | Executed successfully | `pytest.txt` (`test_backup.py`, 30 tests), `backup-restore-demo.txt` |
| 7 | `ops-logclean`: compress/delete by age inside one dir, no symlink following, dry-run default, `--apply`, dangerous dirs refused | Executed successfully | `pytest.txt` (`test_logclean.py`, 18 tests), `logclean-demo.txt` |
| 8 | Config `KEY=VALUE`, env overrides file, parsed not sourced, malicious lines not executed | Executed successfully | `test_config.py` (8 tests) |
| 9 | `lib/common.sh`, `set -Eeuo pipefail`, `--help` everywhere, usage errors exit 64 | Executed successfully | `test_cli.py` (18 tests), `test_common.py` (3 tests: EXIT trap, worst-wins on failure, JSON escaping), usage tests in netdiag/restore/logclean |
| 10 | `opsreport` Python CLI (stdlib), counts, latest problems with hints, `--since`, exit = worst status, pytest unit tests | Executed successfully | `test_opsreport.py` (22 tests), `ops-report-demo.txt` |
| 11 | systemd service + timer (oneshot, every 5 min, `Persistent=true`, hardening) validated with `systemd-analyze verify` | Executed successfully | `systemd-check.txt` |
| 11a | Unit sandbox actually works (transient `systemd-run` with the unit's `[Service]` settings, cleaned up) | Executed successfully | `systemd-check.txt` (transient run, exit 0, unit collected) |
| 11b | Timer firing every 5 minutes on an installed host | Statically validated | `systemd-analyze calendar` output in `systemd-check.txt`; units were deliberately not installed/enabled on the WSL host |
| 12 | Ansible playbook: installs to `/opt/ops-toolkit`, config 0640 root, idempotent, `--check --diff`, syntax check, ansible-lint | Executed successfully (in a throwaway ubuntu:24.04 container) | `ansible-docker.txt` |
| 12a | Ansible systemd tasks (copy units, enable/start timer) | Statically validated | syntax check + ansible-lint `production` profile in `ansible-docker.txt`; skipped in the container (`ops_toolkit_manage_systemd=false`) because it has no systemd |
| 13 | `samples/` captured from real runs | Executed successfully | written by `collect-demos.sh` from stdout of real runs (see README for the exact commands) |
| 14 | `docs/troubleshooting.md` runbook answering the interview questions | Statically validated (written and reviewed; the commands are standard Ubuntu tools and were not each executed here) | `docs/troubleshooting.md` |
| 15 | shellcheck zero findings (justified inline disables only) | Executed successfully | `shellcheck.txt` |
| 16 | ruff (optional) | Executed successfully | `ruff.txt` |
| 17 | CI workflow (shellcheck, pytest, ansible syntax + container test, systemd-analyze verify) | Statically validated (actionlint); the same commands were executed locally. Running it on GitHub: Blocked by environment (no push allowed) | `actionlint.txt` |
| 18 | Mermaid architecture diagram | Statically validated (parsed with the mermaid 11.4.1 parser; not visually rendered) | `mermaid-parse.txt` |

## Excerpts

### Tests, lint

```text
$ python -m pytest -v            (Python 3.12.3, pytest 9.1.1)
============================= 155 passed in 12.05s =============================

$ shellcheck -x bin/ops-* lib/common.sh tests/systemd-check.sh ansible/test-in-docker.sh   (0.9.0)
exit code: 0 (no output above = zero findings)
lib/common.sh:341:  # shellcheck disable=SC2016 # $1/$2 are expanded by the inner bash, on purpose
lib/common.sh:371:# shellcheck disable=SC2034 # OPS_HTTP_CODE/OPS_HTTP_RC/OPS_PROBE_MS are outputs read by the caller

$ ruff check . && ruff format --check .   (ruff 0.16.9)
All checks passed!
16 files already formatted

$ actionlint -verbose .github/workflows/ci.yml   (1.7.12)
verbose: Found total 0 errors in 166 ms for .github/workflows/ci.yml
```

Selected test names from `pytest.txt` (all PASSED):

```text
tests/test_backup.py::test_backup_restore_round_trip_is_byte_identical
tests/test_backup.py::test_tampered_archive_is_rejected
tests/test_backup.py::test_force_refuses_to_follow_symlinked_directory
tests/test_backup.py::test_force_replaces_symlinked_file_instead_of_writing_through_it
tests/test_backup.py::test_retention_keeps_newest_and_ignores_other_files
tests/test_backup.py::test_concurrent_backup_is_blocked_by_lock
tests/test_backup.py::test_restore_rejects_unsafe_member_paths_even_with_valid_checksum
tests/test_config.py::test_shell_code_in_config_is_never_executed
tests/test_config.py::test_environment_overrides_file
tests/test_health.py::test_disk_thresholds[92-CRIT-2-90]
tests/test_health.py::test_critical_beats_unknown
tests/test_logclean.py::test_dry_run_is_default_and_changes_nothing
tests/test_logclean.py::test_apply_only_touches_eligible_files
tests/test_logclean.py::test_refuses_symlink_that_resolves_to_system_directory
tests/test_netdiag.py::test_dns_failure_gives_resolver_hints_and_skips_tcp
tests/test_services.py::test_http_200_ok_and_503_critical
tests/test_services.py::test_tcp_open_port_ok_and_closed_port_critical
```

### systemd units (`systemd-check.txt`)

```text
== systemd-analyze verify (systemd 255)
verify: OK
== systemd-analyze calendar (timer schedule)
  Original form: *:0/5
Normalized form: *-*-* *:00/5:00
== systemd-analyze security --offline (exposure score of the service sandbox)
-> Overall exposure level for ops-health.service: 3.2 OK
== transient run with the unit's [Service] settings
...
transient unit exit status: 0
log file written inside the sandbox:
drwxr-x--- 2 root root 4096 Oct 10 21:32 /var/log/ops-toolkit
transient unit ops-health-transient-test-20743.service is gone (collected)
removed /var/log/ops-toolkit created by this test
```

(The arrow and an emoji in the security line were simplified here; the raw file has the original
characters.) The transient run reported a real WARNING (`/` at 84 %), exit 1, which
`SuccessExitStatus=1` maps to a successful unit, hence `systemd-run` exit status 0.

`verify` checks that `ExecStart` exists, so `tests/systemd-check.sh` copies the units to a temp
dir and points `ExecStart` at the checkout instead of `/opt/ops-toolkit`. Everything else in the
unit is verified as written.

### Ansible (`ansible-docker.txt`)

```text
ansible-playbook [core 2.21.4]
== 1. syntax check
playbook: ansible/site.yml
== 2. first run
localhost                  : ok=6    changed=6    unreachable=0    failed=0    skipped=2    rescued=0    ignored=0
== 3. second run (idempotency: expect changed=0)
localhost                  : ok=6    changed=0    unreachable=0    failed=0    skipped=2    rescued=0    ignored=0
== 4. --check --diff (expect changed=0)
localhost                  : ok=6    changed=0    unreachable=0    failed=0    skipped=2    rescued=0    ignored=0
== 5. smoke test of the installed toolkit
drwxr-x--- root:root /etc/ops-toolkit
-rw-r----- root:root /etc/ops-toolkit/ops-toolkit.conf
-rwxr-xr-x root:root /opt/ops-toolkit/bin/ops-health
lrwxrwxrwx root:root /usr/local/bin/ops-health
ops-health exit code in container: 2
== 6. ansible-lint
ansible-lint 26.9.0 using ansible-core:2.21.4 ansible-compat:26.9.0 ruamel-yaml:0.19.1
Passed: 0 failure(s), 0 warning(s) in 3 files processed of 3 encountered. Last profile that met the validation criteria was 'production'.
== result: first run changed=6, second run changed=0, check mode changed=0
PASS: idempotent (second run and check mode report changed=0)
```

The `skipped=2` are the two systemd tasks. `ops-health` exited 2 in the container because the
bind-mounted `/src` (the Windows E: drive) was 91 % full - a real reading, not a failure of the
install. The container was removed afterwards (the "containers left behind" list is empty).

### Demo runs on the WSL host

Real `ops-health` (default thresholds) - `ops-health-wsl-host.txt`, exit code 2:

```text
{"level":"CRIT","check":"disk","msg":"/mnt/d is 99% full (>= crit threshold 90%)"}
{"level":"CRIT","check":"summary","msg":"ops-health CRITICAL: 4 critical, 1 warning, 0 unknown, 6 ok"}
```

`ops-netdiag --http github.com 443` - exit code 0, five OK steps (resolver, dns, route, tcp,
http 200). `ops-netdiag ops-toolkit-demo.invalid 443` - exit code 2, `dns` CRIT with `getent_exit`
2 and the resolver hint, `tcp` UNKNOWN "skipped".

Backup/restore - `backup-restore-demo.txt`: three backups with `OPS_BACKUP_KEEP=2` left two
archives (mode `-rw-------`) and pruned the oldest; restore verified the checksum, `diff -r`
between source and restored tree returned exit 0; a second restore without `--force` was refused
(exit 1), with `--force` it succeeded with a WARN event; a copy with one changed byte was refused
with "checksum mismatch" and the target directory was never created; `OPS_BACKUP_SOURCES=/`, a
destination inside the source and restoring into `/` were all refused (exit 1).

Log cleanup - `logclean-demo.txt`: the dry run listed 1 delete and 2 compressions and changed
nothing; `--apply` did exactly that; the symlink pointing outside the directory and its target
were untouched; `/` and `/etc` were refused.

## Code review and fixes

A separate read-only review agent examined the code for injection, path-safety, `set -e`, JSON and
exit-code defects. It reported 3 High, 5 Medium and 4 Low findings. All were fixed before the
final evidence above was produced. Each fix has a regression test that passed in `pytest.txt`:

| Finding | Fix | Regression test |
|---|---|---|
| High: attacker-created (empty) restore target could get `etc -> /` planted during the restore | existing target must be owned by the caller and not group/other-writable; a new target is created with `mkdir -m 0700` (no `-p`) after verification | `test_restore_refuses_existing_target_writable_by_others`, `test_restore_creates_new_target_private` |
| High: ops-logclean re-opened listed paths, so a swapped directory could redirect `gzip`/`rm` | `find ... -delete` / `-execdir gzip` act on the file found | existing dry-run/apply/symlink tests (the race itself is not reproduced) |
| High: `python3 -m` put the current directory on `sys.path` (planted `json.py`) | `python3 -I .../report.py` | `test_ops_report_does_not_import_from_current_directory` |
| Medium: `/proc/stat` fields evaluated by bash arithmetic (`a[$(cmd)]`) | numeric check before `$(( ))` | `test_proc_stat_content_is_never_evaluated` |
| Medium: curl URL globbing / `~/.curlrc`; `TAR_OPTIONS` honoured | `curl -q -g`; `unset TAR_OPTIONS GZIP` | `test_url_globbing_is_disabled`, `test_tar_options_from_environment_are_ignored` |
| Medium: `/var/log` and non-`$HOME` home directories not refused | added `/var/log` and every `getent passwd` home | `test_refuses_var_log_itself`, `test_refuses_any_home_directory_from_passwd` |
| Medium: backup destination ownership not checked (planted lock/partial symlinks) | owner/mode check, lock opened with `>>`, refuse pre-existing `.partial` | `test_backup_refuses_destination_writable_by_others` |
| Medium: retention could prune the archive just written (clock skew) | never prune the current archive | `test_retention_never_deletes_the_archive_just_written` |
| Low: silent `df` failure or `timeout` exit 137 reported as OK | UNKNOWN whenever nothing was measured; 137 treated as timeout | `test_silent_df_failure_is_unknown_not_ok` |
| Low: fatal error after a CRIT exited 3; shell errors (unset variable) exited 1 = WARNING | worst status kept on die; EXIT trap maps unexpected exits to UNKNOWN | `test_failure_after_critical_keeps_critical`, `test_unexpected_shell_error_is_unknown_not_warning` |
| Low: timeout 0 accepted (= no timeout) | timeouts must be >= 1 | `test_zero_timeout_is_rejected[*]` |
| Low: backslash in a path broke the sha256sum manifest | hash read from stdin | `test_backslash_in_destination_path_round_trip` |

## Restarts during evidence collection

The WSL `dev` distro was shut down several times on 2026-10-10 by WSL's idle timeout, which
restarts Docker with it (the lead engineer identified the cause and started a keepalive). None of
the commands used here stop or restart services. The final evidence in this document was produced
between 15:32 and 15:36 UTC. systemd PID 1 had been running since 15:24:59 UTC, and the journal
shows no Docker stop in that window, so no run spans a restart. An earlier, superseded evidence
set (14:42-14:50 UTC) was replaced entirely.

## Not verified / known gaps

- GitHub Actions itself was not run (no push permitted). Its steps were run locally in WSL. On
  GitHub they run as the non-root `runner` user. To cover that difference, the full suite was also
  run as the unprivileged `nobody` user (`setpriv --reuid=nobody`):
  `155 passed in 12.08s` (`pytest-nonroot.txt`). Any other differences between the GitHub runner
  and this environment were not exercised.
- The playbook's systemd tasks were not applied on a systemd host (see 12a). To verify on a test
  VM: `ansible-playbook -i <inventory> ansible/site.yml`, run it again and expect `changed=0`,
  then `systemctl list-timers ops-health.timer` and `journalctl -u ops-health.service`.
- The scripts were exercised on Ubuntu 24.04 only.
- Executable bits: `/mnt/e` (9p) shows every file as 0777, so the bits that end up in git depend
  on how the files are committed. CI fails fast if `bin/ops-*` or the test scripts are not
  executable (`git update-index --chmod=+x <file>` fixes it).
