#!/usr/bin/env python3
"""Read-only preflight: can this machine run the records container?

    python3 tools/records_host_check.py [--target DIR] [--wrapper FILE] [--json-out NEWFILE]

One row per host requirement in fixed order: pass | fail | unknown, the measured value, the requirement and the
attended remedy, then a verdict. --target is the runtime directory whose free space is checked. --wrapper is the
installed RUNTIME_DIR/run-records.sh; only its EXPECTED_IMAGE (the run's own image pin) and MIN_FREE_BYTES lines
are read. Never installs, pulls, starts, stops or restarts anything, loads no module, changes no limit, makes no
network or registry call, and writes only --json-out (0600, created new, outside the checkout). A probe it cannot
run without changing the host is `unknown`, never `pass`. Output names no host, user, address, home path,
environment value or mount list. Exit 0 host ready, 1 not ready (or NEWFILE: not written), 2 usage.
"""
import argparse, contextlib, importlib.util, json, locale, os, platform, re, shutil, stat, subprocess, sys, tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("_host_check_env", Path(__file__).resolve().parent / "check_test_env.py")
test_env = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(test_env)  # type: ignore[union-attr]
PARSERS = REPO / "requirements-records-parsers.txt"

ROW_IDS = ("os-kernel", "user-namespaces", "cgroup-v2", "container-runtime", "rootless-ok", "image-pin",
           "parsers", "ocr-engine", "disk-headroom", "tmp-space", "locale-utf8")
BLOCKING = {"os-kernel", "user-namespaces", "cgroup-v2", "container-runtime", "rootless-ok", "image-pin", "disk-headroom"}
LINUX_ONLY = ("user-namespaces", "cgroup-v2", "rootless-ok")
MIN_KERNEL, MIN_DOCKER, MIN_OCR = (5, 11), (20, 10), (5, 3)
DEFAULT_MIN_FREE = 2147483648  # deploy/records-runtime/README.md starting point (2 GiB) when no --wrapper
TMP_MIN_FREE = 1073741824
# Fixed, read-only argv per allowlisted binary; nothing else is ever executed.
COMMANDS = {"docker-version": ("docker", "--version"),
            "docker-rootless": ("docker", "info", "--format", "{{json .SecurityOptions}}"),
            "docker-images": ("docker", "image", "ls", "--no-trunc", "--quiet"),
            "tesseract-version": ("tesseract", "--version")}
ALLOWED_BINARIES = frozenset(argv[0] for argv in COMMANDS.values())
TIMEOUT_SECONDS, OUTPUT_CAP = 10, 65536
VERSION = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")
IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")
NOT_LINUX = "not probed: host is not Linux"
REMEDY = {
    "os-kernel": "Run the records container on a Linux host with kernel 5.11 or newer; where it runs is G's decision",
    "user-namespaces": "Attended: enable unprivileged user namespaces (sysctl user.max_user_namespaces, persisted in /etc/sysctl.d)",
    "cgroup-v2": "Attended: boot with the unified cgroup hierarchy (systemd.unified_cgroup_hierarchy=1) and reboot",
    "container-runtime": "Attended: install Docker 20.10 or newer with the rootless extras (docs.docker.com/engine/security/rootless/)",
    "rootless-ok": "Attended: set up rootless Docker (dockerd-rootless-setuptool.sh) and point DOCKER_HOST at it",
    "image-pin": "Attended: give --wrapper RUNTIME_DIR/run-records.sh; load or build the pinned image (container/flock-ocr/preflight.py)",
    "parsers": "Attended: install requirements-records-parsers.txt into the isolated parser environment",
    "ocr-engine": "Attended: install tesseract-ocr 5.3 (the pinned image already carries it)",
    "disk-headroom": "Free space on the runtime filesystem, or pass --target RUNTIME_DIR",
    "tmp-space": "Free space on the temporary filesystem",
    "locale-utf8": "Attended: set a UTF-8 locale (for example LANG=C.UTF-8) for the run",
}


