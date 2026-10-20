"""Offline deployment tests. No host, Docker daemon, or real secret is used."""

from __future__ import annotations

import fcntl
import hashlib
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "python:3.12.15-alpine3.24@sha256:1b668429b3511ab407d8e00648891631b0b1a4d7e15e3ca70f38ab5b91ad4ab4"
PAYLOAD = {
    "compose.yaml", "railway_bridge.py", "railway-bridge.env", "deployment.env",
    "manifest.sha256", "systemd", "systemd/railway-bridge-reconcile.sh",
    "systemd/railway-bridge-reconcile.service",
}
VALUES = {
    "RAILWAY_BRIDGE_TARGET_HOST": "deploy@example.invalid",
    "RAILWAY_BRIDGE_BIND_IP": "100.64.23.17",
    "RAILWAY_BRIDGE_BIND_INTERFACE": "tailscale0",
    "RAILWAY_BRIDGE_TOKEN": "literal $(touch /tmp/never) ; `false`  ",
    "RAILWAY_BRIDGE_PROJECT_ID": "00000000-0000-0000-0000-000000000001",
    "RAILWAY_BRIDGE_ENVIRONMENT_ID": "00000000-0000-0000-0000-000000000002",
    "RAILWAY_BRIDGE_SERVICE_ID": "00000000-0000-0000-0000-000000000003",
    "RAILWAY_BRIDGE_LABEL": " service label with spaces  ",
}
RELEASE_RE = re.compile(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_secrets(path: Path, values=VALUES, mode=0o600):
    path.write_text("# local deployment settings\n" + "".join(f"{k}={v}\n" for k, v in values.items()))
    path.chmod(mode)


def load_render():
    spec = importlib.util.spec_from_file_location("deployment_render", ROOT / "render.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_bash(script: str, env: dict[str, str], timeout=30):
    return subprocess.run(["bash", "-c", script], env={**os.environ, **env},
                          capture_output=True, text=True, timeout=timeout, check=False)


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.secrets = self.base / "secrets.env"
        write_secrets(self.secrets)
        self.out = self.base / "out"
        self.out.mkdir(mode=0o700)

    def render(self):
        module = load_render()
        return module.render(ROOT, self.out, self.secrets)

    def test_private_regular_owned_secrets_required(self):
        for mode in (0o640, 0o644, 0o666):
            with self.subTest(mode=oct(mode)):
                self.secrets.chmod(mode)
                with self.assertRaisesRegex(Exception, "mode 600"):
                    self.render()
        self.secrets.chmod(0o600)
        link = self.base / "link"
        link.symlink_to(self.secrets)
        module = load_render()
        with self.assertRaisesRegex(Exception, "symbolic link"):
            module.render(ROOT, self.out, link)

    def test_hostile_values_are_literal_and_whitespace_is_preserved(self):
        self.render()
        env = (self.out / "railway-bridge.env").read_text()
        self.assertIn("RAILWAY_TOKEN=literal $(touch /tmp/never) ; `false`  \n", env)
        self.assertIn("SERVICE_LABEL= service label with spaces  \n", env)
        self.assertFalse((Path("/tmp") / "never").exists())
        self.assertNotIn(VALUES["RAILWAY_BRIDGE_TARGET_HOST"], env)

    def test_duplicate_missing_malformed_and_multiline_secrets_fail_closed(self):
        module = load_render()
        cases = {
            "duplicate": self.secrets.read_text() + "RAILWAY_BRIDGE_TOKEN=again\n",
            "missing": "RAILWAY_BRIDGE_TOKEN=x\n",
            "malformed": self.secrets.read_text() + "not an assignment\n",
            "newline": self.secrets.read_text().replace(
                "RAILWAY_BRIDGE_LABEL= service label with spaces  \n",
                "RAILWAY_BRIDGE_LABEL=first\ncontinued secret\n",
            ),
        }
        for name, text in cases.items():
            with self.subTest(name=name):
                self.secrets.write_text(text)
                self.secrets.chmod(0o600)
                with self.assertRaises(Exception):
                    module.render(ROOT, self.out, self.secrets)
                write_secrets(self.secrets)

    def test_only_private_or_tailnet_bind_addresses_are_accepted(self):
        module = load_render()
        for address in ("127.0.0.1", "0.0.0.0", "8.8.8.8", "169.254.1.1", "::1"):
            with self.subTest(address=address):
                values = {**VALUES, "RAILWAY_BRIDGE_BIND_IP": address}
                write_secrets(self.secrets, values)
                with self.assertRaisesRegex(Exception, "private IPv4"):
                    module.render(ROOT, self.out, self.secrets)
        for address in ("10.2.3.4", "172.16.1.2", "192.168.5.6", "100.64.0.1"):
            with self.subTest(address=address):
                write_secrets(self.secrets, {**VALUES, "RAILWAY_BRIDGE_BIND_IP": address})
                module.render(ROOT, self.out, self.secrets)
                self.assertIn(address, (self.out / "deployment.env").read_text())

    def test_manifest_is_exact_sorted_and_covers_special_file_bytes(self):
        digest = self.render()
        manifest = self.out / "manifest.sha256"
        self.assertEqual(digest, sha(manifest.read_bytes()))
        lines = manifest.read_text().splitlines()
        paths = [line.split("  ", 1)[1] for line in lines]
        self.assertEqual(paths, sorted(paths, key=str.encode))
        self.assertEqual(set(paths), PAYLOAD - {"manifest.sha256", "systemd"})
        self.assertEqual(stat.S_IMODE(manifest.stat().st_mode), 0o600)
        # Hashing is byte exact, including no final newline and embedded newlines.
        weird = self.out / "railway_bridge.py"
        weird.write_bytes(b"line one\nline two without final newline")
        text = load_render().manifest_text(self.out)
        self.assertIn(sha(weird.read_bytes()) + "  railway_bridge.py", text)

    def test_manifest_refuses_links_extra_directories_and_unsafe_names(self):
        module = load_render()
        self.render()
        (self.out / "railway_bridge.py").unlink()
        (self.out / "railway_bridge.py").symlink_to(ROOT / "railway_bridge.py")
        with self.assertRaisesRegex(Exception, "regular file"):
            module.manifest_text(self.out)
        (self.out / "railway_bridge.py").unlink()
        shutil.copy2(ROOT / "railway_bridge.py", self.out / "railway_bridge.py")
        (self.out / "bad\nname").write_text("x")
        with self.assertRaisesRegex(Exception, "unsafe"):
            module.manifest_text(self.out)


class StaticPolicyTests(unittest.TestCase):
    def test_compose_is_exactly_pinned_and_hardened(self):
        text = (ROOT / "compose.yaml").read_text()
        self.assertIn(f"image: {IMAGE}", text)
        self.assertIn('${RAILWAY_BRIDGE_BIND_IP:?set in deployment.env}:8098:8098', text)
        for required in ('user: "65534:65534"', "read_only: true", "cap_drop:", "- ALL",
                         "no-new-privileges:true", "./railway_bridge.py:/app/railway_bridge.py:ro"):
            self.assertIn(required, text)
        self.assertNotIn("network_mode: host", text)
        self.assertNotRegex(text, r'[- ](?:0\.0\.0\.0|127\.0\.0\.1):8098')
        mounts = re.findall(r"^\s+- (\./[^\n]+)$", text, re.MULTILINE)
        self.assertEqual(mounts, ["./railway_bridge.py:/app/railway_bridge.py:ro"])

    def test_remote_installer_is_not_executable_and_deploy_streams_it(self):
        installer = ROOT / "remote" / "install.sh"
        self.assertTrue(installer.is_file())
        self.assertFalse(installer.stat().st_mode & stat.S_IXUSR)
        deploy = (ROOT / "deploy.sh").read_text()
        self.assertIn("sudo /bin/bash -s --", (ROOT / "lib" / "remote_script.sh").read_text())
        self.assertNotIn("remote/install.sh\" \"$HOST:", deploy)

    def test_public_tree_contains_no_private_identity_or_address(self):
        # A user and host joined by an at-sign is private; an image digest
        # (name@sha256:...) is not. Neither is a whole private range in CIDR
        # form, such as the allow-list 10.0.0.0/8.
        ip = r"(?<![\d.])(?:100|192\.168|10)(?:\.\d+){2,3}(?![.\d]|/\d)"
        # RFC 2606 reserves example.invalid, which the fixtures use.
        # scan-exempt-begin: the pattern has to name what it looks for.
        # Private names sit outside the tree, one per line, so this check can't publish them.
        terms = Path.home() / ".config" / "railway-bridge" / "private-terms"
        names = [re.escape(t) + "|" for t in terms.read_text().split()] if terms.is_file() else []
        forbidden = re.compile(r"(?i)(" + "".join(names) + ip
                               + r"|(?<![\d.])172\.(?:1[6-9]|2\d|3[01])(?:\.\d+){2}(?![.\d]|/\d)"
                               + r"|fd7a:115c:a1e0|\.ts\.net\b|\.lan\b|/home/[a-z]"
                               + r"|[a-z0-9_]@(?!sha256:|example\.invalid)[a-z0-9-]{2,})")
        # scan-exempt-end
        mark = "# scan-exempt-"
        exempt = re.compile(re.escape(mark) + "begin.*?" + re.escape(mark) + "end", re.S)
        # Built at run time so this file does not trip its own check.
        self.assertIsNotNone(forbidden.search("ssh admin" + "@board" + ".internal"))
        self.assertIsNotNone(forbidden.search("bind 192.168" + ".1.23:8098"))
        for leak in ("172.20" + ".0.5", "box.tail1234." + "ts" + ".net", "fd7a:115c" + ":a1e0::1", "/home" + "/someone/x", "nas." + "l" + "an"):
            self.assertIsNotNone(forbidden.search(leak), leak)
        self.assertIsNone(forbidden.search("python:3.12@sha256:" + "0" * 64))
        self.assertIsNone(forbidden.search('("10.0.0.0/8", "192.168.0.0/16")'))
        # The made-up addresses the fixtures use. Any other address fails.
        fixtures = {"100.64.23.17", "100.64.23.18", "100.64.23.99", "100.64.0.1", "10.2.3.4", "192.168.5.6", "172.16.1.2"}
        exempted = 0
        for path in ROOT.rglob("*"):
            if not path.is_file() or ".git" in path.parts or "__pycache__" in path.parts:
                continue
            text = path.read_text(errors="ignore")
            text, n = exempt.subn("", text)
            exempted += n
            found = [m.group(0) for m in forbidden.finditer(text) if m.group(0) not in fixtures]
            self.assertEqual(found, [], path)
        # Only this one block may be exempt, anywhere in the tree.
        self.assertEqual(exempted, 1)

    def test_readme_documents_install_verify_reboot_rollback_and_gates(self):
        text = (ROOT / "README.md").read_text().lower()
        for phrase in ("install-only", "--start", "reboot", "rollback", "prometheus",
                       "uptime kuma", "stop the source", "migration gate", "systemctl"):
            self.assertIn(phrase, text)


FAKE_SSH = r'''#!/usr/bin/env python3
import json, os, subprocess, sys
args=sys.argv[1:]
log=os.environ["FAKE_LOG"]
while args and args[0].startswith("-"):
    flag=args.pop(0)
    if flag in ("-o","-O"):
        value=args.pop(0)
        if flag == "-O": sys.exit(0)
host=args[0]; command=" ".join(args[1:])
with open(log,"a") as h: h.write(json.dumps({"host":host,"command":command})+"\n")
env=dict(os.environ, TMPDIR=os.environ["FAKE_REMOTE_TMP"])
if "mktemp -d" in command:
    sys.exit(subprocess.run(["sh","-c",command],env=env).returncode)
if "tar -C" in command:
    if os.environ.get("FAKE_UPLOAD_FAILS"): sys.stdin.buffer.read(); sys.exit(1)
    sys.exit(subprocess.run(["sh","-c",command],env=env).returncode)
if "sudo /bin/bash -s --" in command:
    command=command.replace("sudo /bin/bash -s --",os.environ["FAKE_ROOT"])
    sys.exit(subprocess.run(["sh","-c",command],env=env).returncode)
if command.startswith("rm -rf -- "):
    sys.exit(subprocess.run(["sh","-c",command],env=env).returncode)
sys.exit(99)
'''
FAKE_ROOT = r'''#!/usr/bin/env python3
import json, os, shutil, sys
from pathlib import Path
out=Path(os.environ["FAKE_OUT"]); out.mkdir(exist_ok=True)
(out/"script").write_bytes(sys.stdin.buffer.read())
(out/"args.json").write_text(json.dumps(sys.argv[1:]))
stage=Path(sys.argv[1])
(out/"listing.json").write_text(json.dumps(sorted(p.relative_to(stage).as_posix() for p in stage.rglob("*"))))
shutil.copy(stage/"manifest.sha256",out/"manifest.sha256")
sys.exit(int(os.environ.get("FAKE_STATUS","0")))
'''


class DeployTests(unittest.TestCase):
    # deploy.sh once ran this whole suite before deploying, and these tests run
    # deploy.sh: an exponential fork bomb that exhausted RAM and swap. So:
    # 1. refuse to run any deploy.sh that still invokes a test runner;
    # 2. refuse to run at all when already nested inside a deploy;
    # 3. mark every deploy started here, so a nested run trips rule 2.
    RUNNER = re.compile(r"unittest|pytest|tests/")

    def setUp(self):
        if os.environ.get("RAILWAY_BRIDGE_IN_TESTS"):
            self.fail("DeployTests started inside a deploy started by DeployTests")
        script = (ROOT / "deploy.sh").read_text()
        code = "\n".join(l for l in script.splitlines() if not l.lstrip().startswith("#"))
        if self.RUNNER.search(code):
            self.fail("deploy.sh invokes a test runner; refusing to run it recursively")
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name); self.bin = self.base / "bin"; self.bin.mkdir()
        for name, text in (("ssh", FAKE_SSH), ("fake-root", FAKE_ROOT)):
            p=self.bin/name; p.write_text(text); p.chmod(0o700)
        for name in ("local", "remote"): (self.base/name).mkdir(mode=0o700)
        self.secrets=self.base/"secrets.env"; write_secrets(self.secrets)
        self.log=self.base/"ssh.log"; self.out=self.base/"out"

    def deploy(self, *args, **extra):
        env={**os.environ,"PATH":f"{self.bin}:{os.environ['PATH']}","TMPDIR":str(self.base/"local"),
             "RAILWAY_BRIDGE_SECRETS":str(self.secrets),"FAKE_LOG":str(self.log),
             "FAKE_REMOTE_TMP":str(self.base/"remote"),"FAKE_ROOT":str(self.bin/"fake-root"),
             "FAKE_OUT":str(self.out),"RAILWAY_BRIDGE_IN_TESTS":"1",**extra}
        return subprocess.run(["bash",str(ROOT/"deploy.sh"),*args],env=env,capture_output=True,text=True,timeout=60)

    def commands(self):
        return [] if not self.log.exists() else [json.loads(x)["command"] for x in self.log.read_text().splitlines()]

    def assert_clean(self):
        self.assertEqual(list((self.base/"local").iterdir()), [])
        self.assertEqual(list((self.base/"remote").iterdir()), [])

    def test_default_is_install_only_and_start_is_explicit(self):
        for args, start in (((),"0"),(('--start',),"1")):
            with self.subTest(args=args):
                result=self.deploy(*args)
                self.assertEqual(result.returncode,0,result.stderr)
                sent=json.loads((self.out/"args.json").read_text())
                self.assertEqual(sent[1],start)
                self.assertEqual(sent[2],sha((self.out/"manifest.sha256").read_bytes()))
                self.assertEqual(set(json.loads((self.out/"listing.json").read_text())),PAYLOAD)
                self.assertEqual((self.out/"script").read_bytes(),(ROOT/"remote"/"install.sh").read_bytes())
                self.assert_clean()
                shutil.rmtree(self.out); self.log.unlink()

    def test_secrets_never_enter_remote_argv_and_failures_clean_temporaries(self):
        result=self.deploy(FAKE_STATUS="3")
        self.assertEqual(result.returncode,3)
        for command in self.commands():
            for value in VALUES.values(): self.assertNotIn(value,command)
        self.assert_clean()

    def test_invalid_options_bad_secret_mode_and_failed_upload_stop_safely(self):
        result=self.deploy("--bad")
        self.assertEqual(result.returncode,2); self.assertEqual(self.commands(),[])
        self.secrets.chmod(0o644)
        result=self.deploy(); self.assertNotEqual(result.returncode,0); self.assertEqual(self.commands(),[]); self.assert_clean()
        self.secrets.chmod(0o600)
        result=self.deploy(FAKE_UPLOAD_FAILS="1")
        self.assertEqual(result.returncode,1)
        self.assertFalse(any("sudo" in x for x in self.commands()))
        self.assert_clean()


RECONCILE = r'''
source "$REPO/systemd/railway-bridge-reconcile.sh"
ROOT_UID="$(id -u)"
OPT="$T_OPT"; LINK="$T_OPT/railway-bridge"; RELEASES="$T_OPT/railway-bridge-releases"
LOCK="$T_RUN/railway-bridge-install.lock"; LOCK_WAIT_SECONDS="${T_LOCK_WAIT:-2}"
WAIT_SECONDS="$T_WAIT"; SLEEP_SECONDS=0; COMPOSE_WAIT_SECONDS=5
# Never the host's real docker or ip: only the fakes in this test's bin.
DOCKER="$T_BIN/docker"; IP="$T_BIN/ip"
main
'''
FAKE_IP = r'''#!/usr/bin/env bash
n=$(( $(cat "$IP_COUNT" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$IP_COUNT"
if (( n >= READY_AFTER )); then printf '7: %s inet %s/32 scope global %s\n' "$WANT_IF" "$WANT_IP" "$WANT_IF"; fi
'''
FAKE_DOCKER_RECONCILE = r'''#!/usr/bin/env bash
printf '%s\n' "$*" >> "$DOCKER_LOG"
exit "${DOCKER_STATUS:-0}"
'''


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup); self.base=Path(self.tmp.name)
        self.opt=self.base/"opt"; self.run_dir=self.base/"run"; self.bin=self.base/"bin"
        self.release=self.opt/"railway-bridge-releases"/"20260101T000000Z-0123456789ab"
        for p in (self.run_dir,self.bin,self.release): p.mkdir(parents=True,mode=0o700)
        (self.release/"compose.yaml").write_text("services: {}\n"); (self.release/"deployment.env").write_text("RAILWAY_BRIDGE_BIND_IP=100.64.23.17\nRAILWAY_BRIDGE_BIND_INTERFACE=tailscale0\n")
        (self.opt/"railway-bridge").symlink_to(self.release)
        for name,text in (("ip",FAKE_IP),("docker",FAKE_DOCKER_RECONCILE)):
            p=self.bin/name;p.write_text(text);p.chmod(0o700)
        self.ip_count=self.base/"ip-count"; self.docker_log=self.base/"docker.log"

    def reconcile(self, ready=1, wait=4, **extra):
        env={"REPO":str(ROOT),"PATH":f"{self.bin}:{os.environ['PATH']}","T_OPT":str(self.opt),"T_RUN":str(self.run_dir),"T_BIN":str(self.bin),
             "T_WAIT":str(wait),"IP_COUNT":str(self.ip_count),"READY_AFTER":str(ready),"WANT_IP":"100.64.23.17",
             "WANT_IF":"tailscale0","DOCKER_LOG":str(self.docker_log),**extra}
        return run_bash(RECONCILE,env)

    def test_reads_env_without_execution_waits_for_exact_ip_then_recreates_healthy(self):
        marker=self.base/"owned"
        (self.release/"deployment.env").write_text(f"RAILWAY_BRIDGE_BIND_IP=100.64.23.17\nRAILWAY_BRIDGE_BIND_INTERFACE=tailscale0$(touch {marker})\n")
        bad=self.reconcile(); self.assertEqual(bad.returncode,1); self.assertFalse(marker.exists()); self.assertFalse(self.docker_log.exists())
        (self.release/"deployment.env").write_text("RAILWAY_BRIDGE_BIND_IP=100.64.23.17\nRAILWAY_BRIDGE_BIND_INTERFACE=tailscale0\n")
        good=self.reconcile(ready=3); self.assertEqual(good.returncode,0,good.stderr); self.assertEqual(self.ip_count.read_text(),"3\n")
        self.assertIn("--env-file",self.docker_log.read_text()); self.assertIn("--force-recreate --wait --wait-timeout 5",self.docker_log.read_text())

    def test_timeout_wrong_prefix_and_no_docker_fail_before_compose(self):
        result=self.reconcile(ready=9,wait=2); self.assertEqual(result.returncode,1); self.assertFalse(self.docker_log.exists())
        self.ip_count.unlink(missing_ok=True)
        result=self.reconcile(ready=1,WANT_IP="100.64.23.18"); self.assertEqual(result.returncode,1); self.assertFalse(self.docker_log.exists())
        docker=self.bin/"docker"; docker.unlink(); result=self.reconcile(); self.assertEqual(result.returncode,1); self.assertIn("docker",result.stderr)

    def test_waits_for_installer_lock_and_revalidates_release_after_lock(self):
        lock=self.run_dir/"railway-bridge-install.lock"; ready=self.base/"held"
        holder=subprocess.Popen([shutil.which("flock") or "/usr/bin/flock",str(lock),"sh","-c",f"touch {ready}; sleep .3"])
        self.addCleanup(lambda: holder.poll() is None and holder.terminate())
        for _ in range(100):
            if ready.exists(): break
            time.sleep(.01)
        started=time.monotonic(); result=self.reconcile(); elapsed=time.monotonic()-started; holder.wait()
        self.assertEqual(result.returncode,0,result.stderr); self.assertGreaterEqual(elapsed,.2)

    def test_unit_retry_arithmetic_and_dependencies_are_coherent(self):
        helper=(ROOT/"systemd"/"railway-bridge-reconcile.sh").read_text(); unit=(ROOT/"systemd"/"railway-bridge-reconcile.service").read_text()
        def val(pattern,text):
            m=re.search(pattern,text,re.M); self.assertIsNotNone(m); return int(m.group(1))
        lock=val(r"^LOCK_WAIT_SECONDS=(\d+)$",helper); address=val(r"^WAIT_SECONDS=(\d+)$",helper); compose=val(r"^COMPOSE_WAIT_SECONDS=(\d+)$",helper)
        timeout=val(r"^TimeoutStartSec=(\d+)s$",unit); restart=val(r"^RestartSec=(\d+)s$",unit); burst=val(r"^StartLimitBurst=(\d+)$",unit); interval=val(r"^StartLimitIntervalSec=(\d+)s$",unit)
        self.assertEqual(timeout-lock-address-compose,180)
        self.assertGreaterEqual(interval-(burst*timeout+(burst-1)*restart),270)
        self.assertIn("Requires=docker.service tailscaled.service",unit); self.assertIn("Restart=on-failure",unit)
        self.assertNotIn("EnvironmentFile",unit)


