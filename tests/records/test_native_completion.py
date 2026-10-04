"""Native receipts and typed locators: synthetic, local, no received mail."""
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from campaign_tool.records.intake import eml_export,mail_delta,mail_wire,native_msg
from campaign_tool.records.runner.contracts import Preserved
from campaign_tool.records.runner.core import attachment_identity_ok,observed_runtime
from tests.records import native_msg_fixtures as native
from tests.records.test_mail_wire_intake import chain,mixed,leaf,binary


class NativeCompletionTests(unittest.TestCase):
    def test_required_ci_parser_is_not_silently_skipped(self):
        if os.environ.get("REQUIRE_NATIVE_MSG") == "1":
            self.assertTrue(native.available(), "native CI must reach extract-msg")

    def test_locator_projection_preserves_source_and_does_not_mutate_inventory(self):
        raw,_,_,_=chain()
        plan=mail_wire.prepare(raw)
        before=json.dumps(plan.units,sort_keys=True)
        projected=mail_wire.project_units(plan)
        self.assertEqual(json.dumps(plan.units,sort_keys=True),before)
        for old,new in zip(plan.units,projected):
            self.assertEqual(new["data"]["wire_provenance"]["locator"],old["locator"])
            self.assertEqual(new["locator"]["part"],old["locator"]["source_part"])
            self.assertNotIn("source_part",new["locator"])
            self.assertEqual(new["data"]["source_original_sha256"],old["data"]["source_original_sha256"])

    @unittest.skipUnless(native.available(),"optional native MSG parser unavailable")
    def test_native_projection_exact_storage_and_stream_context(self):
        raw=native.message(embedded=True,attachment=leaf())
        plan=mail_wire.prepare(raw,"msg")
        projected=mail_wire.project_units(plan)
        native_part=plan.native_items[0][0]["part"]
        embedded=next(unit for unit in projected if unit["kind"]=="msg_embedded_item")
        self.assertEqual(embedded["locator"],{"part":native_part})
        self.assertEqual(embedded["data"]["wire_provenance"]["locator"]["source_part"],"0")
        body=next(unit for unit in projected if unit["kind"]=="msg_body" and unit["locator"]["part"]==native_part)
        self.assertEqual(body["data"]["source_original_sha256"],hashlib.sha256(raw).hexdigest())
        bad=replace(plan,native_items=plan.native_items+plan.native_items)
        with self.assertRaises(mail_wire.MailWireError):mail_wire.project_units(bad)

    @unittest.skipUnless(native.available(),"optional native MSG parser unavailable")
    def test_stream_tamper_is_checked_against_exact_native_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);root.chmod(0o700)
            raw=native.message(embedded=True)
            path=eml_export.export_message(raw,mail_root=root/"mail",account="synthetic",
                                          mailbox="INBOX",uidvalidity=1,uid=1,original_format="msg")
            receipt,_,items=mail_delta.load_receipt(path)
            first=receipt["native_items"][0]["streams"][0]
            target=root/"mail"/first["path"]
            self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(),first["sha256"])
            target.write_bytes(b"synthetic changed stream")
            out=root/"out";(out/"blobs").mkdir(mode=0o700,parents=True);out.chmod(0o700)
            for item in items:mail_delta.stage_source(item,root/"mail",out)
            items[0].update(native_root=root/"mail",native_output=out)
            with self.assertRaises(mail_delta.Rejected):mail_delta.bind_parts(items,receipt)
            self.assertFalse((out/"native-items").exists())

    @unittest.skipUnless(native.available(),"optional native MSG parser unavailable")
    def test_shared_native_byte_depth_count_budgets(self):
        raw=mixed([leaf(),binary(native.message(embedded=True,attachment=chain()[1]),b"outer.msg")])
        full=mail_wire.prepare(raw)
        for limits in ({"max_captures":1},{"max_parts":1},{"max_depth":1},{"max_total_bytes":len(raw)}):
            with self.subTest(limits=limits),self.assertRaises(mail_wire.MailWireError):
                mail_wire.prepare(raw,**limits)
        self.assertGreater(full.budget["messages"],3)
        self.assertEqual(full.budget["captures"],len(full.captures)+len(full.native_items))

    def test_native_sources_are_in_runtime_fingerprint(self):
        from campaign_tool.records.intake import native_receipt
        baseline=observed_runtime("sha256:"+"a"*64)
        with tempfile.TemporaryDirectory() as temporary:
            for module in (mail_wire,native_msg,native_receipt):
                path=Path(temporary)/Path(module.__file__).name
                path.write_bytes(Path(module.__file__).read_bytes()+b"\n# synthetic fingerprint change\n")
                with patch.object(module,"__file__",str(path)):
                    self.assertNotEqual(observed_runtime("sha256:"+"a"*64)["code_sha256"],baseline["code_sha256"])

    def test_typed_native_family_cannot_launder_arbitrary_locators(self):
        value=Preserved("a"*64,"b"*64,"c"*64,("b"*64,"d"*64),(("arbitrary:secret","d"*64),),
                        (("arbitrary:secret",None),),(),(("arbitrary:secret","0","b"*64),),
                        (),"eml",mail_wire.SCHEMA)
        self.assertFalse(attachment_identity_ok(value))

    def test_limits_cannot_be_raised_or_boolean(self):
        for limits in ({"max_parts":True},{"max_depth":33},{"max_total_bytes":513*1024**2}):
            with self.assertRaises(mail_wire.MailWireError):mail_wire.prepare(leaf(),**limits)


