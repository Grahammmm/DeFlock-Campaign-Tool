"""Human approval as a verified signature, never as a typed name or a sentence.

An approval is an SSH signature (``ssh-keygen -Y sign -n deflock-approval``) over
the exact payload printed for review: the full text that would go out plus its
sha256. It counts only when it verifies against the root-owned trust file
``TRUSTED_SIGNERS`` and the root-owned ``SSH_KEYGEN``. An agent running as the
campaign user can neither edit the trust file nor substitute the verifier, so
it cannot approve its own proposal. There is no override argument, environment
variable or flag; tests patch this module in-process.

Setup (owner, once, with sudo; see docs/APPROVALS.md):
  install -d -o root -g root -m 0755 /etc/deflock
  install -o root -g root -m 0644 approvers.allowed_signers /etc/deflock/
"""
import hashlib
import os
import stat
import subprocess
import tempfile
from pathlib import Path

NAMESPACE = "deflock-approval"
TRUSTED_SIGNERS = Path("/etc/deflock/approvers.allowed_signers")
SSH_KEYGEN = Path("/usr/bin/ssh-keygen")
MAX_SIGNATURE = 8192
HEADER = "deflock-approval-v1"


class ApprovalError(Exception):
    """The evidence does not prove a human approver's decision on these bytes."""


class TrustUnavailable(ApprovalError):
    """The protected trust anchor or verifier is unavailable; retry, never consume approval."""


def payload(scope, subject, content, round_=0):
    """The exact bytes the approver reads and signs: header lines, then the full content."""
    for value in (scope, subject):
        if not isinstance(value, str) or not value or any(c in value for c in "\r\n\0"):
            raise ApprovalError("invalid approval scope or subject")
    if not isinstance(content, str) or type(round_) is not int or round_ < 0:
        raise ApprovalError("invalid approval content")
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return (f"{HEADER}\nscope: {scope}\nsubject: {subject}\nround: {round_}\nsha256: {digest}\n"
            f"--- exact text being approved ---\n{content}").encode("utf-8")


def _require_root_host(path):
    """Refuse unless the path and every ancestor are root-owned and not group/other writable."""
    path = Path(path)
    if not path.is_absolute():
        raise TrustUnavailable("trust path must be absolute")
    try:
        leaf = os.lstat(path)
    except OSError as exc:
        raise TrustUnavailable(f"trust file missing: {path}") from exc
    if not stat.S_ISREG(leaf.st_mode):
        raise TrustUnavailable(f"trust path is not a regular file: {path}")
    for part in (path, *path.parents):
        st = os.lstat(part)
        if st.st_uid != 0 or st.st_mode & 0o022:
            raise TrustUnavailable(f"trust path is not root-owned and protected: {part}")


EXPECTED_MOUNT = "/etc/deflock"
EXPECTED_ROOT = "/etc/deflock"
ANON_FS = {"overlay", "tmpfs", "ramfs", "devtmpfs", "proc", "sysfs", "cgroup", "cgroup2", "devpts", "mqueue", "squashfs"}
INIT_MAP = ["0", "0", "4294967295"]


def init_userns():
    """True only for the identity map of the initial user namespace; unreadable means not proven (False)."""
    try:
        with open("/proc/self/uid_map") as fh:
            return fh.read().split() == INIT_MAP
    except OSError:
        return False


def overflowuid():
    with open("/proc/sys/kernel/overflowuid") as fh:
        return int(fh.read().strip())


def _unescape(field):
    out, i = [], 0
    while i < len(field):
        if field[i] == "\\" and i + 3 < len(field) and field[i + 1:i + 4].isdigit():
            out.append(chr(int(field[i + 1:i + 4], 8)))
            i += 4
        else:
            out.append(field[i])
            i += 1
    return "".join(out)


def mountinfo():
    rows = []
    with open("/proc/self/mountinfo") as fh:
        for line in fh:
            parts = line.rstrip("\n").split(" ")
            sep = parts.index("-")
            rows.append({"dev": parts[2], "root": _unescape(parts[3]), "mountpoint": _unescape(parts[4]),
                         "opts": parts[5].split(","), "fstype": parts[sep + 1]})
    return rows


def _covers(mp, path):
    return mp == "/" or path == mp or path.startswith(mp.rstrip("/") + "/")


def covering(rows, path):
    """The topmost mount whose mount point covers path (longest mount point; later lines stack on earlier ones)."""
    best = None
    for row in rows:
        if _covers(row["mountpoint"], path) and (best is None or len(row["mountpoint"]) >= len(best["mountpoint"])):
            best = row
    return best


def _need(cond, reason):
    if not cond:
        raise TrustUnavailable(reason)


def _ro(path):
    return bool(os.statvfs(path).f_flag & os.ST_RDONLY)


