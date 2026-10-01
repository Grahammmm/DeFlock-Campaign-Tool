"""Optional WP1 integration. Imports the installed dependency; never embeds its schema."""
import hashlib
import importlib
import json
import os
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from .policy import PortalError, safe_ancestors
from .store import check_hash, digest_bytes, sync_dir

ADAPTER_VERSION = "portal-wp1-pending-1"


def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(",",":"),allow_nan=False)


def verify_cas(path, digest, size):
    path=safe_ancestors(path)
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    try:
        before=os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_uid!=os.geteuid() or before.st_mode&0o077 or before.st_size!=size:
            raise PortalError("canonical_cas_invalid")
        sha=hashlib.sha256()
        while chunk:=os.read(fd,65536):
            sha.update(chunk)
        after=os.fstat(fd)
        if sha.hexdigest()!=digest or (before.st_ino,before.st_size,before.st_mtime_ns)!=(after.st_ino,after.st_size,after.st_mtime_ns):
            raise PortalError("canonical_cas_invalid")
    finally:
        os.close(fd)


class WP1LedgerAdapter:
    """Enrollment only: no preserve/catalog/review gate is bypassed."""
    def __init__(self, *, queue, database, cas_root, store_api=None, fault=None):
        self.queue=queue
        self.database=Path(database)
        self.cas=safe_ancestors(cas_root)
        self.cas.mkdir(mode=0o700,parents=False,exist_ok=True)
        info=self.cas.stat()
        if info.st_uid!=os.geteuid() or info.st_mode&0o077:
            raise PortalError("canonical_cas_not_private")
        try:
            self.store=store_api or importlib.import_module("campaign_tool.records.ledger.store")
        except ImportError:
            raise PortalError("wp1_dependency_unavailable") from None
        self.fault=fault

    def _bound(self, receipt, path):
        required={"id","item","sha256","byte_count","previous_sha256","attempt","provenance"}
        if not isinstance(receipt,dict) or set(receipt)!=required:
            raise PortalError("portal_receipt_shape")
        for name in ("id","item","sha256"):
            check_hash(receipt[name])
        if type(receipt["byte_count"]) is not int or not 0<receipt["byte_count"]<=100*1024*1024 or type(receipt["attempt"]) is not int:
            raise PortalError("portal_receipt_shape")
        if receipt["previous_sha256"] is not None:
            check_hash(receipt["previous_sha256"])
        version=self.queue.db.execute("SELECT * FROM portal_versions WHERE id=?",(receipt["id"],)).fetchone()
        if version is None:
            raise PortalError("portal_version_missing")
        expected=dict(version);expected.pop("ledger_state")
        expected["provenance"]=json.loads(expected.pop("provenance_json"))
        if canonical(expected)!=canonical(receipt):
            raise PortalError("portal_receipt_changed")
        attempt=self.queue.db.execute("SELECT * FROM portal_attempts WHERE id=?",(receipt["attempt"],)).fetchone()
        item=self.queue.db.execute("SELECT * FROM portal_items WHERE id=?",(receipt["item"],)).fetchone()
        provenance=receipt["provenance"]
        if not attempt or not item or attempt["item"]!=item["id"] or attempt["result"]!="received" or attempt["finished"] is None:
            raise PortalError("portal_attempt_unbound")
        if attempt["generation"]!=provenance.get("generation") or any(provenance.get(k)!=item[k] for k in ("host","request_id","item_id")):
            raise PortalError("portal_identity_unbound")
        computed=digest_bytes(json.dumps([item["host"],item["request_id"],item["item_id"]],separators=(",",":")).encode())
        rid=digest_bytes(json.dumps([item["id"],receipt["sha256"],receipt["previous_sha256"],receipt["attempt"]],separators=(",",":")).encode())
        if computed!=item["id"] or rid!=receipt["id"]:
            raise PortalError("portal_identity_unbound")
        evidence=json.loads(attempt["evidence_json"])
        if evidence!=provenance.get("http_evidence") or not evidence or evidence[-1].get("http_status")!=200 or evidence[-1].get("bytes")!=receipt["byte_count"]:
            raise PortalError("portal_http_evidence_unbound")
        sources=provenance.get("source_sha256s")
        if not isinstance(sources,list) or not sources or sources!=sorted(set(sources)):
            raise PortalError("portal_notice_unbound")
        for source in sources:
            check_hash(source)
            if not self.queue.db.execute("SELECT 1 FROM portal_notices WHERE item=? AND source_sha256=?",(item["id"],source)).fetchone():
                raise PortalError("portal_notice_unbound")
        if Path(os.path.abspath(path))!=self.queue.objects/receipt["sha256"]:
            raise PortalError("portal_object_path_unbound")
        self.queue.verify_object(receipt["sha256"],receipt["byte_count"])
        return item,attempt

    def _copy_cas(self, source, sha, size):
        destination=self.cas/sha
        if destination.exists() or destination.is_symlink():
            verify_cas(destination,sha,size)
            return destination
        fd,name=tempfile.mkstemp(prefix=".portal-",dir=self.cas)
        try:
            try:
                src=os.open(source,os.O_RDONLY|os.O_NOFOLLOW)
            except BaseException:
                os.close(fd)
                raise
            try:
                with os.fdopen(fd,"wb") as out:
                    count=0;digest=hashlib.sha256()
                    while chunk:=os.read(src,65536):
                        count+=len(chunk)
                        if count>size:
                            raise PortalError("source_changed")
                        digest.update(chunk);out.write(chunk)
                    if count!=size or digest.hexdigest()!=sha:
                        raise PortalError("source_changed")
                    out.flush();os.fsync(out.fileno())
            finally:
                os.close(src)
            try:
                os.link(name,destination)
                os.chmod(destination,0o400)
                sync_dir(self.cas)
            except FileExistsError:
                pass
            verify_cas(destination,sha,size)
            return destination
        finally:
            Path(name).unlink(missing_ok=True)

    def record_original(self, receipt, object_path):
        item,attempt=self._bound(receipt,object_path)
        sha=receipt["sha256"];size=receipt["byte_count"]
        path=self._copy_cas(object_path,sha,size)
        if self.fault:
            self.fault("after_canonical_bytes")
        stamp=datetime.fromtimestamp(attempt["finished"],timezone.utc).isoformat()
        run_id="portal-delivery:"+receipt["id"]
        occurrence_id="portal:"+receipt["id"]
        source_ref="portal-item:"+item["id"]
        attempts_for_item=self.queue.db.execute("SELECT count(*) FROM portal_attempts WHERE item=? AND id<=?",(item["id"],receipt["attempt"])).fetchone()[0]
        summary=canonical({"adapter":ADAPTER_VERSION,"receipt":receipt,"cas_path":str(path),"stage_promotions":0})
        evidence=canonical({"receipt":receipt,"cas_path":str(path),"source_notice_binding":"hash_reference_only"})
        with self.store.ledger(self.database) as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                old_run=con.execute("SELECT * FROM runs WHERE run_id=?",(run_id,)).fetchone()
                if old_run and (old_run["summary"]!=summary or old_run["kind"]!="portal_enrollment" or old_run["status"]!="pending_stage_validation"):
                    raise PortalError("canonical_run_changed")
                original=con.execute("SELECT * FROM originals WHERE sha256=?",(sha,)).fetchone()
                if original and (original["bytes"]!=size or original["storage_path"]!=str(path)):
                    raise PortalError("canonical_original_binding_conflict")
                occurrence=con.execute("SELECT * FROM occurrences WHERE id=?",(occurrence_id,)).fetchone()
                if occurrence and (occurrence["original_sha256"]!=sha or occurrence["kind"]!="portal" or occurrence["source_ref"]!=source_ref or occurrence["evidence"]!=evidence or occurrence["acquired_at"]!=stamp or occurrence["parent_occurrence_id"] is not None or occurrence["acquisition_method"]!=ADAPTER_VERSION):
                    raise PortalError("canonical_occurrence_changed")
                if bool(old_run)!=bool(occurrence):
                    raise PortalError("canonical_checkpoint_mismatch")
                if not old_run:
                    con.execute("INSERT INTO runs(run_id,kind,started_at,ended_at,engine_version,image_digest,config_sha256,coverage_cutoff,status,summary) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (run_id,"portal_enrollment",stamp,stamp,ADAPTER_VERSION,None,None,None,"pending_stage_validation",summary))
                if not original:
                    con.execute("INSERT INTO originals(sha256,bytes,mime_detected,first_seen_at,role,scope,storage_path,preservation_status,legacy_format,provenance_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (sha,size,receipt["provenance"]["http_evidence"][-1]["content_type"],stamp,"portal_original","unresolved",str(path),"receipt_validation_pending",None,summary))
                    for stage in self.store.STAGES:
                        con.execute("INSERT INTO stage_state(original_sha256,stage,status,receipt_sha256,owner,updated_at,run_id,reason) VALUES(?,?,?,?,?,?,?,?)",
                            (sha,stage,"pending",None,"trusted_stage_adapter",stamp,run_id,"portal bytes verified; trusted stage validation pending"))
                else:
                    stages=con.execute("SELECT stage FROM stage_state WHERE original_sha256=?",(sha,)).fetchall()
                    if {r[0] for r in stages}!=set(self.store.STAGES):
                        raise PortalError("canonical_stage_slots_missing")
                portal=con.execute("SELECT * FROM portal_items WHERE id=?",(item["id"],)).fetchone()
                if portal and any(portal[k]!=item[v] for k,v in (("portal_host","host"),("request_id","request_id"),("item_id","item_id"))):
                    raise PortalError("canonical_portal_changed")
                if old_run and not portal:
                    raise PortalError("canonical_portal_missing")
                # Replays of old receipts never overwrite a later item's current version.
                previous=con.execute("SELECT evidence FROM occurrences WHERE source_ref=? AND kind='portal' LIMIT 10001",(source_ref,)).fetchall()
                if len(previous)>10000:
                    raise PortalError("portal_history_limit")
                history=[json.loads(r[0])["receipt"] for r in previous]
                latest_receipt=max(history,key=lambda r:r["attempt"]) if history else None
                latest=latest_receipt["attempt"] if latest_receipt else 0
                if portal and latest_receipt and portal["original_sha256"]!=latest_receipt["sha256"]:
                    raise PortalError("canonical_portal_version_changed")
                if portal and not history and portal["original_sha256"] is not None:
                    raise PortalError("canonical_portal_history_unbound")
                if not occurrence:
                    con.execute("INSERT INTO occurrences(id,original_sha256,kind,source_ref,parent_occurrence_id,acquired_at,acquisition_method,evidence) VALUES(?,?,?,?,?,?,?,?)",
                        (occurrence_id,sha,"portal",source_ref,None,stamp,ADAPTER_VERSION,evidence))
                if portal is None:
                    con.execute("INSERT INTO portal_items(id,portal_host,request_id,item_id,agency_id,title,notice_occurrence_id,retrieval_status,last_attempt_at,attempts,last_error,original_sha256) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (item["id"],item["host"],item["request_id"],item["item_id"],None,None,None,"bytes_verified_stage_pending",stamp,attempts_for_item,None,sha))
                elif receipt["attempt"]>latest:
                    con.execute("UPDATE portal_items SET original_sha256=?,retrieval_status='bytes_verified_stage_pending',last_attempt_at=?,attempts=?,last_error=NULL WHERE id=?",
                        (sha,stamp,attempts_for_item,item["id"]))
                elif receipt["attempt"]==latest and portal["original_sha256"]!=sha:
                    raise PortalError("canonical_portal_version_changed")
                if self.fault:
                    self.fault("before_canonical_commit")
                con.commit()
            except BaseException:
                con.rollback()
                raise
        if self.fault:
            self.fault("after_canonical_commit")
        return {"subject_sha256":sha,"run_id":run_id,"stage_promotions":0,"preserve":"pending","catalog":"pending"}

    def pending_card_inputs(self,limit=100):
        if type(limit) is not int or not 1<=limit<=1000:
            raise ValueError("invalid_limit")
        with self.store.ledger(self.database,readonly=True) as con:
            return [dict(row) for row in con.execute(
                "SELECT o.sha256,o.bytes,o.storage_path,s.status FROM originals o JOIN stage_state s ON s.original_sha256=o.sha256 WHERE s.stage='catalog' AND s.status='pending' AND EXISTS(SELECT 1 FROM occurrences x WHERE x.original_sha256=o.sha256 AND x.kind='portal') ORDER BY o.sha256 LIMIT ?",(limit,))]

    def validate_and_promote(self,*args,**kwargs):
        raise PortalError("trusted_portal_stage_adapter_unconfigured")
