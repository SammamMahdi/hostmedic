"""ops-health: thresholds, exit codes, JSON events, hints. Uses fake df/nproc and a fake /proc."""

import re

import pytest
from conftest import events

TS = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")


def test_healthy_host_exits_ok(tk):
    tk.healthy()
    p = tk.run("ops-health")
    assert p.returncode == 0, p.stderr
    assert {e["check"] for e in p.events} == {"cpu", "memory", "disk", "inode", "summary"}
    assert all(e["level"] == "OK" for e in p.events)
    assert events(p, "summary")[0]["msg"].startswith("ops-health OK")


@pytest.mark.parametrize(
    "used, level, code, threshold",
    [(79, "OK", 0, None), (80, "WARN", 1, 80), (89, "WARN", 1, 80), (90, "CRIT", 2, 90), (92, "CRIT", 2, 90)],
)
def test_disk_thresholds(tk, used, level, code, threshold):
    tk.fake_proc()
    tk.fake_df([("/dev/sda1", "ext4", f"{used}%", "3%", "/")])
    p = tk.run("ops-health")
    assert p.returncode == code
    disk = events(p, "disk")[0]
    assert (disk["level"], disk["value"], disk["mount"]) == (level, used, "/")
    assert (disk["warn"], disk["crit"]) == (80, 90)
    if threshold is None:
        assert "hint" not in disk
    else:
        assert disk["threshold"] == threshold
        assert f">= {level.lower()} threshold {threshold}%" in disk["msg"]
        assert "du -xh --max-depth=1 / | sort -h" in disk["hint"]
        assert "lsof +L1" in disk["hint"]


def test_inode_exhaustion_is_critical_with_df_i_hint(tk):
    tk.fake_proc()
    tk.fake_df([("/dev/sdb1", "ext4", "41%", "97%", "/var/lib/app")])
    p = tk.run("ops-health")
    assert p.returncode == 2
    assert events(p, "disk")[0]["level"] == "OK"
    inode = events(p, "inode")[0]
    assert (inode["level"], inode["value"], inode["threshold"]) == ("CRIT", 97, 90)
    assert "df -i /var/lib/app" in inode["hint"]


def test_load_above_cores_is_critical_and_lists_top_processes(tk):
    tk.fake_proc(load="9.00 6.00 3.00 5/300 1234", cores=4)  # 9.0 / 4 cores = 225 %
    tk.fake_df([("/dev/sda1", "ext4", "40%", "10%", "/")])
    p = tk.run("ops-health")
    assert p.returncode == 2
    cpu = events(p, "cpu")[0]
    assert (cpu["level"], cpu["value"], cpu["cores"], cpu["threshold"]) == ("CRIT", 225, 4, 200)
    assert cpu["load1"] == 9.0
    assert cpu["top_cpu"]  # real `ps` output: "comm(pid) x%, ..."
    assert "ps -eo pid,ppid,cmd,%cpu --sort=-%cpu" in cpu["hint"]


def test_memory_uses_memavailable(tk):
    tk.fake_proc(mem_total_kb=1_000_000, mem_avail_kb=100_000)  # 90 % used
    tk.fake_df([("/dev/sda1", "ext4", "40%", "10%", "/")])
    p = tk.run("ops-health")
    assert p.returncode == 1
    mem = events(p, "memory")[0]
    assert (mem["level"], mem["value"], mem["threshold"]) == ("WARN", 90, 85)
    assert "ps -eo pid,comm,rss --sort=-rss" in mem["hint"]


def test_worst_status_wins(tk):
    tk.fake_proc(mem_total_kb=1_000_000, mem_avail_kb=10_000)  # 99 % -> CRIT
    tk.fake_df([("/dev/sda1", "ext4", "85%", "10%", "/")])  # WARN
    p = tk.run("ops-health")
    assert p.returncode == 2
    assert events(p, "summary")[0]["level"] == "CRIT"


