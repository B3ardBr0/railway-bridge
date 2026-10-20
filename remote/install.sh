#!/usr/bin/env bash
# Install a staged railway-bridge release on the destination host. Runs as
# root, from stdin.
#
#   sudo /bin/bash -s -- STAGE START MANIFEST_SHA256 < remote/install.sh
#
# deploy.sh never uploads this file, and nothing in STAGE is ever executed. It
# uploads the rendered payload into STAGE, a mktemp directory of the SSH user,
# then sends this script from the repository, base64-encoded inside the SSH
# command, and pipes the decoded bytes into sudo /bin/bash -s. START is 1 to
# recreate the container afterwards and 0 to install the files only.
# MANIFEST_SHA256 is the hash deploy.sh computed locally of the manifest
# render.py wrote; it is not a secret. This script only reads STAGE; deploy.sh
# removes it afterwards.
#
# As root, holding /run/railway-bridge-install.lock:
#
#   1. copies STAGE, without following links, into a new directory under the
#      root-only /opt/railway-bridge-releases (directories 700, files 600,
#      owned by root, no special bits). From then on it reads only that copy,
#      and before opening any file in it for its content it checks the exact
#      set of paths and types, the manifest's hash, and the hash of every file
#      against the manifest
#   2. checks railway-bridge.env holds exactly the five lines the container
#      reads and deployment.env exactly the bind address and interface, has
#      `docker compose config` validate the copy (it prints nothing), checks
#      the only service is railway-bridge and its image the approved digest,
#      and pulls it
#   3. atomically installs and enables the root-owned boot helper and systemd
#      unit, retaining their exact previous files and enablement for rollback
#   4. names the release YYYYMMDDTHHMMSSZ-<first 12 of the manifest hash> and
#      publishes it by pointing a new symlink at it and renaming that over
#      /opt/railway-bridge (mv -T, one rename). /opt/railway-bridge is never
#      missing and is always a whole release, old or new
#   5. with START=1 only, recreates the container and waits for it to be
#      healthy. If that fails, /opt/railway-bridge is pointed back at the
#      previous release in the same way and that is recreated; on a first
#      install the failed project is brought down, so no unhealthy container is
#      left, and /opt/railway-bridge is removed. Either way the failed release
#      is renamed <name>.failed and kept for diagnosis, and the exit status is 1
#
# A failure, Ctrl-C or TERM before commit restores the prior release link,
# container state, systemd files and exact enabled/disabled state as one
# transaction. Recovery backups are retained if automatic restoration fails.
# During commit, rollback state is cleared with signals masked before the only
# recovery copy is removed. HUP is ignored throughout, so a dropped SSH
# connection lets the run finish, rollback included, instead of stopping half
# way; its output is lost, and `ls -l /opt/railway-bridge` shows where it ended.
# Nothing prints a secret: railway-bridge.env is hashed and grepped, never
# shown, and compose output is limited to names, digests and container states.
#
# Committed releases are kept, each with its own railway-bridge.env; README
# says how to list and remove old ones. A pre-commit failure removes the new
# release only after rollback succeeds. A running container keeps the release
# it was created from until it is recreated, so START=0 changes nothing it
# serves.
#
# Sourcing this file defines the functions and runs nothing, for the tests. The
# settings below are plain variables only so that the tests can point them at
# a scratch directory after sourcing; run from stdin, nothing can change them.
# Nothing runs before the last line, so a script cut short runs nothing.

set -euo pipefail
umask 077
export LC_ALL=C

ROOT_UID=0
OPT=/opt
RELEASES=/opt/railway-bridge-releases
LINK=/opt/railway-bridge
LOCK=/run/railway-bridge-install.lock
HELPER=/usr/local/libexec/railway-bridge-reconcile
UNIT=/etc/systemd/system/railway-bridge-reconcile.service
SERVICE=railway-bridge-reconcile.service
WANTS=/etc/systemd/system/multi-user.target.wants/railway-bridge-reconcile.service
STAGE_PATTERN='^/[A-Za-z0-9._/-]*/railway-bridge-stage\.[A-Za-z0-9]{10}$'
RELEASE_PATTERN='^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}$'
MANIFEST=manifest.sha256
SERVICES=railway-bridge
# compose.yaml pins the same string, and a test checks they agree.
IMAGES='python:3.12.15-alpine3.24@sha256:1b668429b3511ab407d8e00648891631b0b1a4d7e15e3ca70f38ab5b91ad4ab4'
# What the container reads, in this order, each with a value.
CONTAINER_ENV=(RAILWAY_TOKEN RAILWAY_PROJECT_ID RAILWAY_ENVIRONMENT_ID RAILWAY_SERVICE_ID SERVICE_LABEL)
IPV4='^(25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])(\.(25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])){3}$'
IFACE='^[A-Za-z0-9][A-Za-z0-9_.-]{0,14}$'
WAIT_SECONDS=300

