#!/bin/bash
# Real demo runs of linux-ops-toolkit on the WSL host (Ubuntu 24.04, root). Writes raw output to
# evidence/execution-logs/linux-ops-toolkit/ and captured JSON lines to samples/.
set -u
P=/mnt/e/Devops/selise-devops-preparation/projects/linux-ops-toolkit
E=/mnt/e/Devops/selise-devops-preparation/evidence/execution-logs/linux-ops-toolkit
mkdir -p "$E" "$P/samples"
cd "$P" || exit 1
W=$(mktemp -d /tmp/ops-toolkit-demo.XXXXXX)
export OPS_CONFIG=/dev/null          # built-in defaults; nothing read from /etc
export OPS_LOG_DIR="$W/log"           # keep the demo log out of /var/log
export PATH="$P/bin:$PATH"

header() {
  echo "# $1"
  echo "# date: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "# host: $(uname -srm); $(. /etc/os-release; echo "$PRETTY_NAME"); bash ${BASH_VERSION}; $(curl --version | head -n1 | cut -d' ' -f1-2)"
  echo "# env: OPS_CONFIG=$OPS_CONFIG OPS_LOG_DIR=$OPS_LOG_DIR"
  echo
}
run() { printf '$'; printf ' %q' "$@"; echo; "$@"; echo "[exit code: $?]"; echo; }

# ---- ops-health on the real WSL host ------------------------------------------------------
{
  header "ops-health on the WSL host (default thresholds, all filesystems)"
  run ops-health
  echo "# same, restricted to the Linux root filesystem"
  run env OPS_DISK_MOUNTS=/ ops-health
  echo "# only the problems, via jq:"
  echo "$ ops-health | jq -c 'select(.level != \"OK\") | {level, check, msg}'"
  ops-health | jq -c 'select(.level != "OK") | {level, check, msg}'
} >"$E/ops-health-wsl-host.txt" 2>&1

# samples (stdout only = the JSON lines)
OPS_DISK_MOUNTS=/ OPS_DISK_WARN=85 OPS_DISK_CRIT=95 ops-health >"$P/samples/healthy-run.jsonl" 2>"$W/err1"; echo "healthy sample exit $?"
ops-health >"$P/samples/threshold-violation.jsonl" 2>"$W/err2"; echo "violation sample exit $?"

# ---- ops-services: real systemd units, a closed port and an app answering 503 ---------------
cat >"$W/app.py" <<'PY'
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        code = 200 if self.path == "/" else 503   # /healthz reports "unhealthy"
        self.send_response(code); self.send_header("Content-Length", "0"); self.end_headers()
    def log_message(self, *a): pass
ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
PY
python3 "$W/app.py" 18080 & APP=$!
sleep 1
{
  header "ops-services against real local services"
  echo "# cron.service exists on this host; nginx.service is not installed (systemd reports inactive)."
  echo "# 127.0.0.1:18080 is a tiny Python app: / -> 200, /healthz -> 503. Nothing listens on 127.0.0.1:18081."
  run env OPS_SYSTEMD_UNITS="cron.service nginx.service" \
      OPS_TCP_CHECKS="127.0.0.1:18080 127.0.0.1:18081" \
      OPS_HTTP_CHECKS="http://127.0.0.1:18080/|200 http://127.0.0.1:18080/healthz|200" \
      ops-services
} >"$E/ops-services-demo.txt" 2>&1
OPS_SYSTEMD_UNITS="cron.service nginx.service" OPS_TCP_CHECKS="127.0.0.1:18080 127.0.0.1:18081" \
  OPS_HTTP_CHECKS="http://127.0.0.1:18080/|200 http://127.0.0.1:18080/healthz|200" \
  ops-services >"$P/samples/service-down.jsonl" 2>"$W/err3"; echo "service-down sample exit $?"
kill "$APP"; wait "$APP" 2>/dev/null

# ---- ops-netdiag -------------------------------------------------------------------------------
{
  header "ops-netdiag against github.com (DNS, route, TCP 443, HTTPS status)"
  run ops-netdiag --http github.com 443
} >"$E/ops-netdiag-github.txt" 2>&1
{
  header "ops-netdiag against a non-existent domain (.invalid is reserved by RFC 2606 and never resolves)"
  run ops-netdiag ops-toolkit-demo.invalid 443
} >"$E/ops-netdiag-nonexistent.txt" 2>&1