def test_df_failure_is_unknown(tk):
    tk.fake_proc()
    tk.fake_df([], exit_code=1, stderr="df: /data: No such file or directory")
    p = tk.run("ops-health")
    assert p.returncode == 3
    disk = events(p, "disk")[0]
    assert disk["level"] == "UNKNOWN"
    assert "/data" in disk["msg"]


def test_critical_beats_unknown(tk):
    tk.fake_proc(mem_total_kb=1_000_000, mem_avail_kb=10_000)  # CRIT
    tk.fake_df([], exit_code=1, stderr="df: /data: No such file or directory")  # UNKNOWN
    p = tk.run("ops-health")
    assert p.returncode == 2


def test_thresholds_can_be_set_from_environment(tk):
    tk.healthy()  # disk at 40 %
    p = tk.run("ops-health", env={"OPS_DISK_WARN": "30", "OPS_DISK_CRIT": "50"})
    assert p.returncode == 1
    disk = events(p, "disk")[0]
    assert (disk["level"], disk["warn"], disk["crit"]) == ("WARN", 30, 50)


@pytest.mark.parametrize(
    "env, message",
    [
        ({"OPS_DISK_WARN": "abc"}, "invalid OPS_DISK_WARN='abc'"),
        ({"OPS_DISK_WARN": "08"}, "invalid OPS_DISK_WARN='08'"),
        ({"OPS_DISK_WARN": "95", "OPS_DISK_CRIT": "90"}, "must not be greater than"),
    ],
)
def test_invalid_thresholds_are_unknown(tk, env, message):
    tk.healthy()
    p = tk.run("ops-health", env=env)
    assert p.returncode == 3
    assert message in p.stderr
    assert p.events[-1]["level"] == "UNKNOWN"


def test_events_are_also_appended_to_log_file(tk):
    tk.healthy()
    first = tk.run("ops-health")
    second = tk.run("ops-health")
    logged = tk.logged_events()
    assert logged == first.events + second.events
    for event in logged:
        assert TS.match(event["ts"])
        assert event["host"]
        assert event["tool"] == "ops-health"
        assert event["level"] in {"OK", "WARN", "CRIT", "UNKNOWN"}


def test_odd_mount_names_produce_valid_json(tk):
    tk.fake_proc()
    odd = '/mnt/my "odd" dir\\x'
    tk.fake_df([("/dev/sdc1", "ext4", "95%", "1%", odd)])
    p = tk.run("ops-health")  # tk.run fails if any stdout line is not valid JSON
    assert p.returncode == 2
    assert events(p, "disk")[0]["mount"] == odd


def test_real_host_smoke(tk):
    """No stubs: real /proc, real df on '/', CPU sampled for 1 s."""
    p = tk.run("ops-health", env={"OPS_DISK_MOUNTS": "/", "OPS_CPU_SAMPLE_SECS": "1"})
    assert p.returncode in (0, 1, 2), p.stderr
    checks = {e["check"] for e in p.events}
    assert {"cpu", "memory", "disk", "summary"} <= checks
    cpu = events(p, "cpu")[0]
    assert cpu["cores"] >= 1
    assert "busy_pct" in cpu and "iowait_pct" in cpu and "steal_pct" in cpu


def test_proc_stat_content_is_never_evaluated(tk):
    """Bash arithmetic would run a[$(cmd)]; fields must be validated as numbers first."""
    tk.fake_proc()
    tk.fake_df([("/dev/sda1", "ext4", "40%", "10%", "/")])
    marker = tk.tmp / "pwned"
    (tk.tmp / "proc" / "stat").write_text(f"cpu  a[$(touch {marker})] 0 50 1000 10 0 0 0 0 0\n")
    p = tk.run("ops-health", env={"OPS_CPU_SAMPLE_SECS": "1"})
    assert not marker.exists()
    assert p.returncode == 0, p.stderr
    assert "busy_pct" not in events(p, "cpu")[0]


def test_silent_df_failure_is_unknown_not_ok(tk):
    tk.fake_proc()
    tk.stub("df", "exit 1\n")
    p = tk.run("ops-health")
    assert p.returncode == 3
    assert events(p, "disk")[0]["level"] == "UNKNOWN"
