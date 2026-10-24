"""Offline portable commands. Outputs are private immutable derivatives."""
import argparse,errno,json,os,stat,tempfile,hashlib
from pathlib import Path
from . import evidence,document,correspondence,policy,reconcile
VERSION="records-understanding-cli-v1"
def code_pins():
 root=Path(__file__).parent
 pins={p.name:evidence.sha(p.read_bytes()) for p in sorted(root.glob("*.py"))}
 for name in ("grounded_evidence.py","retained_units.py"):
  pins["records/"+name]=evidence.sha((root.parent/name).read_bytes())
 return pins
def read_input(path,expected=None):
 p=Path(os.path.abspath(path));evidence.need(not any(x.is_symlink() for x in (p,*p.parents)),"input_symlink")
 s=p.stat();evidence.need(stat.S_ISREG(s.st_mode) and s.st_uid==os.geteuid() and not s.st_mode&0o077 and s.st_size<=evidence.MAX_TOTAL_BYTES,"private_input_bound")
 raw=p.read_bytes();pin=evidence.sha(raw)
 evidence.need(expected is None or pin==expected,"input_pin_mismatch")
 return evidence.decode(raw),pin
def normalize_input(value):
 if isinstance(value,dict) and value.get("schema")==evidence.VERSION:return evidence.validate_bundle(value)
 if isinstance(value,dict) and "bundle" in value:return evidence.validate_bundle(value["bundle"])
 evidence.need(isinstance(value,dict) and isinstance(value.get("native_binding"),dict),"input_bundle_or_binding_required")
 b=value["native_binding"];raw=evidence.read_pinned(b["receipt_path"],b["receipt_sha256"])
 receipt=evidence.decode(raw)
 return evidence.normalize_receipt(receipt,b["original_sha256"],b["receipt_sha256"],b.get("format"),b.get("source_context"))
def execute(command,value):
 if command=="document":
  b=normalize_input(value);r=document.analyze(b)
  if isinstance(value,dict) and "model_proposal" in value:r["model_observation"]=document.apply_model(b,value["model_proposal"])
  return r
 if command=="requests":
  b=normalize_input(value) if "bundle" in value or "native_binding" in value else None
  events=value.get("events")
  if events is None:events=correspondence.events_from_bundle(b,value["agency_id"],value["request_numbers"])
  return correspondence.track(events,as_of=value.get("as_of"),deadline_rules=value.get("deadline_rules"),bundle=b)
 if command=="policy":
  versions=value["versions"];out={"validation":policy.validate_versions(versions)}
  if "scope" in value:out["requirements"]=policy.requirements_for(versions,**value["scope"])
  return out
 if command=="reconcile":
  return reconcile.compare(value["facts"],value["versions"],bundle=normalize_input(value),**value["scope"])
 raise ValueError("unknown_understanding_command")
def private_root(path):
 p=Path(os.path.abspath(path))
 evidence.need(not any(x.is_symlink() or (x/".git").exists() for x in (p,*p.parents)),"output_symlink_or_git")
 engine=Path(__file__).parents[3]
 evidence.need(p!=engine and engine not in p.parents,"output_inside_source")
 p.mkdir(mode=0o700,parents=True,exist_ok=True)
 s=p.stat();evidence.need(s.st_uid==os.geteuid() and not s.st_mode&0o077 and stat.S_ISDIR(s.st_mode),"private_output_required")
 return p
def _write(path,raw):
 fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
 with os.fdopen(fd,"wb")as f:f.write(raw);f.flush();os.fsync(f.fileno())
 path.chmod(0o400)
def safe_summary(command,r):
 if command=="document":
  return {"normalized_units":sum(x.get("normalized_unit_count",x.get("native_unit_count",0)) for x in r.get("coverage",[])),"date_mentions":len(r.get("date_mentions",[])),"entities":len(r.get("entity_candidates",[])),"summary_facts":len(r.get("summary_facts",[])),"partial_understanding":r.get("partial_understanding",True),"review_acceptance":False}
 if command=="requests":return {"requests":len(r["requests"]),"timeline_events":sum(len(x["timeline"]) for x in r["requests"]),"due_actions":len(r["due_actions"]),"holds":len(r["holds"]),"review_acceptance":False}
 if command=="reconcile":return {"findings":len(r["findings"]),"gaps":len(r["gaps"]),"legal_conclusions":0,"review_acceptance":False}
 return {"version_validation_valid":r["validation"]["valid"],"issues":len(r["validation"]["issues"]),"gaps":len(r["validation"]["gaps"]),"legal_conclusions":0}
