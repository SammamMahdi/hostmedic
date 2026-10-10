# Linux troubleshooting runbook

Practical steps for the problems ops-toolkit detects, plus the reasoning behind them. Commands
assume Ubuntu 24.04; most need `sudo` to see other users' processes. Each section ends with the
toolkit command that automates part of it.

## 0. First five minutes

```bash
uptime                          # load averages vs. number of cores (nproc)
ops-health | jq -c 'select(.level != "OK")'
dmesg -T | tail -n 30           # OOM kills, disk/IO errors, segfaults
journalctl -p err -S -1h --no-pager | tail -n 50
systemctl --failed
```

Write down what you observed and when before changing anything.

## 1. High CPU / high load

**Load average vs. cores.** The load average is the number of tasks that are running or
waiting to run, *plus* tasks in uninterruptible sleep (state `D`, usually waiting for disk or
NFS). A load of 4.0 on a 4-core machine means "fully busy"; on a 16-core machine it is 25 %.
`ops-health` reports exactly this ratio (`value` = load1 / cores x 100).

```bash
nproc; uptime
top                              # press 1 (per-CPU), P (sort by CPU), M (sort by memory), H (threads)
htop                             # same, easier to read (apt install htop); F5 = process tree
ps -eo pid,ppid,cmd,%cpu --sort=-%cpu | head -n 15
```

`ps %cpu` is CPU time divided by process lifetime, so a process that just started spinning can
look small. For the *current* picture use `top -b -n 2 -d 1` (the second sample) or
`pidstat 1 5` (package `sysstat`).

**Which kind of busy?** Read the `%Cpu(s)` line in `top`, or:

```bash
vmstat 1 5        # r = runnable, b = blocked on IO, us/sy = CPU work, wa = iowait, st = steal
iostat -x 1 3     # per disk: %util near 100 and high r_await/w_await = disk is the bottleneck
```

| Symptom | Meaning | Next step |
|---|---|---|
| high `us`/`sy`, `r` > cores | CPU-bound | find the process (above), `top -H -p PID` for the hot thread, profile or scale out |
| high `wa`, many `D` tasks, high load but idle CPU | waiting on disk/network storage | `iostat -x`, `iotop -o`, check for a full or failing disk (`dmesg -T`) |
| high `st` (steal) | the hypervisor gives this VM less CPU than it wants | noisy neighbour or undersized/burstable VM; resize or move |
| one process at 100 % of one core | single-threaded loop | `top -H -p PID`, `strace -p PID -f -c` for a few seconds |

`ops-health` samples `/proc/stat` for one second and logs `busy_pct`, `iowait_pct` and `steal_pct`
with the top CPU processes, so the first log line already tells which row applies.

## 2. Memory pressure and OOM kills

Linux uses free RAM as page cache, so "free" is usually small. What matters is
**MemAvailable** (free + reclaimable cache), which `ops-health` uses.

```bash
free -m                                           # look at the "available" column
ps -eo pid,comm,rss --sort=-rss | head            # RSS in KiB
journalctl -k | grep -i -E "out of memory|oom-kill"
cat /sys/fs/cgroup/system.slice/<unit>.service/memory.events   # oom_kill count for one systemd unit
```

If a service gets OOM-killed repeatedly: confirm with the kernel log, check for a leak (RSS
growing over time), and set an explicit `MemoryMax=` on the unit so one service cannot take
the whole machine down.

## 3. Disk full

```bash
df -h                                        # space per filesystem
df -i                                        # inodes per filesystem
sudo du -xh --max-depth=1 / | sort -h | tail # -x: stay on this filesystem; drill down level by level
journalctl --disk-usage                      # systemd journal
docker system df                             # images/containers/volumes, if Docker is used
```

**Deleted but still open files.** If `df` says the disk is full but `du` cannot find the space,
a process still holds deleted files open (typical after `rm`-ing a big log that an app writes to):

```bash
sudo lsof +L1                    # open files with link count 0 = deleted but not yet freed
# free the space: restart/reload the process, or truncate through its file descriptor
sudo truncate -s 0 /proc/<PID>/fd/<FD>
```

Prevent it with logrotate `copytruncate` or by signalling the app to reopen its log
(`postrotate ... systemctl reload app`).

