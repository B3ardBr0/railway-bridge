# Sourced by deploy.sh (bash). SSH control sockets and rendered secrets each
# live in a fresh private directory, never at a predictable /tmp name.
#
#   control_dir_open               sets control_dir: made by mktemp, a real
#                                  directory of mine, mode 700
#   control_dir_close SOCKET...    removes those sockets, then the directory
#   private_dir_open               prints a new mktemp directory of mine, mode
#                                  700, for rendered files; the caller removes it
#
# Control sockets are removed by name and rmdir, never rm -rf.

control_dir_open() {
  control_dir="$(mktemp -d "${TMPDIR:-/tmp}/rbctl.XXXXXXXXXX")"
  if [[ ! -d "$control_dir" || -L "$control_dir" || ! -O "$control_dir" ]]; then
    echo "refusing: $control_dir is not a real directory of mine" >&2
    exit 1
  fi
  chmod 700 -- "$control_dir"
  if [[ "$(stat -c %a -- "$control_dir")" != 700 ]]; then
    echo "refusing: $control_dir is not mode 700" >&2
    rmdir -- "$control_dir" 2>/dev/null || true
    exit 1
  fi
}

control_dir_close() {
  local socket
  [[ -n "${control_dir:-}" ]] || return 0
  for socket in "$@"; do
    rm -f -- "$control_dir/$socket"
  done
  rmdir -- "$control_dir" || echo "warning: could not remove $control_dir; remove it by hand" >&2
}

# Make a private local directory for rendered files: mktemp, mine, mode 700.
private_dir_open() {
  local made
  made="$(mktemp -d "${TMPDIR:-/tmp}/railway-bridge-render.XXXXXXXXXX")"
  if [[ ! -d "$made" || -L "$made" || ! -O "$made" ]]; then
    echo "refusing: $made is not a real directory of mine" >&2
    exit 1
  fi
  chmod 700 -- "$made"
  if [[ "$(stat -c %a -- "$made")" != 700 ]]; then
    echo "refusing: $made is not mode 700" >&2
    rmdir -- "$made" 2>/dev/null || true
    exit 1
  fi
  printf '%s\n' "$made"
}
