#!/bin/bash
# Collect test/lint evidence for linux-ops-toolkit (run in WSL as root).
set -u
P=/mnt/e/Devops/selise-devops-preparation/projects/linux-ops-toolkit
E=/mnt/e/Devops/selise-devops-preparation/evidence/test-results/linux-ops-toolkit
V=/root/selise-venvs/linux-ops-toolkit/bin
mkdir -p "$E"
cd "$P" || exit 1
export PYTHONDONTWRITEBYTECODE=1

header() {
  echo "# $1"
  echo "# date: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "# host: $(uname -srm); $(. /etc/os-release; echo "$PRETTY_NAME"); bash ${BASH_VERSION}"
  echo "# cwd: $P"
  echo "# command: $2"
  echo
}

{
  header "pytest" "python -m pytest -v"
  "$V/python" --version
  "$V/python" -m pytest --version
  "$V/python" -m pytest -v 2>&1
  echo "exit code: $?"
} >"$E/pytest.txt" 2>&1

{
  header "shellcheck" "shellcheck -x bin/ops-* lib/common.sh tests/systemd-check.sh ansible/test-in-docker.sh"
  shellcheck --version
  shellcheck -x bin/ops-* lib/common.sh tests/systemd-check.sh ansible/test-in-docker.sh
  echo "exit code: $? (no output above = zero findings)"
  echo
  echo "# inline disables (each has a justification comment):"
  grep -rn "shellcheck disable" bin lib tests ansible
} >"$E/shellcheck.txt" 2>&1

{
  header "ruff" "ruff check . && ruff format --check ."
  "$V/ruff" --version
  "$V/ruff" check --no-cache .
  echo "ruff check exit code: $?"
  "$V/ruff" format --no-cache --check .
  echo "ruff format --check exit code: $?"
} >"$E/ruff.txt" 2>&1

{
  header "actionlint" "actionlint -verbose .github/workflows/ci.yml"
  actionlint -version
  actionlint -verbose .github/workflows/ci.yml
  echo "exit code: $?"
} >"$E/actionlint.txt" 2>&1

{
  header "systemd unit validation" "tests/systemd-check.sh --transient"
  tests/systemd-check.sh --transient
  echo "exit code: $?"
} >"$E/systemd-check.txt" 2>&1

grep -E "passed|failed|error" "$E/pytest.txt" | tail -n 3
grep "exit code" "$E"/*.txt
