"""ops-netdiag: DNS / route / TCP / HTTP steps with fake `ip` and `getent` where needed."""

import pytest
from conftest import events

IP_OK = """\
case "$*" in
  *"show default"*) echo "default via 10.0.0.1 dev eth0 proto dhcp src 10.0.0.5 metric 100" ;;
  *"route get"*) echo "$3 via 10.0.0.1 dev eth0 src 10.0.0.5 uid 0"; echo "    cache" ;;
esac
"""

IP_NO_ROUTE = """\
case "$*" in
  *"show default"*) ;;
  *"route get"*) echo "RTNETLINK answers: Network is unreachable" >&2; exit 2 ;;
esac
"""


def test_reachable_local_port_is_ok(tk, listening_port):
    tk.stub("ip", IP_OK)
    p = tk.run("ops-netdiag", "localhost", str(listening_port))
    assert p.returncode == 0, p.stdout + p.stderr
    assert [e["check"] for e in p.events] == ["resolver", "dns", "route", "tcp", "summary"]
    assert "127.0.0.1" in events(p, "dns")[0]["addresses"]
    assert events(p, "route")[0]["default_route"] == "default via 10.0.0.1 dev eth0"


def test_dns_failure_gives_resolver_hints_and_skips_tcp(tk):
    tk.stub("ip", IP_OK)
    tk.stub("getent", "exit 2\n")  # getent: key not found (NXDOMAIN)
    p = tk.run("ops-netdiag", "no-such-host.invalid", "443")
    assert p.returncode == 2
    dns = events(p, "dns")[0]
    assert dns["level"] == "CRIT"
    assert "NXDOMAIN" in dns["msg"]
    for needle in ("/etc/resolv.conf", "resolvectl", "dig no-such-host.invalid @1.1.1.1", "nsswitch.conf"):
        assert needle in dns["hint"]
    assert events(p, "tcp")[0]["level"] == "UNKNOWN"


def test_dns_timeout_is_reported(tk):
    tk.stub("ip", IP_OK)
    tk.stub("getent", "sleep 10\n")
    p = tk.run("ops-netdiag", "slow-dns.example", env={"OPS_CONNECT_TIMEOUT": "1"})
    assert p.returncode == 2
    assert "timed out" in events(p, "dns")[0]["msg"]


def test_closed_port_is_critical_with_listener_hint(tk, closed_port):
    tk.stub("ip", IP_OK)
    p = tk.run("ops-netdiag", "127.0.0.1", str(closed_port))
    assert p.returncode == 2
    assert events(p, "dns")[0]["msg"] == "127.0.0.1 is an IP address; DNS not needed"
    tcp = events(p, "tcp")[0]
    assert (tcp["level"], tcp["result"]) == ("CRIT", "refused")
    assert f'ss -ltnp "sport = :{closed_port}"' in tcp["hint"]


def test_missing_route_is_critical(tk):
    tk.stub("ip", IP_NO_ROUTE)
    p = tk.run("ops-netdiag", "127.0.0.1")
    assert p.returncode == 2
    route = events(p, "route")[0]
    assert route["level"] == "CRIT"
    assert route["default_route"] == "none"
    assert "ip route" in route["hint"]


def test_http_status_reported(tk, http_server):
    tk.stub("ip", IP_OK)
    p = tk.run("ops-netdiag", "--http", "127.0.0.1", str(http_server))
    assert p.returncode == 0, p.stdout
    http = events(p, "http")[0]
    assert (http["level"], http["value"]) == ("OK", 200)
    assert http["target"] == f"http://127.0.0.1:{http_server}/"


def test_http_5xx_is_warning_network_ok(tk, broken_http_server):
    tk.stub("ip", IP_OK)
    p = tk.run("ops-netdiag", "--http", "127.0.0.1", str(broken_http_server))
    assert p.returncode == 1
    http = events(p, "http")[0]
    assert (http["level"], http["value"]) == ("WARN", 503)
    assert "network path works" in http["msg"]


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["a", "1", "extra"],
        ["bad host!"],
        ["example.com", "0"],
        ["example.com", "70000"],
        ["example.com", "08"],
        ["-oProxyCommand=x"],
        ["--bogus", "example.com"],
    ],
)
def test_usage_errors_exit_64(tk, args):
    p = tk.run("ops-netdiag", *args)
    assert p.returncode == 64
    assert p.stdout == ""