def run_capped(argv, run=subprocess.run, which=shutil.which):
    """(stdout text or None, reason). Fixed argv list, timeout, byte cap, allowlisted binary."""
    if argv[0] not in ALLOWED_BINARIES:
        return None, "binary not allowlisted"
    if not which(argv[0]):
        return None, "missing"
    try:
        done = run(list(argv), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=TIMEOUT_SECONDS, check=False)
    except subprocess.TimeoutExpired:
        return None, "timed out"
    except (OSError, ValueError):
        return None, "could not run"
    out = done.stdout or b""
    if done.returncode != 0:
        return None, f"exit {done.returncode}"
    if len(out) > OUTPUT_CAP:
        return None, "output over cap"
    text = out.decode("utf-8", "replace").strip()
    return (text, "ok") if text else (None, "empty output")


class Host:
    """Real read-only probes. Each returns None when it could not measure."""
    def system(self): return platform.system() or None
    def release(self): return platform.release() or None
    def exists(self, path): return os.path.exists(path)
    def which(self, name): return name in ALLOWED_BINARIES and bool(shutil.which(name))
    def command(self, key): return run_capped(COMMANDS[key])
    def tmp_dir(self): return tempfile.gettempdir()
    def encoding(self): return locale.getpreferredencoding(False) or None
    def read(self, path):
        with contextlib.suppress(OSError, UnicodeError), open(path, "rb") as fh:
            return fh.read(4096).decode("ascii").strip()
        return None
    def free_bytes(self, path):
        with contextlib.suppress(OSError):
            return shutil.disk_usage(path).free
        return None
    def dist_version(self, name):
        with contextlib.suppress(test_env.PackageNotFoundError):
            return test_env.version(name)
        return None


def row(rid, state, measured, required):
    return {"id": rid, "state": state, "measured": measured, "required": required,
            "blocking": rid in BLOCKING, "remedy": REMEDY[rid]}


def version_of(text):
    match = VERSION.search(text or "")
    return tuple(int(p) for p in match.groups() if p is not None) if match else None


def at_least(rid, name, text, reason, minimum, need):
    found = version_of(text)
    if found is None:
        return row(rid, "unknown", f"{name} present, version not measured ({reason})", need)
    return row(rid, "pass" if found[:2] >= minimum else "fail", f"{name} " + ".".join(map(str, found)), need)


def read_wrapper(path):
    """(pin or None, min_free or None, note). Reads only two fixed lines of the run's own wrapper."""
    try:
        st = os.lstat(path)
        if not stat.S_ISREG(st.st_mode) or st.st_size > OUTPUT_CAP:
            return None, None, "wrapper is not a small regular file"
        with open(path, "rb") as fh:
            text = fh.read(OUTPUT_CAP).decode("utf-8")
    except (OSError, UnicodeError):
        return None, None, "wrapper unreadable"
    pin = re.search(r"^EXPECTED_IMAGE=['\"]?(sha256:[0-9a-f]{64})['\"]?$", text, re.M)
    free = re.search(r"^MIN_FREE_BYTES=['\"]?([1-9][0-9]*)['\"]?$", text, re.M)
    return (pin.group(1) if pin else None, int(free.group(1)) if free else None,
            "ok" if pin else "no rendered EXPECTED_IMAGE in wrapper")


