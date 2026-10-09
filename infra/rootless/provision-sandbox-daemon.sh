#!/usr/bin/env bash
# Stand up the dedicated rootless Docker daemon that runs embry0's sandboxes
# (AGENT_RUNTIME=rootless). Run as root on the deploy host, once, and again
# after changing a setting below; every step is idempotent.
#
#   sudo infra/rootless/provision-sandbox-daemon.sh
#
# What it does, and why each step exists, is in docs/ops/provisioning.md
# ("A rootless sandbox daemon"). In short: an unprivileged account runs
# dockerd under rootlesskit as a systemd user service; its socket lives in
# /run/embry0-sandbox, the directory compose mounts into the orchestrator;
# every container it runs sits in one slice that carries the collective
# MemoryMax/CPUQuota/TasksMax ceiling for all sandboxes and proxies together.
# The account's whole user slice is capped too, a little higher, so the
# daemon and image builds are bounded as well.
#
# Settings (environment):
#   SANDBOX_USER         the account that owns the daemon        (embry0-sandbox)
#   SANDBOX_MEMORY_MAX   memory ceiling for all sandboxes        (8G)
#   SANDBOX_CPU_QUOTA    CPU ceiling, 100% per core              (400%)
#   SANDBOX_TASKS_MAX    process/thread ceiling                  (16384)
#   DAEMON_MEMORY_EXTRA  the daemon's room above the ceiling     (2G)
set -euo pipefail

SANDBOX_USER="${SANDBOX_USER:-embry0-sandbox}"
SANDBOX_MEMORY_MAX="${SANDBOX_MEMORY_MAX:-8G}"
SANDBOX_CPU_QUOTA="${SANDBOX_CPU_QUOTA:-400%}"
SANDBOX_TASKS_MAX="${SANDBOX_TASKS_MAX:-16384}"
DAEMON_MEMORY_EXTRA="${DAEMON_MEMORY_EXTRA:-2G}"
SOCKET_DIR=/run/embry0-sandbox
SOCKET="$SOCKET_DIR/docker.sock"
HERE="$(cd "$(dirname "$0")" && pwd)"

die() { echo "provision-sandbox-daemon: $*" >&2; exit 1; }
say() { echo "provision-sandbox-daemon: $*"; }

# A size such as 8G, in bytes.
to_bytes() {
  local value=$1 number unit
  number="${value%[KMGTkmgt]}"
  unit="${value#"$number"}"
  case "$number" in '' | *[!0-9]*) die "not a size: '$value' (use e.g. 8G)" ;; esac
  case "$unit" in
    '') echo "$number" ;;
    [Kk]) echo $((number << 10)) ;;
    [Mm]) echo $((number << 20)) ;;
    [Gg]) echo $((number << 30)) ;;
    [Tt]) echo $((number << 40)) ;;
  esac
}

[ "$(id -u)" = 0 ] || die "run as root"
case "$SANDBOX_USER" in
  '' | *[!a-z0-9_-]*) die "SANDBOX_USER must be a plain account name, got '$SANDBOX_USER'" ;;
esac
case "$SANDBOX_CPU_QUOTA" in
  *[0-9]%) ;;
  *) die "SANDBOX_CPU_QUOTA must be a percentage such as 400%, got '$SANDBOX_CPU_QUOTA'" ;;
esac
case "$SANDBOX_TASKS_MAX" in '' | *[!0-9]*) die "SANDBOX_TASKS_MAX must be a number" ;; esac
user_memory_max=$(($(to_bytes "$SANDBOX_MEMORY_MAX") + $(to_bytes "$DAEMON_MEMORY_EXTRA")))

# --- prerequisites -----------------------------------------------------------
missing=()
for bin in dockerd dockerd-rootless.sh rootlesskit newuidmap newgidmap slirp4netns iptables systemctl loginctl; do
  command -v "$bin" >/dev/null 2>&1 || missing+=("$bin")
done
if [ "${#missing[@]}" -gt 0 ]; then
  die "missing: ${missing[*]}. Install Docker Engine with its rootless extras (docker-ce-rootless-extras), uidmap, slirp4netns, iptables and dbus-user-session from your distribution or Docker's repository."
fi
[ "$(stat -fc %T /sys/fs/cgroup)" = cgroup2fs ] || die "cgroup v2 is required: without it a rootless daemon cannot enforce --memory, --cpus or --pids-limit"

# --- kernel ------------------------------------------------------------------
# An unprivileged namespace cannot load modules, so root loads them here and
# at every boot. br_netfilter is the one the isolation chain depends on: with
# it absent, iptables never sees traffic between two containers on one bridge.
modules=(br_netfilter ip_tables iptable_filter iptable_nat nf_conntrack xt_conntrack xt_limit xt_LOG xt_MASQUERADE xt_addrtype)
printf '%s\n' "# embry0 rootless sandbox daemon (infra/rootless)" "${modules[@]}" >/etc/modules-load.d/embry0-sandbox.conf
for module in "${modules[@]}"; do
  modprobe "$module" 2>/dev/null || say "note: modprobe $module failed (fine if it is built into the kernel)"
done
[ -e /proc/sys/net/bridge/bridge-nf-call-iptables ] || die "br_netfilter is not available; the sandbox isolation chain cannot work without it"

# --- the account -------------------------------------------------------------
if ! id "$SANDBOX_USER" >/dev/null 2>&1; then
  useradd --create-home --user-group --shell /usr/sbin/nologin "$SANDBOX_USER"
  say "created account $SANDBOX_USER"
