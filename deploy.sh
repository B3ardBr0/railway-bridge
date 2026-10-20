#!/usr/bin/env bash
# Deploy railway-bridge to a Docker host. I run this from the desktop.
#
#   ./deploy.sh           render, stage, pull and install; starts nothing
#   ./deploy.sh --start   the same, then create or recreate the container
#
# Settings come from RAILWAY_BRIDGE_SECRETS (default
# ~/.config/railway-bridge/deploy.env); render.py documents them. When
# RAILWAY_BRIDGE_TARGET_NAME is set, the target must answer `uname -n` with
# exactly that name before anything is uploaded, so a wrong SSH alias cannot
# land this on another machine.
#
# This script deploys; it does not test. Run the test suite yourself first,
# as its own step. A deploy script that runs the tests, while the tests run the
# deploy script, recurses without end: that once exhausted a desktop's memory.
#
# Secrets never live in this repo. render.py fills them into a private local
# mktemp directory and writes manifest.sha256 beside them; the manifest's
# SHA-256 is computed here. Only the rendered files travel, as a tar stream on
# SSH's stdin, into a private remote mktemp directory, never in argv.
# remote/install.sh is never uploaded: lib/remote_script.sh sends its text
# base64-encoded inside the SSH command, and the host decodes it straight into
# sudo /bin/bash -s with the stage, START and the manifest hash. Read
# remote/install.sh for what changes on the host. The stage is removed
# afterwards, whatever the installer did.

set -euo pipefail

STAGE_PATTERN='^/[A-Za-z0-9._/-]*/railway-bridge-stage\.[A-Za-z0-9]{10}$'
start=0

case "${1:-}" in
  "") ;;
  --start) start=1 ;;
  -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
  *) echo "unknown option: $1" >&2; exit 2 ;;
esac
if (( $# > 1 )); then
  echo "too many arguments" >&2
  exit 2
fi

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/private_dirs.sh
. "$here/lib/private_dirs.sh"
# shellcheck source=lib/remote_script.sh
. "$here/lib/remote_script.sh"

# The settings are checked before anything is made or contacted.
HOST="$(python3 "$here/render.py" target-host)"
target_name="$(python3 "$here/render.py" target-name)"
# Parse only, so a syntax error stops this here. Nothing is executed or written.
python3 -c 'import ast, sys; ast.parse(open(sys.argv[1], "rb").read(), sys.argv[1])' \
  "$here/railway_bridge.py"
# Encode once now, so a problem with the installer stops this before SSH.
remote_script_command "$here/remote/install.sh" >/dev/null

rendered="$(private_dir_open)"
control_dir=""
ssh_opts=()
stage=""

# The stage holds rendered secrets. Remove it by its checked name; say so if
# that fails, without showing anything in it.
remove_stage() {
  [[ -n "$stage" ]] || return 0
  if ! ssh "${ssh_opts[@]}" "$HOST" "rm -rf -- '$stage'"; then
    echo "warning: could not remove $HOST:$stage, which holds rendered secrets; remove it by hand" >&2
  fi
  stage=""
}

cleanup() {
  remove_stage
  if [[ -n "$control_dir" ]]; then
    ssh -O exit "${ssh_opts[@]}" "$HOST" 2>/dev/null || true
    control_dir_close rb.sock
  fi
  rm -rf -- "$rendered"
}
trap cleanup EXIT

(umask 077 && python3 "$here/render.py" "$rendered" >/dev/null)
manifest_hash="$(sha256sum < "$rendered/manifest.sha256")"
manifest_hash="${manifest_hash%% *}"
echo "  ok  rendered into a private temporary directory"

control_dir_open
ssh_opts=(-o ControlMaster=auto -o ControlPath="$control_dir/rb.sock" -o ControlPersist=300)

# One remote command checks the name, if one is set, and only then makes the
# stage, so nothing is created on a host that is not the target.
remote='umask 077; mktemp -d "${TMPDIR:-/tmp}/railway-bridge-stage.XXXXXXXXXX"'
if [[ -n "$target_name" ]]; then
  remote="n=\"\$(uname -n)\"; [ \"\$n\" = '$target_name' ] || { echo \"refusing: this host answers as '\$n', not $target_name\" >&2; exit 1; }; $remote"
fi
made="$(ssh "${ssh_opts[@]}" "$HOST" "$remote")"
if [[ ! "$made" =~ $STAGE_PATTERN || "$made" == *..* ]]; then
  echo "refusing: unexpected remote temporary directory: '$made'" >&2
  exit 1
fi
stage="$made"
if ! tar -C "$rendered" -cf - manifest.sha256 compose.yaml railway_bridge.py \
      railway-bridge.env deployment.env systemd \
    | ssh "${ssh_opts[@]}" "$HOST" "umask 077 && tar -C '$stage' -xf -"; then
  echo "refusing: could not upload the railway-bridge files to $HOST" >&2
  exit 1
fi
rm -rf -- "$rendered"

if (( start )); then
  echo "installing on $HOST, then recreating railway-bridge"
else
  echo "installing on $HOST; no container is started or recreated"
fi
echo "(sudo may ask for your password there)"
status=0
ssh -t "${ssh_opts[@]}" "$HOST" \
  "$(remote_script_command "$here/remote/install.sh" "$stage" "$start" "$manifest_hash")" \
  || status=$?
remove_stage

if (( status )); then
  echo "the installer on $HOST failed (exit $status); its messages above say what changed" >&2
  exit "$status"
fi
echo
if (( ! start )); then
  echo "Nothing was recreated. A running railway-bridge keeps serving the release"
  echo "it started with until ./deploy.sh --start recreates it."
fi
echo "Verify from a machine that can reach the bind address:"
echo "  curl -s http://<bind address>:8098/health"
echo "  curl -s http://<bind address>:8098/prometheus | grep -c '^railway_'"
