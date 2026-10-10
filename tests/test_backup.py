"""ops-backup / ops-restore: round trip, integrity, retention, locking and refusal of dangerous input."""

import fcntl
import hashlib
import io
import os
import stat
import subprocess
import tarfile

import pytest
from conftest import events


def make_tree(root):
    (root / "conf" / "nested").mkdir(parents=True)
    (root / "empty-dir").mkdir()
    (root / "conf" / "app.ini").write_text("[main]\nport = 8080\n")
    (root / "conf" / "nested" / "secret.key").write_bytes(os.urandom(4096))
    (root / "binary.bin").write_bytes(bytes(range(256)) * 64)
    (root / "name with spaces.txt").write_text("spaces are fine\n")
    (root / "empty.txt").write_text("")
    os.symlink("conf/app.ini", root / "link-to-ini")


def snapshot(root):
    """{relative path: (kind, sha256 of content or symlink target)} for a whole tree."""
    result = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, root)
            if os.path.islink(path):
                result[rel] = ("link", os.readlink(path))
            elif os.path.isdir(path):
                result[rel] = ("dir", None)
            else:
                with open(path, "rb") as handle:
                    result[rel] = ("file", hashlib.sha256(handle.read()).hexdigest())
    return result


@pytest.fixture
def setup(tk):
    src = tk.tmp / "srv" / "app"
    make_tree(src)
    dest = tk.tmp / "backups"
    tk.env.update({"OPS_BACKUP_SOURCES": str(src), "OPS_BACKUP_DEST": str(dest), "OPS_BACKUP_KEEP": "7"})
    return src, dest


def only_archive(dest):
    archives = sorted(dest.glob("ops-backup-*.tar.gz"))
    assert len(archives) == 1
    return archives[0]


def test_backup_restore_round_trip_is_byte_identical(tk, setup):
    src, dest = setup
    p = tk.run("ops-backup")
    assert p.returncode == 0, p.stderr
    archive = only_archive(dest)
    manifest = archive.with_name(archive.name + ".sha256")
    assert stat.S_IMODE(archive.stat().st_mode) == 0o600  # umask 077
    assert stat.S_IMODE(manifest.stat().st_mode) == 0o600
    # The manifest is standard sha256sum format.
    subprocess.run(["sha256sum", "--check", "--quiet", manifest.name], cwd=dest, check=True)
    created = events(p, "backup")[0]
    assert created["level"] == "INFO" and created["archive"] == str(archive)

    target = tk.tmp / "restore"
    r = tk.run("ops-restore", str(archive), str(target))
    assert r.returncode == 0, r.stderr
    assert "checksum verified" in events(r, "restore")[0]["msg"]
    restored = target / src.relative_to("/")
    assert snapshot(restored) == snapshot(src)
    assert (restored / "link-to-ini").is_symlink()