# ---- backup / restore -------------------------------------------------------------------------
mkdir -p "$W/srv/app/conf" "$W/srv/app/data"
printf 'listen = 8080\nworkers = 4\n' >"$W/srv/app/conf/app.ini"
head -c 200000 /dev/urandom >"$W/srv/app/data/blob.bin"
echo "hello" >"$W/srv/app/data/file with spaces.txt"
{
  header "ops-backup / ops-restore demo"
  export OPS_BACKUP_SOURCES="$W/srv/app" OPS_BACKUP_DEST="$W/backups" OPS_BACKUP_KEEP=2
  echo "# sources=$OPS_BACKUP_SOURCES dest=$OPS_BACKUP_DEST keep=$OPS_BACKUP_KEEP"
  run ops-backup
  sleep 1.1; run ops-backup
  sleep 1.1; run ops-backup
  echo "# retention keep=2 -> only the two newest archives remain; modes show umask 077:"
  run ls -l "$W/backups"
  A=$(ls "$W/backups"/ops-backup-*.tar.gz | tail -n 1)
  run cat "$A.sha256"
  run ops-restore "$A" "$W/restore"
  echo "# byte-for-byte comparison of source and restored tree:"
  run diff -r "$W/srv/app" "$W/restore$W/srv/app"
  echo "# restore again into the now non-empty target without --force:"
  run ops-restore "$A" "$W/restore"
  echo "# and with --force (existing files with the same names are replaced):"
  run ops-restore --force "$A" "$W/restore"
  echo "# tamper with one byte of a copy of the archive and its manifest name:"
  cp "$A" "$W/tampered.tar.gz"
  sed "s/$(basename "$A")/tampered.tar.gz/" "$A.sha256" >"$W/tampered.tar.gz.sha256"
  printf '\xff' | dd of="$W/tampered.tar.gz" bs=1 seek=1000 conv=notrunc status=none
  run ops-restore "$W/tampered.tar.gz" "$W/restore2"
  run ls -d "$W/restore2"
  echo "# dangerous inputs:"
  run env OPS_BACKUP_SOURCES=/ ops-backup
  run env OPS_BACKUP_DEST="$W/srv/app/backups" ops-backup
  run ops-restore "$A" /
} >"$E/backup-restore-demo.txt" 2>&1

# ---- logclean ---------------------------------------------------------------------------------
L="$W/applogs"
mkdir -p "$L/nested" "$W/outside"
for f in current.log old.log nested/worker.log; do seq 1 20000 >"$L/$f"; done
touch -d "10 days ago" "$L/old.log" "$L/nested/worker.log"
seq 1 5000 | gzip >"$L/ancient.log.gz"; touch -d "45 days ago" "$L/ancient.log.gz"
seq 1 5000 >"$W/outside/victim.log"; touch -d "45 days ago" "$W/outside/victim.log"
ln -s "$W/outside/victim.log" "$L/link-to-outside.log"; touch -h -d "45 days ago" "$L/link-to-outside.log"
{
  header "ops-logclean demo (dry-run, then --apply, then refusal of dangerous directories)"
  export OPS_LOGCLEAN_DIR="$L"
  run find "$L" "$W/outside" -printf '%M %TY-%Tm-%Td %p -> %l\n'
  run ops-logclean
  run ops-logclean --apply
  run find "$L" "$W/outside" -printf '%M %TY-%Tm-%Td %p -> %l\n'
  run env OPS_LOGCLEAN_DIR=/ ops-logclean --apply
  run env OPS_LOGCLEAN_DIR=/etc ops-logclean --apply
} >"$E/logclean-demo.txt" 2>&1

# ---- report over everything above -------------------------------------------------------------
{
  header "ops-report over the JSON log written by all demo runs above"
  run ops-report "$OPS_LOG_DIR/ops-toolkit.jsonl"
  run ops-report --since 1h --limit 3 "$OPS_LOG_DIR/ops-toolkit.jsonl"
} >"$E/ops-report-demo.txt" 2>&1
cp "$OPS_LOG_DIR/ops-toolkit.jsonl" "$E/demo-ops-toolkit.jsonl"

rm -rf "$W"
ls -l "$E" "$P/samples"
