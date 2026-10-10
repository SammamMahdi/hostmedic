# shellcheck shell=bash
# lib/common.sh - helpers shared by every bin/ops-* command.
#
# Sourced after the caller has enabled `set -Eeuo pipefail`. Provides:
#   - safe KEY=VALUE config parsing (never `source`d) with environment overrides
#   - JSON-lines logging to stdout and to $OPS_LOG_DIR/ops-toolkit.jsonl
#   - Nagios-style status tracking (OK/WARN/CRIT/UNKNOWN -> exit 0/1/2/3)
#   - small probes (TCP connect, HTTP status) shared by ops-services and ops-netdiag

# Predictable, English output from df/ps/sort/strerror regardless of the user's locale.
export LC_ALL=C
# Environment variables that silently add options to tar/gzip must not change what we do
# (TAR_OPTIONS=--to-command=... would even run commands). curl's ~/.curlrc is ignored via -q.
unset TAR_OPTIONS GZIP

OPS_PROG=${0##*/}
OPS_KIND=check          # "check" (Nagios exit codes) or "action" (0 ok / 1 failed)
OPS_LOG_READY=0
OPS_LOG_FILE=
OPS_HOST=
OPS_DONE=0              # set right before every intended exit (see ops_on_exit)
OPS_CLEANUP_FILES=()    # removed by the EXIT trap (e.g. partial archives)

declare -A OPS_EXIT_CODE=([OK]=0 [WARN]=1 [CRIT]=2 [UNKNOWN]=3)
# Which status "wins" when several checks run: a known CRITICAL is more urgent than
# an UNKNOWN (a check that could not run), which still beats OK.
declare -A OPS_SEVERITY=([OK]=0 [INFO]=0 [UNKNOWN]=1 [WARN]=2 [CRIT]=3 [ERROR]=3)
declare -A OPS_COUNT=([OK]=0 [WARN]=0 [CRIT]=0 [UNKNOWN]=0)
OPS_WORST=OK

# Allowed config keys and their defaults. Anything else in the config file is ignored,
# so a config line cannot set PATH, IFS, LD_PRELOAD or any other shell variable.
declare -A OPS_DEFAULTS=(
  [OPS_LOG_DIR]=""
  [OPS_LOAD_WARN]=100 [OPS_LOAD_CRIT]=200
  [OPS_MEM_WARN]=85 [OPS_MEM_CRIT]=95
  [OPS_DISK_WARN]=80 [OPS_DISK_CRIT]=90
  [OPS_INODE_WARN]=80 [OPS_INODE_CRIT]=90
  [OPS_DISK_MOUNTS]=""
  [OPS_DISK_EXCLUDE_TYPES]="tmpfs devtmpfs squashfs overlay efivarfs ramfs"
  [OPS_TOP_N]=5
  [OPS_CPU_SAMPLE_SECS]=1
  [OPS_PROC_ROOT]=/proc
  [OPS_SYSTEMD_UNITS]=""
  [OPS_TCP_CHECKS]=""
  [OPS_HTTP_CHECKS]=""
  [OPS_CONNECT_TIMEOUT]=5
  [OPS_HTTP_TIMEOUT]=10
  [OPS_BACKUP_SOURCES]=""
  [OPS_BACKUP_DEST]=/var/backups/ops-toolkit
  [OPS_BACKUP_KEEP]=7
  [OPS_LOGCLEAN_DIR]=""
  [OPS_LOGCLEAN_COMPRESS_DAYS]=7
  [OPS_LOGCLEAN_DELETE_DAYS]=30
)

ops_warn() { printf '%s: warning: %s\n' "$OPS_PROG" "$*" >&2; }

ops_usage_error() {
  printf "%s: %s\nTry '%s --help' for usage.\n" "$OPS_PROG" "$1" "$OPS_PROG" >&2
  exit 64
}

# ops_die MESSAGE [HINT] - report a fatal problem (stderr + JSON log) and exit.
ops_die() {
  trap - ERR
  OPS_DONE=1
  local msg=$1 hint=${2-} level=ERROR code=1
  if [[ $OPS_KIND == check ]]; then
    level=UNKNOWN code=3
    # Worst result still wins: a CRITICAL found before the failure is not downgraded.
    if ((OPS_SEVERITY[$OPS_WORST] > OPS_SEVERITY[UNKNOWN])); then code=${OPS_EXIT_CODE[$OPS_WORST]}; fi
  fi
  printf '%s: error: %s\n' "$OPS_PROG" "$msg" >&2
  if [[ -n $hint ]]; then printf '%s: hint: %s\n' "$OPS_PROG" "$hint" >&2; fi
  if ((OPS_LOG_READY)); then
    ops_log "$level" "$OPS_PROG" "$msg" ${hint:+"hint=$hint"}
  fi
  exit "$code"
}

# ops_exit CODE - intended exit (the EXIT trap then does not treat it as a crash).
ops_exit() {
  OPS_DONE=1
  exit "$1"
}

ops_on_error() {
  local rc=$? line=$1 cmd=$2
  # Inside $(...) just propagate; the parent shell reports it once.
  if ((BASH_SUBSHELL > 0)); then exit "$rc"; fi
  ops_die "unexpected failure (exit $rc) at line $line: $cmd"
}

# EXIT trap: remove temporary files, and report exits nobody intended. Shell errors such as
# an unset variable or a bad arithmetic expression bypass the ERR trap and would otherwise
# exit 1, which a monitoring system would read as WARNING.
ops_on_exit() {
  local rc=$?
  if ((${#OPS_CLEANUP_FILES[@]})); then rm -f -- "${OPS_CLEANUP_FILES[@]}"; fi
  if ((OPS_DONE || rc == 0)); then return 0; fi
  ops_die "unexpected exit (status $rc); see the shell error above"
}

ops_require_cmd() {
  local c
  for c in "$@"; do
    if ! command -v "$c" >/dev/null 2>&1; then
      ops_die "required command not found: $c" "install it (e.g. apt-get install the package providing '$c')"
    fi
  done
}

# --- configuration ----------------------------------------------------------
# Precedence: environment variable > config file > built-in default.
# The file is parsed line by line instead of being sourced: sourcing would run any
# shell code in it ($(...), backticks, ;) with the caller's privileges - often root.
ops_load_config() {
  local file=${OPS_CONFIG:-/etc/ops-toolkit/ops-toolkit.conf}
  local -A from_env=()
  local key value line n=0

  for key in "${!OPS_DEFAULTS[@]}"; do
    if [[ -n ${!key+x} ]]; then from_env[$key]=1; fi
  done

  if [[ -r $file && ! -d $file ]]; then
    while IFS= read -r line || [[ -n $line ]]; do
      n=$((n + 1))
      line=${line%$'\r'}
      if [[ $line =~ ^[[:space:]]*(#|$) ]]; then continue; fi
      if [[ ! $line =~ ^([A-Z_][A-Z0-9_]*)=(.*)$ ]]; then
        ops_warn "$file:$n: ignoring line that is not KEY=VALUE"
        continue
      fi
      key=${BASH_REMATCH[1]}
      value=${BASH_REMATCH[2]}
      if [[ -z ${OPS_DEFAULTS[$key]+x} ]]; then
        ops_warn "$file:$n: ignoring unknown key $key"
        continue
      fi
      if [[ -n ${from_env[$key]-} ]]; then continue; fi
      # Strip one pair of surrounding quotes. Nothing else is interpreted.
      if [[ $value =~ ^\"(.*)\"$ || $value =~ ^\'(.*)\'$ ]]; then value=${BASH_REMATCH[1]}; fi
      printf -v "$key" '%s' "$value"
    done <"$file"
  elif [[ -n ${OPS_CONFIG:-} ]]; then
    ops_die "config file not readable: $file" "check the path in OPS_CONFIG and its permissions"
  elif [[ -e $file ]]; then
    ops_warn "cannot read $file (root-only?); using built-in defaults and environment"
  fi

  for key in "${!OPS_DEFAULTS[@]}"; do
    if [[ -z ${!key+x} ]]; then printf -v "$key" '%s' "${OPS_DEFAULTS[$key]}"; fi
  done
}

# ops_require_uint KEY... - each named setting must be a plain non-negative integer.
ops_require_uint() {
  local key
  for key in "$@"; do
    if [[ ! ${!key} =~ ^(0|[1-9][0-9]{0,8})$ ]]; then
      ops_die "invalid $key='${!key}': expected a non-negative integer" "fix it in ${OPS_CONFIG:-/etc/ops-toolkit/ops-toolkit.conf} or the environment"
    fi
  done
}

# ops_require_min MIN KEY... - integers of at least MIN (a timeout of 0 would mean "no timeout").
ops_require_min() {
  local min=$1 key
  shift
  ops_require_uint "$@"
  for key in "$@"; do
    if ((${!key} < min)); then ops_die "$key must be at least $min (got ${!key})"; fi
  done
}

# ops_require_thresholds WARN_KEY CRIT_KEY - integers with warn <= crit.
ops_require_thresholds() {
  ops_require_uint "$1" "$2"
  if ((${!1} > ${!2})); then
    ops_die "$1 (${!1}) must not be greater than $2 (${!2})"
  fi
}

# --- logging ----------------------------------------------------------------
ops_setup_logging() {
  if [[ -z $OPS_LOG_DIR ]]; then
    if ((EUID == 0)); then
      OPS_LOG_DIR=/var/log/ops-toolkit
    elif [[ -n ${XDG_STATE_HOME:-} || -n ${HOME:-} ]]; then
      OPS_LOG_DIR=${XDG_STATE_HOME:-$HOME/.local/state}/ops-toolkit
    fi
  fi
  OPS_LOG_FILE=
  if [[ $OPS_LOG_DIR != /* ]]; then
    ops_warn "OPS_LOG_DIR must be an absolute path (got '$OPS_LOG_DIR'); events go to stdout only"
  elif [[ -L $OPS_LOG_DIR/ops-toolkit.jsonl ]]; then
    # Never append through a symlink someone planted (classic /tmp symlink attack).
    ops_warn "$OPS_LOG_DIR/ops-toolkit.jsonl is a symlink; refusing to write to it, events go to stdout only"
  elif (umask 027 && mkdir -p -- "$OPS_LOG_DIR" && touch -- "$OPS_LOG_DIR/ops-toolkit.jsonl") 2>/dev/null; then
    OPS_LOG_FILE=$OPS_LOG_DIR/ops-toolkit.jsonl
  else
    ops_warn "cannot write to log directory '$OPS_LOG_DIR'; events go to stdout only"
  fi
}

# ops_json_str STRING - sets REPLY to STRING as a quoted JSON string.
ops_json_str() {
  local s=$1 bs=$'\\' q='"'
  s=${s//"$bs"/"$bs$bs"}
  s=${s//"$q"/"$bs$q"}
  s=${s//$'\n'/"${bs}n"}
  s=${s//$'\r'/"${bs}r"}
  s=${s//$'\t'/"${bs}t"}
  s=${s//[[:cntrl:]]/}
  REPLY="\"$s\""
}

# ops_log LEVEL CHECK MESSAGE [key=value ...] - one JSON object per line.
# Values that look like numbers are emitted as JSON numbers.
ops_log() {
  local level=$1 check=$2 msg=$3 ts line kv k v
  shift 3
  ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  line="{\"ts\":\"$ts\",\"level\":\"$level\",\"tool\":\"$OPS_PROG\""
  ops_json_str "$check" && line+=",\"check\":$REPLY"
  ops_json_str "$OPS_HOST" && line+=",\"host\":$REPLY"
  ops_json_str "$msg" && line+=",\"msg\":$REPLY"
  for kv in "$@"; do
    k=${kv%%=*}
    v=${kv#*=}
    if [[ $v =~ ^-?(0|[1-9][0-9]*)(\.[0-9]+)?$ ]]; then
      line+=",\"$k\":$v"
    else
      ops_json_str "$v" && line+=",\"$k\":$REPLY"
    fi
  done
  line+="}"
  printf '%s\n' "$line"
  if [[ -n $OPS_LOG_FILE ]]; then
    if ! printf '%s\n' "$line" >>"$OPS_LOG_FILE" 2>/dev/null; then
      ops_warn "cannot append to $OPS_LOG_FILE; continuing with stdout only"
      OPS_LOG_FILE=
    fi
  fi
}

# ops_status LEVEL CHECK MESSAGE [key=value ...] - log a check result and track the worst one.
ops_status() {
  local level=$1
  ops_log "$@"
  OPS_COUNT[$level]=$((${OPS_COUNT[$level]:-0} + 1))
  if ((OPS_SEVERITY[$level] > OPS_SEVERITY[$OPS_WORST])); then OPS_WORST=$level; fi
}

# ops_finish - emit a summary event and exit with the Nagios code of the worst status.
ops_finish() {
  local word
  case $OPS_WORST in
    OK) word=OK ;;
    WARN) word=WARNING ;;
    CRIT) word=CRITICAL ;;
    *) word=UNKNOWN ;;
  esac
  ops_log "$OPS_WORST" summary \
    "$OPS_PROG $word: ${OPS_COUNT[CRIT]} critical, ${OPS_COUNT[WARN]} warning, ${OPS_COUNT[UNKNOWN]} unknown, ${OPS_COUNT[OK]} ok" \
    "crit_count=${OPS_COUNT[CRIT]}" "warn_count=${OPS_COUNT[WARN]}" \
    "unknown_count=${OPS_COUNT[UNKNOWN]}" "ok_count=${OPS_COUNT[OK]}"
  ops_exit "${OPS_EXIT_CODE[$OPS_WORST]}"
}

# ops_classify VALUE WARN CRIT - sets REPLY to OK, WARN or CRIT (higher value = worse).
ops_classify() {
  if (($1 >= $3)); then
    REPLY=CRIT
  elif (($1 >= $2)); then
    REPLY=WARN
  else
    REPLY=OK
  fi
}

ops_init() {
  OPS_KIND=$1
  trap 'ops_on_error "$LINENO" "$BASH_COMMAND"' ERR
  trap ops_on_exit EXIT
  OPS_HOST=${HOSTNAME:-$(uname -n)}
  ops_load_config
  ops_setup_logging
  OPS_LOG_READY=1
}

# --- safety -----------------------------------------------------------------
# ops_is_dangerous_dir RESOLVED_PATH - true for directories no tool may clean, back up into
# or restore into: the root, system trees, /var/log itself (logrotate's job; rsyslog keeps
# quiet files open) and every home directory (sudo sets HOME=/root, so check passwd too).
ops_is_dangerous_dir() {
  local d=$1 home
  case $d in
    / | /bin | /boot | /dev | /etc | /home | /lib | /lib32 | /lib64 | /libx32 | /media | /mnt | \
      /opt | /proc | /root | /run | /sbin | /srv | /sys | /tmp | /usr | /var | /var/lib | /var/log | \
      /bin/* | /boot/* | /dev/* | /etc/* | /lib/* | /lib64/* | /proc/* | /sbin/* | /sys/* | /usr/*)
      return 0
      ;;
  esac
  if [[ -n ${HOME:-} && $d == "$(realpath -m -- "$HOME")" ]]; then return 0; fi
  while IFS=: read -r _ _ _ _ _ home _; do
    if [[ -n $home && $d == "$home" ]]; then return 0; fi
  done < <(getent passwd 2>/dev/null)
  return 1
}

# ops_require_safe_dir DIR WHAT - DIR must be a real directory owned by the current user and
# not writable by group/others. Otherwise another local user could swap a path inside it for
# a symlink while we work on it as root.
ops_require_safe_dir() {
  local d=$1 what=$2 mode
  if [[ -L $d || ! -d $d ]]; then ops_die "$what $d is not a directory"; fi
  if [[ ! -O $d ]]; then
    ops_die "$what $d is not owned by $(id -un)" "use a directory that only you control"
  fi
  mode=$(stat -c %a -- "$d")
  if ((8#$mode & 8#022)); then
    ops_die "$what $d is writable by group or others (mode $mode)" "chmod go-w $d, or use a new directory"
  fi
}

# --- probes -----------------------------------------------------------------
ops_now_ms() {
  local t=${EPOCHREALTIME/./}
  REPLY=$((t / 1000))
}

# ops_tcp_probe HOST PORT TIMEOUT - connect with bash's /dev/tcp under `timeout`.
# Sets REPLY to open|refused|timeout|dns|unreachable|error and OPS_PROBE_MS.
ops_tcp_probe() {
  local host=$1 port=$2 t=$3 err rc=0 start
  ops_now_ms
  start=$REPLY
  # shellcheck disable=SC2016 # $1/$2 are expanded by the inner bash, on purpose
  err=$(timeout -k 1 "$t" bash -c 'exec 3<>"/dev/tcp/$1/$2"' ops-tcp "$host" "$port" 2>&1) || rc=$?
  ops_now_ms
  OPS_PROBE_MS=$((REPLY - start))
  if ((rc == 0)); then
    REPLY=open
  elif ((rc == 124 || rc == 137)); then
    REPLY=timeout
  elif [[ $err == *"Connection refused"* ]]; then
    REPLY=refused
  elif [[ $err == *"Name or service not known"* || $err == *"name resolution"* ]]; then
    REPLY=dns
  elif [[ $err == *"No route to host"* || $err == *"Network is unreachable"* ]]; then
    REPLY=unreachable
  else
    REPLY=error
  fi
}

# ops_tcp_hint REASON HOST PORT - sets REPLY to a concrete next step.
ops_tcp_hint() {
  case $1 in
    refused) REPLY="host answered but nothing listens on port $3: on $2 run 'ss -ltnp \"sport = :$3\"' and check the service with 'systemctl status <unit>'" ;;
    timeout) REPLY="no answer before the timeout: a firewall/security group is probably dropping packets or the host is down; check 'ip route get <address>', 'sudo nft list ruleset' or 'sudo ufw status' on the server, and 'traceroute -T -p $3 $2'" ;;
    dns) REPLY="name does not resolve: run 'ops-netdiag $2' or 'getent ahosts $2'" ;;
    unreachable) REPLY="no route to the host: check 'ip route' and 'ip addr' (interface up? default gateway present?)" ;;
    *) REPLY="unexpected connect error: retry with 'nc -vz $2 $3' for details" ;;
  esac
}

# shellcheck disable=SC2034 # OPS_HTTP_CODE/OPS_HTTP_RC/OPS_PROBE_MS are outputs read by the caller
# ops_http_probe URL - sets OPS_HTTP_CODE (000 if no response), OPS_HTTP_RC (curl exit code), OPS_PROBE_MS.
ops_http_probe() {
  local url=$1 out rc=0 start
  ops_now_ms
  start=$REPLY
  # -q (must come first): ignore ~/.curlrc. -g: no {a,b}/[1-3] URL globbing (one request only).
  # --proto: never let a config entry make curl read file:// or other schemes.
  out=$(curl -q -g -s -o /dev/null -w '%{http_code}' --proto '=http,https' \
    --connect-timeout "$OPS_CONNECT_TIMEOUT" --max-time "$OPS_HTTP_TIMEOUT" -- "$url") || rc=$?
  ops_now_ms
  OPS_PROBE_MS=$((REPLY - start))
  OPS_HTTP_CODE=${out:-000}
  OPS_HTTP_RC=$rc
}

# ops_curl_hint CURL_EXIT_CODE HOST - sets REPLY to "reason: next step".
ops_curl_hint() {
  case $1 in
    6) REPLY="could not resolve host: run 'ops-netdiag $2' to check DNS" ;;
    7) REPLY="connection failed (refused or unreachable): run 'ops-netdiag $2 <port>'; on the server check 'ss -ltnp'" ;;
    28) REPLY="timed out: the service is hanging or packets are dropped; compare 'curl -sv --max-time 30 <url>' and check the app's logs" ;;
    35) REPLY="TLS handshake failed: inspect with 'openssl s_client -connect $2:443 -servername $2'" ;;
    60) REPLY="TLS certificate not trusted or expired: 'openssl s_client -connect $2:443 -servername $2 | openssl x509 -noout -dates -issuer'" ;;
    52 | 56) REPLY="connection closed/reset by the server or a proxy: check the app and any load balancer logs" ;;
    *) REPLY="curl exit code $1 (see 'man curl', EXIT CODES)" ;;
  esac
}
