#!/usr/bin/env python3
"""Render a railway-bridge release into a private directory.

    python3 render.py OUT          render into OUT (an empty mode-700 directory)
                                   and print the manifest's sha256
    python3 render.py target-host  print the ssh target for deploy.sh
    python3 render.py target-name  print the host name the target must answer
                                   with, or nothing when none is set

Settings come from one local file, by default ~/.config/railway-bridge/deploy.env
(RAILWAY_BRIDGE_SECRETS overrides). It must be a regular file of mine, not a
link, mode 600. It is parsed, never executed: every non-comment line must be
NAME=VALUE, a name may appear once, and a value is kept byte for byte,
including leading and trailing spaces. Lines for other tools are allowed.

Read from the file:
    RAILWAY_BRIDGE_TARGET_HOST      ssh target of the destination host
    RAILWAY_BRIDGE_TARGET_NAME      optional: what `uname -n` must print there;
                                    deploy.sh refuses any other host
    RAILWAY_BRIDGE_BIND_IP          the private IPv4 the exporter listens on
    RAILWAY_BRIDGE_BIND_INTERFACE   the interface that owns that address
    RAILWAY_BRIDGE_TOKEN            the Railway project token
    RAILWAY_BRIDGE_PROJECT_ID, RAILWAY_BRIDGE_ENVIRONMENT_ID,
    RAILWAY_BRIDGE_SERVICE_ID, RAILWAY_BRIDGE_LABEL

Standard library only.
"""

from __future__ import annotations

import hashlib
import ipaddress
import os
from pathlib import Path
import re
import shutil
import stat
import sys