def test_tampered_archive_is_rejected(tk, setup):
    _, dest = setup
    assert tk.run("ops-backup").returncode == 0
    archive = only_archive(dest)
    data = bytearray(archive.read_bytes())
    data[len(data) // 2] ^= 0xFF
    archive.write_bytes(bytes(data))
    target = tk.tmp / "restore"
    r = tk.run("ops-restore", str(archive), str(target))
    assert r.returncode == 1
    assert "checksum mismatch" in r.stderr
    assert r.events[-1]["level"] == "ERROR"
    assert not target.exists()


def test_archive_without_manifest_is_rejected(tk, setup):
    _, dest = setup
    assert tk.run("ops-backup").returncode == 0
    archive = only_archive(dest)
    archive.with_name(archive.name + ".sha256").unlink()
    r = tk.run("ops-restore", str(archive), str(tk.tmp / "restore"))
    assert r.returncode == 1
    assert "manifest missing" in r.stderr


def test_non_empty_target_requires_force(tk, setup):
    src, dest = setup
    assert tk.run("ops-backup").returncode == 0
    archive = only_archive(dest)
    target = tk.tmp / "restore"
    target.mkdir()
    (target / "keep.txt").write_text("user data\n")

    r = tk.run("ops-restore", str(archive), str(target))
    assert r.returncode == 1
    assert "not empty" in r.stderr
    assert sorted(p.name for p in target.iterdir()) == ["keep.txt"]

    r = tk.run("ops-restore", "--force", str(archive), str(target))
    assert r.returncode == 0, r.stderr
    assert (target / "keep.txt").read_text() == "user data\n"
    assert snapshot(target / src.relative_to("/")) == snapshot(src)


def test_force_refuses_to_follow_symlinked_directory(tk, setup):
    src, dest = setup
    assert tk.run("ops-backup").returncode == 0
    archive = only_archive(dest)
    outside = tk.tmp / "outside"
    outside.mkdir()
    target = tk.tmp / "restore"
    target.mkdir()
    first = src.relative_to("/").parts[0]  # e.g. "tmp"
    os.symlink(outside, target / first)
    r = tk.run("ops-restore", "--force", str(archive), str(target))
    assert r.returncode == 1
    assert "is a symlink" in r.stderr
    assert list(outside.iterdir()) == []


def test_force_replaces_symlinked_file_instead_of_writing_through_it(tk, setup):
    src, dest = setup
    assert tk.run("ops-backup").returncode == 0
    archive = only_archive(dest)
    victim = tk.tmp / "victim.txt"
    victim.write_text("must not change\n")
    target = tk.tmp / "restore"
    restored_ini = target / src.relative_to("/") / "conf" / "app.ini"
    restored_ini.parent.mkdir(parents=True)
    os.symlink(victim, restored_ini)
    r = tk.run("ops-restore", "--force", str(archive), str(target))
    assert r.returncode == 0, r.stderr
    assert victim.read_text() == "must not change\n"
    assert not restored_ini.is_symlink()
    assert restored_ini.read_text() == (src / "conf" / "app.ini").read_text()


def test_retention_keeps_newest_and_ignores_other_files(tk, setup):
    _, dest = setup
    dest.mkdir()
    old = [
        "ops-backup-20200101T000000Z.tar.gz",
        "ops-backup-20210101T000000Z.tar.gz",
        "ops-backup-20220101T000000Z.tar.gz",
    ]
    for name in old:
        (dest / name).write_bytes(b"old")
        (dest / (name + ".sha256")).write_text("x\n")
    (dest / "notes.txt").write_text("not a backup\n")
    (dest / "ops-backup-manual.tar.gz").write_bytes(b"hand-made, different name pattern")

    p = tk.run("ops-backup", env={"OPS_BACKUP_KEEP": "2"})
    assert p.returncode == 0, p.stderr
    current = sorted(f.name for f in dest.glob("ops-backup-*Z.tar.gz"))
    new = [name for name in current if name not in old]
    assert len(new) == 1
    assert current == ["ops-backup-20220101T000000Z.tar.gz", new[0]]
    for gone in old[:2]:
        assert not (dest / (gone + ".sha256")).exists()
    assert (dest / "notes.txt").exists()
    assert (dest / "ops-backup-manual.tar.gz").exists()
    pruned = [e for e in events(p, "backup") if e["msg"].startswith("pruned")]
    assert len(pruned) == 2


@pytest.mark.parametrize(
    "sources, dest, message",
    [
        ("/", None, "refusing to back up '/'"),
        ("/proc", None, "pseudo filesystem"),
        ("relative/dir", None, "must be an absolute path"),
        ("{tmp}/does-not-exist", None, "does not exist"),
        ("{tmp}/srv/app", "{tmp}/srv/app/backups", "inside source"),
        ("{tmp}/srv", "{tmp}/srv/app", "inside source"),
        ("{tmp}/srv/app", "/", "refusing to write backups into /"),
        ("{tmp}/srv/app", "/etc", "refusing to write backups into /etc"),
        ("{tmp}/srv/app", "relative/dest", "must be an absolute path"),
        ("", None, "nothing to back up"),
    ],
)
def test_dangerous_backup_inputs_are_refused(tk, setup, sources, dest, message):
    env = {"OPS_BACKUP_SOURCES": sources.format(tmp=tk.tmp)}
    if dest is not None:
        env["OPS_BACKUP_DEST"] = dest.format(tmp=tk.tmp)
    p = tk.run("ops-backup", env=env)
    assert p.returncode == 1
    assert message in p.stderr
    assert p.events[-1]["level"] == "ERROR"


def test_concurrent_backup_is_blocked_by_lock(tk, setup):
    _, dest = setup
    dest.mkdir()
    with open(dest / ".ops-backup.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        p = tk.run("ops-backup")
    assert p.returncode == 1
    assert "already running" in p.stderr
    assert list(dest.glob("*.tar.gz")) == []


def test_restore_refuses_dangerous_targets(tk, setup):
    _, dest = setup
    assert tk.run("ops-backup").returncode == 0
    archive = only_archive(dest)
    for target in ("/", "/etc", str(tk.home)):
        r = tk.run("ops-restore", "--force", str(archive), target)
        assert r.returncode == 1, target
        assert "refusing to restore" in r.stderr


def test_restore_rejects_unsafe_member_paths_even_with_valid_checksum(tk):
    archive = tk.tmp / "evil.tar.gz"
    buf = io.BytesIO(b"pwned\n")
    with tarfile.open(archive, "w:gz") as tar:
        info = tarfile.TarInfo("../escaped.txt")
        info.size = len(buf.getvalue())
        tar.addfile(info, buf)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (tk.tmp / "evil.tar.gz.sha256").write_text(f"{digest}  evil.tar.gz\n")
    r = tk.run("ops-restore", str(archive), str(tk.tmp / "restore"))
    assert r.returncode == 1
    assert "unsafe member path" in r.stderr
    assert not (tk.tmp / "escaped.txt").exists()


@pytest.mark.parametrize("args", [[], ["only-one"], ["a", "b", "c"], ["--bogus", "a", "b"]])
def test_restore_usage_errors(tk, args):
    assert tk.run("ops-restore", *args).returncode == 64


# --- regression tests for review findings -------------------------------------------------


def test_restore_refuses_existing_target_writable_by_others(tk, setup):
    """Another user could pre-create the target and plant symlinks in it while we work."""
    _, dest = setup
    assert tk.run("ops-backup").returncode == 0
    archive = only_archive(dest)
    target = tk.tmp / "shared"
    target.mkdir()
    target.chmod(0o777)
    r = tk.run("ops-restore", str(archive), str(target))
    assert r.returncode == 1
    assert "writable by group or others" in r.stderr
    assert list(target.iterdir()) == []


def test_restore_creates_new_target_private(tk, setup):
    _, dest = setup
    assert tk.run("ops-backup").returncode == 0
    target = tk.tmp / "new" / "restore"
    r = tk.run("ops-restore", str(only_archive(dest)), str(target))
    assert r.returncode == 0, r.stderr
    assert stat.S_IMODE(target.stat().st_mode) == 0o700


def test_backup_refuses_destination_writable_by_others(tk, setup):
    _, dest = setup
    dest.mkdir()
    dest.chmod(0o777)
    p = tk.run("ops-backup")
    assert p.returncode == 1
    assert "writable by group or others" in p.stderr
    assert list(dest.iterdir()) == []


def test_retention_never_deletes_the_archive_just_written(tk, setup):
    """Archives dated in the future (clock went backwards) must not push the new one out."""
    _, dest = setup
    dest.mkdir()
    future = dest / "ops-backup-20991231T235959Z.tar.gz"
    future.write_bytes(b"from the future")
    p = tk.run("ops-backup", env={"OPS_BACKUP_KEEP": "1"})
    assert p.returncode == 0, p.stderr
    remaining = [f.name for f in dest.glob("ops-backup-*Z.tar.gz")]
    assert len(remaining) == 1
    assert remaining[0] != future.name  # the new archive survived, the oldest-by-policy went


def test_tar_options_from_environment_are_ignored(tk, setup):
    p = tk.run("ops-backup", env={"TAR_OPTIONS": "--this-option-does-not-exist"})
    assert p.returncode == 0, p.stderr


def test_backslash_in_destination_path_round_trip(tk, setup):
    src, _ = setup
    dest = tk.tmp / "back\\ups"
    p = tk.run("ops-backup", env={"OPS_BACKUP_DEST": str(dest)})
    assert p.returncode == 0, p.stderr
    target = tk.tmp / "restore"
    r = tk.run("ops-restore", str(only_archive(dest)), str(target))
    assert r.returncode == 0, r.stderr
    assert snapshot(target / src.relative_to("/")) == snapshot(src)
