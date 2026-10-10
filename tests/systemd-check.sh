#!/usr/bin/env bash
# Validate systemd/ops-health.{service,timer} without installing them.
#
#   tests/systemd-check.sh              systemd-analyze verify + calendar + offline security score
#   tests/systemd-check.sh --transient  also run ops-health once as a transient unit (systemd-run)
#                                       with the same sandbox settings; needs root and systemd.
#
# verify checks that ExecStart exists, so the units are copied to a temp dir with ExecStart
# pointing at this checkout instead of /opt/ops-toolkit (where Ansible installs it).
set -Eeuo pipefail
root=$(cd -- "$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}")")/.." && pwd)
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT

install -m 0644 "$root/systemd/ops-health.service" "$root/systemd/ops-health.timer" "$work/"
sed -i "s|^ExecStart=/opt/ops-toolkit/bin/ops-health$|ExecStart=$root/bin/ops-health|" "$work/ops-health.service"

echo "== systemd-analyze verify (systemd $(systemctl --version | awk 'NR==1 {print $2}'))"
systemd-analyze verify "$work/ops-health.service" "$work/ops-health.timer"
echo "verify: OK"

echo "== systemd-analyze calendar (timer schedule)"
systemd-analyze calendar --iterations=3 "$(sed -n 's/^OnCalendar=//p' "$work/ops-health.timer")"

echo "== systemd-analyze security --offline (exposure score of the service sandbox)"
systemd-analyze security --offline=true --no-pager "$work/ops-health.service" | tail -n 1

if [[ ${1-} != --transient ]]; then exit 0; fi

echo "== transient run with the unit's [Service] settings"
props=()
section=""
while IFS= read -r line; do
  if [[ $line =~ ^\[(.*)\]$ ]]; then section=${BASH_REMATCH[1]}; continue; fi
  if [[ $section != Service || ! $line =~ ^[A-Za-z] ]]; then continue; fi
  case $line in ExecStart=* | Type=*) continue ;; esac
  props+=(-p "$line")
done <"$root/systemd/ops-health.service"
# Use the example config (the unit's /etc path does not exist on a dev machine) and only check '/'.
props+=(-p "Environment=OPS_CONFIG=$root/config/ops-toolkit.conf.example" -p "Environment=OPS_DISK_MOUNTS=/")

created_log_dir=0
if [[ ! -d /var/log/ops-toolkit ]]; then created_log_dir=1; fi
unit="ops-health-transient-test-$$"
printf '%s\n' "${props[@]}" | grep -v '^-p$' | sed 's/^/  /'
rc=0
systemd-run --unit="$unit" --wait --pipe --collect --quiet -p Type=oneshot "${props[@]}" "$root/bin/ops-health" || rc=$?
echo "transient unit exit status: $rc"
echo "log file written inside the sandbox:"
ls -ld /var/log/ops-toolkit
tail -n 1 /var/log/ops-toolkit/ops-toolkit.jsonl
if systemctl list-units --all --no-legend "$unit.service" | grep -q .; then
  echo "unit still loaded (unexpected)"
else
  echo "transient unit $unit.service is gone (collected)"
fi
if ((created_log_dir)); then
  rm -rf -- /var/log/ops-toolkit
  echo "removed /var/log/ops-toolkit created by this test"
fi
exit "$rc"
