#!/usr/bin/env python3
"""Quarantine only derived/private copies by exact source path. Never read originals."""
import argparse, fcntl, json, os, pathlib, shutil, sqlite3
if __package__:
    from . import folder as intake
else:
    import folder as intake

def quarantine(out, paths):
    os.umask(0o077); out=pathlib.Path(out).absolute()
    with (out/"writer.lock").open("a") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        db=sqlite3.connect(out/"intake.sqlite"); db.row_factory=sqlite3.Row; db.executescript(intake.SCHEMA)
        hashes=set(); stamp=intake.now()
        for path in paths:
            if not pathlib.Path(path).is_absolute(): raise ValueError("exact absolute source paths required")
            found={r[0] for r in db.execute("SELECT sha FROM occurrences WHERE path=?",(path,))}
            hashes.update(found)
            for sha in found or {""}: db.execute("INSERT OR IGNORE INTO scope_exclusions VALUES(?,?,?,?)",(path,sha,"unrelated_personal_record",stamp))
        edges=[tuple(r) for r in db.execute("SELECT parent,child FROM edges")]
        while True:
            extra={child for parent,child in edges if parent in hashes} | {parent for parent,child in edges if child in hashes}
            if extra<=hashes: break
            hashes.update(extra)
        qdir=out/"quarantine"; qdir.mkdir(exist_ok=True); os.chmod(qdir,0o700)
        q=sqlite3.connect(qdir/"restricted.sqlite"); q.executescript(intake.SCHEMA)
        units=0; derived=0; blobs=0
        for sha in sorted(hashes):
            for table,where in (("docs","sha=?"),("units","sha=?"),("occurrences","sha=?"),("edges","parent=? OR child=?")):
                args=(sha,sha) if table=="edges" else (sha,)
                for row in db.execute("SELECT * FROM "+table+" WHERE "+where,args):
                    q.execute("INSERT OR IGNORE INTO "+table+" VALUES("+",".join("?" for _ in row)+")",tuple(row))
                    if table=="units": units+=1
            q.commit()
            for row in db.execute("SELECT path FROM occurrences WHERE sha=?",(sha,)):
                db.execute("INSERT OR IGNORE INTO scope_exclusions VALUES(?,?,?,?)",(row[0],sha,"unrelated_personal_record_or_container",stamp))
            dest=qdir/sha; dest.mkdir(exist_ok=True)
            for source,name in ((out/"derived"/sha,"derived"),(out/"blobs"/sha,"blob")):
                if source.exists():
                    if (dest/name).exists(): raise ValueError("quarantine destination already exists; manual reconciliation needed")
                    shutil.move(str(source),str(dest/name))
                    if name=="derived": derived+=1
                    else: blobs+=1
            db.execute("DELETE FROM units WHERE sha=?",(sha,)); db.execute("DELETE FROM preservations WHERE sha=?",(sha,))
            db.execute("DELETE FROM edges WHERE parent=? OR child=?",(sha,sha))
            db.execute("UPDATE docs SET stage='out_of_scope',digest=NULL WHERE sha=?",(sha,))
            intake.event(db,"scope-quarantine","scope","quarantined_personal_hash",sha=sha,detail="originals untouched; content removed from active derived store")
        db.commit(); q.close()
        intake.atomic(out/"scope-excluded-hashes.json",sorted({r[0] for r in db.execute("SELECT sha FROM scope_exclusions WHERE sha!=''")}))
        intake.report(db,out)
        result=dict(at=stamp,excluded_paths=len(paths),quarantined_hashes=len(hashes),prior_extracted_units=units,moved_derived_directories=derived,moved_blobs=blobs,originals_modified=0)
        intake.atomic(out/"scope-event.json",result); print(intake.js(result)); db.close()

if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__); p.add_argument("--output",required=True); p.add_argument("paths",nargs="+"); a=p.parse_args(); quarantine(a.output,a.paths)