class NativeCallerBudgetTests(unittest.TestCase):
    def test_eml_worker_intersects_ceiling_and_preserves_lower_allowance(self):
        from campaign_tool.records.intake import folder
        raw=leaf()
        real=mail_wire.prepare
        for allowance in (folder.LIMITS["expanded_bytes"],len(raw)*2):
            with self.subTest(allowance=allowance),tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary);root.chmod(0o700)
                source=root/"synthetic.eml";source.write_bytes(raw)
                dest=root/"derived";dest.mkdir(mode=0o700)
                out=root/"out";(out/"blobs").mkdir(parents=True,mode=0o700);out.chmod(0o700)
                limits={**folder.LIMITS,"expanded_bytes":allowance}
                seen=[]
                def tracked(data,form,**kwargs):
                    seen.append(kwargs["max_total_bytes"])
                    return real(data,form,**kwargs)
                with patch("resource.setrlimit"),patch.object(mail_wire,"prepare",side_effect=tracked):
                    folder.worker(source,hashlib.sha256(raw).hexdigest(),"eml",dest,out,limits,0)
                self.assertEqual(seen,[min(allowance,512*1024**2)])
                self.assertEqual(json.loads((dest/"digest.json").read_bytes())["stage"],"complete")

    @unittest.skipUnless(native.available(),"optional native MSG parser unavailable")
    def test_msg_route_intersects_ceiling_and_retains_lower_rejection(self):
        from campaign_tool.records import extraction_routes
        from campaign_tool.records.intake import folder
        raw=native.message(embedded=True)
        real=mail_wire.prepare
        for allowance in (folder.LIMITS["expanded_bytes"],1):
            with self.subTest(allowance=allowance),tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary);root.chmod(0o700)
                seen=[]
                def tracked(data,form,**kwargs):
                    seen.append(kwargs["max_total_bytes"])
                    return real(data,form,**kwargs)
                with patch.dict(folder.LIMITS,{"expanded_bytes":allowance}),patch.object(mail_wire,"prepare",side_effect=tracked):
                    if allowance==1:
                        with self.assertRaises(mail_wire.MailWireError):
                            extraction_routes._msg("synthetic",raw,root)
                    else:
                        units,_,_,children=extraction_routes._msg("synthetic",raw,root)
                        self.assertEqual(children,[])
                        self.assertTrue(any(unit["kind"]=="msg_embedded_item" for unit in units))
                self.assertEqual(seen,[min(allowance,512*1024**2)])

    @unittest.skipUnless(native.available(),"optional native MSG parser unavailable")
    def test_native_embedded_item_reaches_pipeline_catalog_without_original_inflation(self):
        from campaign_tool.records.run import Pipeline
        with tempfile.TemporaryDirectory() as temporary:
            base=Path(temporary);base.chmod(0o700)
            inbox=base/"inbox";inbox.mkdir(mode=0o700)
            raw=native.message(embedded=True)
            (inbox/"synthetic.msg").write_bytes(raw)
            report=Pipeline(base/"records").run(inbox)
            self.assertEqual(report["intake"]["failures"],[])
            self.assertEqual(report["originals"],1)
            self.assertEqual(report["end_to_end_complete"],1)
            again=Pipeline(base/"records").run(inbox)
            self.assertEqual(again["originals"],1)
            self.assertEqual(again["end_to_end_complete"],1)

class NativeAcceptanceEvidenceTests(unittest.TestCase):
    @unittest.skipUnless(native.available(),"optional native MSG parser unavailable")
    def test_native_writer_hash_and_installed_identity_are_bound(self):
        from campaign_tool.records import extraction_routes, extraction_validation
        from campaign_tool.records.intake import folder
        raw=native.message(embedded=True)
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);root.chmod(0o700)
            source=root/"synthetic.msg";source.write_bytes(raw)
            output=root/"output";output.mkdir(mode=0o700)
            result=extraction_routes.extract(source,hashlib.sha256(raw).hexdigest(),output,form="msg")
            self.assertEqual(result["status"],"complete",result["issues"])
            derived=Path(result["run_path"])/"derived"
            metadata=json.loads((derived/"digest.json").read_bytes())
            self.assertEqual(metadata["artifacts"]["units.jsonl"],
                             hashlib.sha256((derived/"units.jsonl").read_bytes()).hexdigest())
            components,runtime=extraction_validation.installed_parser_identity("msg")
            self.assertEqual(result["parser"],"legacy-intake:msg")
            self.assertEqual(result["parser_components"],components)
            self.assertEqual(components["intake"],folder.VERSION+"/"+folder.ENGINE_REVISION)
            self.assertEqual(len(runtime),64)
            self.assertEqual(set(components),{"intake","extract-msg","olefile"})
            identity=extraction_validation.code_identity()
            for key in ("mail_wire","native_msg","native_receipt","wire_rfc822","rfc822_inventory"):
                self.assertEqual(len(identity[key]),64)