def _require_anchor_ns(path):
    expect_root = EXPECTED_ROOT
    facts = {}
    ovf = overflowuid()
    facts["overflowuid"] = ovf
    parent = os.path.dirname(path)
    _need(os.path.isabs(path) and parent == EXPECTED_MOUNT, "trust path is not directly under " + EXPECTED_MOUNT)
    try:
        leaf, top = os.lstat(path), os.lstat(parent)
    except OSError:
        raise TrustUnavailable("trust file missing") from None
    facts.update(leaf_uid=leaf.st_uid, leaf_mode=oct(leaf.st_mode & 0o7777), dir_uid=top.st_uid,
                 dir_mode=oct(top.st_mode & 0o7777))
    _need(stat.S_ISREG(leaf.st_mode) and stat.S_ISDIR(top.st_mode), "trust path is not a regular file in a directory")
    for st, name in ((leaf, "file"), (top, "dir")):
        _need(st.st_uid == ovf, "trust %s owner %d is not the overflow uid %d (host root)" % (name, st.st_uid, ovf))
        _need(not st.st_mode & 0o022, "trust %s is group/other writable" % name)
    facts["statvfs_ro"] = _ro(path)
    _need(facts["statvfs_ro"], "trust file is on a writable mount")
    rows = mountinfo()
    m = covering(rows, path)
    facts["mount"] = m
    _need(m is not None and m["mountpoint"] == EXPECTED_MOUNT, "trust dir is not its own mount")
    _need("ro" in m["opts"], "trust mount is not read-only")
    _need(m["root"] == expect_root, "trust mount root %r is not %r" % (m["root"], expect_root))
    _need(m["dev"].split(":")[0] != "0" and m["fstype"] not in ANON_FS and not m["fstype"].startswith("fuse"),
          "trust mount is not a block-backed host filesystem (%s %s)" % (m["fstype"], m["dev"]))
    below = [r["mountpoint"] for r in rows if r["mountpoint"].startswith(EXPECTED_MOUNT + "/")]
    _need(not below, "another mount sits under the trust dir: %s" % below)
    part = os.path.dirname(parent)
    while True:
        st = os.lstat(part)
        _need(st.st_uid in (0, ovf) and not st.st_mode & 0o022 and _ro(part), "ancestor not protected: " + part)
        if part == "/":
            break
        part = os.path.dirname(part)
    return True


def _require_verifier_ns(path):
    ovf = overflowuid()
    leaf = os.lstat(path)
    _need(stat.S_ISREG(leaf.st_mode), "verifier is not a regular file")
    part = path
    while True:
        st = os.lstat(part)
        _need(st.st_uid in (0, ovf) and not st.st_mode & 0o022 and _ro(part), "verifier path not protected: " + part)
        if part == "/":
            return True
        part = os.path.dirname(part)


def _require_root(path):
    """Keep host protection; in a user namespace require the fixed protected bind/rootfs."""
    path = Path(path)
    try:
        if init_userns():
            return _require_root_host(path)
        if path == TRUSTED_SIGNERS:
            return _require_anchor_ns(str(path))
        if path == SSH_KEYGEN:
            return _require_verifier_ns(str(path))
        raise TrustUnavailable("unknown trust path")
    except (OSError, ValueError, IndexError) as error:
        raise TrustUnavailable("trust metadata unavailable") from error


def anchor_status():
    """Metadata-only readiness, also checking that the anchor has an approval namespace."""
    try:
        _require_root(TRUSTED_SIGNERS)
        _require_root(SSH_KEYGEN)
        with open(TRUSTED_SIGNERS, encoding="utf-8") as stream:
            if not any('namespaces="deflock-approval"' in line and not line.lstrip().startswith('#')
                       for line in stream):
                raise TrustUnavailable("approval namespace missing")
    except (OSError, UnicodeError) as error:
        raise TrustUnavailable("trust file unreadable") from error
    return {"state": "ok"}


def anchor_state():
    try:
        return anchor_status()
    except TrustUnavailable as error:
        return {"state": "unavailable", "reason": str(error)}


def verify(data, signature):
    """Return the verified principal for ``signature`` over ``data``, or raise ApprovalError."""
    if not isinstance(data, bytes) or not isinstance(signature, str) or len(signature) > MAX_SIGNATURE \
            or "-----BEGIN SSH SIGNATURE-----" not in signature:
        raise ApprovalError("approval requires an SSH signature, not a name")
    anchor_status()
    with tempfile.TemporaryDirectory(prefix="deflock-approval-") as tmp:
        sig = Path(tmp) / "approval.sig"
        sig.write_text(signature, encoding="utf-8")
        try:
            found = subprocess.run([str(SSH_KEYGEN), "-Y", "find-principals", "-s", str(sig), "-f", str(TRUSTED_SIGNERS)],
                                   capture_output=True, text=True, timeout=30, check=False)
            names = found.stdout.split()
            if found.returncode != 0 or len(names) != 1:
                raise ApprovalError("signature is not from a trusted approver")
            done = subprocess.run([str(SSH_KEYGEN), "-Y", "verify", "-f", str(TRUSTED_SIGNERS), "-I", names[0],
                                   "-n", NAMESPACE, "-s", str(sig)], input=data, capture_output=True, timeout=30, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            raise TrustUnavailable("signature check could not run") from exc
    if done.returncode != 0:
        raise ApprovalError("signature does not match the approved text")
    return names[0]


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description="Read-only approval anchor readiness (no key output).")
    parser.add_argument("--anchor-status", action="store_true", required=True)
    parser.parse_args(argv)
    state = anchor_state()
    print("anchor ok" if state["state"] == "ok" else "anchor unavailable: " + state["reason"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
