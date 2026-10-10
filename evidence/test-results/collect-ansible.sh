#!/bin/bash
P=/mnt/e/Devops/selise-devops-preparation/projects/linux-ops-toolkit
E=/mnt/e/Devops/selise-devops-preparation/evidence/test-results/linux-ops-toolkit
cd "$P" || exit 1
{
  echo "# ansible playbook test in a throwaway ubuntu:24.04 container"
  echo "# date: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "# host: $(uname -srm); docker $(docker version --format '{{.Server.Version}}')"
  echo "# command: ansible/test-in-docker.sh   (ANSIBLE_CORE_VERSION=2.21.4, ANSIBLE_LINT_VERSION=26.9.0)"
  echo
  ansible/test-in-docker.sh
  echo "exit code: $?"
  echo
  echo "# containers left behind (expect none):"
  docker ps -a --filter name=ops-toolkit-ansible-test --format '{{.Names}}'
} >"$E/ansible-docker.txt" 2>&1
tail -n 25 "$E/ansible-docker.txt"