class NativeP2RegressionTests(unittest.TestCase):
    def value(self, *, extended, digest="d"*64, documents=None):
        return Preserved("a"*64,"b"*64,"c"*64,
                         ("b"*64,digest) if documents is None else documents,
                         (("1.2",digest),),(("1.2",None),) if extended else (),
                         (), (("1.2","0","b"*64),) if extended else (),
                         (),"eml",mail_wire.SCHEMA if extended else None)

    def test_declared_valid_attachment_accepted_in_both_modes(self):
        for extended in (False,True):
            with self.subTest(extended=extended):
                value=self.value(extended=extended)
                self.assertTrue(attachment_identity_ok(value))
                if extended:self.assertTrue(mail_wire.identity_ok(value))

    def test_invalid_attachment_hash_rejected_even_when_declared_in_both_modes(self):
        for extended in (False,True):
            for digest in ("not-a-hash","D"*64,"d"*63):
                with self.subTest(extended=extended,digest=digest):
                    value=self.value(extended=extended,digest=digest)
                    self.assertFalse(attachment_identity_ok(value))
                    if extended:self.assertFalse(mail_wire.identity_ok(value))

    def test_unlisted_valid_attachment_hash_rejected_in_both_modes(self):
        for extended in (False,True):
            with self.subTest(extended=extended):
                value=self.value(extended=extended,documents=("b"*64,))
                self.assertFalse(attachment_identity_ok(value))
                if extended:self.assertFalse(mail_wire.identity_ok(value))

    def edges(self,raw):
        import sqlite3
        from campaign_tool.records.intake import folder
        outer=hashlib.sha256(raw).hexdigest()
        with tempfile.TemporaryDirectory() as temporary:
            base=Path(temporary);base.chmod(0o700)
            inbox=base/"inbox";inbox.mkdir(mode=0o700)
            (inbox/"synthetic.eml").write_bytes(raw)
            out=base/"out";out.mkdir(mode=0o700)
            config=folder.load_config(roots=[str(inbox)],output=str(out))
            with sqlite3.connect(":memory:") as db:
                db.row_factory=sqlite3.Row;db.executescript(folder.SCHEMA)
                folder.inventory(db,out,[str(inbox)],config)
                folder.extract(db,out,dict(folder.LIMITS),selection=outer,config=config)
                doc=db.execute("SELECT stage,digest FROM docs WHERE sha=?",(outer,)).fetchone()
                self.assertEqual(doc["stage"],"complete",doc["digest"])
                children=json.loads(doc["digest"])["children"]
                self.assertTrue(any(child.get("parent_sha",outer)!=outer for child in children))
                for child in children:
                    parent=child.get("parent_sha",outer);loc=folder.js(child["locator"])
                    self.assertIsNotNone(db.execute(
                        "SELECT 1 FROM edges WHERE parent=? AND locator=? AND child=?",
                        (parent,loc,child["sha"])).fetchone())
                    self.assertIsNotNone(db.execute(
                        "SELECT 1 FROM occurrences WHERE parent=? AND locator=? AND sha=?",
                        (parent,loc,child["sha"])).fetchone())
                    if parent!=outer:
                        self.assertIsNone(db.execute(
                            "SELECT 1 FROM edges WHERE parent=? AND locator=? AND child=?",
                            (outer,loc,child["sha"])).fetchone())
                before=[tuple(row) for row in db.execute("SELECT * FROM edges ORDER BY parent,locator")]
                folder.extract(db,out,dict(folder.LIMITS),retry=True,selection=outer,config=config)
                self.assertEqual(before,[tuple(row) for row in db.execute(
                    "SELECT * FROM edges ORDER BY parent,locator")])

    def test_actual_nested_eml_edges_match_immediate_occurrences_and_replay(self):
        self.edges(chain()[0])

    @unittest.skipUnless(native.available(),"optional native MSG parser unavailable")
    def test_actual_nested_msg_eml_edges_match_immediate_occurrences_and_replay(self):
        msg=native.message(attachment=leaf())
        self.edges(mixed([leaf(),binary(msg,b"outer.msg")]))