fi
uid="$(id -u "$SANDBOX_USER")"
home="$(getent passwd "$SANDBOX_USER" | cut -d: -f6)"
for f in /etc/subuid /etc/subgid; do
  grep -q "^$SANDBOX_USER:" "$f" 2>/dev/null ||
    die "$SANDBOX_USER has no range in $f; add one of at least 65536 ids (usermod --add-subuids / --add-subgids)"
done

# --- socket directory ----------------------------------------------------------
# The orchestrator's compose service mounts this directory (not the socket
# file, which a daemon restart replaces) and joins the account's group.
printf 'd %s 0750 %s %s -\n' "$SOCKET_DIR" "$SANDBOX_USER" "$SANDBOX_USER" >/etc/tmpfiles.d/embry0-sandbox.conf
systemd-tmpfiles --create /etc/tmpfiles.d/embry0-sandbox.conf

# --- cgroups: delegation and the collective ceiling -----------------------------
# Delegation lets the rootless daemon set per-sandbox limits. The ceiling is
# the role the DinD container's own cgroup played (DIND_CPUS / DIND_MEMORY).
# It sits on embry0-sandboxes.slice, where the daemon puts every container
# (daemon.json "cgroup-parent"), so a sandbox that hits it is what the kernel
# kills, not dockerd. The user slice above it holds the daemon and builds as
# well, and gets the same CPU quota and DAEMON_MEMORY_EXTRA more memory.
install -d "/etc/systemd/system/user@$uid.service.d" "/etc/systemd/system/user-$uid.slice.d"
cat >"/etc/systemd/system/user@$uid.service.d/50-embry0-delegate.conf" <<EOF
[Service]
Delegate=cpu cpuset io memory pids
EOF
cat >"/etc/systemd/system/user-$uid.slice.d/50-embry0-ceiling.conf" <<EOF
[Slice]
MemoryMax=$user_memory_max
MemorySwapMax=0
CPUQuota=$SANDBOX_CPU_QUOTA
EOF
systemctl daemon-reload
loginctl enable-linger "$SANDBOX_USER"
systemctl start "user@$uid.service"

# --- the daemon --------------------------------------------------------------
units="$home/.config/systemd/user"
install -d -o "$SANDBOX_USER" -g "$SANDBOX_USER" -m 0700 "$home/.config" "$home/.config/docker" "$home/.config/systemd" "$units"
install -o "$SANDBOX_USER" -g "$SANDBOX_USER" -m 0644 "$HERE/daemon.json" "$home/.config/docker/daemon.json"
install -o "$SANDBOX_USER" -g "$SANDBOX_USER" -m 0644 "$HERE/docker.service" "$units/docker.service"
sed -e "s/@MEMORY_MAX@/$SANDBOX_MEMORY_MAX/" -e "s/@CPU_QUOTA@/$SANDBOX_CPU_QUOTA/" -e "s/@TASKS_MAX@/$SANDBOX_TASKS_MAX/" \
  "$HERE/embry0-sandboxes.slice" >"$units/embry0-sandboxes.slice"
chown "$SANDBOX_USER:$SANDBOX_USER" "$units/embry0-sandboxes.slice"
user_systemctl() { systemctl --user --machine="$SANDBOX_USER@" "$@"; }
user_systemctl daemon-reload
user_systemctl enable docker.service
user_systemctl restart docker.service

for _ in $(seq 1 60); do
  [ -S "$SOCKET" ] && break
  sleep 1
done
[ -S "$SOCKET" ] || die "the daemon did not create $SOCKET; see: journalctl --user -M $SANDBOX_USER@ -u docker"

# --- verify ------------------------------------------------------------------
export DOCKER_HOST="unix://$SOCKET"
info="$(docker info --format '{{json .}}')"
grep -q 'name=rootless' <<<"$info" || die "the daemon on $SOCKET is not rootless"
grep -q '"CgroupVersion":"2"' <<<"$info" || die "the daemon on $SOCKET is not on cgroup v2"
grep -q '"MemoryLimit":true' <<<"$info" || die "the daemon on $SOCKET cannot enforce memory limits (cgroup delegation)"
grep -q '"CpuCfsQuota":true' <<<"$info" || die "the daemon on $SOCKET cannot enforce CPU limits (cgroup delegation)"
grep -q '"PidsLimit":true' <<<"$info" || die "the daemon on $SOCKET cannot enforce pids limits (cgroup delegation)"
# Every sandbox runs under runc: gVisor is not offered under rootless (runsc
# would need --ignore-cgroups, which drops the per-sandbox limits).
grep -q '"DefaultRuntime":"runc"' <<<"$info" || die "the daemon on $SOCKET does not default to runc"
if grep -q 'runsc' <<<"$(docker info --format '{{json .Runtimes}}')"; then
  die "the daemon on $SOCKET registers runsc; gVisor is not offered under AGENT_RUNTIME=rootless (docs/ops/provisioning.md)"
fi
# The ceiling is only real if the containers land under it. Read from the
# host side: inside, a cgroup namespace shows the container its own root.
probe="$(docker run -d --rm --network none busybox:1.36 sleep 30)" || die "could not start a probe container"
cgroup="$(cat "/proc/$(docker inspect -f '{{.State.Pid}}' "$probe")/cgroup")"
docker rm -f "$probe" >/dev/null
grep -q '/embry0-sandboxes.slice/' <<<"$cgroup" || die "containers do not run under embry0-sandboxes.slice: $cgroup"
say "ready: DOCKER_HOST=unix://$SOCKET, group $SANDBOX_USER (gid $(id -g "$SANDBOX_USER")), sandbox ceiling MemoryMax=$SANDBOX_MEMORY_MAX CPUQuota=$SANDBOX_CPU_QUOTA TasksMax=$SANDBOX_TASKS_MAX"
