#!/usr/bin/env python3
"""Rootless driver for the OCR container swap (docs/RECORDS-OCR-CONTAINER-ROOTLESS.md).

One script, four modes:

  --dry-run   Read-only. Runs R1 discovery (docker info/inspect/ps, systemctl
              --user show) and prints every later command without running it.
              Writes nothing, not even the private directory.
  --apply     Executes runbook-rootless.json in order. Any failing step runs
              the rollback path named by its on_fail. Stops at the snapshot
              gate unless snapshot, evidence and restore test all passed.
  --resume    Continues a run held at [A2] (current state differs from the
              snapshot) after the owner reviewed the diff: --accept-a2 SHA256.
  --rollback  Runs the rollback path for the step a held or interrupted run
              stopped at.

No sudo. Data steps run in throwaway helper containers (`docker run --rm`) as
root of the rootless daemon's user namespace, which is the host user plus its
subuid range: exactly the IDs that own the mounted files. Live mounts are
mounted read-only in every helper except the restore of a rollback. Helpers
write only into a fresh run directory below the private directory. Container,
unit, tag and directory names are arguments; none are stored in Git.

Exit codes: 0 done, 1 failed and rolled back, 2 refused before any change,
3 held at [A2] for owner review, 4 rollback failed (everything left stopped).
"""
import argparse
import datetime
import fcntl
import hashlib
import http.client
import importlib.util
import json
import os
import re
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

RECIPE = Path(__file__).resolve().parent
RUNBOOK = RECIPE / "runbook-rootless.json"
LIVE, PRIV, REC = "/live", "/private", "/recipe"
HELPER_LABEL = "deflock.flock-swap.helper=1"
HELPER_CAPS = ("CHOWN", "DAC_OVERRIDE", "FOWNER", "FSETID", "SETFCAP")
TAR = ("tar", "--numeric-owner", "--acls", "--xattrs")
OVERFLOW_ID = "65534"
BUSY = {"active", "activating", "deactivating", "reloading", "refreshing"}
EXIT_OK, EXIT_ROLLED_BACK, EXIT_REFUSED, EXIT_HELD, EXIT_ESCALATE = 0, 1, 2, 3, 4
IGNORED_TOP = {"Id", "Created", "Image", "State", "ResolvConfPath", "HostnamePath", "HostsPath",
               "LogPath", "GraphDriver", "RestartCount", "NetworkSettings", "SizeRw",
               "SizeRootFs", "ExecIDs",
               # Docker API >= 1.48 reports the image's own manifest descriptor (digest =
               # the image ID) on the container: image identity, like "Image".
               "ImageManifestDescriptor"}
# HostConfig booleans the daemon stores as null when a container was created by an
# older daemon or without the field, and as false when the same body is POSTed to a
# newer daemon (seen at C2 on dockerd 29.6, API 1.55: saved null -> created false).
# null and false mean the same thing; treat them as equal when diffing.
HOSTCONFIG_NULL_IS_FALSE = ("OomKillDisable",)
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
UNIT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9@_.:-]*")
TAG_RE = re.compile(r"[a-z0-9][a-z0-9._/-]*:[A-Za-z0-9_][A-Za-z0-9_.-]*")
IMAGE_ID_RE = re.compile(r"sha256:[0-9a-f]{64}")
HEX64_RE = re.compile(r"[0-9a-f]{64}")
ATTEMPT_RE = re.compile(r"attempt-(\d{3,})")
_MISSING = object()


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, RECIPE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


snapshot_evidence = _load("flock_ocr_snapshot_evidence", "snapshot_evidence.py")
preflight = _load("flock_ocr_preflight", "preflight.py")
check_runbook = _load("flock_ocr_check_runbook", "check_runbook.py")


class StepFailed(Exception):
    """A step failed; the driver takes the step's rollback path."""


class Held(Exception):
    """[A2] found differences that need the owner's review."""


def load_plan(path=RUNBOOK):
    plan = json.loads(Path(path).read_text())
    errors = check_runbook.check(plan)
    if errors:
        raise StepFailed("runbook-rootless.json violates ordering rules: " + "; ".join(errors))
    return plan


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def within(child, parent):
    child, parent = os.path.normpath(child), os.path.normpath(parent)
    return child == parent or child.startswith(parent.rstrip("/") + "/")


def overflow_owners(manifest):
    """Paths whose uid or gid is the overflow ID: unmapped in the namespace."""
    return sorted(p for p, e in manifest["entries"].items() if OVERFLOW_ID in e["owner"].split(":"))


def describe_non_archivable(paths, kinds=None):
    """One log fragment naming every excluded special file by path."""
    kinds = kinds or {}
    return ", ".join(f"{p} ({kinds[p]})" if p in kinds else p for p in paths)


# ---- container settings: create body and inspect diff (pure functions) ----

def _own_ids(inspect):
    ident = inspect.get("Id") or ""
    return {ident, ident[:12]}


def _endpoints(inspect):
    nets = (inspect.get("NetworkSettings") or {}).get("Networks") or {}
    own = _own_ids(inspect)
    result = {}
    for name, endpoint in sorted(nets.items()):
        endpoint = endpoint or {}
        entry = {}
        aliases = sorted(a for a in endpoint.get("Aliases") or [] if a not in own)
        if aliases:
            entry["Aliases"] = aliases
        for key in ("IPAMConfig", "Links", "DriverOpts"):
            if endpoint.get(key):
                entry[key] = endpoint[key]
        result[name] = entry
    return result


def create_body(before, new_image_id):
    """Engine API create body: the saved settings with only the image changed."""
    body = json.loads(json.dumps(before["Config"]))
    body["Image"] = new_image_id
    host = json.loads(json.dumps(before["HostConfig"]))
    body["HostConfig"] = host
    mode = host.get("NetworkMode") or ""
    if mode not in ("host", "none") and not mode.startswith("container:"):
        body["NetworkingConfig"] = {"EndpointsConfig": _endpoints(before)}
    return body


def _env_map(values):
    out = {}
    for item in values or []:
        key, sep, value = item.partition("=")
        out[key] = value if sep else None
    return out


def _normalize(inspect):
    data = {k: v for k, v in inspect.items() if k not in IGNORED_TOP}
    config = dict(data.get("Config") or {})
    for key in ("Image", "Env", "Labels"):
        config.pop(key, None)
    data["Config"] = config
    host = dict(data.get("HostConfig") or {})
    for key in HOSTCONFIG_NULL_IS_FALSE:
        if host.get(key) is None:  # absent or null: the daemon's false
            host[key] = False
    data["HostConfig"] = host
    data["Mounts"] = sorted(data.get("Mounts") or [],
                            key=lambda m: (m.get("Destination", ""), m.get("Type", "")))
    data["Networks"] = _endpoints(inspect)
    return data


def inspect_diff(before, after, old_image_config, new_image_config):
    """Setting names that differ beyond image, ID, creation and state.

    Env and Labels may only gain keys that the new image itself adds (keys the
    old image did not have, with the new image's value); every saved key must
    keep its value.
    """
    diffs = []
    a, b = _normalize(before), _normalize(after)
    for key in sorted(set(a) | set(b)):
        left, right = a.get(key), b.get(key)
        if isinstance(left, dict) and isinstance(right, dict) and key in ("Config", "HostConfig"):
            diffs.extend(f"{key}.{sub}" for sub in sorted(set(left) | set(right))
                         if left.get(sub) != right.get(sub))
        elif left != right:
            diffs.append(key)
    for field, convert in (("Env", _env_map), ("Labels", lambda v: dict(v or {}))):
        old = convert((before.get("Config") or {}).get(field))
        new = convert((after.get("Config") or {}).get(field))
        old_image = convert((old_image_config or {}).get(field))
        new_image = convert((new_image_config or {}).get(field))
        diffs.extend(f"Config.{field}.{k}" for k, v in sorted(old.items()) if new.get(k, _MISSING) != v)
        diffs.extend(f"Config.{field}.{k}" for k in sorted(set(new) - set(old))
                     if k in old_image or new_image.get(k, _MISSING) != new[k])
    return diffs


