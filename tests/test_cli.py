"""Every command: --help exits 0 (even with a broken config), unknown options exit 64."""

import pytest

COMMANDS = [
    "ops-health",
    "ops-services",
    "ops-netdiag",
    "ops-backup",
    "ops-restore",
    "ops-logclean",
    "ops-report",
]


@pytest.mark.parametrize("cmd", COMMANDS)
def test_help_exits_zero(tk, cmd):
    p = tk.run(cmd, "--help", env={"OPS_CONFIG": "/nonexistent/broken.conf"})
    assert p.returncode == 0
    assert "usage" in p.stdout.lower()
    assert p.stderr == ""


@pytest.mark.parametrize("cmd", COMMANDS)
def test_unknown_option_exits_64(tk, cmd):
    p = tk.run(cmd, "--no-such-option")
    assert p.returncode == 64
    assert p.stdout == ""
    assert "--no-such-option" in p.stderr or "unrecognized arguments" in p.stderr


@pytest.mark.parametrize("cmd", ["ops-health", "ops-services", "ops-backup", "ops-logclean"])
def test_unexpected_positional_argument_exits_64(tk, cmd):
    assert tk.run(cmd, "extra").returncode == 64
