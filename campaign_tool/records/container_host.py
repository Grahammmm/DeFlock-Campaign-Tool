"""Private, inert per-run Docker lifecycle candidate. No deployment activation."""
import argparse
from contextlib import contextmanager
from datetime import date
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import selectors
import stat
import subprocess
import sys
import time

MODULE = "campaign_tool.records.container_host"
WORKER_MODULE = "campaign_tool.records.worker_lifecycle"
MAX_REPLY = 256 * 1024
MAX_CARDS = 100000
MAX_BOARD = 64 * 1024 * 1024
STAGES = ("preserve", "extract", "catalog", "detect", "review", "compare", "privacy")
STATES = ("pending", "in_progress", "done", "blocked", "inapplicable", "unknown")
SHA = re.compile(r"[0-9a-f]{64}")
JOB = re.compile(r"[0-9a-f]{32}")
WORKER_CODES = {"completed": 0, "worker_failed": 10, "cancelled": 20,
                "timed_out": 21, "invalid_state": 30, "unsupported": 31,
                "not_quiescent": 32, "capacity_exceeded": 33}
PROFILE_KEYS = {"schema", "name_prefix", "image_id", "release_commit", "package_version",
                "dependency_lock_sha256", "docker_path", "python", "release_root",
                "parser_path", "tmp_dir", "root", "mail_config", "worker_state_parent",
                "board_dir", "host_receipts", "wall_seconds", "owner_launch_approved",
                "mounts", "seccomp_path", "seccomp_sha256", "memory_bytes", "pids_limit", "cpus", "network"}
PATH_KEYS = {"docker_path", "python", "release_root", "parser_path", "tmp_dir", "root",
             "mail_config", "worker_state_parent", "board_dir", "host_receipts", "seccomp_path"}
RECEIPT_KEYS = {"schema", "job_id", "profile_sha256", "container_id", "phase", "hold",
                "launch_started", "quiescent", "ack", "worker_status", "worker_exit",
                "board", "health", "healthy", "cards", "error", "cancel_requested",
                "worker_removed", "reporter_id", "reporter_removed", "worker_cgroup", "reporter_cgroup"}
PHASES = {"prepared", "preflight", "launching", "quiescent", "postrun", "finished", "hold"}


class Rejected(Exception):
    """Fixed-state rejection, never carrying private input text."""


class TransportError(Exception):
    """Docker-client failure is not container-worker quiescence."""


def require(condition):
    if not condition:
        raise Rejected()


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _pairs(items):
    result = {}
    for key, value in items:
        require(key not in result)
        result[key] = value
    return result


def decoded(raw, limit=MAX_REPLY):
    require(len(raw) <= limit)
    return json.loads(raw, object_pairs_hook=_pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(Rejected()))


def _absolute(path):
    require(isinstance(path, str) and len(path) <= 4096 and path.startswith("/"))
    parts = path.split("/")[1:]
    require(parts and all(part not in ("", ".", "..") for part in parts)
            and not any(ord(character) < 32 for character in path))
    return parts


def _fingerprint(value):
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


