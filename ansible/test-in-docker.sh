#!/usr/bin/env bash
# Test the playbook in a throwaway ubuntu:24.04 container (no systemd inside, so the
# systemd tasks are skipped with -e ops_toolkit_manage_systemd=false):
#   1. --syntax-check   2. first run (changes expected)   3. second run (must be changed=0)
#   4. --check --diff (must be changed=0)   5. smoke test of the installed toolkit
#   6. ansible-lint (set LINT=0 to skip)
# The container is removed on exit.
set -Eeuo pipefail
here=$(cd -- "$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}")")" && pwd)
root=$(cd -- "$here/.." && pwd)
image=${IMAGE:-ubuntu:24.04}
ansible_core=${ANSIBLE_CORE_VERSION:-2.21.4}
ansible_lint=${ANSIBLE_LINT_VERSION:-26.9.0}
name="ops-toolkit-ansible-test-$$"

cleanup() { docker rm -f "$name" >/dev/null 2>&1 || true; }
trap cleanup EXIT

in_ctr() { docker exec -e ANSIBLE_NOCOLOR=1 -e ANSIBLE_FORCE_COLOR=0 -w /src "$name" "$@"; }
play() { in_ctr /opt/venv/bin/ansible-playbook -i ansible/inventory.ini ansible/site.yml -e ops_toolkit_manage_systemd=false "$@"; }
changed_count() { sed -n 's/^localhost .*changed=\([0-9]*\).*/\1/p' <<<"$1"; }

echo "== start $image (repo mounted read-only at /src)"
docker run -d --name "$name" -v "$root:/src:ro" "$image" sleep infinity >/dev/null
in_ctr bash -c 'apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3 python3-venv python3-apt sudo >/dev/null'
in_ctr bash -c "python3 -m venv /opt/venv && /opt/venv/bin/pip install -q --disable-pip-version-check ansible-core==$ansible_core"
in_ctr /opt/venv/bin/ansible-playbook --version | sed -n 1p

echo "== 1. syntax check"
play --syntax-check

echo "== 2. first run"
out=$(play) || { echo "$out"; exit 1; }
echo "$out"
first=$(changed_count "$out")

echo "== 3. second run (idempotency: expect changed=0)"
out=$(play) || { echo "$out"; exit 1; }
echo "$out"
second=$(changed_count "$out")

echo "== 4. --check --diff (expect changed=0)"
out=$(play --check --diff) || { echo "$out"; exit 1; }
echo "$out"
check=$(changed_count "$out")

echo "== 5. smoke test of the installed toolkit"
in_ctr stat -c '%A %U:%G %n' /etc/ops-toolkit /etc/ops-toolkit/ops-toolkit.conf /opt/ops-toolkit/bin/ops-health /usr/local/bin/ops-health
# The container root is an overlay filesystem, which is excluded by default; include it here.
rc=0
in_ctr env OPS_DISK_EXCLUDE_TYPES=tmpfs /usr/local/bin/ops-health || rc=$?
echo "ops-health exit code in container: $rc"
in_ctr /usr/local/bin/ops-report --since 1h || true

echo "== 6. ansible-lint"
if [[ ${LINT:-1} == 1 ]]; then
  in_ctr bash -c "/opt/venv/bin/pip install -q --disable-pip-version-check ansible-lint==$ansible_lint && cp -r /src /tmp/lint"
  docker exec -w /tmp/lint "$name" /opt/venv/bin/ansible-lint --version
  docker exec -w /tmp/lint -e ANSIBLE_NOCOLOR=1 -e NO_COLOR=1 "$name" /opt/venv/bin/ansible-lint --offline ansible/site.yml
else
  echo "skipped (LINT=0)"
fi

echo "== result: first run changed=$first, second run changed=$second, check mode changed=$check"
if [[ -z $first || $first -eq 0 || $second != 0 || $check != 0 ]]; then
  echo "FAIL: playbook is not idempotent"
  exit 1
fi
echo "PASS: idempotent (second run and check mode report changed=0)"
