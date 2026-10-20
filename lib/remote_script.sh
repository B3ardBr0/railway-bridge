# Sourced by deploy.sh (bash).
#
#   remote_script_command SCRIPT ARG...
#
# Prints the remote command that runs SCRIPT, a file in this repository, as
# root on the far side. Its bytes travel base64-encoded inside the command; the
# remote shell decodes them into a variable, and only if that worked pipes them
# into `sudo /bin/bash -s -- ARG...`. Nothing is uploaded, and nothing on the
# remote host is executed from a file, so nobody there can change what runs.
#
# The command holds no secret: SCRIPT is code and every ARG is a path, a
# number or a hash. Each ARG must be plain (letters, digits, . _ / -) to travel
# single-quoted. Refuses, printing nothing on stdout, if SCRIPT or an ARG is
# unsuitable or the command would not fit in one argument on the remote host.

REMOTE_SCRIPT_MAX=100000

remote_script_command() {
  local script="$1" encoded arg command
  shift
  if [[ ! -f "$script" || -L "$script" ]]; then
    echo "refusing: $script is not a regular file" >&2
    return 1
  fi
  encoded="$(base64 -w 0 -- "$script")"
  if [[ ! "$encoded" =~ ^[A-Za-z0-9+/]+=*$ ]]; then
    echo "refusing: $script did not encode cleanly" >&2
    return 1
  fi
  if (( ${#encoded} > REMOTE_SCRIPT_MAX )); then
    echo "refusing: $script is too large to send inside the SSH command" >&2
    return 1
  fi
  # $( ) drops the script's final line feed, so printf puts one back: bash
  # reads exactly the bytes of SCRIPT.
  command="s=\"\$(printf %s '$encoded' | base64 -d)\" && [ -n \"\$s\" ]"
  command+=" && printf '%s\\n' \"\$s\" | sudo /bin/bash -s --"
  for arg in "$@"; do
    if [[ ! "$arg" =~ ^[A-Za-z0-9._/-]+$ ]]; then
      echo "refusing: argument '$arg' cannot travel to the remote host" >&2
      return 1
    fi
    command+=" '$arg'"
  done
  printf '%s\n' "$command"
}
