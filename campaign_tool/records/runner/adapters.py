"""Offline exporter and explicit legacy intake bridge. No live mail connection."""
import hashlib
import json
from pathlib import Path
import sqlite3
from .contracts import Folder,Preserved,IntegrationGap
from campaign_tool.records.intake import mail_delta,folder as intake_folder

class LegacyIntakeBackend:
    """Real byte-preservation bridge to legacy intake; canonical promotion unavailable."""
    def __init__(self,mail_root,intake_output):
        self.mail_root=Path(mail_root);self.output=Path(intake_output)
    def preserve(self,receipt_path,account,scope,uid):
        receipt,identity,items=mail_delta.load_receipt(receipt_path)
        if identity!=(account,scope.name,str(scope.uidvalidity),str(uid)):
            raise mail_delta.Rejected('export_scope_mismatch')
        # Verifies exact EML/attachment bytes and MIME locators, preserving existing catalog.
        mail_delta.import_receipt(receipt_path,self.mail_root,self.output)
        mid=hashlib.sha256(json.dumps(['mail',account,scope.name,scope.uidvalidity,uid],separators=(',',':')).encode()).hexdigest()
        attached=[];parents=[];eml_parts=[]
        with sqlite3.connect(self.output/'intake.sqlite') as con:
            for item in items[1:]:
                oid=intake_folder.hid(intake_folder.js(['mail-receipt-v1',*identity,item['part']]))
                row=con.execute('SELECT sha,locator FROM occurrences WHERE oid=?',(oid,)).fetchone()
                if not row or row[0]!=item['sha']:raise mail_delta.Rejected('attachment_occurrence_missing')
                loc=item['part'] if receipt.get('schema') else json.loads(row[1])['mime']
                attached.append((loc,row[0]))
                if receipt.get('schema'):
                    parent=item['source']['parent_part']
                    parents.append((loc,None if parent=='0' else parent))
                    if item['kind']=='eml':eml_parts.append(loc)
        # Bind receipt bytes to the verified parsed receipt; reject changes during bridge work.
        verified,verified_identity,_=mail_delta.load_receipt(receipt_path)
        if verified!=receipt or verified_identity!=identity:raise mail_delta.Rejected('receipt_changed')
        with intake_folder.secure_open(receipt_path) as f:raw=f.read(mail_delta.MAX_RECEIPT+1)
        if len(raw)>mail_delta.MAX_RECEIPT or json.loads(raw)!=receipt:raise mail_delta.Rejected('receipt_changed')
        return Preserved(mid,items[0]['sha'],hashlib.sha256(raw).hexdigest(),
                         tuple(sorted({i['sha'] for i in items})),tuple(attached),tuple(parents),tuple(eml_parts))
    def validate_and_promote(self,receipt,run_identity):
        raise IntegrationGap('wp1_promotion_adapter_unavailable')

class OfflineExporter:
    """Explicit frozen export manifest; does not invoke a provider or exporter script."""
    def __init__(self,root,manifest):
        self.root=Path(root)
        path=mail_delta.source_path(self.root,manifest,self.root/'__unused_output__')
        with intake_folder.secure_open(path) as f:raw=f.read(1024*1024+1)
        if len(raw)>1024*1024:raise ValueError('export_manifest_bound')
        obj=json.loads(raw)
        if obj.get('version')!=1 or obj.get('complete') is not True or not isinstance(obj.get('folders'),list) or len(obj['folders'])>100:raise ValueError('export_manifest_invalid')
        self.data={}
        for item in obj['folders']:
            name=item['name'];valid=item['uidvalidity'];messages=item['messages']
            if not isinstance(name,str) or not name or len(name)>256 or type(valid) is not int or not 0<valid<2**63 or not isinstance(messages,list) or len(messages)>10000 or name in self.data:raise ValueError('export_folder_invalid')
            observed={}
            for m in messages:
                uid=m['uid']
                if type(uid) is not int or not 0<uid<2**63 or uid in observed:raise ValueError('export_uid_invalid')
                observed[uid]=str(mail_delta.source_path(self.root,m['receipt'],self.root/'__unused_output__'))
            self.data[name]=(valid,observed)
    def folders(self):return [Folder(name,data[0]) for name,data in self.data.items()]
    def uids(self,scope,after_uid):return [u for u in sorted(self.data[scope.name][1]) if u>after_uid]
    def receipt(self,scope,uid):return self.data[scope.name][1][uid]
