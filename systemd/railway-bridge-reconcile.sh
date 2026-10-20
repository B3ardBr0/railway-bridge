#!/usr/bin/env bash
# Reconcile the published railway-bridge release after boot, once the private
# address it listens on exists. Installed root-owned at
# /usr/local/libexec/railway-bridge-reconcile by remote/install.sh, started by
# railway-bridge-reconcile.service.
#
# Docker starts containers before a VPN interface such as tailscale0 has its
# address, and a port published on an address that does not exist yet fails.
# So this waits, boundedly, for the exact address on the exact interface, then
# recreates the project and waits for its healthcheck.
#
# deployment.env is parsed, never sourced: it must be exactly two lines,
# RAILWAY_BRIDGE_BIND_IP and RAILWAY_BRIDGE_BIND_INTERFACE.

set -euo pipefail
umask 077
export LC_ALL=C

ROOT_UID=0
OPT=/opt
LINK=/opt/railway-bridge
RELEASES=/opt/railway-bridge-releases
# Shared with remote/install.sh, so a boot never races an install.
LOCK=/run/railway-bridge-install.lock
DOCKER=/usr/bin/docker
IP=/usr/sbin/ip
PROJECT=railway-bridge
# Time budget; railway-bridge-reconcile.service's TimeoutStartSec is the sum
# of these three plus 180 seconds of overhead, and a test enforces it.
LOCK_WAIT_SECONDS=600
WAIT_SECONDS=120
COMPOSE_WAIT_SECONDS=300
SLEEP_SECONDS=1

RELEASE_NAME='^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}$'
IPV4='^(25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])(\.(25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])){3}$'
IFACE='^[A-Za-z0-9][A-Za-z0-9_.-]{0,14}$'

fail() { echo "railway-bridge-reconcile: $*" >&2; exit 1; }

# A real object of root's, not a link, not writable by group or other.
check_secure() {
  local path="$1" kind="$2" uid mode
  [[ ! -L "$path" ]] || fail "$path is a symbolic link"
  case "$kind" in
    directory) [[ -d "$path" ]] || fail "$path is not a directory" ;;
    file) [[ -f "$path" ]] || fail "$path is not a regular file" ;;
  esac
  read -r uid mode < <(stat -c '%u %a' -- "$path") || fail "cannot stat $path"
  [[ "$uid" == "$ROOT_UID" ]] || fail "$path is not owned by root"
  (( (8#$mode & 8#022) == 0 )) || fail "$path is writable by group or other"
}

# Sets release to the published release directory, or fails.
resolve_release() {
  local target
  check_secure "$OPT" directory
  check_secure "$RELEASES" directory
  [[ -L "$LINK" ]] || fail "$LINK is not the release link"
  target="$(readlink -- "$LINK")" || fail "cannot read $LINK"
  [[ "$(dirname -- "$target")" == "$RELEASES" ]] || fail "$LINK points outside $RELEASES"
  [[ "$(basename -- "$target")" =~ $RELEASE_NAME ]] || fail "$LINK points at an odd name"
  release="$target"
  check_secure "$release" directory
  check_secure "$release/compose.yaml" file
  check_secure "$release/deployment.env" file
}

# Sets bind_ip and bind_if from deployment.env without executing it.
read_deployment_env() {
  local lines=()
  mapfile -t lines < "$release/deployment.env"
  (( ${#lines[@]} == 2 )) || fail "deployment.env must be exactly two lines"
  [[ "${lines[0]}" == RAILWAY_BRIDGE_BIND_IP=* ]] || fail "deployment.env line 1 is not the bind address"
  [[ "${lines[1]}" == RAILWAY_BRIDGE_BIND_INTERFACE=* ]] || fail "deployment.env line 2 is not the interface"
  bind_ip="${lines[0]#RAILWAY_BRIDGE_BIND_IP=}"
  bind_if="${lines[1]#RAILWAY_BRIDGE_BIND_INTERFACE=}"
  [[ "$bind_ip" =~ $IPV4 ]] || fail "deployment.env bind address is not IPv4"
  private_ipv4 "$bind_ip" || fail "deployment.env bind address is not in a private range"
  [[ "$bind_if" =~ $IFACE ]] || fail "deployment.env interface name is not safe"
}

# 10/8, 172.16/12, 192.168/16 or 100.64/10, as render.py and the installer.
private_ipv4() {
  local a b rest
  IFS=. read -r a b rest <<<"$1"
  (( a == 10 )) && return 0
  (( a == 172 && b >= 16 && b <= 31 )) && return 0
  (( a == 192 && b == 168 )) && return 0
  (( a == 100 && b >= 64 && b <= 127 )) && return 0
  return 1
}

# True once the interface holds exactly bind_ip/32.
address_ready() {
  local output
  output="$("$IP" -4 -o addr show dev "$bind_if" 2>/dev/null)" || return 1
  awk -v want="$bind_ip/32" -v dev="$bind_if" \
    '$2 == dev && $3 == "inet" && $4 == want { found = 1 } END { exit !found }' <<<"$output"
}

wait_for_address() {
  local attempt
  for (( attempt = 1; attempt <= WAIT_SECONDS; attempt++ )); do
    if address_ready; then
      echo "railway-bridge-reconcile: $bind_if has $bind_ip after $attempt check(s)"
      return 0
    fi
    sleep "$SLEEP_SECONDS"
  done
  fail "$bind_ip/32 did not appear on $bind_if within $WAIT_SECONDS checks"
}

main() {
  local release bind_ip bind_if
  [[ -x "$DOCKER" ]] || fail "docker not found at $DOCKER"
  [[ -x "$IP" ]] || fail "ip not found at $IP"
  # The lock file is opened, never replaced, so every holder locks one inode.
  exec 9>>"$LOCK" || fail "cannot open $LOCK"
  flock -w "$LOCK_WAIT_SECONDS" 9 || fail "install lock still held after $LOCK_WAIT_SECONDS s"
  # Validated under the lock, so an install that just finished is what runs.
  resolve_release
  read_deployment_env
  wait_for_address
  "$DOCKER" compose --env-file "$release/deployment.env" --project-directory "$release" \
    -f "$release/compose.yaml" -p "$PROJECT" \
    up -d --force-recreate --wait --wait-timeout "$COMPOSE_WAIT_SECONDS" \
    || fail "compose did not reach healthy"
  echo "railway-bridge-reconcile: $PROJECT healthy from $(basename -- "$release")"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
