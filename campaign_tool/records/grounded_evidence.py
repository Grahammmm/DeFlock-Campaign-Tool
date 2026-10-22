"""Exact retained-text evidence; structural support never implies semantic acceptance."""
import hashlib,json,re
from . import retained_units as ru
VERSION="records-grounded-evidence-v1"
MAX_TEXT=1024*1024
MAX_TOKENS=4096
MAX_DEPTH=64
KEYS=("unit_id","original_sha256","import_id","artifact_sha256","text_sha256")
def _need(ok,code):
    if not ok: raise ValueError(code)
def _digest(v):return hashlib.sha256(ru._canon(v)).hexdigest()
def _unit(u):
    _need(type(u)is dict and all(type(u.get(k))is str and ru.HEX.fullmatch(u[k]) for k in KEYS),"evidence_unit_binding")
    _need(type(u.get("text"))is str and type(u.get("locator"))is dict,"evidence_unit_shape")
    raw=u["text"].encode("utf-8");_need(len(raw)<=MAX_TEXT,"evidence_text_bound")
    _need(hashlib.sha256(raw).hexdigest()==u["text_sha256"],"evidence_text_hash")
    return raw
def _tokens(text):
    # Strict JSON decode rejects duplicate keys/nonfinite values. Token offsets
    # are character offsets converted once to UTF-8 byte boundaries.
    ru._json(text,"evidence_json_invalid")
    out={};count=0;decoder=json.JSONDecoder()
    def ws(i):
        while i<len(text) and text[i] in " \t\r\n":i+=1
        return i
    def walk(i,path,depth):
        nonlocal count
        count+=1;_need(count<=MAX_TOKENS and depth<=MAX_DEPTH,"evidence_json_bound")
        i=ws(i);start=i;_need(i<len(text),"evidence_json_invalid")
        if text[i]=="{":
            i=ws(i+1)
            if i<len(text) and text[i]=="}":i+=1
            else:
                while True:
                    key,j=decoder.raw_decode(text,i);_need(type(key)is str,"evidence_json_invalid")
                    i=ws(j);_need(i<len(text) and text[i]==":","evidence_json_invalid")
                    i=walk(i+1,path+"/"+key.replace("~","~0").replace("/","~1"),depth+1);i=ws(i)
                    _need(i<len(text),"evidence_json_invalid")
                    if text[i]=="}":i+=1;break
                    _need(text[i]==",","evidence_json_invalid");i=ws(i+1)
        elif text[i]=="[":
            i=ws(i+1);n=0
            if i<len(text) and text[i]=="]":i+=1
            else:
                while True:
                    i=walk(i,path+"/"+str(n),depth+1);n+=1;i=ws(i)
                    _need(i<len(text),"evidence_json_invalid")
                    if text[i]=="]":i+=1;break
                    _need(text[i]==",","evidence_json_invalid");i=ws(i+1)
        else:_,i=decoder.raw_decode(text,i)
        out[path]=(len(text[:start].encode("utf-8")),len(text[:i].encode("utf-8")))
        return i
    end=walk(0,"",0);_need(ws(end)==len(text),"evidence_json_invalid")
    return out
def span_support(unit,start,end):
    raw=_unit(unit)
    _need(type(start)is int and type(end)is int and 0<=start<end<=len(raw),"evidence_span_bound")
    try:raw[:start].decode("utf-8");quote=raw[start:end].decode("utf-8");raw[end:].decode("utf-8")
    except UnicodeError:raise ValueError("evidence_utf8_boundary") from None
    return {"schema":VERSION,**{k:unit[k] for k in KEYS},"locator":unit["locator"],
            "span":{"start_byte":start,"end_byte":end},"quote":quote,"json_pointer":None}
def pointer_support(unit,pointer):
    _need(type(pointer)is str and len(pointer)<=1024,"evidence_pointer_invalid")
    _unit(unit);tokens=_tokens(unit["text"]);_need(pointer in tokens,"evidence_pointer_missing")
    start,end=tokens[pointer];s=span_support(unit,start,end);s["json_pointer"]=pointer
    return s