def probe(host, target=None, pin=None, pin_note="pin not provided (--wrapper)", min_free=None):
    rows, system = {}, host.system()
    linux, need = system == "Linux", f"Linux kernel >= {MIN_KERNEL[0]}.{MIN_KERNEL[1]}"
    if system is None:
        rows["os-kernel"] = row("os-kernel", "unknown", "platform not measured", need)
    elif not linux:
        rows["os-kernel"] = row("os-kernel", "fail", re.match(r"[A-Za-z0-9_-]*", system).group()[:20] + " (not Linux)", need)
    else:
        kernel = version_of(host.release())
        rows["os-kernel"] = (row("os-kernel", "unknown", "Linux, release not measured", need) if kernel is None else
                             row("os-kernel", "pass" if kernel[:2] >= MIN_KERNEL else "fail",
                                 "Linux " + ".".join(map(str, kernel)), need))
    if linux:
        users, clone = host.read("/proc/sys/user/max_user_namespaces"), host.read("/proc/sys/kernel/unprivileged_userns_clone")
        need = "max_user_namespaces > 0 and unprivileged clone allowed"
        rows["user-namespaces"] = (row("user-namespaces", "unknown", "not readable", need) if not (users or "").isdigit() else
                                   row("user-namespaces", "fail", "disabled", need) if int(users) == 0 or clone == "0" else
                                   row("user-namespaces", "pass", "enabled", need))
        need = "cgroup v2 (unified)"
        rows["cgroup-v2"] = (row("cgroup-v2", "pass", "unified hierarchy", need) if host.exists("/sys/fs/cgroup/cgroup.controllers") else
                             row("cgroup-v2", "fail", "v1 or hybrid hierarchy", need) if host.exists("/sys/fs/cgroup") else
                             row("cgroup-v2", "unknown", "no cgroup filesystem seen", need))
    need = f"Docker >= {MIN_DOCKER[0]}.{MIN_DOCKER[1]}"
    rows["container-runtime"] = (at_least("container-runtime", "docker", *host.command("docker-version"), MIN_DOCKER, need)
                                 if host.which("docker") else row("container-runtime", "fail", "docker not found", need))
    runtime_ok = rows["container-runtime"]["state"] == "pass"
    if linux:
        need = "daemon reports rootless; a container start is not attempted"
        text, reason = host.command("docker-rootless") if runtime_ok else (None, "")
        rows["rootless-ok"] = (row("rootless-ok", "unknown", "not probed: container runtime not passing", need) if not runtime_ok else
                               row("rootless-ok", "unknown", f"daemon not queried ({reason})", need) if text is None else
                               row("rootless-ok", "pass", "daemon is rootless", need) if "name=rootless" in text else
                               row("rootless-ok", "fail", "daemon is not rootless", need))
    need = "a local image equal to the run's EXPECTED_IMAGE (no pull)"
    if pin is None or not runtime_ok:
        rows["image-pin"] = row("image-pin", "unknown", pin_note if pin is None else "not probed: container runtime not passing", need)
    else:
        text, reason = host.command("docker-images")
        local = set(IMAGE_ID.findall(text or ""))
        rows["image-pin"] = (row("image-pin", "unknown", f"no local image metadata ({reason})", need) if not local else
                             row("image-pin", "pass", f"local image matches {pin[:19]}", need) if pin in local else
                             row("image-pin", "fail", f"no local image matches {pin[:19]} ({len(local)} local image(s))", need))
    need = "requirements-records-parsers.txt"
    try:
        pins = test_env.parse(PARSERS.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        pins = None
    if not pins:
        rows["parsers"] = row("parsers", "unknown", "parser requirements not readable", need)
    else:
        bad = [f"{name} {status}" for (name, _, _), (_, status, _) in
               zip(pins, test_env.check(pins, lambda n: _dist(host, n))) if status != "ok"]
        rows["parsers"] = row("parsers", "fail" if bad else "pass", "; ".join(bad) or f"{len(pins)}/{len(pins)} at pinned version", need)
    rows["ocr-engine"] = (at_least("ocr-engine", "tesseract", *host.command("tesseract-version"), MIN_OCR, "tesseract 5.3")
                          if host.which("tesseract") else row("ocr-engine", "fail", "tesseract not found", "tesseract 5.3"))
    minimum = min_free or DEFAULT_MIN_FREE
    free, need = host.free_bytes(target) if target else None, f">= {minimum} bytes free on the target"
    rows["disk-headroom"] = (row("disk-headroom", "unknown", "not measured" if target else "no --target", need) if free is None else
                             row("disk-headroom", "pass" if free >= minimum else "fail", f"{free} bytes free", need))
    free, need = host.free_bytes(host.tmp_dir()), f">= {TMP_MIN_FREE} bytes free"
    rows["tmp-space"] = (row("tmp-space", "unknown", "not measured", need) if free is None else
                         row("tmp-space", "pass" if free >= TMP_MIN_FREE else "fail", f"{free} bytes free", need))
    name = re.match(r"[A-Za-z0-9_-]*", host.encoding() or "").group()[:20]
    rows["locale-utf8"] = (row("locale-utf8", "unknown", "not measured", "UTF-8") if not name else
                           row("locale-utf8", "pass" if name.lower().replace("_", "-") in ("utf-8", "utf8") else "fail", name, "UTF-8"))
    for rid in LINUX_ONLY:
        rows.setdefault(rid, row(rid, "unknown", NOT_LINUX if system else "not probed: platform not measured", "Linux host"))
    return [rows[rid] for rid in ROW_IDS]


def _dist(host, name):
    if (found := host.dist_version(name)) is None:
        raise test_env.PackageNotFoundError(name)
    return found


def verdict(rows):
    fail = next((r["id"] for r in rows if r["blocking"] and r["state"] == "fail"), None)
    unverified = next((r["id"] for r in rows if r["blocking"] and r["state"] != "pass"), None)
    return ((False, f"not ready — {fail}") if fail else (False, f"not ready — unverified: {unverified}")
            if unverified else (True, "host ready"))


def out_problem(target):
    parent = os.path.dirname(target)
    inside = target == str(REPO) or target.startswith(str(REPO) + os.sep)
    rules = ((not os.path.isabs(target), "be an absolute path"), (os.path.normpath(target) != target, "be normalized"),
             (os.path.lexists(target), "not exist (never overwritten)"),
             (os.path.realpath(parent) != parent, "have no symlink on its folder"),
             (not os.path.isdir(parent), "be in an existing folder"), (inside, "be outside the repository"))
    return next((f"--json-out must {rule}" for bad, rule in rules if bad), None)


def write_new(target, doc):
    data = memoryview((json.dumps(doc, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except OSError:
        return False
    try:
        while data:
            data = data[os.write(fd, data):]
        os.close(fd)
        return True
    except OSError:
        for undo in (lambda: os.close(fd), lambda: os.unlink(target)):
            with contextlib.suppress(OSError):
                undo()
        return False


def main(argv=None, host=None):
    ap = argparse.ArgumentParser(prog="records_host_check.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--target", metavar="DIR", help="runtime directory whose free space is checked")
    ap.add_argument("--wrapper", metavar="FILE", help="installed run-records.sh (its EXPECTED_IMAGE and MIN_FREE_BYTES)")
    ap.add_argument("--json-out", metavar="NEWFILE")
    args = ap.parse_args(argv)
    if args.json_out is not None and (problem := out_problem(args.json_out)):
        print(problem, file=sys.stderr)
        print("NEWFILE: not written")
        return 1
    pin, min_free, note = read_wrapper(args.wrapper) if args.wrapper else (None, None, "pin not provided (--wrapper)")
    rows = probe(host or Host(), args.target, pin, note, min_free)
    ready, text = verdict(rows)
    for r in rows:
        print(f"{r['id']:<18} {r['state']:<8} {r['measured']} | requires {r['required']}" + (" | blocking" if r["blocking"] else ""))
        if r["state"] != "pass":
            print(f"{'':<18} remedy: {r['remedy']}")
    if any(r["measured"] == NOT_LINUX for r in rows):
        print(f"non-Linux host: {', '.join(LINUX_ONLY)} are unknown, not fail; this host cannot run the records container")
    counts = {s: sum(r["state"] == s for r in rows) for s in ("pass", "fail", "unknown")}
    counts["blocking_fail"] = sum(r["blocking"] and r["state"] == "fail" for r in rows)
    print(f"verdict: {text}")
    print(f"host-check | pass={counts['pass']} fail={counts['fail']} unknown={counts['unknown']} "
          f"blocking-fail={counts['blocking_fail']} | read-only; nothing installed or started")
    if args.json_out is not None and not write_new(args.json_out, {
            "schema": "records-host-check-v1", "review_required": True, "ready": ready, "verdict": text,
            "rows": rows, "counts": counts,
            "note": "read-only preflight; nothing installed or started; a container start was not attempted"}):
        print("NEWFILE: not written")
        return 1
    return 0 if ready else 1


if __name__ == "__main__":
    sys.exit(main())
