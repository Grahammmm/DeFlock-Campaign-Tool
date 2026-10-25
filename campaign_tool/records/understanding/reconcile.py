"""Cited factual reconciliation with explicit applicability and legal holds."""
from collections import defaultdict
from copy import deepcopy
from . import evidence,policy
VERSION="records-evidence-reconciliation-v1"
UNKNOWN={"","n/a","na","unknown","not provided","not available","null","none"}
def unknown(v):return v is None or isinstance(v,str) and v.strip().casefold() in UNKNOWN
def test_value(value,test):
 op=test.get("operator");expected=test.get("expected")
 if op=="nonempty":return not unknown(value)
 if op=="equals":return value==expected
 if op=="not_equals":return value!=expected
 if op=="contains":return isinstance(value,str) and isinstance(expected,str) and expected.casefold() in value.casefold()
 if op in ("at_most","at_least"):
  if type(value)not in (int,float) or type(expected)not in (int,float):return None
  return value<=expected if op=="at_most" else value>=expected
 return None
FINDING_IDENTITY_VERSION="content-bound-v2"
def canonical_citations(cites):
 # New sorted list of the same citation objects; inputs are never reordered in place.
 return sorted(cites,key=evidence.canonical) if type(cites)is list else cites
def canonical_facts(facts):
 # Copies ordered by the unique fact_id, each with canonically ordered citations.
 return [dict(f,citations=canonical_citations(f.get("citations"))) for f in sorted(facts,key=lambda f:f["fact_id"])]
def canonical_cited(item):
 # Copy of one cited item (requirement, exception or term) with canonically ordered citations.
 return dict(item,citations=canonical_citations(item["citations"])) if type(item)is dict and "citations" in item else item
CITED_POLICY_FIELDS=("exceptions","incorporated_terms")
def canonical_governing(version):
 # Selected-version payload minus requirements (bound separately); every cited list item keeps its
 # position but its citation sequence is canonical. Order-significant value arrays are untouched.
 if version is None:return None
 governing={k:v for k,v in version.items() if k!="requirements"}
 for k in CITED_POLICY_FIELDS:
  if type(governing.get(k))is list:governing[k]=[canonical_cited(x) for x in governing[k]]
 return governing
def finding_identity(*,snapshot_sha256,scope,family_id,rule_selection,version,requirement,event_key,facts):
 """One versioned canonical preimage binding a finding to its evidence and governing policy."""
 return evidence.sha({"finding_identity_version":FINDING_IDENTITY_VERSION,"snapshot_sha256":snapshot_sha256,
  "scope":scope,"family_id":family_id,"rule_selection":rule_selection,"policy_version":canonical_governing(version),
  "requirement":canonical_cited(requirement),
  "event_key":event_key,"facts":canonical_facts(facts)})
def rule_support_bound(req,version,bundle):
 cites=req.get("citations")
 if type(cites)is not list or not cites:return False
 try:
  for cite in cites:
   evidence.grounded_evidence.validate_support(cite,bundle)
   if cite.get("original_sha256")!=version["source"].get("content_sha256"):return False
 except (ValueError,KeyError,TypeError):return False
 # A span must support the transcribed requirement, not merely share its source.
 return any(req["text"] in c.get("quote","") for c in cites)