**Reserved blocks.** ext4 reserves 5 % for root by default (`sudo tune2fs -l /dev/sdX | grep -i
reserved`), so non-root services fail before `df` shows 100 %.

**Inode exhaustion.** "No space left on device" while `df -h` shows free space means the
filesystem ran out of inodes, usually because of millions of tiny files (sessions, cache, mail
queue):

```bash
df -i /var
sudo find /var -xdev -printf '%h\n' | sort | uniq -c | sort -rn | head   # directories with most entries
```

Delete or archive the small files and fix whatever creates them. On ext4 the inode count is
fixed at `mkfs` time; XFS allocates inodes dynamically.

Toolkit: `ops-health` raises disk/inode WARN/CRIT with these hints; `ops-logclean` compresses and
expires old application logs safely (dry-run first).

## 4. Which process is listening on a port?

```bash
sudo ss -ltnp                          # TCP listeners, numeric, with process (root shows all users)
sudo ss -ltnp 'sport = :8080'          # one port
sudo ss -lunp                          # UDP
sudo lsof -i :8080                     # every socket on port 8080 (listening and connected)
sudo lsof -nP -iTCP:8080 -sTCP:LISTEN  # only the listener, no DNS/port-name lookups
systemctl status <PID>                 # which systemd unit owns that PID
```

Read the local address: `127.0.0.1:8080` is reachable only from the host itself,
`0.0.0.0:8080` / `[::]:8080` from every interface. "Connection refused" from a client means the
host answered but nothing listens there; a timeout means packets are dropped (firewall, security
group, routing) or the host is down.

Toolkit: `ops-services` (`OPS_TCP_CHECKS`) and `ops-netdiag HOST PORT` print exactly this
distinction (`result: refused` vs `timeout`).

## 5. How DNS resolution works on Linux

1. The application calls `getaddrinfo()` (glibc).
2. glibc reads the `hosts:` line of **/etc/nsswitch.conf**, typically `files dns`.
3. `files` = **/etc/hosts**. A match there wins and DNS is never asked.
4. `dns` = the servers in **/etc/resolv.conf**. On Ubuntu that file points to
   `nameserver 127.0.0.53`, the local **systemd-resolved stub**. resolved forwards queries to the
   real upstream servers it learned from DHCP/netplan (`resolvectl status` shows them per link)
   and caches answers.
5. The upstream is a **recursive resolver** (ISP, corporate, 1.1.1.1, 8.8.8.8, a cloud VPC
   resolver). On a cache miss it walks root -> TLD (`.com`) -> the domain's authoritative servers.
6. Every answer carries a **TTL**; resolvers and systemd-resolved cache it for that long. After a
   DNS change, clients may see the old address until the TTL expires
   (`sudo resolvectl flush-caches` clears the local cache only).

```bash
getent ahosts example.com       # same path as applications (nsswitch, /etc/hosts, DNS)
dig example.com                 # DNS only (bypasses /etc/hosts), shows TTL and the server used
dig example.com @1.1.1.1        # bypass the local resolver: is it a local or a global problem?
dig +trace example.com          # follow the delegation from the root servers
resolvectl status               # upstream servers behind 127.0.0.53
resolvectl query example.com
```

Interpreting: `getent` works but `dig` fails -> the name comes from /etc/hosts. `dig @1.1.1.1`
works but plain `dig` fails -> the local/upstream resolver is the problem. NXDOMAIN everywhere
-> the record does not exist (typo, wrong zone, not yet created). In containers the resolver is
different: Docker's embedded DNS at 127.0.0.11, Kubernetes CoreDNS with search domains and
`ndots:5`.

Toolkit: `ops-netdiag HOST` shows the nameservers, the nsswitch `hosts:` line, whether
`/etc/hosts` has an entry, the resolved addresses and time, and the next commands on failure.

## 6. How Linux permissions affect services

- **Service user.** A unit runs as `User=` (root if unset). Check with
  `systemctl show -p User,Group <unit>` and `ps -o user,pid,cmd -C <name>`. Test access as that
  user: `sudo -u www-data cat /etc/app/config.yml`.