def findings_markdown(result):
 lines=["# Private derived findings","", "Every observation remains qualified for factual/applicability/legal review.",""]
 for f in result.get("findings",[]):
  lines.extend(["- "+f["classification"]+": "+str(f.get("requirement_id") or "untranscribed requirement")+" ("+f["reason"]+")"])
  for c in f.get("citations",[]):
   loc=json.dumps(c["locator"],sort_keys=True);span=c["span"]
   lines.append("  Source "+c["original_sha256"]+" "+loc+" bytes "+str(span["start_byte"])+"–"+str(span["end_byte"])+": "+c["quote"].replace("\n"," "))
 return ("\n".join(lines)+"\n").encode()
_DIR_FLAGS=getattr(os,"O_PATH",os.O_RDONLY)|os.O_DIRECTORY|os.O_NOFOLLOW|getattr(os,"O_CLOEXEC",0)
_RUN_FLAGS=os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|getattr(os,"O_CLOEXEC",0)
_LEAF_FLAGS=os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK|getattr(os,"O_CLOEXEC",0)
def _refuse(e,symlink_code):
 """Map a validation OSError to a stable non-payload code (never the message or path)."""
 if e.errno in (errno.ELOOP,errno.ENOTDIR):return ValueError(symlink_code)
 if e.errno in (errno.ENOENT,getattr(errno,"ESTALE",errno.ENOENT)):return ValueError("existing_run_changed")
 return ValueError("existing_run_artifact_unreadable")
def _ident(st):return (st.st_dev,st.st_ino)
def _entry(dfd,name):
 """No-follow identity of one name inside an already opened directory."""
 st=os.stat(name,dir_fd=dfd,follow_symlinks=False)
 return (st.st_dev,st.st_ino,stat.S_IFMT(st.st_mode),st.st_mode,st.st_uid,st.st_size,st.st_mtime_ns,st.st_ctime_ns)
def _read_replay_artifact(dfd,name,data,entry):
 """Bounded no-follow, nonblocking descriptor read of one private saved artifact,
 opened relative to the checked run-directory descriptor and bound to its listed entry."""
 try:fd=os.open(name,_LEAF_FLAGS,dir_fd=dfd)
 except OSError as e:raise _refuse(e,"symlink_artifact")from None
 try:
  before=os.fstat(fd)
  evidence.need(stat.S_ISREG(before.st_mode) and before.st_uid==os.geteuid() and not before.st_mode&0o077 and before.st_size<=len(data),"private_artifact_bound")
  evidence.need(_ident(before)==entry[:2],"existing_run_changed")
  chunks=[];size=0
  while True:
   b=os.read(fd,min(65536,len(data)-size+1))
   if not b:break
   size+=len(b);evidence.need(size<=len(data),"private_artifact_bound");chunks.append(b)
  after=os.fstat(fd)
  evidence.need((before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns)==(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns),"artifact_changed")
 except OSError as e:raise _refuse(e,"symlink_artifact")from None
 finally:os.close(fd)
 raw=b"".join(chunks)
 evidence.need(evidence.sha(raw)==evidence.sha(data),"existing_run_conflict")
 return raw
def _open_chain(dest):
 """Open every ancestor of dest from / with no-follow directory descriptors, then dest itself."""
 chain=[]
 try:
  fd=os.open("/",_DIR_FLAGS&~os.O_NOFOLLOW);chain.append((fd,None,os.fstat(fd)))
  parts=dest.parts[1:]
  for i,part in enumerate(parts):
   last=i==len(parts)-1
   fd=os.open(part,_RUN_FLAGS if last else _DIR_FLAGS,dir_fd=chain[-1][0])
   chain.append((fd,part,os.fstat(fd)))
   evidence.need(stat.S_ISDIR(chain[-1][2].st_mode),"existing_run_symlink")
  return chain
 except OSError as e:
  _close(chain);raise _refuse(e,"existing_run_symlink")from None
 except ValueError:
  _close(chain);raise
