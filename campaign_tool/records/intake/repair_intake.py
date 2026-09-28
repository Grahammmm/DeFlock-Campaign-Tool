#!/usr/bin/env python3
"""Approved CAS permission and DOCX repairs, limited to private intake output."""
import argparse, collections, fcntl, hashlib, json, os, pathlib, sqlite3, time, uuid
if __package__:
    from . import folder as intake
else:
    import folder as intake

def run(out):
    os.umask(0o077); out=pathlib.Path(out).absolute()
    if out.resolve()!=out: raise ValueError("output must not contain symlinks")
    started=time.monotonic(); repair_id=uuid.uuid4().hex
    with (out/"writer.lock").open("a") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        db=sqlite3.connect(out/"intake.sqlite"); db.row_factory=sqlite3.Row; db.executescript(intake.SCHEMA)
        before,inv=intake.active(db); before_set=set(before)
        receipt_columns="oid,sha,path,root,parent,locator,receipt,first_seen"
        def receipt_hash():
            h=hashlib.sha256()
            for row in db.execute("SELECT "+receipt_columns+" FROM occurrences ORDER BY oid,sha"): h.update((intake.js(list(row))+"\n").encode())
            return h.hexdigest()
        receipts_before=receipt_hash(); old_history=db.execute("SELECT count(*) FROM edge_history").fetchone()[0]
        changed_modes=[]; errors=[]; checked=0; checked_bytes=0
        for sha in sorted(before):
            try:
                result=intake.verify_blob(sha,out,make_readonly=True); checked+=1; checked_bytes+=result["bytes"]
                if result["before_mode"]!=result["after_mode"]: changed_modes.append(result)
            except Exception as e: errors.append(dict(sha256=sha,error=repr(e)))
        changes=[]
        if not errors:
            for sha in sorted(before):
                doc=db.execute("SELECT format FROM docs WHERE sha=?",(sha,)).fetchone()
                if doc[0]!="zip": continue
                with intake.secure_open(out/"blobs"/sha) as f: head=f.read(16384)
                name=db.execute("SELECT path FROM occurrences WHERE sha=? AND lower(path) LIKE '%.docx' LIMIT 1",(sha,)).fetchone()
                detected=intake.fmt(name[0] if name else sha,head,out/"blobs"/sha)
                if detected=="docx" and intake.upgrade_format(db,sha,"docx"): changes.append(sha)
            db.commit()
            for sha in sorted(intake.active(db)[0]):
                row=db.execute("SELECT format,stage FROM docs WHERE sha=?",(sha,)).fetchone()
                if row[0]=="docx" and row[1] in {"pending","running"}: intake.extract(db,out,intake.LIMITS,selection=sha)
        db.commit(); after,inv=intake.active(db); after_set=set(after); blocked={r[0] for r in db.execute("SELECT sha FROM scope_exclusions WHERE sha!=''")}
        # Recheck all previously active copies, including newly retired DOCX parts.
        for sha in sorted(before_set|after_set):
            try:
                check=intake.verify_blob(sha,out)
                if check["after_mode"]!=0o400: raise ValueError("blob_not_read_only")
            except Exception as e: errors.append(dict(sha256=sha,phase="postcheck",error=repr(e)))
        receipts_after=receipt_hash(); integrity=[r[0] for r in db.execute("PRAGMA integrity_check")]; fk=[tuple(r) for r in db.execute("PRAGMA foreign_key_check")]
        scope_units=sum(db.execute("SELECT count(*) FROM units WHERE sha=?",(sha,)).fetchone()[0] for sha in blocked)
        scope_paths=sum((out/"blobs"/sha).exists() or (out/"derived"/sha).exists() for sha in blocked)
        if receipts_before!=receipts_after: errors.append(dict(error="occurrence_receipts_changed"))
        if integrity!=["ok"] or fk: errors.append(dict(error="sqlite_integrity_failure"))
        if after_set&blocked or scope_units or scope_paths: errors.append(dict(error="scope_quarantine_integrity_failure"))
        intake.report(db,out)
        result=dict(repair_id=repair_id,version=intake.VERSION,inventory_run=inv,source_originals_opened=0,source_originals_modified=0,
                    active_hashes_before=len(before),active_hashes_after=len(after),retired_hashes=sorted(before_set-after_set),
                    blobs_verified=checked,verified_bytes=checked_bytes,permissions_repaired=changed_modes,docx_hashes_reclassified=changes,
                    edges_retired_this_run=db.execute("SELECT count(*) FROM edge_history").fetchone()[0]-old_history,
                    occurrence_receipt_sha256_before=receipts_before,occurrence_receipt_sha256_after=receipts_after,
                    sqlite_integrity=integrity,foreign_key_violations=fk,quarantined_hashes_active=len(after_set&blocked),
                    quarantined_units_active=scope_units,quarantined_blob_or_derived_paths_active=scope_paths,
                    errors=errors,seconds=round(time.monotonic()-started,3))
        intake.atomic(out/"repairs"/(repair_id+".json"),result); intake.atomic(out/"repair-latest.json",result)
        print(intake.js(result),flush=True); db.close()
        return 1 if errors else 0

if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__); p.add_argument("--output",required=True); a=p.parse_args(); raise SystemExit(run(a.output))