DATA_VERSION="records-grounded-data-evidence-v1"
def data_pointer_support(unit,pointer):
    _unit(unit)
    _need(unit.get("authority_basis")=="receipt_bound_derivative_not_canonical","data_evidence_derivative_binding")
    raw_payload={k:unit.get(k) for k in ("kind","locator","text","data")}
    _need(_digest(raw_payload)==unit["artifact_sha256"],"data_evidence_artifact_binding")
    _need(type(unit.get("data"))is dict and type(pointer)is str and len(pointer)<=1024,"data_evidence_shape")
    raw=ru._canon(unit["data"]);_need(len(raw)<=MAX_TEXT,"data_evidence_bound")
    text=raw.decode("utf-8");tokens=_tokens(text);_need(pointer in tokens,"evidence_pointer_missing")
    start,end=tokens[pointer]
    return {"schema":DATA_VERSION,**{k:unit[k] for k in KEYS},"locator":unit["locator"],
            "span":{"start_byte":start,"end_byte":end},"quote":raw[start:end].decode("utf-8"),
            "json_pointer":pointer,"data_sha256":hashlib.sha256(raw).hexdigest(),"span_basis":"canonical_retained_unit_data"}
def validate_support(support,read_result):
    _need(type(read_result)is dict and ru.HEX.fullmatch(read_result.get("snapshot_sha256","")),"evidence_snapshot")
    _need(type(support)is dict,"evidence_support_shape")
    data_support=support.get("schema")==DATA_VERSION
    shape={"schema",*KEYS,"locator","span","quote","json_pointer"}
    if data_support:shape|={"data_sha256","span_basis"}
    _need(set(support)==shape,"evidence_support_shape")
    matches=[u for u in read_result.get("units",[]) if u.get("unit_id")==support["unit_id"]]
    _need(len(matches)==1,"evidence_unit_not_current")
    u=matches[0];_need(all(support[k]==u[k] for k in KEYS) and support["locator"]==u["locator"],"evidence_binding_mismatch")
    _need(type(support["span"])is dict and set(support["span"])=={"start_byte","end_byte"},"evidence_span_shape")
    if data_support:expected=data_pointer_support(u,support["json_pointer"])
    elif support["json_pointer"] is None:
        expected=span_support(u,support["span"]["start_byte"],support["span"]["end_byte"])
    else:expected=pointer_support(u,support["json_pointer"])
    _need(support==expected,"evidence_token_or_quote_mismatch")
    return {"structural_support":"verified_exact_retained_data_projection" if data_support else "verified_exact_retained_bytes","semantic_assessment":"NOT_ASSESSED",
            "review_acceptance":False,"snapshot_sha256":read_result["snapshot_sha256"]}
def derived_claim(read_result,context_sha256,claim,supports):
    _need(type(context_sha256)is str and ru.HEX.fullmatch(context_sha256),"evidence_context_pin")
    _need(type(claim)is str and 0<len(claim.encode("utf-8"))<=8192,"evidence_claim_bound")
    _need(type(supports)is list and 0<len(supports)<=32,"evidence_support_bound")
    for s in supports:validate_support(s,read_result)
    body={"schema":VERSION,"snapshot_sha256":read_result["snapshot_sha256"],"context_sha256":context_sha256,
          "claim":claim,"supports":supports,"derived":True,"semantic_assessment":"NOT_ASSESSED","review_acceptance":False}
    return {**body,"evidence_sha256":_digest(body)}
def validate_claim(claim,read_result,context_sha256):
    _need(type(claim)is dict,"evidence_claim_shape")
    expected=derived_claim(read_result,context_sha256,claim.get("claim"),claim.get("supports"))
    _need(claim==expected,"evidence_claim_stale_or_changed")
    return {"structural_support":"verified_exact_retained_bytes","semantic_assessment":"NOT_ASSESSED","review_acceptance":False}
