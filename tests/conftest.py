"""Shared fixtures: run the real bin/ops-* scripts in an isolated environment.

Every test gets its own temp directory with
  - a stub bin directory prepended to PATH (fake df, nproc, systemctl, ip, getent ...)
  - an empty config file (OPS_CONFIG) so /etc/ops-toolkit is never read
  - its own OPS_LOG_DIR and HOME
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import textwrap
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

import pytest

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "bin"


class Toolkit:
    def __init__(self, tmp_path: Path):
        self.tmp = tmp_path
        self.stubs = tmp_path / "stubs"
        self.stubs.mkdir()
        self.home = tmp_path / "home"
        self.home.mkdir()
        self.log_dir = tmp_path / "log"
        self.config = tmp_path / "ops-toolkit.conf"
        self.config.write_text("")
        self.env = {
            "PATH": f"{self.stubs}:{os.environ['PATH']}",
            "HOME": str(self.home),
            "LC_ALL": "C",
            "OPS_CONFIG": str(self.config),
            "OPS_LOG_DIR": str(self.log_dir),
            "OPS_CPU_SAMPLE_SECS": "0",
        }

    @property
    def log_file(self) -> Path:
        return self.log_dir / "ops-toolkit.jsonl"

    def stub(self, name: str, body: str) -> Path:
        """Create an executable fake command that shadows the real one on PATH."""
        path = self.stubs / name
        path.write_text("#!/bin/sh\n" + textwrap.dedent(body))
        path.chmod(0o755)
        return path

    def fake_df(
        self, rows: list[tuple[str, str, str, str, str]], exit_code: int = 0, stderr: str = ""
    ) -> None:
        lines = ["Filesystem     Type  Use% IUse% Mounted on"]
        lines += [" ".join(row) for row in rows]
        body = "cat <<'EOF'\n" + "\n".join(lines) + "\nEOF\n"
        if stderr:
            body += f"echo '{stderr}' >&2\n"
        body += f"exit {exit_code}\n"
        self.stub("df", body)

    def fake_proc(
        self,
        load: str = "0.50 0.40 0.30 1/200 999",
        cores: int = 4,
        mem_total_kb: int = 8_000_000,
        mem_avail_kb: int = 6_000_000,
    ) -> None:
        proc = self.tmp / "proc"
        proc.mkdir(exist_ok=True)
        (proc / "loadavg").write_text(load + "\n")
        (proc / "meminfo").write_text(
            f"MemTotal:       {mem_total_kb} kB\nMemFree:          100000 kB\n"
            f"MemAvailable:   {mem_avail_kb} kB\nBuffers:          1000 kB\n"
        )
        (proc / "stat").write_text("cpu  100 0 50 1000 10 0 0 0 0 0\n")
        self.env["OPS_PROC_ROOT"] = str(proc)
        self.stub("nproc", f"echo {cores}\n")

    def healthy(self) -> None:
        """A host where every ops-health check is comfortably OK."""
        self.fake_proc()
        self.fake_df([("/dev/sda1", "ext4", "40%", "10%", "/")])

    def write_config(self, text: str) -> None:
        self.config.write_text(textwrap.dedent(text))

    def run(self, cmd: str, *args: str, env: dict | None = None, timeout: int = 60):
        full_env = dict(self.env)
        full_env.update(env or {})
        proc = subprocess.run(
            [str(BIN / cmd), *args],
            env=full_env,
            cwd=self.tmp,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        # The ops-* check/action tools must print only JSON lines on stdout; this fails the test otherwise.
        json_output = cmd != "ops-report" and "--help" not in args
        lines = proc.stdout.splitlines() if json_output else []
        proc.events = [json.loads(line) for line in lines if line.strip()]
        return proc

    def logged_events(self) -> list[dict]:
        return [json.loads(line) for line in self.log_file.read_text().splitlines()]


def events(proc, check: str) -> list[dict]:
    return [e for e in proc.events if e["check"] == check]


@pytest.fixture
def tk(tmp_path):
    return Toolkit(tmp_path)


@pytest.fixture
def listening_port():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(16)
    yield sock.getsockname()[1]
    sock.close()


@pytest.fixture
def closed_port():
    """A port that was free a moment ago: connecting to it is refused."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class _Handler(BaseHTTPRequestHandler):
    routes: ClassVar[dict] = {"/": 200, "/ok": 200, "/healthz": 200, "/down": 503}
    default = 404

    def do_GET(self):
        code = self.routes.get(self.path, self.default)
        body = f"status {code}\n".encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class _Always503(_Handler):
    routes: ClassVar[dict] = {}
    default = 503


def _serve(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


@pytest.fixture
def http_server():
    """Local HTTP server: / and /ok -> 200, /down -> 503, anything else -> 404."""
    server = _serve(_Handler)
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


@pytest.fixture
def broken_http_server():
    """Local HTTP server that answers 503 to every request."""
    server = _serve(_Always503)
    yield server.server_address[1]
    server.shutdown()
    server.server_close()