# Progress lines. A closed terminal must not stop the install half way.
say() { printf '  %s\n' "$*" || true; }
die() { printf 'refusing: %s\n' "$*" >&2; exit 1; }

# A named point in the install. Does nothing here; the tests redefine it after
# sourcing to fail or signal at that point. No argument or variable reaches it.
fault() { :; }

# DIR must be a real directory owned by UID that nobody else can write to.
check_root_dir() {
  local dir="$1" uid="$2" owner mode
  [[ -d "$dir" && ! -L "$dir" ]] || die "$dir is not a real directory"
  read -r owner mode < <(stat -c '%u %a' -- "$dir")
  [[ "$owner" == "$uid" ]] || die "$dir is not owned by uid $uid"
  (( (8#$mode & 8#022) == 0 )) || die "$dir is writable by group or others"
}

# STAGE must be a real directory of OWNER with no group or other permissions.
# Its contents are not trusted here; only the root copy is checked and used.
check_stage() {
  local stage="$1" owner="$2" uid mode
  [[ -d "$stage" && ! -L "$stage" ]] || die "$stage is not a real directory"
  read -r uid mode < <(stat -c '%u %a' -- "$stage")
  [[ "$uid" == "$owner" ]] || die "$stage is not owned by uid $owner"
  (( (8#$mode & 8#077) == 0 )) || die "$stage is open to group or others (mode $mode)"
}

# Hold the install lock on fd 9 until exit, or refuse at once.
take_lock() {
  local owner mode
  check_root_dir "$(dirname -- "$LOCK")" "$ROOT_UID"
  if [[ -e "$LOCK" || -L "$LOCK" ]]; then
    [[ -f "$LOCK" && ! -L "$LOCK" ]] || die "$LOCK is not a regular file"
    read -r owner mode < <(stat -c '%u %a' -- "$LOCK")
    [[ "$owner" == "$ROOT_UID" ]] || die "$LOCK is not owned by uid $ROOT_UID"
    (( (8#$mode & 8#022) == 0 )) || die "$LOCK is writable by group or others"
  fi
  exec 9>>"$LOCK"
  flock -n 9 || die "another railway-bridge install is running"
}

# Remove copies an interrupted run left behind. Only called under the lock, so
# no other run can own them.
sweep_incoming() {
  local dir
  for dir in "$RELEASES"/.incoming.*; do
    [[ -d "$dir" && ! -L "$dir" ]] || continue
    rm -rf -- "$dir"
    say "removed $dir, left by an interrupted run"
  done
}

copy_stage() {
  local stage="$1" new="$2" owner="$3"
  # The SSH user owns the stage's parent, so the path could be swapped for a
  # link after check_stage. Enter it once, re-check what we are actually in,
  # and copy from "." so the copy reads the directory that was checked.
  (
    cd -P -- "$stage" || die "cannot enter $stage"
    check_stage . "$owner"
    # -R with --no-dereference copies a link as a link and a FIFO or device as
    # itself, never what it points at; check_tree then refuses it. No mode or
    # owner of the stage survives: everything is root's, directories 700,
    # files 600, so no setuid bit or open permission can come along.
    cp -R --no-dereference --no-preserve=all -- ./. "$new/"
  )
  find "$new" -type d -exec chmod 700 -- {} +
  find "$new" -type f -exec chmod 600 -- {} +
}

# The container runs as nobody (65534) and bind-mounts railway_bridge.py, which
# keeps the host file's owner and mode. It holds no secret, so it is made
# world-readable; Docker resolves the mount path as root, so the 700
# directories above it do not stop the container reading it. Only called after
# check_tree and verify_manifest, and it re-checks here, so chmod (which
# follows links) can only ever touch the regular file in the root-only copy.
open_script() {
  local file="$1/railway_bridge.py"
  [[ -f "$file" && ! -L "$file" ]] || die "railway_bridge.py is not a regular file"
  chmod 644 -- "$file"
}

# DIR must hold exactly the payload: these paths and types, nothing else.
check_tree() {
  local dir="$1" name type path entry
  local -A expected=(
    [compose.yaml]=f [railway_bridge.py]=f [railway-bridge.env]=f
    [deployment.env]=f [$MANIFEST]=f [systemd]=d
    [systemd/railway-bridge-reconcile.sh]=f
    [systemd/railway-bridge-reconcile.service]=f
  ) seen=()
  while IFS= read -r -d '' entry; do
    path="${entry#"$dir"/}"
    [[ "$path" =~ ^[A-Za-z0-9._][A-Za-z0-9._/-]*$ && "/$path/" != */../* ]] \
      || die "the payload has a path containing a control character or unsafe name"
    if [[ -d "$entry" && ! -L "$entry" ]]; then
      type=d
    elif [[ -f "$entry" && ! -L "$entry" ]]; then
      type=f
    else
      type=other
    fi
    [[ "${expected[$path]:-}" == "$type" ]] \
      || die "unexpected entry in the payload: $path (type $type)"
    seen[$path]=1
  done < <(find "$dir" -mindepth 1 -print0)
  for name in "${!expected[@]}"; do
    [[ -n "${seen[$name]:-}" ]] || die "$name is missing from the payload"
  done
}

# The manifest in DIR must hash to EXPECTED and list every other file in DIR,
# once, sorted, each with its own SHA-256. Nothing else in DIR is read first.
verify_manifest() {
  local dir="$1" expected="$2" actual line hash path previous="" count=0 entry
  local -A listed=()
  actual="$(sha256sum -- "$dir/$MANIFEST")"
  [[ "${actual%% *}" == "$expected" ]] || die "the manifest does not have the hash deploy.sh computed"
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ "$line" =~ ^([0-9a-f]{64})\ \ ([A-Za-z0-9._][A-Za-z0-9._/-]*)$ ]] \
      || die "the manifest has a line that is not 'SHA256  PATH'"
    hash="${BASH_REMATCH[1]}" path="${BASH_REMATCH[2]}"
    [[ "/$path/" != */../* && "$path" != "$MANIFEST" ]] || die "the manifest lists $path"
    [[ -z "$previous" || "$path" > "$previous" ]] || die "the manifest is not sorted, or lists $path twice"
    [[ -f "$dir/$path" && ! -L "$dir/$path" ]] || die "the manifest lists $path, which is not in the payload"
    actual="$(sha256sum -- "$dir/$path")"
    [[ "${actual%% *}" == "$hash" ]] || die "$path does not match the manifest"
    listed[$path]=1 previous="$path" count=$((count + 1))
  done < "$dir/$MANIFEST"
  (( count > 0 )) || die "the manifest is empty"
  while IFS= read -r -d '' entry; do
    path="${entry#"$dir"/}"
    [[ "$path" =~ ^[A-Za-z0-9._][A-Za-z0-9._/-]*$ && "/$path/" != */../* ]] \
      || die "the payload has a file containing a control character or unsafe name"
    [[ "$path" == "$MANIFEST" || -n "${listed[$path]:-}" ]] || die "$path is not in the manifest"
  done < <(find "$dir" -type f -print0)
}

# True when the dotted quad (already shape-checked) is in 10/8, 172.16/12,
# 192.168/16 or 100.64/10: the same allow-list render.py applies. The root side
# checks again rather than trusting the desktop.
private_ipv4() {
  local a b rest
  IFS=. read -r a b rest <<<"$1"
  (( a == 10 )) && return 0
  (( a == 172 && b >= 16 && b <= 31 )) && return 0
  (( a == 192 && b == 168 )) && return 0
  (( a == 100 && b >= 64 && b <= 127 )) && return 0
  return 1
}

# railway-bridge.env: exactly the five container lines, in order, each with a
# value. deployment.env: exactly the bind address and interface. Values are
# matched, never printed. Sets bind_ip for check_compose.
check_env() {
  local env="$1/railway-bridge.env" deployment="$1/deployment.env" i=0 name line
  local -a lines=()
  mapfile -t lines < "$env"
  (( ${#lines[@]} == ${#CONTAINER_ENV[@]} )) \
    || die "railway-bridge.env is not exactly the ${#CONTAINER_ENV[@]} lines the container reads"
  for name in "${CONTAINER_ENV[@]}"; do
    [[ "${lines[i]}" == "$name="?* ]] \
      || die "railway-bridge.env is not exactly the ${#CONTAINER_ENV[@]} lines the container reads"
    i=$((i + 1))
  done
  mapfile -t lines < "$deployment"
  (( ${#lines[@]} == 2 )) || die "deployment.env is not exactly the bind address and interface"
  line="${lines[0]#RAILWAY_BRIDGE_BIND_IP=}"
  [[ "${lines[0]}" == RAILWAY_BRIDGE_BIND_IP=* && "$line" =~ $IPV4 ]] && private_ipv4 "$line" \
    || die "deployment.env does not name one private IPv4 bind address"
  bind_ip="$line"
  line="${lines[1]#RAILWAY_BRIDGE_BIND_INTERFACE=}"
  [[ "${lines[1]}" == RAILWAY_BRIDGE_BIND_INTERFACE=* && "$line" =~ $IFACE ]] \
    || die "deployment.env does not name a safe interface"
}

compose() {
  local dir="$1"
  shift
  docker compose --env-file "$dir/deployment.env" --project-directory "$dir" \
    -f "$dir/compose.yaml" "$@"
}

check_compose() {
  local dir="$1" services images ports
  compose "$dir" config --quiet || die "docker compose rejects the new compose.yaml"
  services="$(compose "$dir" config --services | sort)"
  [[ "$services" == "$SERVICES" ]] || die "the only service is not railway-bridge"
  images="$(compose "$dir" config --images | sort)"
  [[ "$images" == "$IMAGES" ]] || die "the image is not the approved digest"
  # Exactly one published port: tcp 8098 on the bind address, nothing else.
  ports="$(compose "$dir" config --format json | python3 -I -c '
import json, sys
services = json.load(sys.stdin)["services"]
for name, service in sorted(services.items()):
    for port in service.get("ports", []):
        print(name, port.get("host_ip", ""), port.get("published", ""), port.get("target", ""), port.get("protocol", "tcp"))
')" || die "could not read the published ports from docker compose"
  [[ "$ports" == "railway-bridge $bind_ip 8098 8098 tcp" ]] \
    || die "the only published port is not tcp 8098 on the bind address"
}

check_managed_destination() {
  local path="$1" owner mode
  [[ ! -e "$path" && ! -L "$path" ]] && return 0
  [[ -f "$path" && ! -L "$path" ]] || die "$path is not a regular file"
  read -r owner mode < <(stat -c '%u %a' -- "$path")
  [[ "$owner" == "$ROOT_UID" ]] || die "$path is not owned by uid $ROOT_UID"
  (( (8#$mode & 8#022) == 0 )) || die "$path is writable by group or others"
}

remove_managed_temporary() {
  [[ -n "$managed_temporary" ]] || return 0
  if rm -f -- "$managed_temporary"; then
    managed_temporary=""
    return 0
  fi
  return 1
}

install_managed_file() {
  local source="$1" destination="$2" mode="$3" temporary
  managed_temporary="$(mktemp "$(dirname -- "$destination")/.railway-bridge-reconcile.XXXXXXXXXX")"
  temporary="$managed_temporary"
  if ! install -m "$mode" -- "$source" "$temporary"; then
    remove_managed_temporary
    return 1
  fi
  if ! mv -T -- "$temporary" "$destination"; then
    remove_managed_temporary
    return 1
  fi
  managed_temporary=""
}

restore_managed_file() {
  local source="$1" destination="$2" temporary
  managed_temporary="$(mktemp "$(dirname -- "$destination")/.railway-bridge-reconcile.XXXXXXXXXX")"
  temporary="$managed_temporary"
  rm -f -- "$temporary"
  if ! cp -a -- "$source" "$temporary"; then
    remove_managed_temporary
    return 1
  fi
  if ! mv -T -- "$temporary" "$destination"; then
    remove_managed_temporary
    return 1
  fi
  managed_temporary=""
}

systemd_state() {
  local state status
  if state="$(systemctl is-enabled "$SERVICE" 2>/dev/null)"; then
    status=0
  else
    status=$?
  fi
  if [[ "$state" == enabled && "$status" == 0 ]]; then
    printf 'enabled\n'
  elif [[ "$state" == disabled && "$status" == 1 ]]; then
    printf 'disabled\n'
  elif [[ "$state" == not-found && "$status" == 4 \
          && ! -e "$HELPER" && ! -L "$HELPER" && ! -e "$UNIT" && ! -L "$UNIT" ]]; then
    printf 'absent\n'
  else
    die "unsupported prior $SERVICE enablement state: ${state:-unknown} (exit $status)"
  fi
}

# Save and atomically replace the two root-owned boot artifacts. Cleanup puts
# the exact previous files and enablement back on every later failure.
install_systemd() {
  local release="$1" helper_dir
  helper_dir="$(dirname -- "$HELPER")"
  if [[ ! -e "$helper_dir" && ! -L "$helper_dir" ]]; then
    check_root_dir "$(dirname -- "$helper_dir")" "$ROOT_UID"
    helper_dir_created=1
  else
    check_root_dir "$helper_dir" "$ROOT_UID"
  fi
  check_root_dir "$(dirname -- "$UNIT")" "$ROOT_UID"
  check_managed_destination "$HELPER"
  check_managed_destination "$UNIT"
  [[ -n "$systemd_previous_state" ]] || systemd_previous_state="$(systemd_state)"
  systemd_backup="$(mktemp -d "$RELEASES/.railway-bridge-systemd-backup.XXXXXXXXXX")"
  [[ ! -e "$HELPER" && ! -L "$HELPER" ]] || cp -a -- "$HELPER" "$systemd_backup/helper"
  [[ ! -e "$UNIT" && ! -L "$UNIT" ]] || cp -a -- "$UNIT" "$systemd_backup/unit"
  systemd_changed=1
  (( ! helper_dir_created )) || mkdir -m 755 -- "$helper_dir"
  install_managed_file "$release/systemd/railway-bridge-reconcile.sh" "$HELPER" 755
  install_managed_file "$release/systemd/railway-bridge-reconcile.service" "$UNIT" 644
  systemctl daemon-reload || die "systemctl daemon-reload failed; restoring the previous boot files"
  systemctl enable "$SERVICE" >/dev/null \
    || die "could not enable $SERVICE; restoring the previous boot files"
  say "installed and enabled $SERVICE"
}

restore_systemd() {
  local failed=0 restore_action=disable
  (( systemd_changed )) || return 0
  # Disable first, while the unit file this install wrote still exists: real
  # systemctl cannot reliably remove the wants symlink of a unit whose file is
  # already gone, and is-enabled can then report not-found over a dangling one.
  # When it was enabled before, this also drops any link only the new unit's
  # [Install] made; the old unit is re-enabled below.
  if [[ -f "$UNIT" ]]; then
    systemctl disable "$SERVICE" >/dev/null 2>&1 || failed=1
  fi
  if [[ -f "$systemd_backup/helper" ]]; then
    restore_managed_file "$systemd_backup/helper" "$HELPER" || failed=1
  else
    rm -f -- "$HELPER" || failed=1
  fi
  if [[ -f "$systemd_backup/unit" ]]; then
    restore_managed_file "$systemd_backup/unit" "$UNIT" || failed=1
  else
    rm -f -- "$UNIT" || failed=1
  fi
  systemctl daemon-reload || failed=1
  if [[ "$systemd_previous_state" == enabled ]]; then
    restore_action=enable
    systemctl enable "$SERVICE" >/dev/null 2>&1 || failed=1
  fi
  # Whatever systemctl says, there must be no enablement link left unless the
  # unit was enabled before.
  if [[ "$systemd_previous_state" != enabled ]] \
      && [[ -e "$WANTS" || -L "$WANTS" ]]; then
    failed=1
  fi
  if [[ "$systemd_previous_state" == absent ]] \
      && [[ "$(systemd_state 2>/dev/null || true)" != absent ]]; then
    failed=1
  fi
  if (( helper_dir_created )); then
    rmdir -- "$(dirname -- "$HELPER")" || failed=1
  fi
  if (( failed )); then
    if [[ "$systemd_previous_state" == absent ]]; then
      recovery "Could not completely restore the previous systemd state. The recovery backup was retained." \
        "backup: $systemd_backup" \
        "$HELPER and $UNIT were absent before this install; remove them before rebooting" \
        "run: systemctl disable $SERVICE" \
        "verify: systemctl is-enabled $SERVICE must report not-found"
    else
      recovery "Could not completely restore the previous systemd state. The recovery backup was retained." \
        "backup: $systemd_backup" \
        "restore $HELPER and $UNIT from it before rebooting" \
        "restore enablement with: systemctl $restore_action $SERVICE"
    fi
  else
    say "restored the previous $SERVICE files and enablement"
    systemd_changed=0
    helper_dir_created=0
  fi
  return "$failed"
}

restore_publication() {
  local current
  (( release_changed )) || return 0
  current="$(readlink -- "$LINK" 2>/dev/null || true)"
  if [[ -n "$previous" ]]; then
    if [[ "$current" != "$previous" ]] && ! publish "$previous"; then
      recovery "Could not restore the previous release link." \
        "ln -s $previous $OPT/.railway-bridge.link-new && mv -T $OPT/.railway-bridge.link-new $LINK"
      return 1
    fi
  elif [[ -e "$LINK" || -L "$LINK" ]]; then
    if [[ "$current" != "$published_release" ]] || ! rm -f -- "$LINK"; then
      recovery "Could not remove the first-install release link." \
        "inspect $LINK and remove it only if it points at $published_release"
      return 1
    fi
  fi
  release_changed=0
  say "restored the previous release link"
}

restore_containers() {
  (( start_attempted && ! containers_restored )) || return 0
  if [[ -n "$previous" ]]; then
    compose "$LINK" up --detach --force-recreate --wait --wait-timeout "$WAIT_SECONDS" || return 1
  else
    compose "$published_release" down --remove-orphans || return 1
  fi
  containers_restored=1
}

commit_transaction() {
  fault commit-armed
  trap '' INT TERM
  # Clear rollback state before deleting its only recovery copy. Signals are
  # ignored until deletion is complete, so they cannot split this ordering.
  release_changed=0
  systemd_changed=0
  transaction_committed=1
  helper_dir_created=0
  containers_restored=1
  unpublished=""
  fault commit-disarmed || true
  if rm -rf -- "$systemd_backup"; then
    systemd_backup=""
  else
    say "warning: committed successfully but could not remove $systemd_backup"
  fi
  fault commit-backup-removed || true
  trap 'exit 130' INT TERM
}

# Print the release /opt/railway-bridge points at, or nothing when there is
# none. Anything but a link to a real, root-owned release under RELEASES is
# refused.
current_release() {
  local target owner mode
  if [[ ! -e "$LINK" && ! -L "$LINK" ]]; then
    return 0
  fi
  [[ -L "$LINK" ]] || die "$LINK exists and is not a symlink; move it aside by hand first"
  target="$(readlink -- "$LINK")"
  [[ "${target%/*}" == "$RELEASES" && "${target##*/}" =~ $RELEASE_PATTERN ]] \
    || die "$LINK points at $target, which is not a release in $RELEASES"
  [[ -d "$target" && ! -L "$target" ]] || die "$LINK points at $target, which is not a real directory"
  read -r owner mode < <(stat -c '%u %a' -- "$target")
  [[ "$owner" == "$ROOT_UID" ]] || die "$target is not owned by uid $ROOT_UID"
  (( (8#$mode & 8#022) == 0 )) || die "$target is writable by group or others"
  check_managed_destination "$target/compose.yaml"
  printf '%s\n' "$target"
}

# Point LINK at RELEASE in one rename. Before and after, LINK is a whole
# release. Returns non-zero, with LINK unchanged, if it could not.
publish() {
  local release="$1"
  tmp_link="$OPT/.railway-bridge.link-new"
  if [[ -e "$tmp_link" || -L "$tmp_link" ]]; then
    # Only root can write in OPT, so this is left from an interrupted run.
    [[ -L "$tmp_link" ]] || die "$tmp_link exists and is not a symlink"
    rm -f -- "$tmp_link" || return 1
  fi
  ln -s -- "$release" "$tmp_link" || return 1
  fault link-made || return 1
  mv -T -- "$tmp_link" "$LINK" || return 1
  tmp_link=""
}

recovery() {
  printf '\nRECOVERY NEEDED. %s\n' "$1" >&2
  shift
  printf '  %s\n' "$@" >&2
}

# The commands that point LINK at RELEASE by hand and recreate from it.
manual_rollback() {
  printf '%s\n' \
    "ln -s $1 $OPT/.railway-bridge.link-new && mv -T $OPT/.railway-bridge.link-new $LINK" \
    "docker compose --env-file $LINK/deployment.env -f $LINK/compose.yaml up --detach --force-recreate --wait"
}

# Rename a release that did not come up, so it can never be linked again.
mark_failed() {
  local release="$1"
  if mv -T -- "$release" "$release.failed"; then
    printf 'the failed release is kept as %s\n' "$release.failed" >&2
  else
    printf 'warning: could not rename %s to %s.failed\n' "$release" "$release" >&2
  fi
}

# Recreate the container from LINK. On failure go back to OLD, or for a first
# install take the failed project down. Exits 1 on every failure path.
start_release() {
  local release="$1" old="$2"
  local -a by_hand
  phase=start
  fault starting
  say "recreating railway-bridge"
  start_attempted=1
  if compose "$LINK" up --detach --force-recreate --wait --wait-timeout "$WAIT_SECONDS"; then
    fault started
    phase=done
    say "railway-bridge is up and healthy"
    return 0
  fi
  if [[ -z "$old" ]]; then
    printf 'the first install did not come up healthy; taking it down\n' >&2
    if ! compose "$LINK" down --remove-orphans; then
      phase=done
      recovery "The first install did not come up healthy and could not be taken down." \
        "docker compose --env-file $LINK/deployment.env -f $LINK/compose.yaml ps" \
        "docker compose --env-file $LINK/deployment.env -f $LINK/compose.yaml down --remove-orphans"
      exit 1
    fi
    containers_restored=1
    rm -f -- "$LINK"
    mark_failed "$release"
    phase=done
    printf 'nothing is running and %s is gone again\n' "$LINK" >&2
    exit 1
  fi
  printf 'the new release did not come up healthy; pointing %s back at %s\n' "$LINK" "$old" >&2
  if ! publish "$old"; then
    phase=done
    mapfile -t by_hand < <(manual_rollback "$old")
    recovery "$LINK still points at the failed release $release and could not be pointed back." \
      "${by_hand[@]}"
    exit 1
  fi
  mark_failed "$release"
  if ! compose "$LINK" up --detach --force-recreate --wait --wait-timeout "$WAIT_SECONDS"; then
    phase=done
    recovery "$LINK points at the previous release $old again, but it did not come up healthy either." \
      "docker compose --env-file $LINK/deployment.env -f $LINK/compose.yaml ps" \
      "docker compose --env-file $LINK/deployment.env -f $LINK/compose.yaml logs --tail 100"
    exit 1
  fi
  containers_restored=1
  phase=done
  printf 'rolled back: the previous release is running again\n' >&2
  exit 1
}

# What the EXIT trap removes or reports. Global so the trap can see them.
incoming="" unpublished="" tmp_link="" managed_temporary="" phase="" previous="" published_release="" bind_ip=""
systemd_backup="" systemd_changed=0 systemd_previous_state="" helper_dir_created=0
release_changed=0 start_attempted=0 containers_restored=0
transaction_committed=0

cleanup() {
  local status=$? removed=0 restored=1 temp_ready=1
  local -a by_hand=()
  trap '' INT TERM
  set +e
  # Remove an interrupted file-operation temporary before rollback can start a
  # second managed-file operation and overwrite the only path to the first.
  if ! remove_managed_temporary; then
    status=1
    restored=0
    temp_ready=0
    recovery "Could not remove an interrupted managed-file temporary." \
      "inspect and remove: $managed_temporary"
  fi
  if ! restore_publication; then
    status=1
    restored=0
  fi
  if (( restored )) && ! restore_containers; then
    status=1
    restored=0
    recovery "Could not restore the previous container state." \
      "docker compose --env-file $LINK/deployment.env -f $LINK/compose.yaml up --detach --force-recreate --wait"
  fi
  if (( temp_ready )) && ! restore_systemd; then
    status=1
    restored=0
  fi
  if ! remove_managed_temporary; then
    status=1
    restored=0
    recovery "Could not remove a restoration temporary." \
      "inspect and remove: $managed_temporary"
  fi
  if (( restored && ! transaction_committed )) \
      && [[ -n "$systemd_backup" && -d "$systemd_backup" && ! -L "$systemd_backup" ]]; then
    if rm -rf -- "$systemd_backup"; then
      systemd_backup=""
    else
      status=1
      recovery "Could not remove the now-unneeded systemd backup." "inspect $systemd_backup"
    fi
  fi
  if [[ -n "$tmp_link" && -L "$tmp_link" ]]; then
    rm -f -- "$tmp_link"
  fi
  if [[ -n "$incoming" && -d "$incoming" && ! -L "$incoming" ]]; then
    rm -rf -- "$incoming"
    removed=1
  fi
  # Never remove what LINK points at, even if the rename landed just before
  # a signal arrived.
  if [[ -n "$unpublished" && -d "$unpublished" && ! -L "$unpublished" \
        && "$(readlink -- "$LINK" 2>/dev/null)" != "$unpublished" ]]; then
    rm -rf -- "$unpublished"
    removed=1
  fi
  if (( restored )) && [[ -n "$published_release" && -d "$published_release" && ! -L "$published_release" \
        && "$(readlink -- "$LINK" 2>/dev/null)" != "$published_release" ]]; then
    rm -rf -- "$published_release" || status=1
    removed=1
  fi
  if (( removed )); then
    printf 'nothing was changed: %s and the container are as they were\n' "$LINK" >&2
  fi
  if [[ "$phase" == start && ! restored ]]; then
    [[ -z "$previous" ]] || mapfile -t by_hand < <(manual_rollback "$previous")
    recovery "Stopped while the container was being recreated." \
      "$LINK points at: $(readlink -- "$LINK" 2>/dev/null || echo nothing)" \
      "the previous release: ${previous:-none}" \
      "docker compose --env-file $LINK/deployment.env -f $LINK/compose.yaml ps" \
      "${by_hand[@]}"
  fi
  exit "$status"
}

main() {
  local stage start manifest_hash owner old release name
  exec </dev/null
  # Nothing below may pick anything up from the SSH user's directory, and the
  # Docker CLI reads root's own config whatever HOME sudo passed through.
  cd /
  export HOME=/root DOCKER_CONFIG=/root/.docker
  (( $# == 3 )) || die "usage: install.sh STAGE START MANIFEST_SHA256"
  stage="$1" start="$2" manifest_hash="$3"
  [[ "$start" == 0 || "$start" == 1 ]] || die "START must be 0 or 1"
  [[ "$stage" =~ $STAGE_PATTERN && "$stage" != *..* ]] || die "unexpected stage path: $stage"
  [[ "$manifest_hash" =~ ^[0-9a-f]{64}$ ]] || die "the manifest hash is not 64 hex digits"
  [[ "$(id -u)" == "$ROOT_UID" ]] || die "run this through sudo, as deploy.sh does"
  owner="${SUDO_UID:-}"
  [[ "$owner" =~ ^[0-9]+$ ]] || die "SUDO_UID is not set; run this through sudo"
  command -v docker >/dev/null || die "docker is not installed"
  docker compose version >/dev/null || die "docker compose is not available"
  command -v systemctl >/dev/null || die "systemctl is not installed"
  command -v install >/dev/null || die "install is not installed"
  command -v flock >/dev/null || die "flock is not installed"
  command -v python3 >/dev/null || die "python3 is not installed"
  trap '' HUP

  take_lock
  check_root_dir "$OPT" "$ROOT_UID"
  systemd_previous_state="$(systemd_state)"
  if [[ ! -e "$RELEASES" && ! -L "$RELEASES" ]]; then
    mkdir -m 700 -- "$RELEASES"
  fi
  check_root_dir "$RELEASES" "$ROOT_UID"
  (( (8#$(stat -c %a -- "$RELEASES") & 8#077) == 0 )) || die "$RELEASES is open to group or others"
  old="$(current_release)"
  previous="$old"
  check_stage "$stage" "$owner"
  sweep_incoming

  trap cleanup EXIT
  trap 'exit 130' INT TERM
  incoming="$(mktemp -d "$RELEASES/.incoming.XXXXXXXXXX")"
  copy_stage "$stage" "$incoming" "$owner"
  fault copied
  check_tree "$incoming"
  verify_manifest "$incoming" "$manifest_hash"
  open_script "$incoming"
  say "payload copied and every file matches the manifest"
  fault verified
  check_env "$incoming"
  check_compose "$incoming"
  say "docker compose accepts it; the image is the approved digest"
  fault checked
  compose "$incoming" pull --quiet || die "could not pull the image; nothing changed"
  say "image pulled"
  fault pulled
  install_systemd "$incoming"
  fault systemd-installed

  name="$(date -u +%Y%m%dT%H%M%SZ)-${manifest_hash:0:12}"
  release="$RELEASES/$name"
  [[ ! -e "$release" && ! -L "$release" && ! -e "$release.failed" ]] || die "$release already exists"
  unpublished="$release"
  mv -T -- "$incoming" "$release"
  incoming=""
  fault released
  published_release="$release"
  release_changed=1
  publish "$release"
  unpublished=""
  say "published $LINK -> $release"
  [[ -z "$old" ]] || say "the previous release is $old"
  fault published

  if (( start )); then
    start_release "$release" "$old"
  else
    say "no container was started or recreated"
  fi
  commit_transaction
}

if ! (return 0 2>/dev/null); then
  main "$@"
fi
