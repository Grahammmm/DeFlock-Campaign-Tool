"""Synthetic-only portal queue tests: no sockets, live URLs or host policy reads."""
import contextlib
import hashlib
import io
import json
import os
import sqlite3
import stat
import tempfile
import unittest
import zipfile
from datetime import datetime,timezone,timedelta
from pathlib import Path
from unittest.mock import patch
from campaign_tool.records.portal import Queue,Response,Limits,fetch_queue
from campaign_tool.records.portal.policy import PortalError,parse_approval,load_approval
from campaign_tool.records.portal.notice import inventory_notice
from campaign_tool.records.portal.__main__ import main

NOW=datetime(2030,1,1,tzinfo=timezone.utc)
HOST="portal.example.test"
OBJECT="objects.example.test"
URL="https://portal.example.test/documents/42/download?token=synthetic-only"
PDF=b"%PDF-1.4 synthetic fixture\n%%EOF"
SOURCE=hashlib.sha256(b"fictional notice").hexdigest()


def policy_data():
    return {"version":1,"enabled":True,"purpose":"portal-document-retrieval",
        "expires_utc":(NOW+timedelta(days=1)).isoformat(),"portal_hosts":[HOST],
        "redirect_hosts":[OBJECT],"path_prefixes":{HOST:["/documents/"],OBJECT:["/production/"]}}


def approval():
    return parse_approval(json.dumps(policy_data()),uid=0,mode=stat.S_IFREG|0o644,now=NOW)


class FakeTransport:
    def __init__(self,*responses):
        self.responses=list(responses)
        self.calls=[]
        self.closed=0

    def open(self,url,*,timeout):
        self.calls.append((url,timeout))
        response=self.responses.pop(0)
        if isinstance(response,BaseException):
            raise response
        response.close=lambda:setattr(self,"closed",self.closed+1)
        return response


def response(raw=PDF,content_type="application/pdf",status=200,headers=None):
    return Response(status,{"Content-Type":content_type,**(headers or {})},[raw])


class MemoryLedger:
    def __init__(self):
        self.rows={}
        self.calls=0
        self.crash=False

    def record_original(self,receipt,path):
        self.calls+=1
        assert hashlib.sha256(path.read_bytes()).hexdigest()==receipt["sha256"]
        self.rows[receipt["id"]]=receipt
        if self.crash:
            self.crash=False
            raise RuntimeError("crash after idempotent ledger write")


class PortalTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)/"queue"
        self.queue=Queue(self.root)
        self.key=self.queue.inventory(HOST,"26-001","42",URL,SOURCE)

    def tearDown(self):
        self.queue.close()
        self.temp.cleanup()

    def run_fetch(self,transport=None,**kwargs):
        return fetch_queue(self.queue,apply=True,approval_loader=approval,
            transport=transport or FakeTransport(response()),egress=lambda url:True,
            wall=lambda:NOW.timestamp(),**kwargs)

    def item(self,key=None):
        return dict(self.queue.db.execute("SELECT * FROM portal_items WHERE id=?",(key or self.key,)).fetchone())

    def reason(self):
        return self.item()["reason"]

    def test_success_hash_original_and_outbox(self):
        ledger=MemoryLedger()
        result=self.run_fetch(ledger=ledger)
        self.assertEqual(result["received"],1)
        self.assertEqual(result["ledger_delivered"],1)
        self.assertEqual(len(ledger.rows),1)
        sha=hashlib.sha256(PDF).hexdigest()
        self.assertEqual(self.item()["current_sha256"],sha)
        self.assertEqual((self.queue.objects/sha).read_bytes(),PDF)
        self.assertEqual(stat.S_IMODE((self.queue.objects/sha).stat().st_mode),0o400)

    def test_dry_run_never_opens_transport(self):
        t=FakeTransport()
        self.assertEqual(fetch_queue(self.queue,transport=t)["mode"],"inventory_only")
        self.assertEqual(t.calls,[])

    def test_missing_approval_refuses(self):
        with self.assertRaisesRegex(PortalError,"approval_required"):
            fetch_queue(self.queue,apply=True)

    def test_separate_egress_required(self):
        with self.assertRaisesRegex(PortalError,"egress_unconfigured"):
            fetch_queue(self.queue,apply=True,approval_loader=approval,transport=FakeTransport())

    def test_egress_denial_no_transport_call(self):
        t=FakeTransport()
        fetch_queue(self.queue,apply=True,approval_loader=approval,egress=lambda url:False,transport=t,wall=lambda:NOW.timestamp())
        self.assertEqual(t.calls,[])
        self.assertEqual(self.reason(),"egress_denied")

    def test_expired_approval_before_network(self):
        data=policy_data();data["expires_utc"]=NOW.isoformat()
        with self.assertRaisesRegex(PortalError,"approval_expired"):
            parse_approval(json.dumps(data),uid=0,mode=stat.S_IFREG|0o644,now=NOW)

    def test_bad_ownership_and_modes(self):
        for uid,mode in [(1000,stat.S_IFREG|0o644),(0,stat.S_IFREG|0o666),(0,stat.S_IFDIR|0o644)]:
            with self.subTest(uid=uid,mode=mode),self.assertRaises(PortalError):
                parse_approval(json.dumps(policy_data()),uid=uid,mode=mode,now=NOW)

    def test_policy_file_absent_refuses(self):
        with self.assertRaises(PortalError):
            load_approval(Path(self.temp.name)/"absent",NOW)

    def test_policy_symlink_refuses(self):
        target=Path(self.temp.name)/"real";target.write_text(json.dumps(policy_data()))
        link=Path(self.temp.name)/"link";link.symlink_to(target)
        with self.assertRaises(PortalError):
            load_approval(link,NOW)

    def test_unapproved_origin(self):
        data=policy_data();data["portal_hosts"]=["different.example.test"]
        data["path_prefixes"]={"different.example.test":["/documents/"],OBJECT:["/production/"]}
        auth=parse_approval(json.dumps(data),uid=0,mode=stat.S_IFREG|0o644,now=NOW)
        t=FakeTransport()
        fetch_queue(self.queue,apply=True,approval_loader=lambda:auth,egress=lambda url:True,transport=t,wall=lambda:NOW.timestamp())
        self.assertEqual(t.calls,[])
        self.assertEqual(self.reason(),"portal_not_approved")

    def test_expired_link(self):
        self.run_fetch(FakeTransport(response(status=403)))
        self.assertEqual(self.reason(),"expired_or_denied_link")
        self.assertEqual(self.queue.status()["versions"],0)

    def test_login_content_type(self):
        self.run_fetch(FakeTransport(response(b"login","text/html")))
        self.assertEqual(self.reason(),"login_page")

    def test_sniffed_login_split_and_utf16(self):
        for chunks in ([b"  <ht",b"ml><form>"],["<html><form>".encode("utf-16")],[b" ",b"<input type=password>"]):
            with self.subTest(chunks=chunks):
                self.queue.db.execute("UPDATE portal_items SET state='pending',tries=0");self.queue.db.commit()
                self.run_fetch(FakeTransport(Response(200,{"Content-Type":"application/octet-stream"},chunks)))
                self.assertEqual(self.reason(),"login_page")
                self.assertEqual(self.queue.status()["versions"],0)

    def test_pdf_with_form_markup_is_not_login_page(self):
        body=b"%PDF-1.7\n1 0 obj <</XFA (<form><script>synthetic</script></form>)>>\n%%EOF"
        for chunks in ([body],[b"%P",b"DF-1.7 <fo",b"rm><script>x</script>\n%%EOF"]):
            with self.subTest(chunks=chunks):
                self.queue.db.execute("UPDATE portal_items SET state='pending',tries=0");self.queue.db.commit()
                result=self.run_fetch(FakeTransport(Response(200,{"Content-Type":"application/pdf"},chunks)))
                self.assertEqual(result["received"],1)
                self.assertNotEqual(self.item()["reason"],"login_page")
                raw=b"".join(chunks)
                self.assertEqual(self.item()["current_sha256"],hashlib.sha256(raw).hexdigest())

    def test_non_pdf_prefix_still_sniffed(self):
        self.run_fetch(FakeTransport(Response(200,{"Content-Type":"application/octet-stream"},[b"%PD",b"F <form>"])))
        self.assertEqual(self.reason(),"login_page")

    def test_permitted_redirect_has_evidence_and_closes(self):
        t=FakeTransport(Response(302,{"Location":"https://objects.example.test/production/a?sig=fictional"}),response())
        self.run_fetch(t)
        self.assertEqual(t.closed,2)
        evidence=json.loads(self.queue.db.execute("SELECT evidence_json FROM portal_attempts").fetchone()[0])
        self.assertEqual([x["http_status"] for x in evidence],[302,200])
        self.assertEqual([x["host"] for x in evidence],[HOST,OBJECT])
        self.assertNotIn("sig",json.dumps(evidence))

    def test_redirect_denied_before_request(self):
        t=FakeTransport(Response(302,{"Location":"https://denied.example.test/production/a?sig=fictional"}))
        self.run_fetch(t)
        self.assertEqual(len(t.calls),1)
        self.assertEqual(self.reason(),"redirect_not_approved")

    def test_redirect_egress_independent(self):
        t=FakeTransport(Response(302,{"Location":"https://objects.example.test/production/a"}))
        fetch_queue(self.queue,apply=True,approval_loader=approval,transport=t,egress=lambda url:OBJECT not in url,wall=lambda:NOW.timestamp())
        self.assertEqual(len(t.calls),1)
        self.assertEqual(self.reason(),"egress_denied")

    def test_redirect_limit(self):
        t=FakeTransport(*[Response(302,{"Location":URL}) for _ in range(4)])
        self.run_fetch(t)
        self.assertEqual(self.reason(),"redirect_limit")
        self.assertEqual(len(t.calls),4)

    def test_bad_url_and_encoded_traversal(self):
        for url in ("http://portal.example.test/documents/42","https://u:p@portal.example.test/documents/42","https://portal.example.test/documents/%2e%2e/admin","https://portal.example.test/documents/42#x"):
            with self.subTest(url=url),self.assertRaises(PortalError):
                self.queue.inventory(HOST,"26-001","42",url,SOURCE)

    def test_duplicate_notice(self):
        self.assertEqual(self.queue.inventory(HOST,"26-001","42",URL,SOURCE),self.key)
        self.run_fetch()
        self.run_fetch(FakeTransport())
        self.assertEqual(self.queue.status()["versions"],1)
        self.assertEqual(self.queue.status()["attempts"],1)

    def test_refreshed_link_same_bytes(self):
        self.run_fetch()
        self.queue.inventory(HOST,"26-001","42",URL+"-refreshed",SOURCE,refresh=True,expected_generation=1)
        self.run_fetch()
        self.assertEqual(len(self.queue.status()["items"]),1)
        self.assertEqual(self.queue.status()["versions"],1)
        self.assertEqual(self.item()["generation"],2)

    def test_changed_bytes_version_link(self):
        self.run_fetch()
        self.queue.inventory(HOST,"26-001","42",URL+"-refreshed",SOURCE,refresh=True,expected_generation=1)
        self.run_fetch(FakeTransport(response(PDF+b" changed")))
        rows=self.queue.db.execute("SELECT * FROM portal_versions ORDER BY rowid").fetchall()
        self.assertEqual(len(rows),2)
        self.assertEqual(rows[1]["previous_sha256"],rows[0]["sha256"])

    def test_changed_request_distinct_identity(self):
        key=self.queue.inventory(HOST,"26-002","42",URL,SOURCE)
        self.assertNotEqual(key,self.key)

    def test_one_blocked_item_does_not_halt_queue(self):
        self.queue.inventory(HOST,"26-001","43",URL.replace("42","43"),SOURCE)
        result=self.run_fetch(FakeTransport(response(status=403),response()))
        self.assertEqual(result["blocked"],1)
        self.assertEqual(result["received"],1)

    def test_timeout_backoff_and_retry_limit(self):
        limits=Limits(backoff=0,max_attempts=2)
        self.run_fetch(FakeTransport(TimeoutError("sensitive value")),limits=limits)
        self.assertEqual(self.item()["state"],"retry")
        self.run_fetch(FakeTransport(TimeoutError("sensitive value")),limits=limits)
        self.assertEqual(self.item()["state"],"blocked")
        self.assertEqual(self.item()["tries"],2)

    def test_deadline_during_stream(self):
        ticks=iter([0,0,26])
        self.run_fetch(mono=lambda:next(ticks))
        self.assertEqual(self.reason(),"timeout")
        self.assertEqual(self.queue.status()["versions"],0)

    def test_size_limit_stream_and_header(self):
        for headers in ({},{"Content-Length":"999999"}):
            with self.subTest(headers=headers):
                self.queue.db.execute("UPDATE portal_items SET state='pending',tries=0");self.queue.db.commit()
                self.run_fetch(FakeTransport(response(headers=headers)),limits=Limits(max_bytes=5))
                self.assertEqual(self.reason(),"size_limit")
                self.assertEqual(self.queue.status()["versions"],0)

    def test_length_mismatch(self):
        self.run_fetch(FakeTransport(response(headers={"Content-Length":"1"})))
        self.assertEqual(self.reason(),"length_mismatch")

    def test_empty_document(self):
        self.run_fetch(FakeTransport(response(b"")))
        self.assertEqual(self.reason(),"empty_document")

    def test_signature_mismatch(self):
        self.run_fetch(FakeTransport(response(b"not a PDF")))
        self.assertEqual(self.reason(),"signature_mismatch")

    def test_zip_bomb_bounded_handoff(self):
        buf=io.BytesIO()
        with zipfile.ZipFile(buf,"w",compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("member.txt",b"0"*10000)
        self.run_fetch(FakeTransport(response(buf.getvalue(),"application/zip")),limits=Limits(zip_ratio=10))
        self.assertEqual(self.reason(),"archive_limits")
        self.assertEqual(self.queue.status()["versions"],0)

    def test_zip_path_traversal(self):
        buf=io.BytesIO()
        with zipfile.ZipFile(buf,"w") as archive:
            archive.writestr("../escape",b"x")
        self.run_fetch(FakeTransport(response(buf.getvalue(),"application/zip")))
        self.assertEqual(self.reason(),"archive_unsafe_path")

    def test_small_zip_preserved_without_extraction(self):
        buf=io.BytesIO()
        with zipfile.ZipFile(buf,"w") as archive:
            archive.writestr("member.txt",b"synthetic")
        self.run_fetch(FakeTransport(response(buf.getvalue(),"application/zip")))
        self.assertEqual(self.item()["state"],"received")
        self.assertFalse((self.root/"member.txt").exists())

    def test_crash_after_bytes_before_receipt_recovers(self):
        def crash(stage):
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.run_fetch(fault=crash)
        self.assertEqual(self.queue.status()["versions"],0)
        self.assertEqual(self.item()["state"],"fetching")
        self.run_fetch()
        self.assertEqual(self.queue.status()["versions"],1)
        self.assertEqual(self.item()["state"],"received")
        self.assertEqual(self.queue.db.execute("SELECT result FROM portal_attempts ORDER BY id").fetchone()[0],"interrupted")

    def test_existing_corrupt_object_no_false_receipt(self):
        digest=hashlib.sha256(PDF).hexdigest()
        (self.queue.objects/digest).write_bytes(b"wrong")
        self.run_fetch()
        self.assertEqual(self.reason(),"object_integrity_failed")
        self.assertEqual(self.queue.status()["versions"],0)

    def test_symlink_object_no_false_receipt(self):
        digest=hashlib.sha256(PDF).hexdigest()
        elsewhere=Path(self.temp.name)/"elsewhere";elsewhere.write_bytes(PDF)
        (self.queue.objects/digest).symlink_to(elsewhere)
        self.run_fetch()
        self.assertEqual(self.queue.status()["versions"],0)
        self.assertNotEqual(self.item()["state"],"received")

    def test_outbox_crash_after_ledger_write_is_idempotent(self):
        ledger=MemoryLedger();ledger.crash=True
        result=self.run_fetch(ledger=ledger)
        self.assertEqual(result["ledger_error"],"ledger_delivery_pending")
        self.assertEqual(self.queue.status()["ledger_pending"],1)
        self.run_fetch(FakeTransport(),ledger=ledger)
        self.assertEqual(len(ledger.rows),1)
        self.assertEqual(ledger.calls,2)
        self.assertEqual(self.queue.status()["ledger_pending"],0)

    def test_report_does_not_contain_signed_urls_or_errors(self):
        self.run_fetch(FakeTransport(RuntimeError(URL)))
        encoded=json.dumps(self.queue.status())
        evidence=self.queue.db.execute("SELECT evidence_json FROM portal_attempts").fetchone()[0]
        for text in (encoded,evidence):
            self.assertNotIn("token=",text)
            self.assertNotIn("synthetic-only",text)
            self.assertNotIn("https://",text)

    def test_inventory_notice_html(self):
        outcome=inventory_notice(self.queue,body='<a href="'+URL+'">file</a>',request_id="26-001",source_sha256=SOURCE,known_hosts={HOST})
        self.assertEqual(outcome,{"items":1})
        self.assertEqual(len(self.queue.status()["items"]),1)

    def test_cli_status_and_inventory_only(self):
        for args in (["status"],["fetch"]):
            with self.subTest(args=args),contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(main(["--root",str(self.root),*args]),0)
            self.assertNotIn("token=",out.getvalue())

    def test_cli_apply_refuses_without_approval(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(["--root",str(self.root),"fetch","--apply"]),2)
        self.assertNotIn("token=",out.getvalue())

    def test_interrupted_final_attempt_becomes_blocked(self):
        def crash(stage):
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.run_fetch(fault=crash,limits=Limits(max_attempts=1))
        result=self.run_fetch(FakeTransport(),limits=Limits(max_attempts=1))
        self.assertEqual(result["attempted"],0)
        self.assertEqual(self.item()["state"],"blocked")
        self.assertEqual(self.reason(),"retry_exhausted")
        self.assertEqual(self.queue.status()["versions"],0)

    def test_provenance_is_frozen_across_ledger_retry(self):
        ledger=MemoryLedger();ledger.crash=True
        self.run_fetch(ledger=ledger)
        first=dict(next(iter(ledger.rows.values())))
        self.queue.inventory(HOST,"26-001","42",URL,hashlib.sha256(b"second fictional notice").hexdigest())
        self.run_fetch(FakeTransport(),ledger=ledger)
        self.assertEqual(next(iter(ledger.rows.values())),first)
        self.assertEqual(first["provenance"]["source_sha256s"],[SOURCE])
        self.assertEqual(first["provenance"]["request_id"],"26-001")
        self.assertNotIn("token=",json.dumps(first))

    def test_notice_entity_decoding_keeps_single_url_generation(self):
        url=URL+"&part=1"
        inventory_notice(self.queue,body='<a href="'+url.replace("&","&amp;")+'">file</a>',request_id="26-001",source_sha256=hashlib.sha256(b"entity-decoded notice fixture").hexdigest(),known_hosts={HOST})
        self.assertEqual(self.item()["last_url_private"],url)
        self.assertEqual(self.item()["generation"],2)

    def test_resource_limit_validation(self):
        for kwargs in ({"max_items":0},{"max_bytes":True},{"timeout":121},{"zip_ratio":1001}):
            with self.subTest(kwargs=kwargs),self.assertRaises(ValueError):
                Limits(**kwargs)


if __name__=="__main__":
    unittest.main()