MANIFEST = "manifest.sha256"
FILES = (
    "compose.yaml",
    "railway_bridge.py",
    "systemd/railway-bridge-reconcile.service",
    "systemd/railway-bridge-reconcile.sh",
)
GENERATED = ("deployment.env", "railway-bridge.env")
PAYLOAD = tuple(sorted(FILES + GENERATED, key=str.encode))
DIRECTORIES = ("systemd",)
REQUIRED = (
    "RAILWAY_BRIDGE_TARGET_HOST",
    "RAILWAY_BRIDGE_BIND_IP",
    "RAILWAY_BRIDGE_BIND_INTERFACE",
    "RAILWAY_BRIDGE_TOKEN",
    "RAILWAY_BRIDGE_PROJECT_ID",
    "RAILWAY_BRIDGE_ENVIRONMENT_ID",
    "RAILWAY_BRIDGE_SERVICE_ID",
    "RAILWAY_BRIDGE_LABEL",
)
# What the container reads, in this order, and nothing else.
CONTAINER_ENV = (
    ("RAILWAY_TOKEN", "RAILWAY_BRIDGE_TOKEN"),
    ("RAILWAY_PROJECT_ID", "RAILWAY_BRIDGE_PROJECT_ID"),
    ("RAILWAY_ENVIRONMENT_ID", "RAILWAY_BRIDGE_ENVIRONMENT_ID"),
    ("RAILWAY_SERVICE_ID", "RAILWAY_BRIDGE_SERVICE_ID"),
    ("SERVICE_LABEL", "RAILWAY_BRIDGE_LABEL"),
)
NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
SAFE_PART = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
INTERFACE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,14}")
HOST = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.@-]*")
TARGET_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,62}")
ALLOWED_NETWORKS = tuple(ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10"))


class RenderError(Exception):
    """A refusal; the message says why and never contains a value."""


def default_secrets() -> Path:
    override = os.environ.get("RAILWAY_BRIDGE_SECRETS")
    if override:
        return Path(override)
    return Path.home() / ".config" / "railway-bridge" / "deploy.env"


def read_settings(path: Path) -> dict[str, str]:
    """Parse the settings file without executing it."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        raise RenderError(f"{path}: not found") from None
    if stat.S_ISLNK(info.st_mode):
        raise RenderError(f"{path}: is a symbolic link")
    if not stat.S_ISREG(info.st_mode):
        raise RenderError(f"{path}: is not a regular file")
    if info.st_uid != os.getuid():
        raise RenderError(f"{path}: is not owned by me")
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise RenderError(f"{path}: must be mode 600")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as handle:
        if os.fstat(handle.fileno()).st_ino != info.st_ino:
            raise RenderError(f"{path}: changed while opening")
        data = handle.read()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise RenderError(f"{path}: is not UTF-8") from None
    if "\0" in text or "\r" in text:
        raise RenderError(f"{path}: contains NUL or carriage return")
    settings: dict[str, str] = {}
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    for number, line in enumerate(lines, 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        name, sep, value = line.partition("=")
        if not sep or not NAME.fullmatch(name):
            raise RenderError(f"{path}: line {number} is not NAME=VALUE")
        if name in settings:
            raise RenderError(f"{path}: {name} appears more than once")
        settings[name] = value
    missing = [n for n in REQUIRED if not settings.get(n)]
    if missing:
        raise RenderError(f"{path}: no value for: {' '.join(missing)}")
    return settings


def check_bind_ip(value: str) -> str:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        raise RenderError("RAILWAY_BRIDGE_BIND_IP must be a private IPv4 address") from None
    if address.version != 4 or not any(address in n for n in ALLOWED_NETWORKS):
        raise RenderError("RAILWAY_BRIDGE_BIND_IP must be a private IPv4 address "
                          "(10/8, 172.16/12, 192.168/16 or 100.64/10)")
    return str(address)


def check_interface(value: str) -> str:
    if not INTERFACE.fullmatch(value):
        raise RenderError("RAILWAY_BRIDGE_BIND_INTERFACE is not an interface name")
    return value


def check_host(value: str) -> str:
    if not HOST.fullmatch(value):
        raise RenderError("RAILWAY_BRIDGE_TARGET_HOST is not a plain ssh target")
    return value


def check_target_name(value: str) -> str:
    if value and not TARGET_NAME.fullmatch(value):
        raise RenderError("RAILWAY_BRIDGE_TARGET_NAME is not a plain host name")
    return value


def check_value(name: str, value: str) -> str:
    if "\n" in value or "\0" in value:
        raise RenderError(f"{name} contains a newline")
    return value


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def check_name(relative: str) -> None:
    parts = relative.split("/")
    if not all(SAFE_PART.fullmatch(p) for p in parts):
        raise RenderError(f"unsafe name in payload: {relative!r}")


def payload_files(directory: Path) -> list[str]:
    """Every entry under directory, refusing anything odd. Returns file paths."""
    files: list[str] = []
    seen_dirs: list[str] = []
    for current, dirnames, filenames in os.walk(directory, followlinks=False):
        base = Path(current).relative_to(directory)
        for name in sorted(dirnames + filenames):
            relative = (base / name).as_posix()
            check_name(relative)
            info = os.lstat(Path(current) / name)
            if stat.S_ISDIR(info.st_mode):
                seen_dirs.append(relative)
            elif stat.S_ISREG(info.st_mode):
                files.append(relative)
            else:
                raise RenderError(f"{relative}: not a regular file or directory")
    if sorted(seen_dirs) != sorted(DIRECTORIES):
        raise RenderError("unexpected directory set in payload")
    return sorted(files, key=str.encode)


def manifest_text(directory: Path) -> str:
    files = [f for f in payload_files(directory) if f != MANIFEST]
    if tuple(files) != PAYLOAD:
        raise RenderError("unexpected entry set in payload")
    lines = []
    for relative in files:
        lines.append(f"{sha256((directory / relative).read_bytes())}  {relative}\n")
    return "".join(lines)


def write_private(path: Path, data: bytes, mode: int = 0o600) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    os.chmod(path, mode)


def write_manifest(directory: Path) -> str:
    data = manifest_text(directory).encode()
    write_private(directory / MANIFEST, data)
    return sha256(data)


def render(root: Path, out: Path, secrets: Path) -> str:
    root, out = Path(root), Path(out)
    settings = read_settings(Path(secrets))
    check_host(settings["RAILWAY_BRIDGE_TARGET_HOST"])
    check_target_name(settings.get("RAILWAY_BRIDGE_TARGET_NAME", ""))
    bind_ip = check_bind_ip(settings["RAILWAY_BRIDGE_BIND_IP"])
    interface = check_interface(settings["RAILWAY_BRIDGE_BIND_INTERFACE"])
    for _, source in CONTAINER_ENV:
        check_value(source, settings[source])
    info = os.lstat(out)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise RenderError(f"{out}: not a directory of mine")
    os.chmod(out, 0o700)
    for entry in list(out.iterdir()):
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink()
    (out / "systemd").mkdir(mode=0o700)
    for relative in FILES:
        write_private(out / relative, (root / relative).read_bytes())
    container = "".join(f"{target}={settings[source]}\n" for target, source in CONTAINER_ENV)
    write_private(out / "railway-bridge.env", container.encode())
    deployment = (f"RAILWAY_BRIDGE_BIND_IP={bind_ip}\n"
                  f"RAILWAY_BRIDGE_BIND_INTERFACE={interface}\n")
    write_private(out / "deployment.env", deployment.encode())
    return write_manifest(out)


def main(argv: list[str]) -> int:
    try:
        if argv == ["target-host"]:
            settings = read_settings(default_secrets())
            print(check_host(settings["RAILWAY_BRIDGE_TARGET_HOST"]))
            return 0
        if argv == ["target-name"]:
            settings = read_settings(default_secrets())
            print(check_target_name(settings.get("RAILWAY_BRIDGE_TARGET_NAME", "")))
            return 0
        if len(argv) == 1 and not argv[0].startswith("-"):
            root = Path(__file__).resolve().parent
            print(render(root, Path(argv[0]), default_secrets()))
            return 0
    except (RenderError, OSError) as error:
        print(f"render: {error}", file=sys.stderr)
        return 1
    print("usage: render.py OUT | render.py target-host | render.py target-name", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