def compare(facts,versions,*,bundle,agency_id,jurisdiction,regulated_role,event_at,request_number=None):
 evidence.validate_bundle(bundle)
 evidence.need(type(facts)is list and len(facts)<=10000,"fact_count_bound")
 evidence.need(all(type(x)is str and x for x in (agency_id,jurisdiction,regulated_role)),"reconciliation_context")
 checked=policy.validate_versions(versions)
 if not checked["valid"]:return deepcopy({"schema":VERSION,"findings":[],"gaps":checked["issues"],"status":"invalid_versions","review_acceptance":False,"legal_conclusions":0})
 byfield=defaultdict(list);fact_gaps=[];ids=set()
 for f in facts:
  evidence.need(type(f)is dict and type(f.get("fact_id"))is str and f["fact_id"] not in ids and type(f.get("field"))is str and f["field"],"fact_identity")
  ids.add(f["fact_id"]);cites=f.get("citations")
  if type(cites)is not list or not cites:fact_gaps.append({"fact_id":f["fact_id"],"reason":"fact_uncited"});continue
  try:
   for cite in cites:evidence.grounded_evidence.validate_support(cite,bundle)
  except (ValueError,KeyError,TypeError):fact_gaps.append({"fact_id":f["fact_id"],"reason":"fact_citation_unverified"});continue
  if f.get("agency_id",agency_id)!=agency_id or request_number is not None and f.get("request_number",request_number)!=request_number:
   fact_gaps.append({"fact_id":f["fact_id"],"reason":"fact_scope_mismatch"});continue
  byfield[(f.get("event_key","request-level"),f["field"])].append(f)
 families=defaultdict(list)
 for v in versions:families[v["family_id"]].append(v)
 findings=[]
 for family,rows in sorted(families.items()):
  selected=policy.requirements_for(rows,agency_id=agency_id,jurisdiction=jurisdiction,regulated_role=regulated_role,event_at=event_at)
  version=selected["selection"].get("version")
  requirements=selected["requirements"] if version else [dict(r,version_id=v["version_id"],source_pointer=v["source"]) for v in rows for r in v.get("requirements",[])]
  if not requirements:
   findings.append({"family_id":family,"requirement_id":None,"classification":"missing_evidence","reason":"requirements_not_transcribed","rule_selection":selected["status"],"citations":[],"legal_assessment":"NOT_ASSESSED","review_acceptance":False});continue
  for req in requirements:
   rid=req.get("requirement_id");evidence.need(type(rid)is str and rid and type(req.get("text"))is str and req["text"],"requirement_shape")
   test=req.get("test",{});field=test.get("field") if type(test)is dict else None
   events=sorted({k[0] for k in byfield if k[1]==field}) or ["request-level"]
   for event in events:
    raw=byfield.get((event,field),[]);obs=canonical_facts(raw);cites=[c for f in obs for c in f["citations"]]
    fid=finding_identity(snapshot_sha256=bundle["snapshot_sha256"],scope={"agency_id":agency_id,"request_number":request_number,"jurisdiction":jurisdiction,"regulated_role":regulated_role,"event_at":event_at},family_id=family,rule_selection=selected["status"],version=version,requirement=req,event_key=event,facts=raw)
    base={"finding_id":fid,"finding_identity_version":FINDING_IDENTITY_VERSION,"agency_id":agency_id,"request_number":request_number,"family_id":family,"version_id":req.get("version_id"),"requirement_id":rid,"requirement_text":req["text"],"rule_source":req.get("source_pointer"),"rule_selection":selected["status"],"event_key":event,"fact_ids":[f["fact_id"] for f in obs],"citations":cites,"counterevidence":[f["fact_id"] for f in obs if f.get("counterevidence")],"exceptions":[f["fact_id"] for f in obs if f.get("exception_applies")],"legal_assessment":"NOT_ASSESSED","review_acceptance":False,"derived":True}
    if version is None:classification="applicability_unresolved";reason=selected["status"]
    elif not evidence.is_hash(version["source"].get("content_sha256")) or version["source"].get("verification_status") not in ("verified","verified_primary","verified_exact_source") or not rule_support_bound(req,version,bundle):
     classification="applicability_unresolved";reason="rule_source_or_requirement_span_unbound"
    elif not field:classification="not_assessed";reason="record_field_mapping_not_supplied"
    elif not obs or all(unknown(f.get("value")) for f in obs):classification="missing_evidence";reason="missing_or_NA_is_not_violation"
    elif any(f.get("counterevidence") for f in obs):classification="contradictory_evidence";reason="counterevidence_requires_resolution"
    elif len({evidence.sha(f.get("value")) for f in obs if not unknown(f.get("value"))})>1:classification="contradictory_evidence";reason="same_event_field_disagrees"
    elif any(f.get("exception_applies") for f in obs):classification="exception_applies";reason="cited_exception_candidate_requires_review"
    else:
     outcomes=[test_value(f["value"],test) for f in obs if not unknown(f.get("value"))]
     if not outcomes or any(x is None for x in outcomes):classification="not_assessed";reason="comparison_type_or_operator_unconfigured"
     elif all(outcomes):classification="no_conflict_observed";reason="only_supplied_fields_no_global_compliance_claim"
     else:classification="potential_conflict";reason="authenticated_factual_exception_and_terms_review_not_established"
    base.update(classification=classification,reason=reason,next_action="Resolve source/applicability/record gaps or independently review cited facts; do not infer legal violation.")
    findings.append(base)
 # Snapshot only the public return boundary; identity preimages and classification stay unchanged.
 return deepcopy({"schema":VERSION,"snapshot_sha256":bundle["snapshot_sha256"],"context":{"agency_id":agency_id,"request_number":request_number,"jurisdiction":jurisdiction,"regulated_role":regulated_role,"event_at":event_at},"findings":findings,"gaps":fact_gaps,"status":"derived_findings_needing_review","review_acceptance":False,"legal_conclusions":0})