- **File modes and ownership.** Reading a file needs `r` on the file and `x` on every parent
  directory; writing a log needs `w` on the directory to create files. `namei -l /path/to/file`
  prints owner and mode for every path component, which finds the blocking directory quickly.
  Also check ACLs (`getfacl`) and the service's `UMask=`.
- **systemd sandboxing.** `ProtectSystem=strict` makes the whole filesystem read-only except
  `ReadWritePaths=` / `StateDirectory=` / `LogsDirectory=`; writes elsewhere fail with
  "Read-only file system" even as root. `ProtectHome=` hides or freezes /home, `PrivateTmp=`
  gives the service its own /tmp (files you put in /tmp are not visible to it).
  `systemd-analyze security <unit>` scores a unit's exposure.
- **Ports below 1024** are privileged. A non-root service needs `CAP_NET_BIND_SERVICE`
  (`AmbientCapabilities=CAP_NET_BIND_SERVICE` in the unit, or `setcap cap_net_bind_service=+ep
  /path/to/binary`), or listen on a high port behind a reverse proxy.
  `sysctl net.ipv4.ip_unprivileged_port_start` shows the boundary.
- **Mandatory access control.** SELinux (RHEL family: `getenforce`, `ausearch -m avc -ts recent`,
  `restorecon -Rv /path`) and AppArmor (Ubuntu: `aa-status`,
  `journalctl -k | grep 'apparmor="DENIED"'`) can deny access that normal Unix permissions allow.

This toolkit's own unit runs as root (to read the 0640 root-owned config) but with an empty
`CapabilityBoundingSet=`, `NoNewPrivileges=yes`, `ProtectSystem=strict` and only
`/var/log/ops-toolkit` writable.

## 7. Automating a recurring health check

| | systemd timer + service | cron |
|---|---|---|
| Logs | stdout/stderr in the journal (`journalctl -u ops-health`) | mailed or lost unless redirected |
| Missed runs (machine off) | `Persistent=true` runs once at boot | skipped (anacron for daily jobs) |
| Overlap | a oneshot service cannot start twice at once | needs `flock` |
| Status / exit code | `systemctl status`, `systemctl --failed`, `OnFailure=` hook | only what the script logs |
| Sandboxing, resource limits | full unit options | none |
| Simplicity / portability | systemd hosts only | everywhere |

Design used here:

- `ops-health.timer` (`OnCalendar=*:0/5`, `Persistent=true`, `RandomizedDelaySec=30s`) starts
  `ops-health.service` (`Type=oneshot`).
- The script uses Nagios exit codes (0 OK, 1 WARNING, 2 CRITICAL, 3 UNKNOWN), so the same
  command can be called by Nagios/Icinga/Zabbix agents. In the unit, `SuccessExitStatus=1`
  keeps a WARNING from failing the unit; CRITICAL/UNKNOWN fail it, which shows in
  `systemctl --failed` and can trigger `OnFailure=notify@%n.service` (a small unit that posts to
  a chat webhook or pager).
- Events are JSON lines, so a log shipper (Promtail, Fluent Bit, Vector) can index them and
  alert on `level="CRIT"`. `ops-report --since 1h` summarizes them on the host.
- Also alert when the check *stops* running (a dead man's switch): the newest event in the log
  should never be older than ~10 minutes. `systemctl list-timers ops-health.timer` shows the last
  and next run.

## 8. Troubleshooting the toolkit itself

| Symptom | Cause / fix |
|---|---|
| exit 3, `config file not readable` | `OPS_CONFIG` points to a missing file, or a non-root user cannot read the 0640 config |
| exit 3, `invalid OPS_..._WARN` | value is not a plain integer, or WARN > CRIT |
| `cannot write to log directory` | non-root user and `/var/log/ops-toolkit` is root-only; set `OPS_LOG_DIR` |
| a disk you do not care about is CRITICAL | set `OPS_DISK_MOUNTS="/ /var"` or add its type to `OPS_DISK_EXCLUDE_TYPES` |
| timer does not run | `systemctl list-timers ops-health.timer`, `journalctl -u ops-health.service -n 50` |
| unit fails with `226/NAMESPACE` | a path in `ReadWritePaths=` does not exist; `LogsDirectory=` normally creates it |
