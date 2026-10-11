#!/usr/bin/env bash
# Update railway-bridge on the board and recreate its container.
#
#   ./deploy.sh
#
# Settings come from ~/.config/harbourmaster.env on the desktop:
#   RAILWAY_BRIDGE_HOST            ssh target for the board, e.g. me@192.168.1.10
#   RAILWAY_BRIDGE_TOKEN           the Railway project token (set it with `hm-secret`)
#   RAILWAY_BRIDGE_PROJECT_ID      the project
#   RAILWAY_BRIDGE_ENVIRONMENT_ID  the environment, normally production
#   RAILWAY_BRIDGE_SERVICE_ID      the service
#   RAILWAY_BRIDGE_LABEL           the `service` label on every metric (default: service)
# The env file on the board is rebuilt from these on every deploy, so rotating the
# token is `hm-secret RAILWAY_BRIDGE_TOKEN` and then this script. The token is
# copied in a file readable only by its owner and never appears on a command line.
#
# Docker reads an env file when a container is created, not when it restarts,
# so the container is removed and run again.

set -euo pipefail

APP="/DATA/AppData/railway-bridge"
STAGE="/tmp/railway-bridge-$$"
SECRETS="${HARBOURMASTER_SECRETS:-$HOME/.config/harbourmaster.env}"

setting() {
  local value
  value="$(grep -m1 "^$1=" "$SECRETS" 2>/dev/null | cut -d= -f2- || true)"
  [ -n "$value" ] || [ -n "${2:-}" ] || { echo "no $1 in $SECRETS" >&2; exit 1; }
  echo "${value:-${2:-}}"
}

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
python3 -m py_compile "$here/railway_bridge.py"
python3 -m unittest discover -s "$here/tests" -q

# Assigned one by one so a missing setting stops the script under set -e.
HOST="$(setting RAILWAY_BRIDGE_HOST)"
token="$(setting RAILWAY_BRIDGE_TOKEN)"
project="$(setting RAILWAY_BRIDGE_PROJECT_ID)"
environment="$(setting RAILWAY_BRIDGE_ENVIRONMENT_ID)"
service="$(setting RAILWAY_BRIDGE_SERVICE_ID)"
label="$(setting RAILWAY_BRIDGE_LABEL service)"

local_env="$(mktemp)"
chmod 600 "$local_env"
trap 'rm -f "$local_env"' EXIT
cat >"$local_env" <<EOF
RAILWAY_TOKEN=$token
RAILWAY_PROJECT_ID=$project
RAILWAY_ENVIRONMENT_ID=$environment
RAILWAY_SERVICE_ID=$service
SERVICE_LABEL=$label
POLL_SECONDS=300
BRIDGE_PORT=8098
EOF
unset token

ctl="/tmp/railwaydeploy-$$.sock"
ssh_opts=(-o ControlMaster=auto -o ControlPath="$ctl" -o ControlPersist=300)
trap 'rm -f "$local_env"; ssh -O exit "${ssh_opts[@]}" "$HOST" 2>/dev/null || true' EXIT

ssh "${ssh_opts[@]}" "$HOST" "umask 077 && mkdir -p '$STAGE'"
scp "${ssh_opts[@]}" -q "$here/railway_bridge.py" "$HOST:$STAGE/"
scp "${ssh_opts[@]}" -q "$local_env" "$HOST:$STAGE/railway-bridge.env"

echo "installing (sudo password follows)"
ssh -t "${ssh_opts[@]}" "$HOST" "
  set -e
  trap \"rm -rf '$STAGE'\" EXIT
  sudo mkdir -p '$APP/docker-config'
  sudo install -m 644 '$STAGE/railway_bridge.py' '$APP/railway_bridge.py'
  sudo install -m 600 '$STAGE/railway-bridge.env' '$APP/railway-bridge.env'
  D=\"sudo env DOCKER_CONFIG=$APP/docker-config docker\"
  \$D rm -f railway-bridge >/dev/null 2>&1 || true
  \$D run -d --name railway-bridge --restart unless-stopped --network host \
     --env-file '$APP/railway-bridge.env' -v '$APP/railway_bridge.py:/app/railway_bridge.py:ro' \
     python:3.12-alpine python /app/railway_bridge.py >/dev/null
  \$D ps --filter name=railway-bridge --format '{{.Names}}  {{.Status}}'
"
echo; echo "check:  curl -s http://${HOST#*@}:8098/health"
