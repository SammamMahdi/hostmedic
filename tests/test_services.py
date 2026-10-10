"""ops-services: systemd (fake systemctl), TCP (real sockets), HTTP (local server returning 200/503)."""

import pytest
from conftest import events

FAKE_SYSTEMCTL = """\
# fake: systemctl is-active UNIT
case "$2" in
  web.service) echo active; exit 0 ;;
  db.service) echo failed; exit 3 ;;
  worker.service) echo inactive; exit 3 ;;
  boot.service) echo activating; exit 3 ;;
  *) echo unknown; exit 4 ;;
esac
"""


@pytest.fixture
def systemctl(tk):
    tk.stub("systemctl", FAKE_SYSTEMCTL)


def test_active_unit_is_ok(tk, systemctl):
    p = tk.run("ops-services", env={"OPS_SYSTEMD_UNITS": "web.service"})
    assert p.returncode == 0, p.stderr
    assert events(p, "systemd")[0]["state"] == "active"


def test_failed_and_inactive_units_are_critical(tk, systemctl):
    p = tk.run("ops-services", env={"OPS_SYSTEMD_UNITS": "web.service db.service worker.service"})
    assert p.returncode == 2
    by_unit = {e["unit"]: e for e in events(p, "systemd")}
    assert by_unit["web.service"]["level"] == "OK"
    assert (by_unit["db.service"]["level"], by_unit["db.service"]["state"]) == ("CRIT", "failed")
    assert (by_unit["worker.service"]["level"], by_unit["worker.service"]["state"]) == ("CRIT", "inactive")
    assert "journalctl -u db.service" in by_unit["db.service"]["hint"]
    assert "systemctl status db.service" in by_unit["db.service"]["hint"]


def test_activating_unit_is_warning(tk, systemctl):
    p = tk.run("ops-services", env={"OPS_SYSTEMD_UNITS": "web.service boot.service"})
    assert p.returncode == 1
    assert events(p, "systemd")[1]["level"] == "WARN"


def test_unexpected_systemctl_answer_is_unknown(tk, systemctl):
    p = tk.run("ops-services", env={"OPS_SYSTEMD_UNITS": "mystery.service"})
    assert p.returncode == 3
    assert events(p, "systemd")[0]["level"] == "UNKNOWN"


def test_tcp_open_port_ok_and_closed_port_critical(tk, listening_port, closed_port):
    p = tk.run("ops-services", env={"OPS_TCP_CHECKS": f"127.0.0.1:{listening_port} 127.0.0.1:{closed_port}"})
    assert p.returncode == 2
    ok, bad = events(p, "tcp")
    assert (ok["level"], ok["result"]) == ("OK", "open")
    assert (bad["level"], bad["result"]) == ("CRIT", "refused")
    assert bad["target"] == f"127.0.0.1:{closed_port}"
    assert "ss -ltnp" in bad["hint"]


def test_tcp_timeout_is_reported(tk):
    # 192.0.2.0/24 is TEST-NET-1 (RFC 5737): never routed, so the connect hangs or fails fast.
    p = tk.run("ops-services", env={"OPS_TCP_CHECKS": "192.0.2.1:9", "OPS_CONNECT_TIMEOUT": "1"}, timeout=30)
    assert p.returncode == 2
    tcp = events(p, "tcp")[0]
    assert tcp["result"] in {"timeout", "unreachable"}
    assert tcp["hint"]


def test_http_200_ok_and_503_critical(tk, http_server):
    base = f"http://127.0.0.1:{http_server}"
    p = tk.run("ops-services", env={"OPS_HTTP_CHECKS": f"{base}/ok|200 {base}/down|200"})
    assert p.returncode == 2
    ok, down = events(p, "http")
    assert (ok["level"], ok["value"], ok["expected"]) == ("OK", 200, 200)
    assert (down["level"], down["value"], down["expected"]) == ("CRIT", 503, 200)
    assert "application failed" in down["hint"]
    assert f"curl -sv {base}/down" in down["hint"]


def test_http_expected_status_defaults_to_200_and_can_be_changed(tk, http_server):
    base = f"http://127.0.0.1:{http_server}"
    p = tk.run("ops-services", env={"OPS_HTTP_CHECKS": f"{base}/healthz {base}/down|503"})
    assert p.returncode == 0, p.stdout
    assert [e["expected"] for e in events(p, "http")] == [200, 503]


def test_http_connection_refused_is_critical(tk, closed_port):
    p = tk.run("ops-services", env={"OPS_HTTP_CHECKS": f"http://127.0.0.1:{closed_port}/"})
    assert p.returncode == 2
    http = events(p, "http")[0]
    assert http["curl_exit"] == 7
    assert "ss -ltnp" in http["hint"]


@pytest.mark.parametrize(
    "env",
    [
        {"OPS_TCP_CHECKS": "no-port-here"},
        {"OPS_TCP_CHECKS": "host:0"},
        {"OPS_TCP_CHECKS": "-oProxyCommand=x:22"},
        {"OPS_HTTP_CHECKS": "file:///etc/passwd"},
        {"OPS_HTTP_CHECKS": "http://127.0.0.1/|abc"},
        {"OPS_SYSTEMD_UNITS": "--global"},
    ],
)
def test_invalid_entries_are_unknown_and_not_executed(tk, systemctl, env):
    p = tk.run("ops-services", env=env)
    assert p.returncode == 3
    assert p.events[0]["level"] == "UNKNOWN"
    assert "invalid" in p.events[0]["msg"]


def test_no_checks_configured_is_unknown(tk):
    p = tk.run("ops-services")
    assert p.returncode == 3
    assert "no service checks configured" in p.stderr


def test_mixed_checks_worst_wins(tk, systemctl, http_server, listening_port):
    p = tk.run(
        "ops-services",
        env={
            "OPS_SYSTEMD_UNITS": "web.service boot.service",  # OK + WARN
            "OPS_TCP_CHECKS": f"127.0.0.1:{listening_port}",  # OK
            "OPS_HTTP_CHECKS": f"http://127.0.0.1:{http_server}/ok",  # OK
        },
    )
    assert p.returncode == 1
    summary = events(p, "summary")[0]
    assert (summary["warn_count"], summary["ok_count"]) == (1, 3)


def test_url_globbing_is_disabled(tk, http_server):
    """Without curl -g, {ok,down} would make two requests and concatenate the status codes."""
    p = tk.run("ops-services", env={"OPS_HTTP_CHECKS": f"http://127.0.0.1:{http_server}/{{ok,down}}|200"})
    http = events(p, "http")[0]
    assert http["value"] == 404  # one request for the literal path
    assert p.returncode == 2


@pytest.mark.parametrize("key", ["OPS_CONNECT_TIMEOUT", "OPS_HTTP_TIMEOUT"])
def test_zero_timeout_is_rejected(tk, key):
    p = tk.run("ops-services", env={"OPS_TCP_CHECKS": "127.0.0.1:22", key: "0"})
    assert p.returncode == 3
    assert f"{key} must be at least 1" in p.stderr