def anonymous_volumes(inspect):
    """Volume mounts not named in Binds or Mounts: a create would make new ones."""
    host = inspect.get("HostConfig") or {}
    named = {b.split(":")[1] for b in host.get("Binds") or [] if b.count(":") >= 1}
    named |= {m.get("Target") for m in host.get("Mounts") or []}
    return sorted(m["Destination"] for m in inspect.get("Mounts") or []
                  if m.get("Type") == "volume" and m.get("Destination") not in named)


# ---- side effects (replaced by a fake in tests) ----

class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout=120):
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._path)
        self.sock = sock


class System:
    """Real commands, Engine API, process scan, clock and disk space."""

    def run(self, argv, timeout):
        try:
            result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            return 124, "", f"timed out after {timeout}s"
        except OSError as error:
            return 127, "", str(error)
        return result.returncode, result.stdout, result.stderr

    def _socket_path(self):
        host = os.environ.get("DOCKER_HOST", "")
        if not host:
            code, out, _ = self.run(["docker", "context", "inspect", "--format",
                                     "{{.Endpoints.docker.Host}}"], 30)
            host = out.strip() if code == 0 else ""
        if not host.startswith("unix://"):
            raise StepFailed("the Docker endpoint is not a local unix socket")
        return host[len("unix://"):]

    def docker_api(self, method, path, body):
        connection = _UnixConnection(self._socket_path())
        try:
            connection.request(method, path, body=json.dumps(body),
                               headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            raw = response.read().decode("utf-8", "replace")
        finally:
            connection.close()
        try:
            return response.status, json.loads(raw) if raw else {}
        except ValueError:
            return response.status, {"message": raw[:400]}

    def open_files_under(self, roots):
        """Own-user processes with a cwd, root or open file under a source.

        Processes of other host users are not visible without privilege; the
        container check in confirm_quiescent is the primary control.
        """
        uid, me, hits = os.getuid(), os.getpid(), []
        for entry in os.scandir("/proc"):
            if not entry.name.isdigit() or int(entry.name) == me:
                continue
            base = f"/proc/{entry.name}"
            try:
                if os.stat(base).st_uid != uid:
                    continue
                links = [os.readlink(f"{base}/{n}") for n in ("cwd",)]
                links += [os.readlink(f"{base}/fd/{fd}") for fd in os.listdir(f"{base}/fd")]
            except OSError:
                continue
            for link in links:
                if any(within(link, root) for root in roots):
                    try:
                        comm = Path(f"{base}/comm").read_text().strip()
                    except OSError:
                        comm = "?"
                    hits.append(f"pid {entry.name} ({comm})")
                    break
        return hits

    def sleep(self, seconds):
        time.sleep(seconds)

    def now(self):
        return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def disk_free(self, path):
        return shutil.disk_usage(path).free


# ---- the driver ----

class Swap:
    def __init__(self, args, system=None, plan=None, out=None):
        self.args = args
        self.system = system or System()
        self.plan = plan or load_plan()
        self.out = out or sys.stdout
        self.dry = args.mode == "dry-run"
        self.state = {}
        self.before = None
        self.rbc_differs = []
        self.lock_handle = None
        self.pins = json.loads((RECIPE / "pins.json").read_text())
        self.main_impl = {"R1": self.r1_record, "Q1": self.q1_pause, "Q2": self.q2_stop,
                          "S1": self.s1_snapshot, "S2": self.s2_evidence, "S3": self.s3_restore_test,
                          "G1": self.g1_gate, "B1": self.b1_wheel, "B2": self.b2_image,
                          "V1": self.v1_verify_image, "C1": self.c1_rename, "C2": self.c2_create,
                          "C3": self.c3_start, "A1": self.a1_requiesce, "A2": self.a2_accept_data,
                          "A3": self.a3_restart, "A4": self.a4_reverify, "A5": self.release_writers}
        self.rollback_impl = {"confirm_stopped": self.rb_confirm_stopped,
                              "validate_data": self.rb_validate_data,
                              "start_old": self.rb_start_old,
                              "release_writers": lambda path: self.release_writers(),
                              "rename_old_back": self.rb_rename_old_back,
                              "stop_all_mount_users": self.rb_stop_all,
                              "set_aside_new": self.rb_set_aside_new,
                              "compare_data": self.rb_compare_data,
                              "restore": self.rb_restore}

    # -- output and commands --

    def log(self, message):
        print(message, file=self.out, flush=True)

    def sh(self, argv, effect=False, timeout=600, check=True):
        """Run argv. In dry-run, commands with an effect are printed, not run."""
        argv = [str(a) for a in argv]
        if self.dry and effect:
            self.log("    would run: " + shlex.join(argv))
            return 0, "", ""
        code, out, err = self.system.run(argv, timeout)
        if effect:
            self.log("    ran: " + shlex.join(argv) + f"  -> exit {code}")
        if check and code != 0:
            raise StepFailed(f"{shlex.join(argv[:4])} ... exit {code}: {(err or out).strip()[-400:]}")
        return code, out, err

    def fail(self, message):
        raise StepFailed(message)

    @property
    def run_dir(self):
        return Path(self.state["run_dir"])

    def mkdir(self, rel):
        if self.dry:
            return
        path = self.run_dir / rel
        path.mkdir(parents=True, exist_ok=True, mode=0o700)

    def write_json(self, rel, data):
        if self.dry:
            self.log(f"    would write $RUN/{rel}")
            return
        path = self.run_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")

    def read_json(self, rel):
        return json.loads((self.run_dir / rel).read_text())

    def save_state(self):
        if self.dry or "run_dir" not in self.state:
            return
        tmp = self.run_dir / "state.json.tmp"
        tmp.write_text(json.dumps(self.state, indent=1, sort_keys=True) + "\n")
        os.replace(tmp, self.run_dir / "state.json")

    # -- docker helpers --

    def inspect(self, ref, effect=False):
        code, out, _ = self.sh(["docker", "inspect", "--type", "container", ref], effect=effect, check=False)
        if self.dry and effect:
            return None
        if code != 0:
            return None
        return json.loads(out)[0]

    def image_config(self, ref):
        code, out, _ = self.sh(["docker", "image", "inspect", ref], check=False)
        if code != 0:
            self.fail(f"cannot inspect image {ref}")
        return json.loads(out)[0].get("Config") or {}

    def mount_users(self):
        """Every container (any state) whose mounts overlap a snapshotted mount."""
        code, out, err = self.sh(["docker", "ps", "-aq", "--no-trunc"])
        ids = out.split()
        if not ids:
            return []
        code, out, err = self.sh(["docker", "inspect", "--type", "container", *ids])
        users = []
        for info in json.loads(out):
            if any(self._overlaps(m, mine) for m in info.get("Mounts") or [] for mine in self.state["mounts"]):
                users.append(info)
        return users

    @staticmethod
    def _overlaps(theirs, mine):
        if theirs.get("Type") == "volume" and mine["type"] == "volume" and theirs.get("Name") == mine["name"]:
            return True
        source = theirs.get("Source") or ""
        return bool(source) and (within(source, mine["source"]) or within(mine["source"], source))

    def helper(self, command, live="ro", only=None, timeout=None, check=True):
        """Run command in a throwaway helper as namespace root.

        /private is this run's directory, /recipe is this directory read-only,
        /live/<label> are the snapshotted mounts (read-only unless live='rw').
        """
        argv = ["docker", "run", "--rm", "--network", "none", "--user", "0:0",
                "--security-opt", "no-new-privileges", "--cap-drop", "ALL"]
        for cap in HELPER_CAPS:
            argv += ["--cap-add", cap]
        argv += ["--label", HELPER_LABEL,
                 "--mount", f"type=bind,src={self.state['run_dir']},dst={PRIV}",
                 "--mount", f"type=bind,src={RECIPE},dst={REC},readonly"]
        if live:
            for mount in self.state["mounts"]:
                if only is None or mount["label"] in only:
                    argv += ["--mount", self._mount_arg(mount, readonly=live == "ro")]
        argv += ["--entrypoint", command[0], self.state["helper_image"], *command[1:]]
        return self.sh(argv, effect=True, timeout=timeout or self.args.helper_timeout, check=check)

    @staticmethod
    def _mount_arg(mount, readonly):
        if mount["type"] == "volume":
            spec = f"type=volume,src={mount['name']},dst={LIVE}/{mount['label']},volume-nocopy"
        else:
            spec = f"type=bind,src={mount['source']},dst={LIVE}/{mount['label']}"
        return spec + (",readonly" if readonly else "")

    def evidence_cmd(self, *args):
        return ["python3", "-B", "-I", f"{REC}/snapshot_evidence.py", *args]

    def live_manifest(self, label):
        _, out, _ = self.helper(self.evidence_cmd("manifest", f"{LIVE}/{label}"), only=[label])
        return None if self.dry else json.loads(out)

    def private_manifest(self, rel):
        _, out, _ = self.helper(self.evidence_cmd("manifest", f"{PRIV}/{rel}"), live=None)
        return None if self.dry else json.loads(out)

    def require_equal(self, left, right, what):
        """Archivable entries must be equal; special files are named, not compared.

        GNU tar cannot archive a socket (and the helper cannot recreate a
        device), so a restored tree never holds them. They are excluded from
        the equality check and listed by path in the log; any regular file,
        directory or symlink difference still fails.
        """
        if self.dry:
            return
        diff, excluded = self.compare(left, right)
        if diff:
            shown = ", ".join(diff[:5]) + (", ..." if len(diff) > 5 else "")
            self.fail(f"{what}: {len(diff)} difference(s): {shown}")
        self.log(f"    {what}: manifests equal")
        if excluded:
            self.log(f"    {what}: {len(excluded)} non-archivable special file(s) excluded from the "
                     f"comparison: {describe_non_archivable(excluded)}")

    @staticmethod
    def compare(left, right):
        """(differing archivable paths, excluded special-file paths)."""
        return snapshot_evidence.compare_archivable(left, right)

    # -- quiescence --

    def unit_state(self, unit):
        code, out, _ = self.sh(["systemctl", "--user", "show", "--property=LoadState,ActiveState", unit],
                               check=False)
        values = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
        return values.get("LoadState", "unknown"), values.get("ActiveState", "unknown")

    def stop_pause_units(self):
        for base in self.state["pause_units"]:
            self.sh(["systemctl", "--user", "stop", f"{base}.timer"], effect=True)
            if not self.dry:
                waited = 0
                while self.unit_state(f"{base}.service")[1] in BUSY:
                    if waited >= self.args.mail_wait:
                        self.fail(f"{base}.service still running after {waited}s; not interrupting it")
                    self.system.sleep(5)
                    waited += 5
            self.sh(["systemctl", "--user", "stop", f"{base}.service"], effect=True)

    def confirm_quiescent(self):
        if self.dry:
            self.log("    would confirm: no container using the mounts is running, pause units inactive,"
                     " no own process has a file open under a mount source")
            return
        running = [c["Name"].lstrip("/") for c in self.mount_users() if c["State"]["Running"]]
        if running:
            self.fail(f"mount users still running: {running}")
        for base in self.state["pause_units"]:
            for unit in (f"{base}.timer", f"{base}.service"):
                if self.unit_state(unit)[1] in BUSY:
                    self.fail(f"{unit} is not stopped")
        hits = self.system.open_files_under([m["source"] for m in self.state["mounts"]])
        if hits:
            self.fail(f"host processes still use a mount source: {hits}")
        self.log("    quiescent: no mount user running, pause units stopped, no open files")

    def stop_users(self):
        for info in self.mount_users():
            if info["State"]["Running"]:
                self.sh(["docker", "stop", "--time", str(self.args.stop_timeout), info["Id"]], effect=True)
            else:
                self.log(f"    already stopped: {info['Name'].lstrip('/')}")

    def wait_running(self, name, expect_id):
        if self.dry:
            return
        info = None
        for _ in range(6):
            info = self.inspect(name)
            if info and info["State"]["Running"]:
                break
            self.system.sleep(5)
        if not info or info["Id"] != expect_id:
            self.fail(f"{name} is not the expected container")
        restarts = info.get("RestartCount", 0)
        self.system.sleep(5)
        info = self.inspect(name)
        if not info["State"]["Running"] or info.get("RestartCount", 0) != restarts:
            self.fail(f"{name} did not stay running")
        waited = 0
        while (info["State"].get("Health") or {}).get("Status") == "starting":
            if waited >= self.args.health_wait:
                break
            self.system.sleep(5)
            waited += 5
            info = self.inspect(name)
        health = (info["State"].get("Health") or {}).get("Status")
        if health not in (None, "healthy"):
            self.fail(f"{name} health is {health}")
        self.log(f"    running: {name}" + (f" ({health})" if health else ""))

    # -- archive, evidence and restore test (shared by S1-S3 and A2) --

    def snapshot_mounts(self, prefix):
        """Baseline manifests, space check, raw archives; no database opened."""
        baselines, total = {}, 0
        for mount in self.state["mounts"]:
            label = mount["label"]
            baseline = self.live_manifest(label)
            self.write_json(f"{prefix}/{label}.baseline.json", baseline)
            if baseline is not None:
                special = snapshot_evidence.non_archivable(baseline)
                self.state.setdefault("non_archivable", {}).setdefault(prefix, {})[label] = special
                if special:
                    self.log(f"    {label}: {len(special)} non-archivable special file(s), recorded in the "
                             "manifest but not restored from the archive; excluded from every "
                             f"equality check: {describe_non_archivable(sorted(special), special)}")
                unmapped = overflow_owners(baseline)
                if unmapped:
                    self.fail(f"{label}: {len(unmapped)} entr(y/ies) owned by an ID outside the "
                              "rootless namespace (overflow 65534); a rootless archive cannot preserve them")
                total += sum(e.get("size", 0) for e in baseline["entries"].values())
            baselines[label] = baseline
        if not self.dry:
            need = 3 * total + (512 << 20)
            free = self.system.disk_free(self.run_dir)
            if free < need:
                self.fail(f"private dir has {free} bytes free, needs {need}")
        self.mkdir(prefix)
        for mount in self.state["mounts"]:
            label = mount["label"]
            self.helper([*TAR, "-cpf", f"{PRIV}/{prefix}/{label}.tar", "-C", f"{LIVE}/{label}", "."],
                        only=[label])
            after = self.live_manifest(label)
            self.write_json(f"{prefix}/{label}.after-tar.json", after)
            self.require_equal(baselines[label], after, f"{label} live after tar vs baseline")
        return baselines

    def accepted_per_mount(self, baselines):
        """Assign each --accept-malformed-sqlite pin to the mount(s) holding it.

        A pin applies to a mount only if that mount's baseline manifest has a
        regular file at RELPATH whose SHA-256 equals the pin. A pin that no
        snapshotted mount holds at that hash fails the step: the owner's
        evidence no longer describes the data.
        """
        accepted = self.state.get("accepted_malformed_sqlite") or {}
        per_mount = {m["label"]: {} for m in self.state["mounts"]}
        if self.dry:
            for rel, digest in sorted(accepted.items()):
                self.log(f"    would exempt from the SQLite integrity requirement only: {rel} (sha256 {digest})")
            return per_mount
        for rel, digest in sorted(accepted.items()):
            hits = [label for label in per_mount
                    if (baselines[label]["entries"].get(rel) or {}).get("type") == "f"
                    and baselines[label]["entries"][rel].get("sha256") == digest]
            if not hits:
                self.fail(f"--accept-malformed-sqlite {rel}: no snapshotted mount holds that path "
                          "with the pinned sha256")
            for label in hits:
                per_mount[label][rel] = digest
        return per_mount

    def sqlite_evidence(self, archives, prefix, baselines):
        """SQLite backup + integrity_check on an isolated extract of each archive.

        Returns (backups, accepted): backup hashes by "label/rel", and the
        hash-pinned malformed databases that were exempted, by "label/rel".
        """
        backups, accepted = {}, {}
        per_mount = self.accepted_per_mount(baselines)
        live_args = [x for m in self.state["mounts"] for x in ("--live", f"{LIVE}/{m['label']}")]
        for mount in self.state["mounts"]:
            label = mount["label"]
            inspect_rel = f"{prefix}/inspect/{label}"
            pins = per_mount[label]
            pin_args = [x for rel, digest in sorted(pins.items())
                        for x in ("--accept-malformed", f"{rel}={digest}")]
            self.mkdir(inspect_rel)
            self.helper([*TAR, "-xpf", f"{PRIV}/{archives}/{label}.tar", "-C", f"{PRIV}/{inspect_rel}"],
                        live=None)
            code, out, err = self.helper(self.evidence_cmd(
                "sqlite-evidence", f"{PRIV}/{inspect_rel}", "--out", f"{PRIV}/{prefix}/sqlite/{label}",
                *live_args, *pin_args), check=False)
            if not self.dry:
                if code != 0:
                    failure = f"{prefix}/{label}.sqlite-evidence-failure.json"
                    self.write_json(failure, {"exit": code, "stderr": err.splitlines()})
                    self.fail(f"{label}: sqlite-evidence exit {code}: {err.strip()[-300:]} "
                              f"(every failure: $RUN/{failure})")
                report = json.loads(out)
                files = sum(e["type"] == "f" for e in baselines[label]["entries"].values())
                if report.get("ok") is not True or report.get("files_inspected") != files:
                    self.fail(f"{label}: sqlite-evidence not ok or inspected "
                              f"{report.get('files_inspected')} of {files} files")
                exempted = {}
                for rel, item in report["databases"].items():
                    if item.get("accepted_malformed"):
                        if pins.get(rel) != item.get("sha256"):
                            self.fail(f"{label}: sqlite-evidence exempted a database that was not pinned")
                        exempted[rel] = item["sha256"]
                        continue
                    backup = self.run_dir / prefix / "sqlite" / label / (rel + ".backup")
                    actual = sha256_file(backup)
                    if actual != item["backup_sha256"]:
                        self.fail(f"{label}: backup hash mismatch for a database")
                    backups[f"{label}/{rel}"] = actual
                if exempted != pins:
                    self.fail(f"{label}: pinned malformed database(s) not reported as exempted: "
                              f"{sorted(set(pins) - set(exempted))}")
                accepted.update({f"{label}/{rel}": digest for rel, digest in exempted.items()})
                self.write_json(f"{prefix}/{label}.sqlite-evidence.json", report)
                self.log(f"    {label}: {len(report['databases']) - len(exempted)} database(s) integrity ok, "
                         f"{len(exempted)} hash-pinned malformed exempted, {files} files inspected")
            self.helper(["rm", "-rf", "--one-file-system", f"{PRIV}/{inspect_rel}"], live=None)
        return backups, accepted

    def restore_test(self, archives, prefix, baselines):
        """Fresh extract equals the baseline; the live mount still equals it."""
        for mount in self.state["mounts"]:
            label = mount["label"]
            rel = f"{prefix}/restore-test/{label}"
            self.mkdir(rel)
            self.helper([*TAR, "-xpf", f"{PRIV}/{archives}/{label}.tar", "-C", f"{PRIV}/{rel}"], live=None)
            restored = self.private_manifest(rel)
            self.write_json(f"{prefix}/{label}.restored.json", restored)
            self.require_equal(baselines[label], restored, f"{label} fresh restore vs baseline")
            live = self.live_manifest(label)
            self.write_json(f"{prefix}/{label}.live-after.json", live)
            self.require_equal(baselines[label], live, f"{label} live vs baseline")
            self.helper(["rm", "-rf", "--one-file-system", f"{PRIV}/{rel}"], live=None)

    def baseline(self, label):
        return self.read_json(f"s1/{label}.baseline.json")

    def gate_ok(self):
        if self.dry:
            return True
        path = self.run_dir / "snapshot-receipt.json"
        return path.is_file() and json.loads(path.read_text()).get("snapshot_ok") is True

    def require_gate(self):
        if not self.gate_ok():
            self.fail("snapshot gate not passed: snapshot-receipt.json lacks snapshot_ok true")

    # -- main steps --

    def check_private(self):
        private = Path(os.path.expanduser(self.args.private))
        if not private.is_absolute():
            self.fail("--private must be an absolute path")
        for parent in (private, *private.parents):
            if (parent / ".git").exists():
                self.fail("--private is inside a Git checkout")
        stamp = re.sub(r"[^0-9TZ]", "", self.system.now())
        self.state["private"] = str(private)
        self.state["run_dir"] = str(private / f"run-{stamp}")
        if not private.exists():
            if self.dry:
                self.log(f"    would create {private} (mode 0700)")
                return
            private.mkdir(parents=True, mode=0o700)
        info = os.lstat(private)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            self.fail("--private must be a real directory owned by this user")
        if stat.S_IMODE(info.st_mode) != 0o700:
            if self.dry:
                self.log("    would chmod 0700 the private directory")
            else:
                os.chmod(private, 0o700)
        if self.dry:
            return
        self.lock_handle = _lock(private)
        os.mkdir(self.run_dir, mode=0o700)
        os.chmod(self.run_dir, 0o700)

    def derive_mounts(self, before):
        if anonymous_volumes(before):
            self.fail(f"anonymous volume(s) at {anonymous_volumes(before)}: a recreate would not keep them")
        for mount in (before.get("HostConfig") or {}).get("Mounts") or []:
            if (mount.get("VolumeOptions") or {}).get("Subpath"):
                self.fail("volume subpath mounts are not supported by this driver")
        mounts, excluded = [], []
        for item in sorted(before.get("Mounts") or [], key=lambda m: m["Destination"]):
            kind = item.get("Type")
            if kind == "tmpfs":
                excluded.append({"type": kind, "destination": item["Destination"], "reason": "not persistent"})
                continue
            if kind not in ("volume", "bind"):
                self.fail(f"unsupported mount type {kind!r}")
            for value in (item.get("Name") or "", item.get("Source") or ""):
                if any(ch in value for ch in ',"\n'):
                    self.fail("a mount name or source contains a comma, quote or newline")
            if not item.get("Source"):
                self.fail("a mount has no source")
            if kind == "bind":
                try:
                    mode = os.stat(item["Source"]).st_mode
                except OSError as error:
                    self.fail(f"cannot classify a bind source: {error.strerror}")
                if stat.S_ISSOCK(mode):
                    excluded.append({"type": "bind-socket", "destination": item["Destination"],
                                     "reason": "socket, not data"})
                    continue
                if not stat.S_ISDIR(mode):
                    self.fail(f"bind source for {item['Destination']} is not a directory; "
                              "file binds are not supported by this driver")
            mounts.append({"label": f"m{len(mounts):02d}", "type": kind, "name": item.get("Name"),
                           "source": os.path.normpath(item["Source"]), "destination": item["Destination"],
                           "rw": item.get("RW")})
        for i, a in enumerate(mounts):
            for b in mounts[i + 1:]:
                if within(a["source"], b["source"]) or within(b["source"], a["source"]):
                    self.fail(f"mount sources of {a['label']} and {b['label']} overlap")
        if not mounts:
            self.fail("the container has no persistent mounts to snapshot")
        return mounts, excluded

    def r1_record(self):
        args, st = self.args, self.state
        st.update({"container": args.container, "pause_units": list(args.pause_unit),
                   "new_tag": args.new_tag, "build_python": args.build_python,
                   "accepted_malformed_sqlite": dict(args.accepted_malformed_sqlite),
                   "done": [], "phase": "running"})
        _, out, _ = self.sh(["docker", "info", "--format", "{{json .SecurityOptions}}"], check=False)
        if "name=rootless" not in out:
            self.fail("the Docker daemon is not rootless; this driver needs rootless Docker")
        for value in (str(RECIPE),):
            if "," in value:
                self.fail("the recipe path contains a comma")
        self.check_private()
        if "," in st["run_dir"]:
            self.fail("the private path contains a comma")
        before = self.inspect(args.container)
        if before is None:
            self.fail(f"container {args.container} not found")
        self.before = before
        st.update({"old_id": before["Id"], "old_image": before["Image"],
                   "target_was_running": bool(before["State"]["Running"])})
        st["helper_image"] = args.helper_image or before["Image"]
        for suffix in ("-pre-ocr", "-ocr-failed"):
            if self.inspect(args.container + suffix) is not None:
                self.fail(f"a container named {args.container}{suffix} already exists")
        if self.sh(["docker", "image", "inspect", args.new_tag], check=False)[0] == 0:
            self.fail(f"image tag {args.new_tag} already exists; choose a new tag")
        st["mounts"], st["excluded"] = self.derive_mounts(before)
        st["timers_active"] = {}
        for base in st["pause_units"]:
            for unit in (f"{base}.timer", f"{base}.service"):
                load, active = self.unit_state(unit)
                if load != "loaded":
                    self.fail(f"user unit {unit} is not loaded (LoadState={load}); no sudo path exists")
            st["timers_active"][base] = self.unit_state(f"{base}.timer")[1] == "active"
        users = self.mount_users()
        st["users_running_at_r1"] = [u["Id"] for u in users if u["State"]["Running"] and u["Id"] != before["Id"]]
        self.log(f"    container {args.container}: image {before['Image'][:19]}..., "
                 f"running={st['target_was_running']}")
        self.log(f"    {len(st['mounts'])} mount(s) to snapshot: "
                 + ", ".join(f"{m['label']}={m['type']}:{m['destination']}" for m in st["mounts"]))
        for item in st["excluded"]:
            self.log(f"    not snapshotted: {item['type']}:{item['destination']} ({item['reason']})")
        self.log("    mount users: " + ", ".join(u["Name"].lstrip("/") + ("*" if u["State"]["Running"] else "")
                                                  for u in users) + "  (* running)")
        self.log("    pause timers active: " + json.dumps(st["timers_active"]))
        for rel, digest in sorted(st["accepted_malformed_sqlite"].items()):
            self.log(f"    hash-pinned malformed SQLite (integrity exemption only): {rel} sha256 {digest}")
        self.write_json("inspect-before.json", before)
        self.write_json("mount-users.json", [{"id": u["Id"], "name": u["Name"], "running": u["State"]["Running"]}
                                             for u in users])
        if before["State"]["Running"]:
            _, top, _ = self.sh(["docker", "top", args.container], check=False)
            self.write_json("processes-before.json", {"docker_top": top.splitlines()})
        self.helper(["sh", "-c", "command -v tar >/dev/null && python3 -c 'import sqlite3' && "
                     "tar --acls --xattrs -cf /dev/null --files-from /dev/null"], live=None, timeout=300)
        self.save_state()

    def q1_pause(self):
        self.stop_pause_units()
        if not self.dry:
            for base in self.state["pause_units"]:
                for unit in (f"{base}.timer", f"{base}.service"):
                    if self.unit_state(unit)[1] in BUSY:
                        self.fail(f"{unit} did not stop")
        self.write_json("pause.json", {"paused_at": self.system.now(), "units": self.state["pause_units"]})

    def q2_stop(self):
        self.stop_users()
        self.confirm_quiescent()

    def s1_snapshot(self):
        self.snapshot_mounts("s1")

    def s1_baselines(self):
        return {m["label"]: (None if self.dry else self.baseline(m["label"])) for m in self.state["mounts"]}

    def s2_evidence(self):
        backups, accepted = self.sqlite_evidence("s1", "s2", self.s1_baselines())
        self.state["sqlite_backups"] = backups
        self.state["sqlite_accepted_malformed"] = accepted

    def s3_restore_test(self):
        self.restore_test("s1", "s3", self.s1_baselines())

    def g1_gate(self):
        missing = [s for s in ("S1", "S2", "S3") if s not in self.state["done"]]
        if missing and not self.dry:
            self.fail(f"snapshot steps not passed: {missing}")
        archives, special = {}, {}
        if not self.dry:
            for mount in self.state["mounts"]:
                archives[mount["label"]] = sha256_file(self.run_dir / "s1" / f"{mount['label']}.tar")
                special[mount["label"]] = snapshot_evidence.non_archivable(self.baseline(mount["label"]))
        receipt = {"snapshot_ok": True, "variant": "rootless", "completed_at": self.system.now(),
                   "archives_sha256": archives, "sqlite_backups_sha256": self.state.get("sqlite_backups", {}),
                   "sqlite_accepted_malformed_sha256": self.state.get("sqlite_accepted_malformed", {}),
                   "non_archivable": special,
                   "old_container_id": self.state["old_id"], "old_image": self.state["old_image"]}
        self.write_json("snapshot-receipt.json", receipt)
        self.state["archives_sha256"] = archives

    def b1_wheel(self):
        self.require_gate()
        _, out, _ = self.sh([sys.executable, "-B", RECIPE / "build_wheel.py", "--build-python",
                             self.state["build_python"], "--provenance-out",
                             Path(self.state["run_dir"]) / "wheel-provenance.json"], effect=True, timeout=3600)
        if self.dry:
            self.state["wheel_sha256"] = "<WHEEL_SHA256>"
            return
        lines = [l for l in out.splitlines() if l.startswith("WHEEL_SHA256=")]
        value = lines[-1].split("=", 1)[1].strip() if lines else ""
        if not HEX64_RE.fullmatch(value):
            self.fail("build_wheel.py did not print WHEEL_SHA256")
        self.state["wheel_sha256"] = value

    def b2_image(self):
        self.require_gate()
        st = self.state
        commit = self.pins["engine"]["commit"]
        self.sh([sys.executable, "-B", RECIPE / "preflight.py", "--base-image", st["old_image"],
                 "--engine-commit", commit, "--wheel-sha256", st["wheel_sha256"], "--tag", st["new_tag"]],
                effect=True, timeout=300)
        command = preflight.build_command(argparse.Namespace(
            base_image=st["old_image"], engine_commit=commit, wheel_sha256=st["wheel_sha256"],
            engine_manifest_sha256=self.pins["engine"]["manifest_sha256"], tag=st["new_tag"]))
        self.sh(command, effect=True, timeout=7200)
        _, out, _ = self.sh(["docker", "image", "inspect", "--format", "{{.Id}}", st["new_tag"]], effect=True)
        st["new_image_id"] = "<NEW_IMAGE_ID>" if self.dry else out.strip()
        if not self.dry and not IMAGE_ID_RE.fullmatch(st["new_image_id"]):
            self.fail("could not resolve the new image ID")

    def v1_verify_image(self):
        self.require_gate()
        image = self.state["new_image_id"]
        _, out, _ = self.sh([sys.executable, "-B", RECIPE / "verify.py", "--image", image],
                            effect=True, timeout=900)
        if not self.dry and "verify: OK" not in out:
            self.fail("verify.py --image did not report verify: OK")
        _, out, _ = self.sh(["docker", "run", "--rm", "--network", "none", "--entrypoint", "find", image,
                             "/opt/campaign-tool", "-maxdepth", "2"], effect=True, timeout=300)
        if self.dry:
            return
        root = "/opt/campaign-tool"
        allowed = {root, f"{root}/ENGINE_COMMIT", f"{root}/requirements.lock", f"{root}/venv", f"{root}/dist",
                   f"{root}/dist/{self.pins['engine']['wheel_filename']}"}
        listed = set(out.split())
        extra = sorted(p for p in listed if p not in allowed and not p.startswith(f"{root}/venv/"))
        if extra or f"{root}/dist/{self.pins['engine']['wheel_filename']}" not in listed:
            self.fail(f"/opt/campaign-tool listing is not exactly the engine layout: {extra}")

    def c1_rename(self):
        self.require_gate()
        name = self.state["container"]
        self.sh(["docker", "rename", name, f"{name}-pre-ocr"], effect=True)
        info = self.inspect(f"{name}-pre-ocr", effect=True)
        if not self.dry and (info is None or info["Id"] != self.state["old_id"]):
            self.fail("renamed container is not the recorded old container")

    def c2_create(self):
        self.require_gate()
        st = self.state
        before = self.before or self.read_json("inspect-before.json")
        body = create_body(before, st["new_image_id"])
        path = "/containers/create?name=" + urllib.parse.quote(st["container"])
        if self.dry:
            self.log(f"    would POST {path} (saved Config/HostConfig/endpoints, Image=<NEW_IMAGE_ID>)")
            self.log("    would diff docker inspect against inspect-before.json")
            return
        status, response = self.system.docker_api("POST", path, body)
        self.log(f"    POST {path} -> {status}")
        if status != 201 or not response.get("Id"):
            self.fail(f"create failed: {status} {str(response.get('message', ''))[:300]}")
        st["new_id"] = response["Id"]
        self.save_state()
        after = self.inspect(st["new_id"])
        diff = inspect_diff(before, after, self.image_config(st["old_image"]), self.image_config(st["new_image_id"]))
        self.write_json("inspect-new.json", after)
        self.write_json("create-diff.json", diff)
        if after["Image"] != st["new_image_id"] or after["State"]["Running"]:
            self.fail("the created container is not the new image or is running")
        if diff:
            self.fail(f"replacement settings differ from the saved ones: {diff}")
        self.log("    created; settings equal apart from image, ID, creation and state")

    def c3_start(self):
        self.require_gate()
        self.sh(["docker", "start", self.state["container"]], effect=True)
        self.wait_running(self.state["container"], self.state.get("new_id"))

    def a1_requiesce(self):
        self.sh(["docker", "stop", "--time", str(self.args.stop_timeout), self.state["container"]], effect=True)
        self.stop_users()
        self.confirm_quiescent()

    def a2_accept_data(self):
        baselines = self.snapshot_mounts("a2")
        self.sqlite_evidence("a2", "a2", baselines)
        self.restore_test("a2", "a2", baselines)
        if self.dry:
            self.log("    would compare each a2 baseline with the s1 baseline; any difference holds for review")
            return
        diff, excluded = {}, {}
        for mount in self.state["mounts"]:
            changed, special = self.compare(self.baseline(mount["label"]), baselines[mount["label"]])
            if changed:
                diff[mount["label"]] = changed
            if special:
                excluded[mount["label"]] = special
        for label, special in sorted(excluded.items()):
            self.log(f"    {label}: {len(special)} non-archivable special file(s) excluded from the S1 "
                     f"comparison: {describe_non_archivable(special)}")
        self.write_json("a2/diff.json", diff)
        if diff:
            digest = sha256_file(self.run_dir / "a2" / "diff.json")
            self.state["a2_diff_sha256"] = digest
            raise Held(f"{sum(map(len, diff.values()))} path(s) differ from the S1 snapshot in "
                       f"{len(diff)} mount(s); review {self.run_dir / 'a2' / 'diff.json'} (sha256 {digest})")
        self.log("    current state equals the S1 snapshot")

    def a3_restart(self):
        self.sh(["docker", "start", self.state["container"]], effect=True)
        self.wait_running(self.state["container"], self.state.get("new_id"))

    def a4_reverify(self):
        _, out, _ = self.sh([sys.executable, "-B", RECIPE / "verify.py", "--container", self.state["container"]],
                            effect=True, timeout=900)
        if not self.dry and "verify: OK" not in out:
            self.fail("verify.py --container did not report verify: OK")

    def release_writers(self):
        for ident in self.state.get("users_running_at_r1", []):
            self.sh(["docker", "start", ident], effect=True)
        for base, active in self.state.get("timers_active", {}).items():
            if active:
                self.sh(["systemctl", "--user", "start", f"{base}.timer"], effect=True)
            else:
                self.log(f"    {base}.timer was not active at R1; left stopped")

    # -- rollback steps --

    def rb_confirm_stopped(self, path):
        st = self.state
        current = self.inspect(st["container"])
        if path == "RB-B" and current is not None and current["Id"] != st["old_id"]:
            if current["State"]["Running"]:
                raise StepFailed("a replacement is running in a path where it never started")
            self.sh(["docker", "rename", st["container"], f"{st['container']}-ocr-failed"], effect=True)
        if "S1" not in st.get("started", []):
            # Failure in Q1/Q2: nothing was copied, opened or restored. The
            # path's start and release steps return every user to its R1 state.
            self.log("    no data step started; nothing to confirm before restoring the R1 state")
            return
        self.confirm_quiescent()

    def rb_validate_data(self, path):
        mounts = [m for m in self.state["mounts"] if (self.run_dir / "s1" / f"{m['label']}.baseline.json").is_file()]
        if not mounts:
            self.log("    S1 never wrote a baseline: nothing was copied, opened or restored")
            return
        for mount in mounts:
            label = mount["label"]
            live = self.live_manifest(label)
            if not self.compare(self.baseline(label), live)[0]:
                self.log(f"    {label}: live equals the S1 baseline")
                continue
            if path == "RB-C":
                raise StepFailed(f"{label}: live differs from the S1 baseline after restore")
            if not self.gate_ok():
                raise StepFailed(f"{label}: live data changed while every user was stopped and no "
                                 "gate-verified archive exists; leaving everything stopped")
            self.restore_mount(label)
            self.require_equal(self.baseline(label), self.live_manifest(label), f"{label} restored vs baseline")

    def rb_start_old(self, path):
        st = self.state
        info = self.inspect(st["container"])
        if info is None or info["Id"] != st["old_id"] or info["Image"] != st["old_image"]:
            raise StepFailed(f"{st['container']} is not the recorded old container and image")
        if not st.get("target_was_running"):
            self.log(f"    {st['container']} was not running at R1; left stopped")
            return
        self.sh(["docker", "start", st["container"]], effect=True)
        self.wait_running(st["container"], st["old_id"])

    def rb_rename_old_back(self, path):
        st = self.state
        old = self.inspect(f"{st['container']}-pre-ocr")
        if old is None:
            current = self.inspect(st["container"])
            if current is None or current["Id"] != st["old_id"]:
                raise StepFailed("the old container is neither renamed nor under its name")
            self.log("    rename never happened; old container keeps its name")
            return
        if old["Id"] != st["old_id"]:
            raise StepFailed(f"{st['container']}-pre-ocr is not the recorded old container")
        if self.inspect(st["container"]) is not None:
            raise StepFailed(f"{st['container']} is still taken; the replacement was not set aside")
        self.sh(["docker", "rename", f"{st['container']}-pre-ocr", st["container"]], effect=True)

    def rb_stop_all(self, path):
        self.stop_pause_units()
        self.stop_users()
        self.confirm_quiescent()

    def rb_set_aside_new(self, path):
        st = self.state
        current = self.inspect(st["container"])
        if current is not None and current["Id"] != st["old_id"]:
            if current["State"]["Running"]:
                raise StepFailed("the replacement is still running")
            self.sh(["docker", "rename", st["container"], f"{st['container']}-ocr-failed"], effect=True)
        else:
            self.log("    no replacement under the container name")

    def rb_compare_data(self, path):
        self.rbc_differs = []
        for mount in self.state["mounts"]:
            label = mount["label"]
            diff, excluded = self.compare(self.baseline(label), self.live_manifest(label))
            if diff:
                self.rbc_differs.append(label)
            if excluded:
                self.log(f"    {label}: {len(excluded)} non-archivable special file(s) excluded from the "
                         f"comparison: {describe_non_archivable(excluded)}")
        self.write_json("rollback/compare.json", {"differs": self.rbc_differs})
        self.log(f"    mounts differing from S1: {self.rbc_differs or 'none'}")

    def rb_restore(self, path):
        for label in self.rbc_differs:
            self.restore_mount(label)
        if not self.rbc_differs:
            self.log("    nothing to restore")

    # Preservation of the pre-restore state. Each capture is a numbered attempt
    # directory $RUN/rollback/<label>/attempt-NNN holding current.json (live
    # manifest), current.tar and, written last, receipt.json. An attempt
    # directory is created exclusively and never written again: a retried or
    # interrupted rollback adds a new attempt instead of truncating an old
    # one, so the first capture (the only copy of replacement-only work) can
    # never be replaced by an empty or partial restore.

    def _attempt_dirs(self, label):
        root = self.run_dir / "rollback" / label
        if not root.is_dir():
            return []
        return sorted(p for p in root.iterdir() if p.is_dir() and ATTEMPT_RE.fullmatch(p.name))

    def validated_preservations(self, label):
        """Validate every receipted attempt for label; return them oldest first.

        Any receipted attempt whose files no longer match its receipt (or the
        hashes recorded in state) stops the rollback before live data is
        touched. Attempts without a receipt were interrupted before they were
        verified; they are kept untouched and never relied on.
        """
        recorded = {a["attempt"]: a for a in self.state.get("preservations", {}).get(label, [])}
        valid = []
        for path in self._attempt_dirs(label):
            receipt_path = path / "receipt.json"
            if not receipt_path.is_file():
                self.log(f"    {label}: {path.name} has no receipt (interrupted capture); kept untouched, not used")
                continue
            receipt = json.loads(receipt_path.read_text())
            problems = []
            if receipt.get("label") != label or receipt.get("attempt") != path.name:
                problems.append("receipt names another attempt")
            for name, key in (("current.tar", "archive_sha256"), ("current.json", "manifest_sha256")):
                f = path / name
                if not f.is_file() or sha256_file(f) != receipt.get(key):
                    problems.append(f"{name} differs from its receipt")
            known = recorded.get(path.name)
            if known is not None and any(known.get(k) != receipt.get(k)
                                         for k in ("archive_sha256", "manifest_sha256")):
                problems.append("receipt differs from the hashes recorded in state.json")
            if problems:
                raise StepFailed(f"{label}: preserved {path.name} failed validation ({'; '.join(problems)}); "
                                 "leaving live data untouched")
            valid.append((path, receipt))
        missing = set(recorded) - {p.name for p, _ in valid}
        if missing:
            raise StepFailed(f"{label}: preserved attempt(s) {sorted(missing)} recorded in state.json are "
                             "missing or unreceipted; leaving live data untouched")
        return valid

    def preserve_current(self, label, current):
        """Capture live <label> into a new, exclusive attempt directory and verify it."""
        dirs = self._attempt_dirs(label)
        number = 1 + max((int(ATTEMPT_RE.fullmatch(p.name).group(1)) for p in dirs), default=0)
        name = f"attempt-{number:03d}"
        rel = f"rollback/{label}/{name}"
        path = self.run_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.mkdir(mode=0o700)  # exclusive: never reuse an attempt directory
        (path / "current.json").write_text(json.dumps(current, indent=1, sort_keys=True) + "\n")
        self.helper([*TAR, "-cpf", f"{PRIV}/{rel}/current.tar", "-C", f"{LIVE}/{label}", "."], only=[label])
        check_rel = f"rollback/verify/{label}-{name}"
        self.mkdir(check_rel)
        self.helper([*TAR, "-xpf", f"{PRIV}/{rel}/current.tar", "-C", f"{PRIV}/{check_rel}"], live=None)
        self.require_equal(current, self.private_manifest(check_rel), f"{label} {name} current-state archive")
        self.helper(["rm", "-rf", "--one-file-system", f"{PRIV}/{check_rel}"], live=None)
        receipt = {"label": label, "attempt": name, "created": self.system.now(),
                   "archive_sha256": sha256_file(path / "current.tar"),
                   "manifest_sha256": sha256_file(path / "current.json"),
                   "non_archivable": snapshot_evidence.non_archivable(current),
                   "verified": "extracted to an isolated directory; manifest equal to live"}
        tmp = path / "receipt.json.tmp"
        tmp.write_text(json.dumps(receipt, indent=1, sort_keys=True) + "\n")
        os.replace(tmp, path / "receipt.json")
        for f in ("current.tar", "current.json", "receipt.json"):
            os.chmod(path / f, 0o400)
        self.state.setdefault("preservations", {}).setdefault(label, []).append(
            {k: receipt[k] for k in ("attempt", "archive_sha256", "manifest_sha256", "created")})
        self.save_state()
        self.log(f"    {label}: current state preserved and verified in $RUN/{rel}")
        return path

    def restore_mount(self, label):
        """Preserve the current state (immutably), verify it, then restore S1."""
        if not self.gate_ok():
            raise StepFailed("refusing to restore without a passed snapshot gate")
        receipt = self.read_json("snapshot-receipt.json")
        archive = self.run_dir / "s1" / f"{label}.tar"
        if sha256_file(archive) != receipt["archives_sha256"].get(label):
            raise StepFailed(f"{label}: S1 archive hash differs from the receipt")
        previous = self.validated_preservations(label)
        current = self.live_manifest(label)
        match = next((p for p, _ in reversed(previous)
                      if not self.compare(json.loads((p / "current.json").read_text()), current)[0]),
                     None)
        if match is not None:
            self.log(f"    {label}: live already preserved in $RUN/rollback/{label}/{match.name}; not re-archived")
        else:
            match = self.preserve_current(label, current)
        # Invariant before any destructive step: a receipted, validated
        # attempt holds exactly the live state about to be cleared.
        self.state.setdefault("restoring", {})[label] = {"preserved_in": match.name, "clear_started": True}
        self.save_state()
        self.helper(["find", f"{LIVE}/{label}", "-mindepth", "1", "-delete"], live="rw", only=[label])
        self.helper([*TAR, "-xpf", f"{PRIV}/s1/{label}.tar", "-C", f"{LIVE}/{label}"], live="rw", only=[label])
        self.state["restoring"][label]["extracted"] = True
        self.save_state()
        kept = [p.name for p, _ in previous if p.name != match.name]
        self.log(f"    {label}: S1 restored; preserved states in $RUN/rollback/{label}/: "
                 f"{', '.join(kept + [match.name])}")
        cleared = snapshot_evidence.non_archivable(current)
        if cleared:
            self.log(f"    {label}: {len(cleared)} non-archivable special file(s) were cleared with the mount; "
                     f"S1 does not restore sockets: {describe_non_archivable(sorted(cleared), cleared)}")

    # -- runners --

    def run_main(self, start_after=None):
        steps = self.plan["main"]
        if start_after is not None:
            steps = steps[[s["id"] for s in steps].index(start_after) + 1:]
        for step in steps:
            self.log(f"[{step['id']}] {step['kind']}: {step.get('does', '')}")
            self.state["current"] = step["id"]
            self.state.setdefault("started", []).append(step["id"])
            self.save_state()
            try:
                self.main_impl[step["id"]]()
            except Held as held:
                self.state["phase"] = "held_a2"
                self.save_state()
                self.log(f"HOLD at [{step['id']}]: {held}")
                self.log("  Everything stays stopped. After owner review run --resume with "
                         "--accept-a2 <sha256>, or --rollback.")
                return EXIT_HELD
            except Exception as error:  # every failure takes the step's path
                detail = str(error) if isinstance(error, StepFailed) else f"{type(error).__name__}: {error}"
                self.log(f"FAIL at [{step['id']}]: {detail}")
                if step["on_fail"] == "stop_unchanged" or self.dry:
                    self.state["phase"] = "refused"
                    self.save_state()
                    self.log("  Nothing was changed.")
                    return EXIT_REFUSED
                return self.rollback(step["on_fail"])
            self.state.setdefault("done", []).append(step["id"])
            self.save_state()
        if self.dry:
            self.log("Rollback paths (taken on failure of the step that names them):")
            for name, steps in self.plan["rollback"].items():
                for step in steps:
                    self.log(f"  [{step['id']}] {step['kind']}: {step.get('does', '')}")
            self.log("dry-run: OK (nothing was changed)")
            return EXIT_OK
        self.state["phase"] = "complete"
        self.save_state()
        self.log(f"swap: OK. Keep {self.state['container']}-pre-ocr, the old image and $RUN until the owner "
                 "signs off; then remove them. Watch the next coordinator and mail job complete.")
        return EXIT_OK

    def rollback(self, path):
        self.log(f"ROLLBACK {path}")
        self.state["phase"] = f"rollback:{path}"
        self.save_state()
        for step in self.plan["rollback"][path]:
            self.log(f"[{step['id']}] {step['kind']}: {step.get('does', '')}")
            self.state["rollback_current"] = step["id"]
            self.save_state()
            try:
                self.rollback_impl[step["kind"]](path)
            except Exception as error:
                detail = str(error) if isinstance(error, StepFailed) else f"{type(error).__name__}: {error}"
                self.state["phase"] = "escalated"
                self.save_state()
                self.log(f"ESCALATE at [{step['id']}]: {detail}")
                self.log("  Rollback stopped. Leave every mount user stopped and the pause units stopped; "
                         "the owner decides the next action.")
                return EXIT_ESCALATE
        self.state["phase"] = "rolled_back"
        self.save_state()
        self.log(f"rollback {path}: complete; the old container and writers are restored")
        return EXIT_ROLLED_BACK

    def resume(self):
        st = self.state
        if st.get("phase") != "held_a2":
            self.log(f"refused: run phase is {st.get('phase')!r}, not held_a2")
            return EXIT_REFUSED
        accepted = (self.args.accept_a2 or "").strip()
        actual = sha256_file(self.run_dir / "a2" / "diff.json")
        if not accepted or accepted != st.get("a2_diff_sha256") or accepted != actual:
            self.log("refused: --accept-a2 must equal the sha256 of the reviewed $RUN/a2/diff.json")
            return EXIT_REFUSED
        self.log("[A2] resume: owner accepted the reviewed differences; re-checking quiescence and data")
        try:
            self.confirm_quiescent()
            for mount in st["mounts"]:
                label = mount["label"]
                self.require_equal(self.read_json(f"a2/{label}.baseline.json"), self.live_manifest(label),
                                   f"{label} live vs reviewed a2 baseline")
        except Exception as error:
            self.log(f"FAIL at [A2] resume: {error}")
            return self.rollback("RB-C")
        st["a2_accepted"] = accepted
        st["phase"] = "running"
        st["done"].append("A2")
        self.save_state()
        return self.run_main(start_after="A2")

    def rollback_held(self):
        phase = self.state.get("phase")
        if phase in ("complete", "rolled_back"):
            self.log(f"refused: run phase is {phase}; a later rollback is the owner's manual runbook")
            return EXIT_REFUSED
        current = self.state.get("current")
        step = next(s for s in self.plan["main"] if s["id"] == current)
        if step["on_fail"] == "stop_unchanged":
            self.log("refused: the run stopped at R1; nothing changed")
            return EXIT_REFUSED
        return self.rollback(step["on_fail"])


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", dest="mode", action="store_const", const="dry-run")
    mode.add_argument("--apply", dest="mode", action="store_const", const="apply")
    mode.add_argument("--resume", metavar="RUN_DIR", help="continue a run held at A2")
    mode.add_argument("--rollback", metavar="RUN_DIR", help="roll back a held or interrupted run")
    parser.add_argument("--container", help="target container name")
    parser.add_argument("--pause-unit", action="append", default=[],
                        help="systemd --user unit base name; BASE.timer is stopped, then BASE.service")
    parser.add_argument("--private", help="owner-private directory outside Git (created 0700)")
    parser.add_argument("--new-tag", help="new image tag that does not exist yet (name:tag)")
    parser.add_argument("--build-python", help="interpreter with the pinned wheel build tools")
    parser.add_argument("--helper-image", help="image for data helpers (default: the old image ID)")
    parser.add_argument("--accept-a2", help="sha256 of the reviewed a2/diff.json (with --resume)")
    parser.add_argument("--mail-wait", type=int, default=900, help="seconds to wait for a running service")
    parser.add_argument("--stop-timeout", type=int, default=120, help="docker stop --time")
    parser.add_argument("--health-wait", type=int, default=300, help="seconds to wait for a health check")
    parser.add_argument("--helper-timeout", type=int, default=7200, help="seconds per helper command (tar, manifest)")
    parser.add_argument("--accept-malformed-sqlite", action="append", default=[], metavar="RELPATH=SHA256",
                        help="exempt one known, intentionally corrupt database from the SQLite integrity "
                             "requirement only; RELPATH is relative to its mount, SHA256 pins its bytes "
                             "(repeatable; --apply/--dry-run)")
    args = parser.parse_args(argv)
    try:
        args.accepted_malformed_sqlite = snapshot_evidence.parse_accepted(args.accept_malformed_sqlite)
    except ValueError as error:
        parser.error(str(error).replace("--accept-malformed ", "--accept-malformed-sqlite ", 1))
    if args.resume or args.rollback:
        if args.accept_malformed_sqlite:
            parser.error("--accept-malformed-sqlite is fixed at --apply; the run's state.json keeps it")
        args.run_dir = args.resume or args.rollback
        args.mode = "resume" if args.resume else "rollback"
        return args
    missing = [f"--{n.replace('_', '-')}" for n in ("container", "private", "new_tag", "build_python")
               if not getattr(args, n)]
    if not args.pause_unit:
        missing.append("--pause-unit")
    if missing:
        parser.error("required: " + ", ".join(missing))
    if not NAME_RE.fullmatch(args.container):
        parser.error("--container must be a plain container name")
    for base in args.pause_unit:
        if not UNIT_RE.fullmatch(base) or base.endswith((".timer", ".service")):
            parser.error("--pause-unit takes the unit base name without .timer/.service")
    if not TAG_RE.fullmatch(args.new_tag):
        parser.error("--new-tag must be name:tag")
    if args.helper_image and not (IMAGE_ID_RE.fullmatch(args.helper_image) or "@sha256:" in args.helper_image):
        parser.error("--helper-image must be an image ID or digest reference")
    return args


def _lock(private):
    handle = open(Path(private) / ".flock-swap.lock", "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise SystemExit("another rootless_swap.py run holds the lock")
    return handle


def main(argv=None, system=None, out=None):
    args = parse_args(argv)
    swap = Swap(args, system=system, out=out)
    if args.mode in ("resume", "rollback"):
        run_dir = Path(args.run_dir).resolve()
        state = json.loads((run_dir / "state.json").read_text())
        if Path(state["run_dir"]).resolve() != run_dir:
            swap.log("refused: state.json belongs to another run directory")
            return EXIT_REFUSED
        swap.state = state
        swap.lock_handle = _lock(state["private"])
        try:
            return swap.resume() if args.mode == "resume" else swap.rollback_held()
        finally:
            swap.lock_handle.close()
    swap.log("rootless_swap: DRY RUN (read-only discovery; nothing below is executed)"
             if swap.dry else "rootless_swap: APPLY")
    try:
        return swap.run_main()
    finally:
        if swap.lock_handle is not None:
            swap.lock_handle.close()


if __name__ == "__main__":
    sys.exit(main())
