#!/bin/bash
# Run the pytest suite as the unprivileged 'nobody' user (closer to the GitHub runner, which is not root).
set -u
P=/mnt/e/Devops/selise-devops-preparation/projects/linux-ops-toolkit
E=/mnt/e/Devops/selise-devops-preparation/evidence/test-results/linux-ops-toolkit
W=$(mktemp -d /tmp/lot-nonroot.XXXXXX)
chown nobody:nogroup "$W"
as_nobody() { setpriv --reuid=nobody --regid=nogroup --clear-groups env -i PATH=/usr/local/bin:/usr/bin:/bin HOME="$W" LC_ALL=C.UTF-8 PYTHONDONTWRITEBYTECODE=1 "$@"; }
as_nobody python3 -m venv "$W/venv" && as_nobody "$W/venv/bin/pip" install -q --disable-pip-version-check pytest==9.1.1
{
  echo "# pytest as the unprivileged user 'nobody' (uid $(id -u nobody)) instead of root"
  echo "# date: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "# command: setpriv --reuid=nobody --regid=nogroup --clear-groups ... python -m pytest -q"
  echo
  cd "$P" && as_nobody sh -c 'id; cd "$0" && "$1/venv/bin/python" -m pytest -q -p no:cacheprovider' "$P" "$W"
  echo "exit code: $?"
} >"$E/pytest-nonroot.txt" 2>&1
rm -rf "$W"
tail -n 8 "$E/pytest-nonroot.txt"