class PrivateDir:
    """No symlinks, trusted non-writable parents, pinned owner-only final directory."""
    def __init__(self, path, *, private=True):
        self.path = os.fspath(path)
        parts = [] if self.path == "/" and not private else _absolute(self.path)
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            value = os.fstat(fd)
            for part in parts:
                new = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                              dir_fd=fd)
                os.close(fd)
                fd = new
                value = os.fstat(fd)
                require(value.st_uid in (0, os.getuid()) and not value.st_mode & 0o022)
            value = os.fstat(fd)
            if private:
                require(value.st_uid == os.getuid() and stat.S_IMODE(value.st_mode) == 0o700)
            self.fd, self.private = fd, private
            self.inode = (value.st_dev, value.st_ino)
        except BaseException:
            os.close(fd)
            raise

    def check(self):
        value = os.fstat(self.fd)
        named = os.stat(self.path, follow_symlinks=False)
        require(stat.S_ISDIR(named.st_mode) and (named.st_dev, named.st_ino) == self.inode
                and value.st_uid in (0, os.getuid()) and not value.st_mode & 0o022)
        if self.private:
            require(value.st_uid == os.getuid() and stat.S_IMODE(value.st_mode) == 0o700)

    def open_file(self, name, *, create=False):
        self.check()
        require("/" not in name and name not in ("", ".", ".."))
        flags = os.O_RDWR if create else os.O_RDONLY
        flags |= os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
        if create:
            flags |= os.O_CREAT
        fd = os.open(name, flags, 0o600, dir_fd=self.fd)
        try:
            value = os.fstat(fd)
            require(stat.S_ISREG(value.st_mode) and value.st_uid == os.getuid()
                    and value.st_nlink == 1 and stat.S_IMODE(value.st_mode) == 0o600)
            return fd
        except BaseException:
            os.close(fd)
            raise

    def read(self, name, limit=MAX_REPLY):
        fd = self.open_file(name)
        try:
            before = os.fstat(fd)
            require(before.st_size <= limit)
            raw = os.read(fd, limit + 1)
            require(len(raw) == before.st_size and _fingerprint(before) == _fingerprint(os.fstat(fd)))
            return raw, _fingerprint(before)
        finally:
            os.close(fd)

    def write(self, name, raw, *, fresh=False):
        self.check()
        require("/" not in name and name not in ("", ".", "..") and len(raw) <= MAX_BOARD)
        temporary = ".atomic-" + os.urandom(16).hex()
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                     0o600, dir_fd=self.fd)
        try:
            os.fchmod(fd, 0o600)
            view = memoryview(raw)
            while view:
                written = os.write(fd, view)
                require(written > 0)
                view = view[written:]
            os.fsync(fd)
            if fresh:
                os.link(temporary, name, src_dir_fd=self.fd, dst_dir_fd=self.fd, follow_symlinks=False)
            else:
                # Refuse non-owner, symlink, hardlinked or malformed replacement targets.
                try:
                    old = self.open_file(name)
                except FileNotFoundError:
                    os.link(temporary, name, src_dir_fd=self.fd, dst_dir_fd=self.fd, follow_symlinks=False)
                else:
                    os.close(old)
                    os.replace(temporary, name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
            os.fsync(self.fd)
        finally:
            os.close(fd)
            try:
                os.unlink(temporary, dir_fd=self.fd)
            except FileNotFoundError:
                pass  # os.replace already consumed this temporary name.

    def child(self, name, *, fresh=False):
        require(JOB.fullmatch(name) is not None)
        if fresh:
            self.check()
            os.mkdir(name, 0o700, dir_fd=self.fd)
            os.fsync(self.fd)
        return PrivateDir(self.path + "/" + name)

    def close(self):
        os.close(self.fd)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


@contextmanager
def locked(directory, name, *, wait=0):
    fd = directory.open_file(name, create=True)
    deadline = time.monotonic() + wait
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise Rejected() from None
                time.sleep(.01)
        yield
    finally:
        os.close(fd)


def input_selection(paths):
    """Fixed selectors only. Legacy profiles/entrypoints remain mailbox mode."""
    mode = paths.get("input_mode") or "mailbox"
    mail, inbox, manifest = (paths.get(k) for k in ("mail_config", "inbox", "inbox_manifest_sha256"))
    require(mode in {"mailbox", "inbox"})
    if mode == "mailbox":
        require(inbox is None and manifest is None)
        _absolute(mail)
        return mode, "mail_config", mail, None
    require(mail is None and isinstance(manifest, str) and SHA.fullmatch(manifest))
    _absolute(inbox)
    return mode, "inbox", inbox, manifest


def input_arguments(paths):
    mode, key, path, manifest = input_selection(paths)
    if mode == "mailbox":
        return ["--mail-config", path]
    return ["--input-mode", "inbox", "--inbox", path,
            "--inbox-manifest-sha256", manifest]


def inbox_snapshot(path, *, expected=None, readonly=False, prior=None):
    """Bounded flat owner-only EML snapshot; no MIME parsing or callbacks."""
    entries, signatures, total = [], [], 0
    with PrivateDir(path) as directory:
        before = _fingerprint(os.fstat(directory.fd))
        if readonly:
            require(bool(os.fstatvfs(directory.fd).f_flag & os.ST_RDONLY))
        names = sorted(os.listdir(directory.fd))
        require(len(names) <= 200)
        for name in names:
            require(name.endswith(".eml") and len(name) <= 255
                    and not any(ord(c) < 32 for c in name))
            fd = directory.open_file(name)
            try:
                value = os.fstat(fd)
                require(0 < value.st_size <= 64 * 1024 * 1024)
                total += value.st_size
                require(total <= 256 * 1024 * 1024)
                if readonly:
                    require(bool(os.fstatvfs(fd).f_flag & os.ST_RDONLY))
                signatures.append((name, _fingerprint(value)))
                if prior is None:
                    hasher, consumed = hashlib.sha256(), 0
                    while True:
                        chunk = os.read(fd, min(1024 * 1024, value.st_size - consumed + 1))
                        if not chunk:
                            break
                        consumed += len(chunk)
                        require(consumed <= value.st_size)
                        hasher.update(chunk)
                    require(consumed == value.st_size)
                    entries.append({"name": name, "bytes": consumed, "sha256": hasher.hexdigest()})
                require(_fingerprint(value) == _fingerprint(os.fstat(fd)))
            finally:
                os.close(fd)
        directory.check()
        require(before == _fingerprint(os.fstat(directory.fd)))
    signature = (before, tuple(signatures))
    if prior is not None:
        require(signature == prior["signature"])
        result = prior
    else:
        result = {"sha256": digest(encoded(entries)), "messages": len(entries),
                  "bytes": total, "signature": signature}
    require(expected is None or result["sha256"] == expected)
    return result


def _overlaps(first, second):
    return first == second or first.startswith(second + "/") or second.startswith(first + "/")


def inbox_mount(value):
    matches = [m for m in value["mounts"] if m["target"] == value["inbox"]]
    require(len(matches) == 1 and matches[0]["readonly"] is True)
    return matches[0]


def check_inbox_mounts(value, runtime):
    """Verify configured provenance and deepest realized read-only coverage."""
    expected = inbox_mount(value)
    actual, configured = runtime.get("mounts"), runtime.get("configured_mounts")
    require(isinstance(actual, list) and isinstance(configured, list)
            and 1 <= len(actual) <= 16 and 1 <= len(configured) <= 16
            and all(isinstance(m, dict) for m in actual + configured))
    target = value["inbox"]
    coverage = [m for m in actual if isinstance(m.get("Destination"), str)
                and (target == m["Destination"] or target.startswith(m["Destination"] + "/"))]
    require(coverage)
    mount = max(coverage, key=lambda m: len(m["Destination"]))
    require(mount["Destination"] == target and mount.get("RW") is False
            and mount.get("Type") == expected["kind"]
            and sum(m.get("Destination") == target for m in actual) == 1
            and not any(isinstance(m.get("Destination"), str)
                        and m["Destination"].startswith(target + "/") for m in actual))
    source = mount.get("Source")
    _absolute(source)
    require(mount.get("Name") == expected["source"] if expected["kind"] == "volume"
            else source == expected["source"])
    require(not any(m is not mount and isinstance(m.get("Source"), str)
                    and _overlaps(source, m["Source"]) for m in actual))
    selected = [m for m in configured if m.get("Target") == target]
    require(len(selected) == 1)
    selected = selected[0]
    require(selected.get("Type") == expected["kind"] and selected.get("Source") == expected["source"]
            and selected.get("ReadOnly") is True)
    if expected["kind"] == "volume":
        options = selected.get("VolumeOptions")
        require(isinstance(options, dict) and options.get("NoCopy") is True
                and options.get("Subpath", "") == expected.get("subpath", ""))


class Profile:
    """Immutable private profile; paths inside the container are checked there."""
    def __init__(self, path):
        self.path = os.fspath(path)
        _absolute(self.path)
        self.parent = PrivateDir(str(Path(self.path).parent))
        try:
            self.raw, self.fingerprint = self.parent.read(Path(self.path).name, 16384)
            value = decoded(self.raw, 16384)
            require(isinstance(value, dict) and set(value) in (
                PROFILE_KEYS, PROFILE_KEYS | {"input_mode"},
                (PROFILE_KEYS - {"mail_config"}) | {"input_mode", "inbox", "inbox_manifest_sha256"})
                and type(value["schema"]) is int and value["schema"] == 1)
            require("input_mode" not in value or type(value["input_mode"]) is str
                    and value["input_mode"] in {"mailbox", "inbox"})
            mode, input_key, _, _ = input_selection(value)
            for key in PATH_KEYS | {"inbox"}:
                if key in value:
                    _absolute(value[key])
            require(isinstance(value["name_prefix"], str)
                    and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value["name_prefix"]))
            require(isinstance(value["image_id"], str)
                    and re.fullmatch(r"sha256:[0-9a-f]{64}", value["image_id"]))
            require(isinstance(value["release_commit"], str)
                    and re.fullmatch(r"[0-9a-f]{40}", value["release_commit"]))
            require(isinstance(value["dependency_lock_sha256"], str)
                    and SHA.fullmatch(value["dependency_lock_sha256"]))
            require(isinstance(value["package_version"], str)
                    and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+_-]{0,63}", value["package_version"]))
            wall = value["wall_seconds"]
            require(type(wall) in (int, float) and math.isfinite(wall) and 0 < wall <= 2700
                    and type(value["owner_launch_approved"]) is bool)
            require(isinstance(value["seccomp_sha256"], str) and SHA.fullmatch(value["seccomp_sha256"]))
            require(type(value["memory_bytes"]) is int and 16777216 <= value["memory_bytes"] <= 34359738368)
            require(type(value["pids_limit"]) is int and 32 <= value["pids_limit"] <= 4096)
            require(type(value["cpus"]) in (int, float) and math.isfinite(value["cpus"]) and .1 <= value["cpus"] <= 16)
            network = value["network"]
            require(isinstance(network,dict) and set(network) == {"mode","container_id"}
                    and network["mode"] in {"none","container"})
            require(network["container_id"] is None if network["mode"] == "none" else
                    isinstance(network["container_id"],str) and SHA.fullmatch(network["container_id"]))
            require(mode != "inbox" or network == {"mode": "none", "container_id": None})
            mounts = value["mounts"]
            require(isinstance(mounts, list) and 1 <= len(mounts) <= 16)
            targets = set()
            for mount in mounts:
                require(isinstance(mount, dict) and set(mount) in ({"kind", "source", "target", "readonly"}, {"kind", "source", "target", "readonly", "subpath"})
                        and mount["kind"] in {"bind", "volume"} and type(mount["readonly"]) is bool
                        and isinstance(mount["source"],str) and isinstance(mount["target"],str))
                subpath = mount.get("subpath")
                require(subpath is None or mount["kind"] == "volume" and isinstance(subpath,str)
                        and len(subpath) <= 4096 and not subpath.startswith("/")
                        and all(part not in ("", ".", "..") for part in subpath.split("/"))
                        and "," not in subpath and not any(ord(c) < 32 for c in subpath))
                _absolute(mount["target"])
                require(mount["target"] not in targets and not any(mount["target"] == x or mount["target"].startswith(x + "/") for x in ("/proc", "/sys", "/dev", "/run", "/var/run"))
                        and "," not in mount["target"] and "," not in mount["source"])
                targets.add(mount["target"])
                if mount["kind"] == "bind":
                    _absolute(mount["source"])
                    # A worker never receives the profile or writable host control state.
                    for control in (self.path, value["host_receipts"], value["seccomp_path"]):
                        require(control != mount["source"] and not control.startswith(mount["source"] + "/"))
                    require(not mount["source"].endswith("docker.sock"))
                    trusted_path(mount["source"])
                else:
                    require(isinstance(mount["source"], str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", mount["source"]))
            required_paths = ("release_root", "parser_path", "root", "tmp_dir",
                              "worker_state_parent", "board_dir", input_key)
            for key in required_paths:
                covers = [m for m in mounts if value[key] == m["target"] or value[key].startswith(m["target"] + "/")]
                require(covers)
                cover = max(covers, key=lambda m: len(m["target"]))
                require(cover["readonly"] == (key in {"release_root", "parser_path", input_key}))
            if mode == "inbox":
                selected = inbox_mount(value)
                for mount in mounts:
                    require(any(value[k] == mount["target"] or value[k].startswith(mount["target"] + "/")
                                for k in required_paths))
                    require(not mount["target"].startswith(value["inbox"] + "/"))
                    if mount is not selected and mount["kind"] == selected["kind"]:
                        require(mount["source"] != selected["source"] if selected["kind"] == "volume"
                                else not _overlaps(mount["source"], selected["source"]))
            self.value, self.sha256 = value, digest(self.raw)
        except BaseException:
            self.parent.close()
            raise

    def check(self):
        require(self.parent.read(Path(self.path).name, 16384) == (self.raw, self.fingerprint))

    def close(self):
        self.parent.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def _client_cleanup(process):
    # Only the owned Docker CLI handle, never a container/process found by PID.
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1)


def call_argv(argv, timeout):
    """Bounded output and wait. Client termination does NOT return a worker ACK."""
    process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, close_fds=True)
    raw = bytearray()
    deadline = time.monotonic() + timeout
    try:
        os.set_blocking(process.stdout.fileno(), False)
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            eof = False
            while not eof or process.poll() is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TransportError()
                for key, _ in selector.select(min(remaining, .05)):
                    chunk = os.read(key.fileobj.fileno(), 8192)
                    if not chunk:
                        eof = True
                        selector.unregister(key.fileobj)
                    else:
                        raw.extend(chunk)
                        if len(raw) > MAX_REPLY:
                            raise TransportError()
            return process.wait(timeout=max(.01, deadline - time.monotonic())), bytes(raw)
    except BaseException:
        _client_cleanup(process)
        raise
    finally:
        process.stdout.close()


