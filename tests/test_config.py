"""Config parsing: KEY=VALUE only, never executed, environment overrides the file."""

import os

from conftest import events


def test_file_values_are_used(tk):
    tk.healthy()  # disk at 40 %
    tk.write_config("OPS_DISK_WARN=30\nOPS_DISK_CRIT=95\n")
    p = tk.run("ops-health")
    disk = events(p, "disk")[0]
    assert (disk["level"], disk["warn"], disk["crit"]) == ("WARN", 30, 95)
    assert p.returncode == 1


def test_environment_overrides_file(tk):
    tk.healthy()
    tk.write_config("OPS_DISK_WARN=30\nOPS_DISK_CRIT=95\n")
    p = tk.run("ops-health", env={"OPS_DISK_WARN": "50"})
    disk = events(p, "disk")[0]
    assert (disk["level"], disk["warn"], disk["crit"]) == ("OK", 50, 95)  # env for WARN, file for CRIT
    assert p.returncode == 0


def test_quotes_comments_blank_lines_and_crlf(tk):
    tk.healthy()
    tk.config.write_bytes(
        b"# comment\r\n\r\n   # indented comment\r\nOPS_DISK_WARN=\"30\"\r\nOPS_DISK_CRIT='95'\r\n"
    )
    p = tk.run("ops-health")
    disk = events(p, "disk")[0]
    assert (disk["warn"], disk["crit"]) == (30, 95)
    assert p.stderr == ""


def test_shell_code_in_config_is_never_executed(tk):
    tk.healthy()
    pwned = tk.tmp / "pwned"
    tk.write_config(
        f"""\
        OPS_TOP_N=$(touch {pwned}-1)
        OPS_DISK_EXCLUDE_TYPES=`touch {pwned}-2`
        OPS_DISK_MOUNTS=/; touch {pwned}-3
        touch {pwned}-4
        export OPS_MEM_WARN=1; touch {pwned}-5
        PATH=/nonexistent
        LD_PRELOAD=/tmp/evil.so
        """
    )
    p = tk.run("ops-health")
    for i in range(1, 6):
        assert not (tk.tmp / f"pwned-{i}").exists(), f"config line {i} was executed"
    # The literal text is treated as a (non-numeric) value and rejected.
    assert p.returncode == 3
    assert f"invalid OPS_TOP_N='$(touch {pwned}-1)'" in p.stderr
    assert "ignoring unknown key PATH" in p.stderr
    assert "ignoring unknown key LD_PRELOAD" in p.stderr
    assert "ignoring line that is not KEY=VALUE" in p.stderr


def test_config_values_reach_commands_literally(tk):
    """A string value with shell syntax is passed to df as one literal argument."""
    tk.fake_proc()
    args_file = tk.tmp / "df-args"
    tk.stub(
        "df",
        f'printf "%s\\n" "$@" > {args_file}\necho "Filesystem Type Use% IUse% Mounted on"\n'
        'echo "/dev/sda1 ext4 10% 1% /"\n',
    )
    payload = f"$(touch${{IFS}}{tk.tmp}/pwned)"  # a shell would run `touch .../pwned`
    tk.write_config(f"OPS_DISK_MOUNTS={payload}\n")
    p = tk.run("ops-health")
    assert p.returncode == 0, p.stderr
    assert not (tk.tmp / "pwned").exists()
    assert payload in args_file.read_text().splitlines()


def test_missing_explicit_config_file_is_an_error(tk):
    tk.healthy()
    p = tk.run("ops-health", env={"OPS_CONFIG": str(tk.tmp / "missing.conf")})
    assert p.returncode == 3
    assert "config file not readable" in p.stderr


def test_log_file_symlink_is_not_followed(tk):
    tk.healthy()
    tk.log_dir.mkdir()
    victim = tk.tmp / "victim.txt"
    victim.write_text("original\n")
    os.symlink(victim, tk.log_file)
    p = tk.run("ops-health")
    assert p.returncode == 0
    assert victim.read_text() == "original\n"
    assert "is a symlink; refusing to write to it" in p.stderr
    assert p.events  # stdout still carries the events


def test_relative_log_dir_is_rejected(tk):
    tk.healthy()
    p = tk.run("ops-health", env={"OPS_LOG_DIR": "relative/logs"})
    assert p.returncode == 0
    assert "OPS_LOG_DIR must be an absolute path" in p.stderr
    assert not (tk.tmp / "relative").exists()
