"""Flock store backup: create, verify and restore one uncompressed tar of a private flock store (agencies/, regions/,
LAW.csv) with a SHA-256 manifest (docs/BACKUP.md "Flock store"). Local files only: no network, upload or encryption."""
import argparse, contextlib, hashlib, io, json, os, shutil, stat, sys, tarfile, tempfile
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MANIFEST, SCHEMA_VERSION, KIND = "manifest.json", 1, "flock-store"
TOP_DIRS, TOP_FILES = ("agencies", "regions"), ("LAW.csv",)
MAX_MANIFEST_BYTES = 64 * 1024 * 1024
HEX = frozenset("0123456789abcdef")

class Fail(Exception): """Refused or failed (exit 1); nothing partial is left behind by create."""

def _abs(path):  # absolute without following a final symlink, so a linked --out or --into is seen as a link
    path = Path(path)
    return path.parent.resolve() / path.name if path.name else path.resolve()

def _inside(path, root):
    return path == root or root in path.parents

def _hash(handle):
    digest = hashlib.sha256()
    for chunk in iter(lambda: handle.read(1024 * 1024), b""): digest.update(chunk)
    return digest.hexdigest()

def _shown(name):  # names only on errors; control characters escaped
    return name.encode("unicode_escape").decode("ascii")

def collect(store):
    """Sorted [(name, path)] of regular files under agencies/, regions/ and LAW.csv, plus skip counts."""
    found, skipped = [], {"skipped_links": 0, "skipped_hidden": 0, "skipped_other": 0}
    def visit(path, name):
        mode = os.lstat(path).st_mode
        if stat.S_ISLNK(mode): skipped["skipped_links"] += 1
        elif path.name.startswith("."): skipped["skipped_hidden"] += 1
        elif stat.S_ISDIR(mode) and (name.split("/")[0] in TOP_DIRS):
            for child in sorted(os.listdir(path)): visit(path / child, name + "/" + child)
        elif stat.S_ISREG(mode) and (name in TOP_FILES or "/" in name): found.append((name, path))
        else: skipped["skipped_other"] += 1
    for entry in sorted(os.listdir(store)): visit(store / entry, entry)
    return found, skipped

def create(store, out):
    store, out = Path(store).resolve(), _abs(out)
    if not store.is_dir(): raise Fail("store is not a folder")
    if _inside(store, REPO): raise Fail("store inside repository")
    if _inside(out, store) or _inside(out, REPO): raise Fail("--out inside the store or the repository")
    if os.path.lexists(out): raise Fail("--out exists; refusing to overwrite")
    if not out.parent.is_dir(): raise Fail("--out folder does not exist")
    found, skipped = collect(store)
    files = {}
    for name, path in found:
        with open(path, "rb") as handle: files[name] = {"sha256": _hash(handle), "bytes": os.fstat(handle.fileno()).st_size}
    counts = {"files": len(files), "bytes": sum(e["bytes"] for e in files.values()), **skipped}
    manifest = {"schema_version": SCHEMA_VERSION, "kind": KIND, "files": files, "counts": counts,
                "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    raw = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    digest = hashlib.sha256(raw).hexdigest()
    fd, tmp = tempfile.mkstemp(prefix="." + out.name + ".", suffix=".tmp", dir=out.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle, tarfile.open(fileobj=handle, mode="w", format=tarfile.PAX_FORMAT) as tar:
            _add(tar, MANIFEST, len(raw), io.BytesIO(raw))
            for name, path in found:
                with open(path, "rb") as source: _add(tar, name, files[name]["bytes"], source)
        _, _, problems = verify(tmp, digest)  # catches a file changed between hashing and writing
        if problems: raise Fail("store changed while archiving: " + "; ".join(problems))
        os.link(tmp, out)  # never replaces an existing --out
    finally:
        with contextlib.suppress(OSError): os.unlink(tmp)
    return counts, digest

def _add(tar, name, size, source):
    info = tarfile.TarInfo(name)
    info.size, info.mode, info.mtime, info.uid, info.gid, info.uname, info.gname = size, 0o600, 0, 0, 0, "", ""
    tar.addfile(info, source)

def _safe_name(name):
    if not isinstance(name, str) or not name or name.startswith("/") or "\\" in name or "\x00" in name: return False
    return not any(part in ("", ".", "..") for part in name.split("/"))

def _no_duplicates(pairs):
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)): raise Fail("manifest.json: duplicate key")
    return dict(pairs)