def trusted_path(path):
    """Pin traversal, not namespace UID access (the container probe checks that)."""
    parts = _absolute(path)
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for index, part in enumerate(parts):
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
            if index != len(parts) - 1:
                flags |= os.O_DIRECTORY
            new = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = new
            st = os.fstat(fd)
            require(stat.S_ISDIR(st.st_mode) or stat.S_ISREG(st.st_mode))
            if index != len(parts) - 1:
                require(st.st_uid in (0, os.getuid()) and not st.st_mode & 0o022)
        return os.fstat(fd)
    finally:
        os.close(fd)


class Containment:
    """Kernel-derived host cgroup-v2 identity; missing observation fails closed.

    Docker may remove an empty cgroup at exit. Absence is accepted only for the
    previously observed full-CID leaf, on the same boot, after terminal runtime.
    """
    def capture(self, cid, pid):
        require(type(pid) is int and pid > 0 and SHA.fullmatch(cid))
        before = Path(f"/proc/{pid}/stat").read_text()
        start = int(before[before.rfind(")") + 2:].split()[19])
        lines = Path(f"/proc/{pid}/cgroup").read_text().splitlines()
        require(len(lines) == 1 and lines[0].startswith("0::/"))
        relative = lines[0][3:]
        _absolute(relative)
        require(relative.split("/")[-1] in {cid, "docker-" + cid + ".scope"})
        path = "/sys/fs/cgroup" + relative
        st = trusted_path(path)
        events = self.events(path)
        require(events == 1)
        after = Path(f"/proc/{pid}/stat").read_text()
        require(int(after[after.rfind(")") + 2:].split()[19]) == start)
        return {"path": relative, "device": st.st_dev, "inode": st.st_ino,
                "boot": Path("/proc/sys/kernel/random/boot_id").read_text().strip()}

    @staticmethod
    def events(path):
        fd = os.open(path + "/cgroup.events", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            raw = os.read(fd, 4097)
            require(len(raw) <= 4096)
            values = dict(line.split() for line in raw.decode("ascii").splitlines())
            require(values.get("populated") in {"0", "1"})
            return int(values["populated"])
        finally:
            os.close(fd)

    def empty(self, cid, proof):
        require(isinstance(proof, dict) and set(proof) == {"path", "device", "inode", "boot"})
        _absolute(proof["path"])
        require(proof["path"].split("/")[-1] in {cid, "docker-" + cid + ".scope"}
                and type(proof["device"]) is int and type(proof["inode"]) is int
                and proof["boot"] == Path("/proc/sys/kernel/random/boot_id").read_text().strip())
        path = "/sys/fs/cgroup" + proof["path"]
        try:
            st = trusted_path(path)
        except FileNotFoundError:
            # Check the surviving parent, not an untrusted missing-path prefix.
            trusted_path(str(Path(path).parent))
            return True
        require((st.st_dev, st.st_ino) == (proof["device"], proof["inode"]))
        return self.events(path) == 0


class Docker:
    def __init__(self, profile, *, invoke=call_argv, containment=None):
        self.profile, self.invoke = profile, invoke
        self.containment = containment or Containment()
        self.input_snapshot = None

    def check_input(self):
        p = self.profile.value
        if input_selection(p)[0] == "inbox":
            mount = inbox_mount(p)
            if mount["kind"] == "bind":
                self.input_snapshot = inbox_snapshot(
                    mount["source"], expected=p["inbox_manifest_sha256"], prior=self.input_snapshot)

    def call(self, args, timeout=10):
        self.profile.check()
        return self.invoke([self.profile.value["docker_path"], *args], timeout)

    def environment(self):
        p = self.profile.value
        return ["/usr/bin/env", "-i", "PATH=/usr/local/bin:/usr/bin:/bin", "PYTHONNOUSERSITE=1",
                "PYTHONDONTWRITEBYTECODE=1", "PYTHONPATH=" + p["release_root"] + ":" + p["parser_path"],
                "TMPDIR=" + p["tmp_dir"], "MODEL_BASE_URL=", "CHALLENGE_MODEL_BASE_URL=", "PRIVACY_TIER=strict_local"]

    def network_mode(self,role):
        network = self.profile.value["network"]
        return "none" if role == "reporter" or network["mode"] == "none" else "container:" + network["container_id"]

    def check_network(self,role):
        if self.network_mode(role) == "none":
            return
        cid = self.profile.value["network"]["container_id"]
        template = '{"id":{{json .Id}},"running":{{json .State.Running}}}'
        code, raw = self.call(["inspect","--type","container","--format",template,cid],timeout=5)
        v = decoded(raw,8192)
        require(code == 0 and isinstance(v,dict) and set(v) == {"id","running"}
                and v["id"] == cid and v["running"] is True)

    def create(self, job_id, role):
        p = self.profile.value
        if role == "worker":
            self.check_input()
        # Verify the actual local security file, never daemon defaults or an image tag.
        st = trusted_path(p["seccomp_path"])
        require(stat.S_ISREG(st.st_mode) and st.st_uid in (0, os.getuid()) and not st.st_mode & 0o022)
        with open(p["seccomp_path"], "rb") as stream:
            raw = stream.read(MAX_REPLY + 1)
        require(len(raw) <= MAX_REPLY and digest(raw) == p["seccomp_sha256"])
        code, raw = self.call(["info", "--format", '{"security":{{json .SecurityOptions}},"cgroup":{{json .CgroupVersion}}}'])
        info = decoded(raw,8192)
        require(code == 0 and isinstance(info,dict) and set(info) == {"security","cgroup"}
                and info["cgroup"] == "2" and isinstance(info["security"],list)
                and "name=rootless" in info["security"])
        self.check_network(role)
        args = ["create", "--log-driver", "json-file", "--log-opt", "max-size=1m", "--log-opt", "max-file=1", "--name", p["name_prefix"] + "-" + job_id + "-" + role,
                "--label", "campaign-tool.job=" + job_id, "--label", "campaign-tool.profile=" + self.profile.sha256,
                "--label", "campaign-tool.role=" + role, "--init", "--restart=no", "--user", "1000:1000",
                "--network", self.network_mode(role), "--cgroupns", "private", "--pull", "never", "--no-healthcheck", "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges=true",
                "--security-opt", "seccomp=" + p["seccomp_path"], "--pids-limit", str(p["pids_limit"]),
                "--memory", str(p["memory_bytes"]), "--memory-swap", str(p["memory_bytes"]),
                "--cpus", str(p["cpus"]), "--workdir", p["release_root"], "--entrypoint", "/usr/bin/env"]
        for m in p["mounts"]:
            if m["kind"] == "volume":
                code, raw = self.call(["volume", "inspect", "--format", "{{.Name}}", m["source"]], timeout=5)
                require(code == 0 and raw.decode("ascii").strip() == m["source"])
            args += ["--mount", "type=" + m["kind"] + ",src=" + m["source"] + ",dst=" + m["target"]
                     + (",readonly" if m["readonly"] else "") + (",volume-nocopy" if m["kind"] == "volume" else "")
                     + (",volume-subpath=" + m["subpath"] if m.get("subpath") is not None else "")]
        args += [p["image_id"], *self.environment()[1:], p["python"], "-B", "-m", MODULE, "entry",
                 "--role", role, "--job-id", job_id, "--wall-seconds", str(p["wall_seconds"])]
        for key in ("root", "worker_state_parent", "board_dir", "python"):
            args += ["--" + key.replace("_", "-"), p[key]]
        args += input_arguments(p)
        code, raw = self.call(args)
        cid = raw.decode("ascii").strip()
        require(code == 0 and SHA.fullmatch(cid))
        return cid

    def inspect(self, cid, job_id, role):
        require(isinstance(cid, str) and SHA.fullmatch(cid))
        template = '{"id":{{json .Id}},"image":{{json .Image}},"state":{{json .State.Status}},"running":{{json .State.Running}},"pid":{{json .State.Pid}},"exit":{{json .State.ExitCode}},"labels":{{json .Config.Labels}},"network":{{json .HostConfig.NetworkMode}},"pid_mode":{{json .HostConfig.PidMode}},"init":{{json .HostConfig.Init}},"restart":{{json .HostConfig.RestartPolicy.Name}}}'
        fields = {"id", "image", "state", "running", "pid", "exit", "labels", "network", "pid_mode", "init", "restart"}
        offline = input_selection(self.profile.value)[0] == "inbox"
        if offline:
            template = template[:-1] + ',"mounts":{{json .Mounts}},"configured_mounts":{{json .HostConfig.Mounts}}}'
            fields |= {"mounts", "configured_mounts"}
        code, raw = self.call(["inspect", "--type", "container", "--format", template, cid])
        require(code == 0)
        v = decoded(raw, MAX_REPLY if offline else 8192)
        require(isinstance(v, dict) and set(v) == fields
                and v["id"] == cid and v["image"] == self.profile.value["image_id"]
                and v["network"] == self.network_mode(role) and v["pid_mode"] == ""
                and v["init"] is True and v["restart"] == "no"
                and v["state"] in {"created", "running", "exited", "dead"}
                and type(v["running"]) is bool and type(v["pid"]) is int and type(v["exit"]) is int
                and isinstance(v["labels"], dict)
                and all(v["labels"].get(k) == x for k, x in {"campaign-tool.job":job_id,
                    "campaign-tool.profile":self.profile.sha256,"campaign-tool.role":role}.items()))
        require((v["running"] and v["state"] == "running" and v["pid"] > 0)
                or (not v["running"] and v["state"] in {"created", "exited", "dead"} and v["pid"] == 0))
        if offline:
            check_inbox_mounts(self.profile.value, v)
        return v

    def exec(self, cid, arguments, timeout=10):
        p = self.profile.value
        return self.call(["exec", "--user", "1000:1000", "--workdir", p["release_root"], cid,
                          *self.environment(), p["python"], "-B", "-m", *arguments], timeout)

    def preflight(self, cid):
        self.check_input()
        code, raw = self.exec(cid, ["campaign_tool.records", "version", "--json"])
        v, p = decoded(raw), self.profile.value
        require(code == 0 and isinstance(v, dict) and v.get("source_dirty") is False
                and v.get("commit") == p["release_commit"] and v.get("package_version") == p["package_version"]
                and v.get("dependency_lock_sha256") == p["dependency_lock_sha256"]
                and type(v.get("ledger_schema_version")) is int and v["ledger_schema_version"] == 1
                and v.get("installation_kind") in {"installed_wheel", "source_checkout"})
        args = [MODULE, "probe"]
        for key in ("root", "tmp_dir", "worker_state_parent", "board_dir", "release_root", "parser_path"):
            args += ["--" + key.replace("_", "-"), p[key]]
        args += input_arguments(p)
        code, raw = self.exec(cid, args)
        require(code == 0 and decoded(raw) == {"schema":1,"status":"runtime_ready","uid":1000})

    def terminal_empty(self, cid, job_id, role, proof, *, never_admitted=False):
        v = self.inspect(cid, job_id, role)
        require(not v["running"])
        if v["state"] == "created":
            require(never_admitted and proof is None)
        else:
            require(proof is not None and self.containment.empty(cid, proof))
        return v

    def remove(self, cid, job_id, role, proof, *, never_admitted=False):
        v = self.terminal_empty(cid, job_id, role, proof, never_admitted=never_admitted)
        code, raw = self.call(["rm", cid])  # never --force; admission lock prevents delayed start.
        require(code == 0 and raw.decode("ascii").strip() == cid)
        return v

    def halt(self, cid, job_id, role, proof, *, never_admitted=False, observe=None):
        v = self.inspect(cid, job_id, role)
        if v["running"]:
            # A live supervisor ACK is useful, but never substitutes for containment.
            if role == "worker":
                try:
                    p = self.profile.value
                    self.exec(cid, [WORKER_MODULE, "cancel", "--state-dir", p["worker_state_parent"] + "/" + job_id,
                                    "--wait-seconds", "10"], timeout=15)
                except Exception:
                    pass
            code, _ = self.call(["stop", "--time", "5", cid], timeout=12)
            if code != 0 or self.inspect(cid, job_id, role)["running"]:
                self.call(["kill", "--signal", "KILL", cid], timeout=5)
            deadline = time.monotonic() + 10
            while True:
                v = self.inspect(cid, job_id, role)
                if not v["running"]:
                    break
                require(time.monotonic() < deadline)
                time.sleep(.05)
        if role == "worker" and observe is not None and v["state"] != "created":
            # Read fresh, exact-CID terminal output before removal. Malformed or
            # unavailable output is unproven, not successful worker completion.
            try:
                code, raw = self.call(["logs", cid], timeout=5)
                result = _worker_result(v["exit"], raw, observed=True) if code == 0 else None
            except Exception:
                result = None
            observe(result)
        return self.remove(cid, job_id, role, proof, never_admitted=never_admitted)


def _receipt(value):
    require(isinstance(value, dict) and set(value) == RECEIPT_KEYS
            and type(value["schema"]) is int and value["schema"] == 1
            and isinstance(value["job_id"], str) and JOB.fullmatch(value["job_id"])
            and isinstance(value["profile_sha256"], str) and SHA.fullmatch(value["profile_sha256"])
            and (value["container_id"] is None or isinstance(value["container_id"], str)
                 and SHA.fullmatch(value["container_id"]))
            and value["phase"] in PHASES and value["ack"] in {"none", "natural", "contained"}
            and value["worker_status"] in set(WORKER_CODES) | {"unknown"}
            and value["board"] in {"not_run", "ok", "failed", "unknown"}
            and value["health"] in {"not_run", "ok", "degraded", "failed", "unknown"}
            and value["error"] in {"none", "preflight", "transport", "identity", "ack", "board", "health", "state", "worker"}
            and all(type(value[k]) is bool for k in ("hold", "launch_started", "quiescent", "healthy", "cancel_requested", "worker_removed", "reporter_removed"))
            and type(value["cards"]) is int and 0 <= value["cards"] <= MAX_CARDS
            and (value["worker_exit"] is None or type(value["worker_exit"]) is int
                 and -255 <= value["worker_exit"] <= 255))
    require(value["reporter_id"] is None or isinstance(value["reporter_id"], str) and SHA.fullmatch(value["reporter_id"]))
    for key in ("worker_cgroup", "reporter_cgroup"):
        proof = value[key]
        require(proof is None or isinstance(proof, dict) and set(proof) == {"path", "device", "inode", "boot"})
    require(value["hold"] or value["worker_removed"] and (value["reporter_id"] is None or value["reporter_removed"]))
    require(not value["healthy"] or value["quiescent"] and value["board"] == value["health"] == "ok"
            and _worker_succeeded(value))
    require(value["hold"] or value["phase"] == "finished" and value["quiescent"])
    return value


class Journal:
    def __init__(self, profile):
        self.profile = profile
        self.directory = PrivateDir(profile.value["host_receipts"])

    def active(self):
        value = decoded(self.directory.read("active.json", 8192)[0])
        require(isinstance(value, dict) and set(value) == {"schema", "job_id", "profile_sha256"}
                and type(value["schema"]) is int and value["schema"] == 1
                and isinstance(value["job_id"], str) and JOB.fullmatch(value["job_id"])
                and isinstance(value["profile_sha256"], str) and SHA.fullmatch(value["profile_sha256"]))
        return value

    def read(self, job_id):
        with self.directory.child(job_id) as job:
            value = _receipt(decoded(job.read("receipt.json", 8192)[0]))
            require(value["job_id"] == job_id)
            return value

    def allocate(self):
        with locked(self.directory, "journal.lock", wait=1):
            try:
                active = self.active()
            except FileNotFoundError:
                active = None
            if active is not None:
                prior = self.read(active["job_id"])
                require(prior["profile_sha256"] == active["profile_sha256"]
                        and not prior["hold"] and prior["phase"] == "finished" and prior["quiescent"])
            job_id = os.urandom(16).hex()
            value = dict(schema=1, job_id=job_id, profile_sha256=self.profile.sha256,
                         container_id=None, phase="prepared", hold=True, launch_started=False,
                         quiescent=False, ack="none", worker_status="unknown", worker_exit=None,
                         board="not_run", health="not_run", healthy=False, cards=0, error="none",
                         cancel_requested=False, worker_removed=False, reporter_id=None, reporter_removed=False,
                         worker_cgroup=None, reporter_cgroup=None)
            with self.directory.child(job_id, fresh=True) as job:
                job.write("receipt.json", encoded(_receipt(value)), fresh=True)
            self.directory.write("active.json", encoded({"schema": 1, "job_id": job_id,
                                                          "profile_sha256": self.profile.sha256}))
            return job_id

    def change(self, job_id, *, expected=None, **changes):
        if changes.get("cancel_requested") is True:
            # Same linearization lock as worker-admit dispatch. Never publish a
            # fence in the middle of an already-reserved admission RPC.
            with locked(self.directory, "cancel-admit.lock", wait=65):
                return self._change(job_id, expected=expected, **changes)
        return self._change(job_id, expected=expected, **changes)

    def _change(self, job_id, *, expected=None, **changes):
        with locked(self.directory, "journal.lock", wait=1):
            active = self.active()
            require(active["job_id"] == job_id and active["profile_sha256"] == self.profile.sha256)
            value = self.read(job_id)
            require(value["profile_sha256"] == self.profile.sha256)
            if expected is not None:
                require(value["phase"] in expected)
            value.update(changes)
            with self.directory.child(job_id) as job:
                job.write("receipt.json", encoded(_receipt(value)))
            return value

    def close(self):
        self.directory.close()


def _output(status, *, quiescent=False, healthy=False, cards=0):
    return {"schema": 1, "status": status, "quiescent": quiescent, "healthy": healthy, "cards": cards}


def _worker_succeeded(receipt):
    """Containment and global health never stand in for this job's success."""
    return (receipt["ack"] == "natural" and not receipt["cancel_requested"]
            and receipt["worker_status"] == "completed" and receipt["worker_exit"] == 0)


def _record_worker_result(journal, job_id, result):
    if result is None:
        return
    current = journal.read(job_id)
    if current["worker_status"] != "unknown":
        require((current["worker_status"], current["worker_exit"])
                == (result["status"], result["worker_exit"]))
    journal.change(job_id, worker_status=result["status"], worker_exit=result["worker_exit"])


def _worker_result(code, raw, *, cancelling=False, observed=False):
    value = decoded(raw, 8192)
    require(isinstance(value, dict) and set(value) == {"schema", "status", "quiescent", "worker_exit", "escalated"}
            and type(value["schema"]) is int and value["schema"] == 1
            and value["status"] in WORKER_CODES and type(value["quiescent"]) is bool
            and type(value["escalated"]) is bool
            and (value["worker_exit"] is None or type(value["worker_exit"]) is int
                 and -255 <= value["worker_exit"] <= 255))
    if cancelling:
        require(code == 0 and value["quiescent"]
                and value["status"] in {"completed", "worker_failed", "cancelled", "timed_out"})
    else:
        require(type(code) is int and code == WORKER_CODES[value["status"]])
    if value["status"] == "completed":
        require(value["quiescent"] and value["worker_exit"] == 0)
    if not observed and not cancelling:
        require(code == 0 and value["quiescent"] and value["status"] == "completed")
    return value


def _hold(journal, job_id, reason):
    try:
        journal.change(job_id, expected=PHASES - {"finished"}, phase="hold", hold=True, healthy=False, error=reason)
    except Exception:
        pass  # Earlier persisted HOLD is never discarded because a write failed.
    return _output("held")


def _launch(journal, docker, job_id, role):
    """Caller holds admission lock. Never starts without durable full CID."""
    current = journal.read(job_id)
    require(current["phase"] == ("preflight" if role == "worker" else "postrun"))
    require(current["reporter_id"] is None if role == "reporter" else current["container_id"] is None)
    require(role == "reporter" or not current["cancel_requested"])
    cid = docker.create(job_id, role)
    field = "container_id" if role == "worker" else "reporter_id"
    journal.change(job_id, **{field:cid})
    require(role == "reporter" or not journal.read(job_id)["cancel_requested"])
    docker.check_network(role)
    code, _ = docker.call(["start", cid], timeout=5)
    require(code == 0)
    v = docker.inspect(cid, job_id, role)
    require(v["running"])
    proof = docker.containment.capture(cid, v["pid"])
    journal.change(job_id, **{role + "_cgroup":proof})
    docker.preflight(cid)
    def dispatch():
        docker.check_network(role)
        if role == "worker":
            docker.check_input()
        code, raw = docker.exec(cid, [MODULE, "admit", "--role", role, "--job-id", job_id,
                                     "--worker-state-parent", journal.profile.value["worker_state_parent"]])
        require(code == 0 and decoded(raw) == {"schema":1,"status":"admitted"})

    if role == "worker":
        with locked(journal.directory, "cancel-admit.lock", wait=65):
            require(not journal.read(job_id)["cancel_requested"])
            journal.change(job_id, phase="launching", launch_started=True)
            dispatch()
    else:
        dispatch()
    return cid


def _postrun(journal, docker, job_id):
    with journal.directory.child(job_id) as job, locked(job, "postrun.lock"):
        current = journal.read(job_id)
        require(current["phase"] == "quiescent" and current["quiescent"] and current["worker_removed"] and current["reporter_id"] is None)
        journal.change(job_id, phase="postrun")
        with locked(journal.directory, "admission.lock", wait=65):
            cid = _launch(journal, docker, job_id, "reporter")
        code, raw = docker.call(["wait", cid], timeout=150)
        require(code == 0 and raw.strip() == b"0")
        code, raw = docker.call(["logs", cid], timeout=5)
        require(code == 0)
        report = decoded(raw, 8192)
        require(isinstance(report, dict) and set(report) == {"schema","status","quiescent","healthy","cards","board","health"}
                and type(report["schema"]) is int and report["schema"] == 1 and report["status"] in {"completed","postrun_failed"}
                and report["quiescent"] is True and type(report["healthy"]) is bool
                and type(report["cards"]) is int and 0 <= report["cards"] <= MAX_CARDS
                and report["board"] in {"ok","failed"} and report["health"] in {"ok","degraded","failed","unknown","not_run"}
                and (not report["healthy"] or report["status"] == "completed" and report["board"] == report["health"] == "ok"))
        with locked(journal.directory, "admission.lock", wait=65):
            current = journal.read(job_id)
            require(not current["reporter_removed"])
            docker.remove(cid, job_id, "reporter", current["reporter_cgroup"])
            worker_ok = _worker_succeeded(current)
            healthy = worker_ok and report["healthy"]
            status = report["status"] if worker_ok else "postrun_failed"
            journal.change(job_id, phase="finished", hold=False, reporter_removed=True,
                           board=report["board"], health=report["health"], healthy=healthy,
                           cards=report["cards"], error="worker" if not worker_ok else "none" if healthy else "health")
        return _output(status,quiescent=True,healthy=healthy,cards=report["cards"])


def run(profile_path, *, invoke=call_argv, containment=None):
    with Profile(profile_path) as profile:
        require(profile.value["owner_launch_approved"])
        journal = Journal(profile)
        try:
            with locked(journal.directory, "launch.lock"):
                job_id = journal.allocate()
                docker = Docker(profile, invoke=invoke, containment=containment)
                try:
                    with locked(journal.directory, "admission.lock", wait=65):
                        journal.change(job_id, phase="preflight")
                        cid = _launch(journal, docker, job_id, "worker")
                    code, raw = docker.call(["wait", cid], timeout=profile.value["wall_seconds"] + 65)
                    require(code == 0)
                    exit_code = int(raw.strip())
                    code, raw = docker.call(["logs", cid], timeout=5)
                    result = _worker_result(exit_code, raw, observed=True) if code == 0 else None
                    with locked(journal.directory, "admission.lock", wait=65):
                        current = journal.read(job_id)
                        require(not current["cancel_requested"] and not current["worker_removed"])
                        _record_worker_result(journal, job_id, result)
                        require(result is not None and exit_code == 0 and result["quiescent"]
                                and result["status"] == "completed" and result["worker_exit"] == 0)
                        v = docker.remove(cid, job_id, "worker", current["worker_cgroup"])
                        require(v["exit"] == exit_code)
                        journal.change(job_id, phase="quiescent", quiescent=True, worker_removed=True,
                                       ack="natural", worker_status=result["status"], worker_exit=result["worker_exit"])
                    return _postrun(journal, docker, job_id)
                except BaseException:
                    # Never overwrite a concurrent stop's already finalized receipt.
                    if journal.read(job_id)["phase"] == "finished":
                        return _output("postrun_failed", quiescent=True)
                    return _hold(journal, job_id, "transport")
        finally:
            journal.close()


def stop(profile_path, *, invoke=call_argv, containment=None):
    """Independent exact-CID stop-post; daemon loss never clears durable HOLD."""
    with Profile(profile_path) as profile:
        journal = Journal(profile)
        try:
            with locked(journal.directory, "stop.lock"):
                active = journal.active()
                require(active["profile_sha256"] == profile.sha256)
                job_id = active["job_id"]
                current = journal.read(job_id)
                require(current["profile_sha256"] == profile.sha256 and current["phase"] != "finished")
                journal.change(job_id, cancel_requested=True)
                docker = Docker(profile, invoke=invoke, containment=containment)
                try:
                    with locked(journal.directory, "admission.lock", wait=65):
                        current = journal.read(job_id)
                        require(current["container_id"] is not None)  # Ambiguous create stays HOLD.
                        for role, field, removed in (("worker","container_id","worker_removed"),
                                                     ("reporter","reporter_id","reporter_removed")):
                            if current[field] is not None and not current[removed]:
                                if current[role + "_cgroup"] is None:
                                    runtime = docker.inspect(current[field], job_id, role)
                                    if runtime["running"]:
                                        proof = docker.containment.capture(current[field], runtime["pid"])
                                        current = journal.change(job_id, **{role + "_cgroup":proof})
                                docker.halt(current[field], job_id, role, current[role + "_cgroup"],
                                            never_admitted=not current["launch_started"] if role == "worker" else False,
                                            observe=lambda result: _record_worker_result(journal, job_id, result))
                                current = journal.change(job_id, **{removed:True})
                        journal.change(job_id, phase="quiescent", quiescent=True, ack="contained", healthy=False)
                    if current["reporter_id"] is not None:
                        journal.change(job_id, phase="finished", hold=False, board="unknown", health="unknown")
                        return _output("postrun_failed", quiescent=True)
                    return _postrun(journal, docker, job_id)
                except BaseException:
                    return _hold(journal, job_id, "ack")
        finally:
            journal.close()


def admit(paths):
    require(os.getuid() == os.geteuid() == 1000 and paths["role"] in {"worker", "reporter"}
            and JOB.fullmatch(paths["job_id"]))
    with PrivateDir(paths["worker_state_parent"]) as parent:
        gate_id = digest((paths["job_id"] + paths["role"]).encode())[:32]
        deadline = time.monotonic() + 5
        while True:
            try:
                with parent.child(gate_id) as gate:
                    identity = {"schema":1,"job_id":paths["job_id"],"role":paths["role"]}
                    require(decoded(gate.read("ready.json",8192)[0]) == identity)
                    gate.write("go.json", encoded(identity), fresh=True)
                break
            except FileNotFoundError:
                require(time.monotonic() < deadline)
                time.sleep(.02)
    return {"schema":1,"status":"admitted"}


def entry(paths):
    """Init's direct child; exec, not fork, installs the worker supervisor."""
    require(os.getuid() == os.geteuid() == 1000 and JOB.fullmatch(paths["job_id"]))
    require(paths["role"] in {"worker", "reporter"} and type(paths["wall_seconds"]) in (int,float)
            and math.isfinite(paths["wall_seconds"]) and 0 < paths["wall_seconds"] <= 2700)
    role, job_id = paths["role"], paths["job_id"]
    mode, _, input_path, manifest = input_selection(paths)
    gate_id = digest((job_id + role).encode())[:32]
    with PrivateDir(paths["worker_state_parent"]) as parent, parent.child(gate_id, fresh=True) as gate:
        identity = {"schema":1,"job_id":job_id,"role":role}
        gate.write("ready.json", encoded(identity), fresh=True)
        deadline = time.monotonic() + 55
        while True:
            try:
                require(decoded(gate.read("go.json",8192)[0]) == identity)
                break
            except FileNotFoundError:
                require(time.monotonic() < deadline)
                time.sleep(.02)
    if role == "worker":
        if mode == "inbox":
            inbox_snapshot(input_path, expected=manifest, readonly=True)
        argv = [paths["python"], "-B", "-m", WORKER_MODULE, "supervise", "--state-dir",
                paths["worker_state_parent"] + "/" + job_id, "--wall-seconds", str(paths["wall_seconds"]),
                "--term-seconds", "5", "--kill-seconds", "5", "--", paths["python"], "-B", "-m",
                "campaign_tool.records", "run", "--root", paths["root"],
                "--inbox" if mode == "inbox" else "--mail-config", input_path,
                "--unattended", "--max-originals-per-run", "200", "--ocr", "--json"]
        os.execve(paths["python"], argv, dict(os.environ))
        raise Rejected()
    return postrun_entry(paths)


def postrun_entry(paths):
    """Host already proved containment; root run.lock still fences the snapshot."""
    cards, board_status, health_status = 0, "failed", "not_run"
    try:
        board = build_board(paths["root"], paths["board_dir"], None, paths["job_id"])
        cards, board_status, health_status = board["cards"], "ok", "unknown"
        health = {"schema":1,"job_id":paths["job_id"],"status":"unknown","exit_code":None,"healthy":False,"cards":cards}
        try:
            code, raw = call_argv([paths["python"], "-B", "-m", "campaign_tool.records", "health", "--root", paths["root"], "--json"],30)
            v = decoded(raw)
            require(isinstance(v, dict) and type(v.get("exit_code")) is int and code == v["exit_code"]
                    and code in {0,1} and v.get("status") in {"ok","late","degraded","missed","failed","stalled","before_first_run"})
            health.update(status=v["status"],exit_code=code,healthy=code == 0 and v["status"] == "ok")
            health_status = "ok" if health["healthy"] else "degraded"
        except Exception:
            health_status = "unknown"
        with PrivateDir(paths["board_dir"]) as parent, parent.child(paths["job_id"]) as output:
            output.write("health.json",encoded(health),fresh=True)
        return dict(_output("completed" if health["healthy"] else "postrun_failed",quiescent=True,
                            healthy=health["healthy"],cards=cards),board=board_status,health=health_status)
    except Exception:
        return dict(_output("postrun_failed",quiescent=True,cards=cards),board=board_status,health=health_status)


def probe(paths):
    """In-container path/UID preflight, without reading mail/config values."""
    require(os.getuid() == os.geteuid() == 1000)
    for key in ("root", "tmp_dir", "worker_state_parent", "board_dir"):
        with PrivateDir(paths[key]):
            pass
    for key in ("release_root", "parser_path"):
        with PrivateDir(paths[key], private=False):
            pass
    mode, _, input_path, manifest = input_selection(paths)
    if mode == "inbox":
        inbox_snapshot(input_path, expected=manifest, readonly=True)
    else:
        with PrivateDir(str(Path(input_path).parent), private=False) as directory:
            fd = directory.open_file(Path(input_path).name)
            os.close(fd)
    return {"schema": 1, "status": "runtime_ready", "uid": 1000}


def _bound_quiescence(state_dir, job_id):
    require(Path(state_dir).name == job_id)
    with PrivateDir(state_dir) as directory:
        state = decoded(directory.read("state.json", 8192)[0])
        receipt = decoded(directory.read("receipt.json", 8192)[0])
    require(isinstance(state, dict) and isinstance(receipt, dict)
            and set(receipt) == {"identity", "boot", "run_id", "result"}
            and receipt["identity"] == state.get("identity") and receipt["boot"] == state.get("boot")
            and receipt["run_id"] == state.get("run_id"))
    with open("/proc/sys/kernel/random/boot_id", "r", encoding="ascii") as stream:
        require(receipt["boot"] == stream.read(64).strip())
    require(isinstance(receipt["identity"], dict) and receipt["identity"].get("uid") == os.getuid())
    _worker_result(0, encoded(receipt["result"]), cancelling=True)
    # This disk binding is only for metadata admission AFTER the host's fresh
    # transport result/ACK. It is NEVER used by host stop or launch to clear HOLD.


def _label(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9 .,'()-]{0,159}", value):
        return "Unknown"
    if re.search(r"(?i)https?|password|secret|credential|token|bearer|api.?key|[A-Za-z0-9]{24,}", value):
        return "Unknown"
    return value


def _date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:T[^\s]*)?", value):
        return None
    try:
        return date.fromisoformat(value[:10]).isoformat()
    except ValueError:
        return None


def _metadata(con, original, tables):
    necessary = {"stage_state", "stage_transitions", "stage_content", "stage_artifacts"}
    if not necessary <= tables:
        return {}
    row = con.execute("SELECT a.sha256,a.payload FROM stage_state s "
                      "JOIN stage_transitions t ON t.receipt_sha256=s.receipt_sha256 "
                      "AND t.subject_sha256=s.original_sha256 AND t.stage=s.stage "
                      "JOIN stage_content c ON c.subject_sha256=t.subject_sha256 AND c.stage=t.stage AND c.revision=t.revision "
                      "JOIN stage_artifacts a ON a.sha256=c.content_sha256 "
                      "WHERE s.original_sha256=? AND s.stage='catalog'", (original,)).fetchone()
    if row is None:
        return {}
    raw = bytes(row["payload"])
    require(len(raw) <= 256 * 1024 and digest(raw) == row["sha256"])
    value = decoded(raw)
    require(isinstance(value, dict) and value.get("schema") == "catalog-classification-v1"
            and value.get("subject_sha256") == original and isinstance(value.get("metadata"), dict))
    return value["metadata"]


def build_board(root, output, state_dir, job_id):
    """Private inventory snapshot only. No promotion, model, digest or publication."""
    require(JOB.fullmatch(job_id) is not None)
    if state_dir is not None:
        _bound_quiescence(state_dir, job_id)
    from .catalog_board import render_board
    from .ledger import store
    with PrivateDir(root) as private_root, PrivateDir(output) as private_output:
        # Same flock inode as records run; no new ledger writes or lock bypass.
        with locked(private_root, "run.lock"):
            fd = private_root.open_file("ledger.sqlite")
            os.close(fd)
            with store.ledger(Path(root) / "ledger.sqlite", readonly=True) as con:
                con.execute("BEGIN")
                tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                total = con.execute("SELECT count(*) FROM originals").fetchone()[0]
                require(type(total) is int and 0 <= total <= MAX_CARDS)
                rows = con.execute("SELECT sha256,bytes,mime_detected,first_seen_at,role,scope,preservation_status,legacy_format "
                                   "FROM originals ORDER BY sha256").fetchmany(MAX_CARDS + 1)
                require(len(rows) == total)
                counters = {stage: {state: 0 for state in STATES} for stage in STAGES}
                cards, metadata_count = [], 0
                for row in rows:
                    identity = row["sha256"]
                    require(isinstance(identity, str) and SHA.fullmatch(identity))
                    states = {stage: "unknown" for stage in STAGES}
                    for state in con.execute("SELECT stage,status FROM stage_state WHERE original_sha256=?", (identity,)):
                        require(state["stage"] in STAGES and state["status"] in STATES[:-1])
                        states[state["stage"]] = state["status"]
                    for stage, state in states.items():
                        counters[stage][state] += 1
                    metadata = _metadata(con, identity, tables)
                    metadata_count += bool(metadata)
                    agencies = []
                    if {"agencies", "joins"} <= tables:
                        agencies = sorted({_label(item[0]) for item in con.execute(
                            "SELECT a.name FROM agencies a JOIN joins j ON j.agency_id=a.id "
                            "WHERE j.original_sha256=? AND j.status='typed'", (identity,))})
                    review = "not_reviewed"
                    if states["review"] == "done":
                        review = "stage_done_not_human_certification"
                        if "receipts" in tables:
                            proof = con.execute("SELECT r.role,r.model_or_tool FROM receipts r JOIN stage_state s "
                                                "ON s.receipt_sha256=r.sha256 WHERE s.original_sha256=? AND s.stage='review'",
                                                (identity,)).fetchone()
                            if proof:
                                review = ("declared_human_unverified" if proof["role"] == "human" or proof["model_or_tool"] == "human"
                                          else "model_or_tool_review" if proof["model_or_tool"] else "review_authority_unknown")
                    kind = metadata.get("doc_type")
                    kind = kind if isinstance(kind, str) and re.fullmatch(r"[a-z][a-z0-9-]{0,47}", kind) else "Unknown"
                    score = metadata.get("value_score")
                    score = score if type(score) is int and 0 <= score <= 100 else None
                    cards.append({"sha256": identity, "canonical_sha256": identity, "bytes": row["bytes"],
                                  "format": kind, "agency_hints": agencies or ["Unknown"],
                                  "catalog_status": "candidate" if metadata else "unknown",
                                  "extraction_stage": states["extract"], "analysis_state": review,
                                  "role": _label(row["role"]), "stages": states,
                                  "dates": {"document_date": _date(metadata.get("date_from")),
                                            "date_from": _date(metadata.get("date_from")), "date_to": _date(metadata.get("date_to")),
                                            "first_seen": _date(row["first_seen_at"]), "last_seen": None},
                                  "value_score": score, "value_score_basis": ["Existing candidate catalog output; not independent review"] if score is not None else ["Unknown"],
                                  "source_paths": [], "parents": [], "flags": ["out_of_scope"] if row["scope"] == "out_of_scope" else [],
                                  "preservation_status": _label(row["preservation_status"]),
                                  "snapshot_declarations": {"review": "Stage completion does not certify human review or publication readiness"}})
                require(len(cards) == total and all(sum(value.values()) == total for value in counters.values()))
                ledger_receipts = con.execute("SELECT count(*) FROM receipts").fetchone()[0] if "receipts" in tables else 0
            catalog = {"schema_version": 1, "snapshot_id": job_id, "cards": cards,
                       "summary": {"originals": total, "metadata_known": metadata_count,
                                   "metadata_unknown": total - metadata_count, "stages": counters}}
            catalog_raw = encoded(catalog)
            html_raw = render_board(catalog).encode()
            require(len(catalog_raw) <= MAX_BOARD and len(html_raw) <= MAX_BOARD)
            receipt = {"schema": 1, "job_id": job_id, "cards": total, "ledger_originals": total,
                       "ledger_receipts": ledger_receipts, "metadata_known": metadata_count,
                       "metadata_unknown": total - metadata_count, "stage_counts": counters,
                       "all_originals_included": True, "healthy": False,
                       "json_sha256": digest(catalog_raw), "html_sha256": digest(html_raw)}
            with private_output.child(job_id, fresh=True) as artifacts:
                artifacts.write("catalog.json", catalog_raw, fresh=True)
                artifacts.write("index.html", html_raw, fresh=True)
                artifacts.write("receipt.json", encoded(receipt), fresh=True)
    return {"schema": 1, "status": "board_ready", "cards": total, "reconciled": True, "healthy": False}


def _input_options(command):
    command.add_argument("--input-mode", choices=("mailbox", "inbox"))
    selector = command.add_mutually_exclusive_group(required=True)
    selector.add_argument("--mail-config")
    selector.add_argument("--inbox")
    command.add_argument("--inbox-manifest-sha256")


class Parser(argparse.ArgumentParser):
    def error(self, _):
        raise Rejected()


def main(arguments=None):
    try:
        parser = Parser(add_help=False)
        commands = parser.add_subparsers(dest="action", required=True, parser_class=Parser)
        for action in ("run", "stop"):
            command = commands.add_parser(action, add_help=False)
            command.add_argument("--profile", required=True)
        command = commands.add_parser("inbox-manifest", add_help=False)
        command.add_argument("--inbox", required=True)
        command = commands.add_parser("probe", add_help=False)
        _input_options(command)
        for name in ("root", "tmp_dir", "worker_state_parent", "board_dir", "release_root", "parser_path"):
            command.add_argument("--" + name.replace("_", "-"), required=True)
        for action in ("entry", "admit"):
            command = commands.add_parser(action, add_help=False)
            command.add_argument("--role", choices=("worker", "reporter"), required=True)
            command.add_argument("--job-id", required=True)
            command.add_argument("--worker-state-parent", required=True)
            if action == "entry":
                _input_options(command)
                for name in ("root", "board_dir", "python"):
                    command.add_argument("--" + name.replace("_", "-"), required=True)
                command.add_argument("--wall-seconds", type=float, required=True)
        command = commands.add_parser("metadata", add_help=False)
        for name in ("root", "output", "state_dir", "job_id"):
            command.add_argument("--" + name.replace("_", "-"), required=True)
        args = parser.parse_args(arguments)
        if args.action == "run":
            result = run(args.profile)
        elif args.action == "stop":
            result = stop(args.profile)
        elif args.action == "entry":
            result = entry(vars(args))
        elif args.action == "admit":
            result = admit(vars(args))
        elif args.action == "probe":
            result = probe(vars(args))
        elif args.action == "inbox-manifest":
            snapshot = inbox_snapshot(args.inbox)
            result = {k: snapshot[k] for k in ("sha256", "messages", "bytes")}
            result.update(schema=1, status="inbox_manifest")
        else:
            result = build_board(args.root, args.output, args.state_dir, args.job_id)
    except BaseException:
        result = _output("invalid_state")
    print(json.dumps(result, sort_keys=True), flush=True)
    return {"completed": 0, "quiescent": 0, "runtime_ready": 0, "board_ready": 0, "inbox_manifest": 0,
            "admitted": 0, "postrun_failed": 0 if getattr(locals().get("args"), "action", None) == "entry" else 10, "held": 32, "invalid_state": 30}[result["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
