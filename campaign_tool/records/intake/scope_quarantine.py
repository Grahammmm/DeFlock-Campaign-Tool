#!/usr/bin/env python3
"""Quarantine only derived/private copies by exact source path. Never read originals."""
import argparse, collections, fcntl, json, os, pathlib, shutil, sqlite3, sys
if __package__:
    from . import folder as intake
else:
    import folder as intake

def quarantine_hashes(named, edges, outside_uses=()):
    """Select downward scope without taking bytes still used by another parent.

    Only explicitly named hashes add their direct containers. Discovered children
    never add parents: a shared logo must not pull unrelated archives into scope.
    Prune outside-parent dependencies transitively, including initial seeds, so a
    retained nested container also protects all of its descendants. A discovered
    descendant in ``outside_uses`` is still needed by an unnamed in-scope top-level
    source file (an occurrence with no container, so no edge parent) and is pruned
    the same way. Named hashes and their direct containers are the operator's
    explicit target, so their own top-level occurrences do not protect them.
    """
    parents=collections.defaultdict(set); children=collections.defaultdict(set)
    for parent,child in edges:
        parents[child].add(parent); children[parent].add(child)
    hashes=set(named)
    for sha in named: hashes.update(parents[sha])
    seeds=set(hashes)
    todo=collections.deque(hashes)
    while todo:
        for child in children[todo.popleft()]:
            if child not in hashes:
                hashes.add(child); todo.append(child)
    outside_uses=set(outside_uses)-seeds
    todo=collections.deque(sha for sha in hashes if parents[sha]-hashes or sha in outside_uses)
    while todo:
        sha=todo.popleft()
        if sha in hashes:
            hashes.remove(sha)
            todo.extend(children[sha])
    return hashes


def quarantine(out, paths, config=None):
    config=config if config is not None else intake.load_config(output=str(out))
    config={**config,"output":str(pathlib.Path(out).absolute())}
    intake.validate_config(config,"report")
    os.umask(0o077); out=pathlib.Path(out).absolute()
    with (out/"writer.lock").open("a") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        db=sqlite3.connect(out/"intake.sqlite"); db.row_factory=sqlite3.Row; db.executescript(intake.SCHEMA)
        named=set(); matches={}; stamp=intake.now()
        for path in paths:
            if not pathlib.Path(path).is_absolute(): raise ValueError("exact absolute source paths required")
            found={r[0] for r in db.execute("SELECT sha FROM occurrences WHERE path=?",(path,))}
            named.update(found); matches[path]=found
        edges=[tuple(r) for r in db.execute("SELECT parent,child FROM edges")]
        # Top-level source files have no edge parent. Any such occurrence at a path
        # that is neither named now nor already scope-excluded still uses its bytes.
        excluded={r[0] for r in db.execute("SELECT path FROM scope_exclusions")}
        outside={r[1] for r in db.execute("SELECT path,sha FROM occurrences WHERE parent IS NULL")
                 if r[0] not in matches and r[0] not in excluded}
        hashes=quarantine_hashes(named,edges,outside)
        retained={}
        for path,found in matches.items():
            # A path can be excluded without globally excluding shared bytes.
            for sha in found & hashes:
                db.execute("INSERT OR IGNORE INTO scope_exclusions VALUES(?,?,?,?)",(path,sha,"unrelated_personal_record",stamp))
            if found-hashes:
                # Named bytes stay active because another in-scope use still needs them.
                retained[path]=sorted(found-hashes)
                db.execute("INSERT OR IGNORE INTO scope_exclusions VALUES(?,?,?,?)",(path,"","unrelated_personal_record_shared_bytes_retained",stamp))
                for sha in retained[path]:
                    intake.event(db,"scope-quarantine","scope","named_hash_retained_shared",path,sha,detail="named path excluded; bytes still used by an unnamed in-scope source or container, so not moved")
            elif not found:
                db.execute("INSERT OR IGNORE INTO scope_exclusions VALUES(?,?,?,?)",(path,"","unrelated_personal_record",stamp))
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
        intake.report(db,out,config)
        retained_hashes=sorted({sha for shas in retained.values() for sha in shas})
        result=dict(at=stamp,excluded_paths=len(paths),quarantined_hashes=len(hashes),prior_extracted_units=units,moved_derived_directories=derived,moved_blobs=blobs,originals_modified=0,
                    unmatched_paths=sum(not found for found in matches.values()),retained_named_paths=len(retained),retained_named_hashes=len(retained_hashes),
                    retained_named=[dict(path=path,sha256=shas) for path,shas in sorted(retained.items())])
        intake.atomic(out/"scope-event.json",result); print(intake.js(result)); db.close()
        if retained:
            print("warning: %d named path(s) excluded by path only; their bytes are still used by an unnamed in-scope source or container and remain in the active store (see retained_named)"%len(retained),file=sys.stderr)

if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__); p.add_argument("--output"); p.add_argument("--config"); p.add_argument("paths",nargs="+"); a=p.parse_args()
    try:
        config=intake.load_config(a.config,output=a.output)
        intake.validate_config(config,"report")
    except (OSError,ValueError,TypeError) as error: p.error(str(error))
    quarantine(config["output"],a.paths,config)