def _manifest(raw):
    try: manifest = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicates)
    except (UnicodeDecodeError, ValueError): raise Fail("manifest.json: not valid JSON") from None
    if not isinstance(manifest, dict) or type(manifest.get("schema_version")) is not int \
            or manifest["schema_version"] != SCHEMA_VERSION or manifest.get("kind") != KIND \
            or not isinstance(manifest.get("files"), dict) or not isinstance(manifest.get("counts"), dict):
        raise Fail("manifest.json: unsupported shape")
    files, counts = manifest["files"], manifest["counts"]
    parents = {"/".join(n.split("/")[:i]) for n in files if isinstance(n, str) for i in range(1, n.count("/") + 1)}
    for name, entry in files.items():
        if not _safe_name(name) or name in parents or not (name in TOP_FILES or name.split("/")[0] in TOP_DIRS and "/" in name):
            raise Fail("manifest.json: unsafe_member_name " + _shown(str(name)))
        if not isinstance(entry, dict) or not (isinstance(entry.get("sha256"), str) and len(entry["sha256"]) == 64
                                               and set(entry["sha256"]) <= HEX) \
                or type(entry.get("bytes")) is not int or entry["bytes"] < 0:
            raise Fail("manifest.json: invalid entry " + _shown(name))
    if counts.get("files") != len(files) or counts.get("bytes") != sum(e["bytes"] for e in files.values()):
        raise Fail("manifest.json: count_mismatch")
    return manifest

def verify(path, expected_manifest_sha256=None):
    """(manifest, manifest_sha256, problems): every member re-hashed against the manifest; problems name the member."""
    try:
        tar = tarfile.open(path, mode="r:")
    except (OSError, tarfile.TarError): raise Fail("archive is not a readable uncompressed tar") from None
    problems, seen = [], set()
    with tar:
        members = tar.getmembers()
        if not members or members[0].name != MANIFEST or not members[0].isreg() or members[0].size > MAX_MANIFEST_BYTES:
            raise Fail("manifest.json missing or not the first member")
        raw = tar.extractfile(members[0]).read()
        digest, manifest = hashlib.sha256(raw).hexdigest(), _manifest(raw)
        if expected_manifest_sha256 is not None and digest != expected_manifest_sha256.strip().lower():
            problems.append("manifest.json: manifest_digest_mismatch")
        files = manifest["files"]
        for member in members[1:]:
            name, shown = member.name, _shown(member.name)
            if name in seen or name == MANIFEST: problems.append(shown + ": duplicate_member")
            elif not _safe_name(name): problems.append(shown + ": unsafe_member_name")
            elif not member.isreg(): problems.append(shown + ": unsafe_member_type")
            elif name not in files: problems.append(shown + ": not_in_manifest")
            elif member.size != files[name]["bytes"]: problems.append(shown + ": size_mismatch")
            elif _hash(tar.extractfile(member)) != files[name]["sha256"]: problems.append(shown + ": hash_mismatch")
            seen.add(name)
        problems += [_shown(name) + ": missing_member" for name in sorted(set(files) - seen)]
    return manifest, digest, problems

def restore(path, into):
    into = _abs(into)
    if _inside(into, REPO): raise Fail("--into inside repository")
    if os.path.islink(into): raise Fail("--into is a symlink")
    if os.path.lexists(into) and (not into.is_dir() or any(into.iterdir())): raise Fail("--into is not an empty folder")
    manifest, _, problems = verify(path)
    if problems: raise Fail("archive refused, nothing extracted: " + "; ".join(problems))
    into.mkdir(mode=0o700, exist_ok=True)
    os.chmod(into, 0o700)
    with tarfile.open(path, mode="r:") as tar:
        for member in tar.getmembers()[1:]:
            parts, folder = member.name.split("/"), into
            for part in parts[:-1]:
                folder = folder / part
                if os.path.islink(folder): raise Fail("restore target changed: " + _shown(member.name))
                if not folder.is_dir(): folder.mkdir(mode=0o700)
                os.chmod(folder, 0o700)
            fd = os.open(folder / parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as out: shutil.copyfileobj(tar.extractfile(member), out)
    bad = []
    for name, entry in sorted(manifest["files"].items()):  # re-hash what is now on disk
        with open(into.joinpath(*name.split("/")), "rb") as handle:
            if _hash(handle) != entry["sha256"]: bad.append(_shown(name) + ": hash_mismatch_on_disk")
    if bad: raise Fail("restored files do not match: " + "; ".join(bad))
    return manifest["counts"]

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("create", help="archive STORE's agencies/, regions/ and LAW.csv into a new --out tar")
    p.add_argument("store"); p.add_argument("--out", required=True)
    p = sub.add_parser("verify", help="re-hash every member against the manifest (and the manifest against HEX)")
    p.add_argument("file"); p.add_argument("--manifest-sha256", metavar="HEX")
    p = sub.add_parser("restore", help="verify, then extract into an empty or absent --into folder (0700/0600)")
    p.add_argument("file"); p.add_argument("--into", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "create":
            counts, digest = create(args.store, args.out)
            print("created files=%d bytes=%d manifest_sha256=%s" % (counts["files"], counts["bytes"], digest))
        elif args.command == "verify":
            manifest, _, problems = verify(args.file, args.manifest_sha256)
            if problems:
                print("\n".join(problems), file=sys.stderr)
                return 1
            print("verified files=%d bytes=%d" % (manifest["counts"]["files"], manifest["counts"]["bytes"]))
        else:
            counts = restore(args.file, args.into)
            print("restored files=%d bytes=%d" % (counts["files"], counts["bytes"]))
    except (Fail, OSError, tarfile.TarError) as err:
        print(err if isinstance(err, Fail) else "io error: %s" % type(err).__name__, file=sys.stderr)
        return 1
    return 0

if __name__ == "__main__":
    sys.exit(main())