def _close(chain):
 for fd,_,_ in chain:os.close(fd)
def _validate_existing_run(dest,artifacts):
 """Reuse a saved run only if it is exactly the private regular artifacts expected; never repair it.

 Every read goes through descriptors bound to checked no-follow ancestry; afterwards the
 whole namespace (ancestor entries, run directory, listing, artifact entries) must still
 name the same inodes, or the replay is refused. Filesystem failures become stable codes."""
 chain=_open_chain(dest)
 try:
  root_st=chain[-2][2];dfd,_,s=chain[-1]
  evidence.need(root_st.st_uid==os.geteuid() and not root_st.st_mode&0o077,"existing_run_unsafe")
  evidence.need(s.st_uid==os.geteuid() and not s.st_mode&0o077,"existing_run_unsafe")
  names=sorted(os.listdir(dfd))
  evidence.need(names==sorted(artifacts),"existing_run_artifact_set")
  entries={name:_entry(dfd,name) for name in names}
  for name,data in artifacts.items():evidence.need(_read_replay_artifact(dfd,name,data,entries[name])==data,"existing_run_conflict")
  a=os.fstat(dfd)
  evidence.need((s.st_dev,s.st_ino,s.st_mode,s.st_uid,s.st_mtime_ns)==(a.st_dev,a.st_ino,a.st_mode,a.st_uid,a.st_mtime_ns),"existing_run_changed")
  evidence.need(sorted(os.listdir(dfd))==names,"existing_run_changed")
  evidence.need(all(_entry(dfd,name)==entries[name] for name in names),"existing_run_changed")
  for (pfd,_,_),(_,part,st) in zip(chain,chain[1:]):
   now=os.stat(part,dir_fd=pfd,follow_symlinks=False)
   evidence.need(stat.S_ISDIR(now.st_mode) and _ident(now)==_ident(st),"existing_run_changed")
 except OSError as e:raise _refuse(e,"existing_run_symlink")from None
 finally:_close(chain)
def run(command,value,input_sha256,output):
 result=execute(command,value);raw=evidence.canonical(result)+b"\n";pins=code_pins()
 binding={"command":command,"input_sha256":input_sha256,"code_pins":pins,"result_sha256":evidence.sha(raw)}
 run_id=evidence.sha(binding);root=private_root(output);dest=root/run_id
 receipt={"schema":VERSION,"run_id":run_id,"bindings":binding,"summary":safe_summary(command,result),"production_database_writes":0,"canonical_promotions":0,"external_content_transmission":0}
 receipt_raw=evidence.canonical(receipt)+b"\n"
 artifacts={"result.json":raw,"receipt.json":receipt_raw}
 if command=="requests":artifacts["due-actions.json"]=evidence.canonical({"schema":"request-due-actions-v1","actions":result["due_actions"],"automatic_scheduling":False,"verification":"derived_needs_review","source_run_id":run_id})+b"\n"
 if command=="reconcile":artifacts["findings.md"]=findings_markdown(result)
 reused=os.path.lexists(dest)
 if reused:_validate_existing_run(dest,artifacts)
 else:
  stage=Path(tempfile.mkdtemp(prefix=".understanding-",dir=root))
  try:
   for name,data in artifacts.items():_write(stage/name,data)
   os.rename(stage,dest)
   fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY);os.fsync(fd);os.close(fd)
  finally:
   if stage.exists():
    for x in stage.iterdir():x.unlink()
    stage.rmdir()
 return {"run_id":run_id,"output":str(dest),"reused":reused,"summary":receipt["summary"]}
def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument("command",choices=["document","requests","policy","reconcile"]);p.add_argument("--input",required=True);p.add_argument("--input-sha256");p.add_argument("--output",required=True)
 a=p.parse_args();value,pin=read_input(a.input,a.input_sha256)
 try:print(json.dumps(run(a.command,value,pin,a.output),sort_keys=True))
 except (ValueError,KeyError,TypeError):raise SystemExit("understanding_input_or_evidence_validation_failed")from None
if __name__=="__main__":main()
