"""ops-logclean: dry-run by default, only eligible files, never follows symlinks, refuses dangerous dirs."""

import gzip
import os
import subprocess

import pytest


def age(path, days):
    """Set mtime with `touch -d` (-h: change a symlink itself, not its target)."""
    subprocess.run(["touch", "-h", "-d", f"{days} days ago", str(path)], check=True)


def tree_state(*roots):
    """Snapshot of every entry under the roots: lstat mtime, size, symlink flag."""
    state = {}
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            for name in dirnames + filenames:
                path = os.path.join(dirpath, name)
                st = os.lstat(path)
                state[path] = (st.st_mtime_ns, st.st_size, os.path.islink(path))
    return state


@pytest.fixture
def logtree(tk):
    logs = tk.tmp / "applogs"
    outside = tk.tmp / "outside"
    (logs / "nested").mkdir(parents=True)
    (outside / "dir").mkdir(parents=True)
    content = "line\n" * 1000
    for rel, days in [
        ("current.log", 0),
        ("old.log", 10),
        ("nested/deep.log", 10),
        ("notes.txt", 40),
    ]:
        (logs / rel).write_text(content)
        age(logs / rel, days)
    for rel, days in [("recent.log.gz", 5), ("ancient.log.gz", 40)]:
        with gzip.open(logs / rel, "wt") as handle:
            handle.write(content)
        age(logs / rel, days)
    (outside / "victim.log").write_text(content)
    age(outside / "victim.log", 40)
    (outside / "dir" / "inner.log").write_text(content)
    age(outside / "dir" / "inner.log", 40)
    os.symlink(outside / "victim.log", logs / "link.log")
    age(logs / "link.log", 40)
    os.symlink(outside / "dir", logs / "linkdir")
    tk.env["OPS_LOGCLEAN_DIR"] = str(logs)
    return logs, outside, content


def test_dry_run_is_default_and_changes_nothing(tk, logtree):
    logs, outside, _ = logtree
    before = tree_state(logs, outside)
    p = tk.run("ops-logclean")
    assert p.returncode == 0, p.stderr
    assert tree_state(logs, outside) == before
    msgs = [e["msg"] for e in p.events]
    assert f"would delete {logs}/ancient.log.gz" in msgs
    assert f"would compress {logs}/old.log" in msgs
    assert f"would compress {logs}/nested/deep.log" in msgs
    assert len(msgs) == 4  # 1 delete + 2 compress + summary
    assert "re-run with --apply" in p.events[-1]["msg"]
    assert p.events[-1]["mode"] == "dry-run"


def test_apply_only_touches_eligible_files(tk, logtree):
    logs, outside, content = logtree
    outside_before = tree_state(outside)
    p = tk.run("ops-logclean", "--apply")
    assert p.returncode == 0, p.stderr
    # compressed (and the original removed)
    for rel in ("old.log", "nested/deep.log"):
        assert not (logs / rel).exists()
        with gzip.open(logs / (rel + ".gz"), "rt") as handle:
            assert handle.read() == content
    # deleted
    assert not (logs / "ancient.log.gz").exists()
    # untouched: too new, not a *.log, or a symlink
    assert (logs / "current.log").read_text() == content
    assert (logs / "recent.log.gz").exists()
    assert (logs / "notes.txt").exists()
    assert (logs / "link.log").is_symlink()
    assert (logs / "linkdir").is_symlink()
    # nothing outside the directory changed, even though symlinks point there
    assert tree_state(outside) == outside_before
    assert not (outside / "victim.log.gz").exists()
    assert not (outside / "dir" / "inner.log.gz").exists()
    summary = p.events[-1]
    assert (summary["delete_count"], summary["compress_count"], summary["mode"]) == (1, 2, "apply")


def test_existing_gz_is_never_overwritten(tk, logtree):
    logs, _, _ = logtree
    (logs / "old.log.gz").write_bytes(b"pre-existing archive")
    p = tk.run("ops-logclean", "--apply")
    assert p.returncode == 1
    assert (logs / "old.log.gz").read_bytes() == b"pre-existing archive"
    assert (logs / "old.log").exists()
    assert p.events[-1]["failed"] == 1


@pytest.mark.parametrize("target", ["/", "/etc", "/usr/lib", "/var", "/var/lib", "/root"])
def test_refuses_system_directories(tk, target):
    p = tk.run("ops-logclean", "--apply", env={"OPS_LOGCLEAN_DIR": target})
    assert p.returncode == 1
    assert "refusing to clean" in p.stderr


def test_refuses_home_directory(tk):
    p = tk.run("ops-logclean", "--apply", env={"OPS_LOGCLEAN_DIR": str(tk.home)})
    assert p.returncode == 1
    assert "refusing to clean" in p.stderr


def test_refuses_symlink_that_resolves_to_system_directory(tk):
    os.symlink("/etc", tk.tmp / "sneaky")
    p = tk.run("ops-logclean", "--apply", env={"OPS_LOGCLEAN_DIR": str(tk.tmp / "sneaky")})
    assert p.returncode == 1
    assert "refusing to clean /etc" in p.stderr


@pytest.mark.parametrize(
    "env, message",
    [
        ({}, "OPS_LOGCLEAN_DIR is not set"),
        ({"OPS_LOGCLEAN_DIR": "relative/logs"}, "must be an absolute path"),
        ({"OPS_LOGCLEAN_DIR": "/nonexistent/ops-toolkit-test"}, "does not exist"),
        ({"OPS_LOGCLEAN_DIR": "/tmp", "OPS_LOGCLEAN_COMPRESS_DAYS": "0"}, "at least 1"),
    ],
)
def test_invalid_configuration_is_refused(tk, env, message):
    p = tk.run("ops-logclean", env=env)
    assert p.returncode == 1
    assert message in p.stderr


def test_usage_error(tk):
    assert tk.run("ops-logclean", "--aply").returncode == 64


def test_refuses_var_log_itself(tk):
    p = tk.run("ops-logclean", "--apply", env={"OPS_LOGCLEAN_DIR": "/var/log"})
    assert p.returncode == 1
    assert "refusing to clean /var/log" in p.stderr


def test_refuses_any_home_directory_from_passwd(tk):
    """sudo sets HOME=/root, so the caller's real home must come from passwd."""
    alice = tk.tmp / "home-alice"
    alice.mkdir()
    tk.stub("getent", f'[ "$1" = passwd ] && echo "alice:x:1000:1000::{alice}:/bin/bash"\n')
    p = tk.run("ops-logclean", "--apply", env={"OPS_LOGCLEAN_DIR": str(alice)})
    assert p.returncode == 1
    assert f"refusing to clean {alice}" in p.stderr