FAKE_DOCKER = r'''#!/usr/bin/env bash
printf '%s\n' "$*" >> "$DOCKER_LOG"
[[ "$1" == compose ]] || exit 3; shift
[[ "$1" != version ]] || exit 0
envfile=""; dir=""
while [[ "$1" == --env-file || "$1" == --project-directory || "$1" == -f ]]; do
  [[ "$1" != --env-file ]] || envfile="$2"; [[ "$1" != --project-directory ]] || dir="$2"; shift 2
done
case "$1" in
 config) case "${2:-}" in --quiet) exit 0;; --services) echo railway-bridge;; --images) sed -n 's/^ *image: *//p' "$dir/compose.yaml";;
   --format) ip=$(sed -n 's/^RAILWAY_BRIDGE_BIND_IP=//p' "$envfile"); [[ -z "${PORT_OVERRIDE:-}" ]] || ip="${PORT_OVERRIDE# }"
     printf '{"services":{"railway-bridge":{"ports":[{"mode":"ingress","host_ip":"%s","target":8098,"published":"8098","protocol":"tcp"}]}}}\n' "$ip";; esac;;
 pull) exit "${PULL_STATUS:-0}";;
 up) n=$(( $(cat "$DOCKER_LOG.count" 2>/dev/null || echo 0)+1)); echo "$n">"$DOCKER_LOG.count"; echo "up $(readlink -f "$dir")" >> "$DOCKER_LOG.runs"; read -ra a<<<"${UP_RESULTS:-0}"; exit "${a[n-1]:-0}";;
 down) echo "down $(readlink -f "$dir")" >> "$DOCKER_LOG.runs"; exit "${DOWN_STATUS:-0}";;
 *) exit 5;;
esac
'''
FAKE_SYSTEMCTL = r'''#!/usr/bin/env bash
# Models real systemd closely enough for rollback: enable creates the wants
# link and needs the unit file; disable of a unit whose file is already gone
# fails and LEAVES the link, as the real one can.
printf '%s\n' "$*" >> "$SYSTEMCTL_LOG"
unit="$T_ETC/systemd/system/railway-bridge-reconcile.service"; wants="$T_ETC/systemd/system/multi-user.target.wants/railway-bridge-reconcile.service"
case "$1" in
 is-enabled) if [[ -L "$wants" ]]; then echo enabled; exit 0; elif [[ -f "$unit" ]]; then echo disabled; exit 1; else echo not-found; exit 4; fi;;
 enable) [[ -f "$unit" ]] || exit 1; mkdir -p "$(dirname "$wants")"; ln -sfn "$unit" "$wants";;
 disable) [[ -f "$unit" ]] || exit 1; rm -f "$wants";;
 daemon-reload) :;; *) exit 2;; esac
'''
INSTALL = r'''
source "$REPO/remote/install.sh"
ROOT_UID="$(id -u)"; OPT="$T_OPT"; RELEASES="$T_OPT/railway-bridge-releases"; LINK="$T_OPT/railway-bridge"
LOCK="$T_RUN/railway-bridge-install.lock"; HELPER="$T_USR/libexec/railway-bridge-reconcile"; UNIT="$T_ETC/systemd/system/railway-bridge-reconcile.service"; WANTS="$T_ETC/systemd/system/multi-user.target.wants/railway-bridge-reconcile.service"; WAIT_SECONDS=5
fault(){ [[ "$1" != "${T_FAULT:-}" ]] || { [[ "${T_KIND:-fail}" != signal ]] || kill -TERM $$; return 1; }; }
main "$T_STAGE" "$T_START" "$T_HASH"
'''


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup); self.base=Path(self.tmp.name)
        self.opt=self.base/"opt"; self.run_dir=self.base/"run"; self.bin=self.base/"bin"; self.usr=self.base/"usr"; self.etc=self.base/"etc"; self.stage=self.base/"railway-bridge-stage.abcdefghij"
        for p in (self.opt,self.run_dir,self.bin,self.usr/"libexec",self.etc/"systemd"/"system"): p.mkdir(parents=True,mode=0o700)
        for name,text in (("docker",FAKE_DOCKER),("systemctl",FAKE_SYSTEMCTL)):
            p=self.bin/name;p.write_text(text);p.chmod(0o700)
        self.docker_log=self.base/"docker.log";self.systemctl_log=self.base/"systemctl.log"
        self.wants=self.etc/"systemd"/"system"/"multi-user.target.wants"/"railway-bridge-reconcile.service"

    def make_stage(self, suffix=""):
        if self.stage.exists(): shutil.rmtree(self.stage)
        self.stage.mkdir(mode=0o700); secrets=self.base/"secrets"; write_secrets(secrets,{**VALUES,"RAILWAY_BRIDGE_LABEL":VALUES["RAILWAY_BRIDGE_LABEL"]+suffix})
        digest=load_render().render(ROOT,self.stage,secrets); return digest

    def install(self,start="0",digest=None,**extra):
        env={"REPO":str(ROOT),"PATH":f"{self.bin}:{os.environ['PATH']}","SUDO_UID":str(os.getuid()),"DOCKER_LOG":str(self.docker_log),
             "SYSTEMCTL_LOG":str(self.systemctl_log),"T_OPT":str(self.opt),"T_RUN":str(self.run_dir),
             "T_USR":str(self.usr),"T_ETC":str(self.etc),"T_STAGE":str(self.stage),"T_START":start,"T_HASH":digest or sha((self.stage/"manifest.sha256").read_bytes()),**extra}
        return run_bash(INSTALL,env)

    def runs(self):
        p=self.base/"docker.log.runs"; return p.read_text().splitlines() if p.exists() else []

    def old(self):
        self.make_stage("old"); r=self.install(); self.assertEqual(r.returncode,0,r.stderr); return Path(os.readlink(self.opt/"railway-bridge"))

    def test_install_only_publishes_immutable_root_release_and_enables_boot_unit(self):
        digest=self.make_stage(); result=self.install(); self.assertEqual(result.returncode,0,result.stderr)
        link=self.opt/"railway-bridge"; release=Path(os.readlink(link)); self.assertRegex(release.name,RELEASE_RE); self.assertTrue(release.name.endswith(digest[:12]))
        self.assertEqual(self.runs(),[]); self.assertEqual(stat.S_IMODE(release.stat().st_mode),0o700)
        for path in release.rglob("*"):
            want = 0o700 if path.is_dir() else 0o644 if path.name == "railway_bridge.py" else 0o600
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), want, path)
        # The container user (uid 65534, not root, no capabilities) must be able
        # to read the one bind-mounted file, and nothing that holds a secret.
        script = release / "railway_bridge.py"
        self.assertTrue(script.stat().st_mode & stat.S_IROTH)
        self.assertIn("./railway_bridge.py:/app/railway_bridge.py:ro", (release / "compose.yaml").read_text())
        for secret in ("railway-bridge.env", "deployment.env", "manifest.sha256"):
            self.assertFalse((release / secret).stat().st_mode & 0o077, secret)
        self.assertEqual(stat.S_IMODE((self.usr/"libexec"/"railway-bridge-reconcile").stat().st_mode),0o755)
        self.assertTrue(self.wants.is_symlink())

    def test_start_success_and_first_install_failure_cleanup(self):
        self.make_stage(); ok=self.install("1"); self.assertEqual(ok.returncode,0,ok.stderr); self.assertEqual(len(self.runs()),1)
        shutil.rmtree(self.opt); self.opt.mkdir(mode=0o700); self.wants.unlink()
        # A fresh host: no boot helper or unit either, or the installer rightly refuses the mixed state.
        (self.usr/"libexec"/"railway-bridge-reconcile").unlink(); (self.etc/"systemd"/"system"/"railway-bridge-reconcile.service").unlink(); self.make_stage("fail")
        (self.base/"docker.log.count").unlink()  # UP_RESULTS counts from this install's first `up`
        bad=self.install("1",UP_RESULTS="1"); self.assertEqual(bad.returncode,1,bad.stderr); self.assertFalse((self.opt/"railway-bridge").exists())
        # Exactly as before: no unit, no helper, and no dangling enablement link.
        self.assertFalse(self.wants.exists() or self.wants.is_symlink()); self.assertNotIn("RECOVERY", bad.stderr)
        self.assertFalse((self.etc/"systemd"/"system"/"railway-bridge-reconcile.service").exists()); self.assertFalse((self.usr/"libexec"/"railway-bridge-reconcile").exists())
        self.assertTrue(any(x.startswith("down ") for x in self.runs()))

    def test_failed_upgrade_rolls_publication_systemd_and_container_back(self):
        old=self.old(); helper=(self.usr/"libexec"/"railway-bridge-reconcile").read_bytes(); self.make_stage("new")
        result=self.install("1",UP_RESULTS="1 0"); self.assertEqual(result.returncode,1); self.assertEqual(Path(os.readlink(self.opt/"railway-bridge")),old)
        self.assertEqual((self.usr/"libexec"/"railway-bridge-reconcile").read_bytes(),helper); self.assertEqual(self.runs()[-2:],[f"up {old.parent / (next(n for n in os.listdir(old.parent) if n.endswith('.failed')).removesuffix('.failed'))}",f"up {old}"])

    def test_exact_manifest_file_set_env_compose_and_image_are_enforced(self):
        changes=[]
        changes.append((lambda: (self.stage/"extra").write_text("x"),"unexpected entry"))
        changes.append((lambda: (self.stage/"railway_bridge.py").write_text("changed"),"manifest"))
        def image():
            p=self.stage/"compose.yaml"; p.write_text(p.read_text().replace("sha256:1b6684","sha256:0b6684")); return load_render().write_manifest(self.stage)
        changes.append((image,"approved digest"))
        def envextra():
            with (self.stage/"railway-bridge.env").open("a") as h:h.write("EXTRA=x\n")
            return load_render().write_manifest(self.stage)
        changes.append((envextra,"exactly"))
        for change,message in changes:
            with self.subTest(message=message):
                self.make_stage(message); value=change(); digest=value if isinstance(value,str) else None; result=self.install(digest=digest)
                self.assertEqual(result.returncode,1); self.assertIn(message,result.stderr)

    def test_symlinked_script_in_stage_is_refused_without_touching_its_target(self):
        # Root must never chmod through a link the stage owner planted.
        victim=self.base/"victim"; victim.write_text("secret\n"); victim.chmod(0o600)
        self.make_stage("link"); script=self.stage/"railway_bridge.py"; script.unlink(); script.symlink_to(victim)
        result=self.install()
        self.assertEqual(result.returncode,1); self.assertIn("unexpected entry",result.stderr)
        self.assertEqual(stat.S_IMODE(victim.stat().st_mode),0o600); self.assertFalse((self.opt/"railway-bridge").exists())

    def test_root_side_rechecks_bind_address_and_published_port(self):
        # A public bind address that somehow got past render.py is refused as root.
        def public():
            (self.stage/"deployment.env").write_text("RAILWAY_BRIDGE_BIND_IP=8.8.8.8\nRAILWAY_BRIDGE_BIND_INTERFACE=tailscale0\n")
            return load_render().write_manifest(self.stage)
        self.make_stage("public"); result=self.install(digest=public())
        self.assertEqual(result.returncode,1); self.assertIn("private IPv4",result.stderr); self.assertFalse((self.opt/"railway-bridge").exists())
        # Compose resolving the port to any other address, 0.0.0.0 included, is refused.
        for other in ("0.0.0.0", "100.64.23.99", ""):
            with self.subTest(host_ip=other):
                self.make_stage("port"+other); result=self.install(PORT_OVERRIDE=other or " ")
                self.assertEqual(result.returncode,1); self.assertIn("only published port",result.stderr)
                self.assertFalse((self.opt/"railway-bridge").exists())

    def test_hangup_does_not_stop_an_install_half_way(self):
        # HUP (a dropped SSH session) is ignored, so the run finishes.
        self.make_stage(); script=INSTALL.replace('main "$T_STAGE"', 'fault(){ [[ "$1" != published ]] || kill -HUP $$; }; main "$T_STAGE"')
        env={"REPO":str(ROOT),"PATH":f"{self.bin}:{os.environ['PATH']}","SUDO_UID":str(os.getuid()),"DOCKER_LOG":str(self.docker_log),
             "SYSTEMCTL_LOG":str(self.systemctl_log),"T_OPT":str(self.opt),"T_RUN":str(self.run_dir),"T_USR":str(self.usr),"T_ETC":str(self.etc),
             "T_STAGE":str(self.stage),"T_START":"1","T_HASH":sha((self.stage/"manifest.sha256").read_bytes())}
        result=run_bash(script,env); self.assertEqual(result.returncode,0,result.stderr)
        self.assertTrue((self.opt/"railway-bridge").is_symlink()); self.assertEqual(len(self.runs()),1)

    def test_lock_contention_faults_signals_and_temporaries_leave_prior_state(self):
        old=self.old(); self.make_stage("new"); before=os.readlink(self.opt/"railway-bridge")
        lock=self.run_dir/"railway-bridge-install.lock"
        with lock.open("a") as h:
            fcntl.flock(h,fcntl.LOCK_EX|fcntl.LOCK_NB); result=self.install()
        self.assertEqual(result.returncode,1); self.assertEqual(os.readlink(self.opt/"railway-bridge"),before)
        for point in ("copied","verified","checked","pulled","systemd-installed","released","link-made","published","starting","started","commit-armed"):
            for kind,status in (("fail",1),("signal",130)):
                with self.subTest(point=point,kind=kind):
                    self.make_stage(point+kind); result=self.install("1",T_FAULT=point,T_KIND=kind)
                    self.assertEqual(result.returncode,status,result.stderr); self.assertEqual(Path(os.readlink(self.opt/"railway-bridge")),old)
                    self.assertFalse((self.opt/".railway-bridge.link-new").exists())
                    self.assertFalse(any(p.name.startswith(".incoming") for p in (self.opt/"railway-bridge-releases").iterdir()))


if __name__ == "__main__":
    unittest.main()
