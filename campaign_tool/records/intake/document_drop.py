"""Verified local PDF/XLSX acquisition. No fabricated mail or origin claims."""
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import zipfile
from . import folder
from ..container_host import PrivateDir, _fingerprint, require, digest, encoded

def snapshot(path, *, expected=None, readonly=False, prior=None):
    """Bounded flat owner-only document snapshot; no format parsing or callbacks."""
    entries, signatures, total = [], [], 0
    with PrivateDir(path) as directory:
        before = _fingerprint(os.fstat(directory.fd))
        if readonly:
            require(bool(os.fstatvfs(directory.fd).f_flag & os.ST_RDONLY))
        names = sorted(os.listdir(directory.fd))
        require(len(names) <= 200)
        for name in names:
            require(Path(name).suffix.lower() in {".pdf", ".xlsx"} and len(name) <= 255
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
        result = {"sha256": digest(encoded(entries)), "documents": len(entries), "entries": entries,
                  "bytes": total, "signature": signature}
    require(expected is None or result["sha256"] == expected)
    return result



def read_entry(path, manifest, entry):
    require(entry in manifest["entries"])
    with PrivateDir(path) as directory:
        require(_fingerprint(os.fstat(directory.fd)) == manifest["signature"][0])
        fd = directory.open_file(entry["name"])
        try:
            before = os.fstat(fd)
            require(bool(os.fstatvfs(fd).f_flag & os.ST_RDONLY))
            expected = dict(manifest["signature"][1])[entry["name"]]
            require(_fingerprint(before) == expected)
            chunks, consumed = [], 0
            while True:
                chunk = os.read(fd, min(1024 * 1024, entry["bytes"] - consumed + 1))
                if not chunk:
                    break
                consumed += len(chunk)
                require(consumed <= entry["bytes"])
                chunks.append(chunk)
            raw = b"".join(chunks)
            require(consumed == entry["bytes"] and digest(raw) == entry["sha256"])
            require(_fingerprint(os.fstat(fd)) == expected)
            require(_fingerprint(os.stat(entry["name"], dir_fd=directory.fd, follow_symlinks=False)) == expected)
            directory.check()
            require(_fingerprint(os.fstat(directory.fd)) == manifest["signature"][0])
            return raw
        finally:
            os.close(fd)


def inspect_format(name, raw):
    suffix = Path(name).suffix.lower()
    if suffix == ".pdf" and raw.startswith(b"%PDF-"):
        return "pdf"
    if suffix != ".xlsx" or not raw.startswith(b"PK"):
        raise ValueError("document_format_invalid")
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        items = archive.infolist()
        names = [item.filename for item in items]
        require(len(items) <= 10000 and len(set(names)) == len(names)
                and {"[Content_Types].xml", "xl/workbook.xml"} <= set(names))
        require(sum(item.file_size for item in items) <= 256 * 1024 * 1024)
        for item in items:
            require(not item.flag_bits & 1 and item.file_size <= 64 * 1024 * 1024
                    and item.file_size <= max(1, item.compress_size) * 1000
                    and not item.filename.startswith("/")
                    and "\\" not in item.filename
                    and all(part not in (".", "..") for part in item.filename.split("/")))
    return "xlsx"


def capture(output, path, manifest, entry):
    raw = read_entry(path, manifest, entry)
    output = Path(output)
    fd, temporary = tempfile.mkstemp(prefix=".document-", dir=output)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        folder.seal_blob(temporary, entry["sha256"], output)
    finally:
        Path(temporary).unlink(missing_ok=True)
    # Invalid formats still retain exact CAS bytes, but receive no accepted stage.
    form = inspect_format(entry["name"], raw)
    source = {"schema": "document-drop-source-v1", "root": str(Path(path).absolute()),
              "name": entry["name"], "manifest_sha256": manifest["sha256"]}
    receipt = {"schema": "document-drop-receipt-v1", "source": source,
               "sha256": entry["sha256"], "bytes": entry["bytes"], "form": form,
               "source_stat": dict(manifest["signature"][1])[entry["name"]]}
    body = encoded(receipt)
    receipt_sha = digest(body)
    directory = output / "document-receipts"
    directory.mkdir(mode=0o700, exist_ok=True)
    with PrivateDir(str(directory)) as target:
        try:
            existing, _ = target.read(receipt_sha, 16384)
            require(existing == body)
        except FileNotFoundError:
            fd = os.open(receipt_sha, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=target.fd)
            with os.fdopen(fd, "wb") as stream:
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
            os.fsync(target.fd)
    return receipt, receipt_sha, str(directory / receipt_sha)


def verify_local_occurrence(row, output, subject):
    source = json.loads(row["source_ref"])
    evidence = json.loads(row["evidence"])
    digest_ = evidence["receipt_sha256"]
    require(isinstance(digest_, str) and len(digest_) == 64
            and all(c in "0123456789abcdef" for c in digest_))
    directory = Path(output) / "document-receipts"
    require(evidence["verification_receipt_path"] == str(directory / digest_))
    with PrivateDir(str(directory)) as target:
        body, _ = target.read(digest_, 16384)
    require(digest(body) == digest_)
    receipt = json.loads(body)
    require(set(receipt) == {"schema", "source", "sha256", "bytes", "form", "source_stat"}
            and receipt["schema"] == "document-drop-receipt-v1"
            and receipt["source"] == source and source["schema"] == "document-drop-source-v1"
            and set(source) == {"schema", "root", "name", "manifest_sha256"}
            and receipt["sha256"] == subject and row["parent_occurrence_id"] is None
            and row["id"] == folder.hid(folder.js(["document-drop-v1", source, subject]))
            and evidence == {"receipt_sha256": digest_, "cas_sha256": subject,
                             "bytes": receipt["bytes"], "verification_receipt_path": str(directory / digest_)})
    with folder.secure_open(Path(output) / "blobs" / subject) as stream:
        raw = stream.read(64 * 1024 * 1024 + 1)
    require(len(raw) == receipt["bytes"] and digest(raw) == subject
            and inspect_format(source["name"], raw) == receipt["form"])
    return digest_, receipt["form"]


def _progress(database, account, path, manifest, safety):
    from ..ledger import store
    from ..run_safety import digest as identity_digest
    key = "document-drop-progress-v1:" + identity_digest([account, str(Path(path).absolute())])
    with store.ledger(database, readonly=True) as con:
        row = con.execute("SELECT value FROM ledger_meta WHERE key=?", (key,)).fetchone()
    cursor = 0
    if row:
        try:
            value = json.loads(row[0])
            require(type(value) is dict and set(value) == {"schema", "manifest_sha256", "next"}
                    and value["schema"] == "document-drop-progress-v1"
                    and type(value["manifest_sha256"]) is str
                    and len(value["manifest_sha256"]) == 64
                    and all(c in "0123456789abcdef" for c in value["manifest_sha256"])
                    and type(value["next"]) is int and 0 <= value["next"] <= 200)
            if value["manifest_sha256"] == manifest["sha256"]:
                require(value["next"] == 0 if manifest["documents"] == 0
                        else value["next"] < manifest["documents"])
                cursor = value["next"]
        except Exception:
            safety.stop("invalid_safety_state")
            raise ValueError("invalid_safety_state") from None
    return key, cursor


def _save_progress(database, key, manifest, cursor):
    from ..ledger import store
    value = {"schema": "document-drop-progress-v1", "manifest_sha256": manifest["sha256"], "next": cursor}
    with store.ledger(database) as con:
        con.execute("INSERT INTO ledger_meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, json.dumps(value, sort_keys=True)))
        con.commit()


def ingest(pipeline, path, expected):
    from ..runner.canonical_documents import CanonicalDocumentsBackend
    from ..run_safety import RunSafety, RunSafetyPolicy, digest as identity_digest, fixed_code
    require(pipeline.model is None and pipeline.challenge is None)
    manifest = snapshot(path, expected=expected, readonly=True)
    safety = getattr(pipeline, "safety", None)
    if safety is None:
        safety = RunSafety(pipeline.root.ledger, RunSafetyPolicy())
    progress_key, cursor = _progress(pipeline.root.ledger, pipeline.account, path, manifest, safety)
    report = {"cursor_start": cursor, "cursor_next": cursor, "input_mode": "documents", "manifest_sha256": manifest["sha256"],
              "messages": 0, "documents": manifest["documents"], "preserved": [],
              "replayed": [], "failures": [], "deferred": 0}
    backend = CanonicalDocumentsBackend(pipeline.root.sub("mail"), pipeline.root.intake, pipeline.root.ledger)
    identity = pipeline._identity()
    identity["run_id"] = "document-" + pipeline.run_id
    backend.start_run(identity)
    try:
        for position, entry in enumerate(manifest["entries"][cursor:], start=cursor):
            binding = identity_digest([pipeline.account, "document-drop", str(Path(path).absolute()),
                                       manifest["sha256"], entry["name"], entry["sha256"]])
            if not safety.begin("intake", binding):
                report["deferred"] = manifest["documents"] - position
                break
            try:
                replay = backend.preserve_document(path, manifest, entry)
                next_cursor = position + 1 if position + 1 < manifest["documents"] else 0
                _save_progress(pipeline.root.ledger, progress_key, manifest, next_cursor)
                safety.finish()
                report["cursor_next"] = next_cursor
                report["replayed" if replay else "preserved"].append({"original_sha256": entry["sha256"]})
            except Exception as error:
                code = fixed_code(error)
                safety.finish("fault", code, (entry["sha256"],))
                report["failures"].append({"identity_sha256": binding, "code": code, "kind": "fault"})
                report["deferred"] = manifest["documents"] - position - 1
                if safety.stopped:
                    break
    finally:
        backend.finish_run(identity, "completed_with_gaps" if report["failures"] else "slice_completed",
                           {"documents": len(report["preserved"]), "failures": len(report["failures"])})
    return report
