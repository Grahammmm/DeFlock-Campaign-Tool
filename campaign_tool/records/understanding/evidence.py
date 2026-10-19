"""Receipt-bound derivative evidence; never substitutes for canonical admission."""
import hashlib,json,re,os,stat
from pathlib import Path
from .. import grounded_evidence
HEX=re.compile(r"[0-9a-f]{64}\Z")
VERSION="records-understanding-input-v1"
MAX_UNIT_BYTES=1024*1024
MAX_RECEIPT_BYTES=64*1024*1024
MAX_UNITS=10000
MAX_TOTAL_BYTES=128*1024*1024
def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=False,allow_nan=False).encode("utf-8")
def sha(value):
    return hashlib.sha256(value if isinstance(value,bytes) else canonical(value)).hexdigest()
def need(ok,code):
    if not ok:raise ValueError(code)
def is_hash(v):
    return type(v)is str and HEX.fullmatch(v) is not None
def decode(raw):
    def pairs(items):
        d={}
        for k,v in items:
            need(k not in d,"duplicate_json_key");d[k]=v
        return d
    return json.loads(raw,object_pairs_hook=pairs,parse_constant=lambda v:(_ for _ in ()).throw(ValueError("nonfinite_json")))
def read_pinned(path,expected,limit=MAX_RECEIPT_BYTES):
    p=Path(os.path.abspath(path))
    need(is_hash(expected),"artifact_hash_required")
    need(not any(x.is_symlink() for x in (p,*p.parents)),"symlink_artifact")
    fd=os.open(p,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        before=os.fstat(fd)
        need(stat.S_ISREG(before.st_mode) and before.st_uid==os.geteuid() and not before.st_mode&0o077 and before.st_size<=limit,"private_artifact_bound")
        chunks=[];size=0
        while True:
            b=os.read(fd,min(65536,limit-size+1))
            if not b:break
            size+=len(b);need(size<=limit,"artifact_size_bound");chunks.append(b)
        after=os.fstat(fd)
        need((before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns)==(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns),"artifact_changed")
        raw=b"".join(chunks);need(sha(raw)==expected,"artifact_hash_mismatch");return raw
    finally:os.close(fd)
def unit(raw,subject,receipt_sha256,ordinal,attempt_kind="native"):
    need(is_hash(subject) and is_hash(receipt_sha256),"unit_source_binding")
    need(type(raw)is dict and set(raw)=={"kind","locator","text","data"},"unit_shape")
    need(type(raw["text"])is str and type(raw["locator"])is dict and type(raw["data"])is dict and type(raw["kind"])is str,"unit_payload_shape")
    need(type(ordinal)is int and ordinal>0,"unit_ordinal")
    payload=canonical(raw);need(len(payload)<=MAX_UNIT_BYTES,"unit_size_bound")
    imp=sha(["receipt-bound-derivative-v1",subject,receipt_sha256,attempt_kind])
    art=sha(payload)
    return {"unit_id":sha([imp,ordinal,art]),"original_sha256":subject,"import_id":imp,"artifact_sha256":art,"text_sha256":sha(raw["text"].encode()),**raw,"ordinal":ordinal,"receipt_sha256":receipt_sha256,"attempt_kind":attempt_kind,"authority_basis":"receipt_bound_derivative_not_canonical"}
def bundle(units,source_context,source_receipts):
    body={"schema":VERSION,"units":sorted(units,key=lambda u:(u["original_sha256"],u["receipt_sha256"],u["ordinal"])),"source_context":source_context,"source_receipts":sorted(source_receipts,key=lambda r:(r["original_sha256"],r["receipt_sha256"]))}
    return validate_bundle({**body,"snapshot_sha256":sha(body)})
def normalize_receipt(receipt,subject,receipt_sha256,format=None,extra_context=None):
    need(type(receipt)is dict and receipt.get("schema")=="format-extraction-v1" and receipt.get("original_sha256")==subject and receipt.get("original_changed")is False,"native_receipt_binding")
    raw_units=receipt.get("units");pages=receipt.get("pages")
    need(type(raw_units)is list and type(pages)is list,"native_receipt_units")
    out=[];gaps=[]
    for ordinal,raw in enumerate(raw_units,1):
        if ordinal>MAX_UNITS:gaps.append({"code":"unit_count_bound","first_unprocessed_ordinal":ordinal,"remaining":len(raw_units)-len(out)});break
        try:out.append(unit(raw,subject,receipt_sha256,ordinal))
        except (ValueError,UnicodeError,TypeError):gaps.append({"code":"unit_normalization_failed","ordinal":ordinal})
    context={"format":format or receipt.get("route",{}).get("format","unknown"),"native_status":receipt.get("status","unknown"),"pages":pages,"declared_unit_count":len(raw_units),"normalized_unit_count":len(out),"normalization_gaps":gaps,"parser":receipt.get("parser"),"parser_version":receipt.get("parser_version"),"native_receipt_sha256":receipt_sha256,"review_acceptance":False,"semantic_acceptance":False}
    if extra_context:
        need(type(extra_context)is dict and not set(extra_context).intersection(context),"source_context_reserved_override")
        context.update(extra_context)
    source={"original_sha256":subject,"receipt_sha256":receipt_sha256,"attempt_kind":"native","declared_unit_count":len(raw_units),"normalized_unit_count":len(out)}
    return bundle(out,{subject:context},[source])
def validate_bundle(value):
    need(type(value)is dict and set(value)=={"schema","snapshot_sha256","units","source_context","source_receipts"} and value["schema"]==VERSION,"bundle_shape")
    need(type(value["units"])is list and type(value["source_context"])is dict and type(value["source_receipts"])is list and is_hash(value["snapshot_sha256"]),"bundle_binding")
    need(len(value["units"])<=MAX_UNITS and len(canonical(value))<=MAX_TOTAL_BYTES,"bundle_bound")
    body={k:v for k,v in value.items() if k!="snapshot_sha256"};need(sha(body)==value["snapshot_sha256"],"bundle_snapshot_mismatch")
    receipts={}
    for r in value["source_receipts"]:
        need(type(r)is dict and is_hash(r.get("original_sha256")) and is_hash(r.get("receipt_sha256")),"receipt_binding")
        key=(r["original_sha256"],r["receipt_sha256"]);need(key not in receipts,"duplicate_receipt");receipts[key]=r
    ids=set()
    for u in value["units"]:
        need(type(u)is dict and u.get("authority_basis")=="receipt_bound_derivative_not_canonical","unit_authority")
        subject=u.get("original_sha256");need(subject in value["source_context"] and (subject,u.get("receipt_sha256")) in receipts,"unit_receipt_missing")
        raw={k:u.get(k) for k in ("kind","locator","text","data")}
        expected=unit(raw,subject,u["receipt_sha256"],u.get("ordinal"),u.get("attempt_kind"))
        need(u==expected,"unit_binding_mismatch");need(u["unit_id"] not in ids,"duplicate_unit");ids.add(u["unit_id"])
    return value
def citation(u,start_byte,end_byte):
    return grounded_evidence.span_support(u,start_byte,end_byte)
def validate_citation(support,value):
    validate_bundle(value)
    return grounded_evidence.validate_support(support,value)
def claim(value,text,citations):
    validate_bundle(value)
    return grounded_evidence.derived_claim(value,sha(value["source_context"]),text,citations)
def validate_claim(item,value):
    validate_bundle(value)
    return grounded_evidence.validate_claim(item,value,sha(value["source_context"]))
def cite_text(u,quote):
    need(type(quote)is str and quote,"quote_required")
    raw=u["text"].encode();needle=quote.encode();start=raw.find(needle)
    need(start>=0,"quote_not_in_source")
    return citation(u,start,start+len(needle))
