"""Synthetic agency-draft-export edge paths. No network, send, apply or live record."""
import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
from unittest import mock

from campaign_tool.records import agency_draft_export as ade, draft_import as di
from campaign_tool.records.agency_run import lint_all, lint_asks, lint_receipt
from campaign_tool.records.outcomes import received_items
from tests.records.test_draft_import import tree
from tests.records.test_mail_drafts import Base, DOC

MD = ("# NEXT-RESPONSE\n\n## sheriff-letter\n\nChannel: email\n\n> Subject: Records follow-up\n>\n"
      "> Dear Records Custodian,\n>\n> 1. Please supply the records. Café.\n> 2. [EDIT ME: date]\n\nafter\n\n"
      "## second-letter\n\n> Subject: Second subject\n>\n> Second body.\n")


def entry(key="letter-1", section="sheriff-letter", subject="Records follow-up", placeholders=("[EDIT ME: date]",)):
    return {"draft_key": key, "agency_id": "agency-1", "request_id": "req-1", "source_run_id": "run-1",
            "source_section": section, "why": "Operator checked custodian", "citations": [],
            "placeholders": list(placeholders),
            "envelope": {"to": "custodian@agency.example", "subject": subject, "thread_item_id": None,
                         "recipient_basis": {"source_type": "custodian", "source_id": "contact-1",
                                             "locator": "records desk", "checked": True}}}


def mapping(*drafts):
    return {"schema": ade.MAPPING_SCHEMA,
            "identity": {"agencies": {"agency-1": ["req-1"], "agency-2": ["req-2"]}, "runs": ["run-1"]},
            "drafts": list(drafts)}


class ExportTests(Base):
    def setUp(self):
        super().setUp()
        self.md = self.root / "next.md"
        self.map = self.root / "map.json"
        self.out = self.root / "out"
        self.md.write_bytes(MD.encode())
        self.calls = []
        real = di.import_draft
        patcher = mock.patch.object(di, "import_draft", side_effect=lambda *a, **k: (
            self.calls.append(k), real(*a, **k))[1])
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_export(self, m, out=None, md=None, raw_map=None):
        self.map.write_bytes(raw_map if raw_map is not None else json.dumps(m).encode())
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = ade.main(["--md", str(md or self.md), "--mapping", str(self.map), "--project", str(self.project),
                             "--out", str(out or self.out)])
        return code, json.loads(buf.getvalue())

    def codes(self, report):
        return [r["code"] for r in report["refused"]]

    def refuse(self, m, code, **kw):
        status, report = self.run_export(m, **kw)
        self.assertEqual((status, self.codes(report)), (1, [code]))
        self.assertEqual(list(self.out.glob("*")) if self.out.exists() else [], [])

    def test_sheriff_shaped_md_passes_real_dry_run(self):
        before = tree(self.project)
        code, report = self.run_export(mapping(entry()))
        self.assertEqual((code, report["ok"]), (0, True))
        path = Path(report["files"][0])
        doc = json.loads(path.read_text("utf-8"))
        self.assertEqual(doc["body"], "Dear Records Custodian,\n\n1. Please supply the records. Café.\n2. [EDIT ME: date]\n")
        self.assertEqual(doc["citations"], [])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(di.main(["--project", str(self.project), "--file", str(path)]), 0)
        result = json.loads(out.getvalue())
        self.assertEqual((result["action"], result["applied"]), ("would_queue", False))
        self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")
        self.assertEqual(tree(self.project), before)

    def test_two_mapped_drafts_give_two_files(self):
        code, report = self.run_export(mapping(entry(), entry("letter-2", "second-letter", "Second subject", ())))
        self.assertEqual((code, len(report["files"])), (0, 2))
        docs = [json.loads(Path(f).read_text("utf-8")) for f in report["files"]]
        self.assertEqual({d["draft_key"] for d in docs}, {"letter-1", "letter-2"})
        self.assertEqual(sorted(d["body"] for d in docs)[1][:7], "Second ")

    def test_each_required_provenance_field_missing_refuses(self):
        for name in ade.REQUIRED:
            e = entry()
            del e[name]
            self.refuse(mapping(e), "field_missing:" + name, out=self.root / ("o-" + name))
            self.out = self.root / ("o-" + name)

    def test_unchecked_basis_refuses(self):
        for value in (False, "true", None):
            e = entry()
            e["envelope"]["recipient_basis"]["checked"] = value
            self.refuse(mapping(e), "basis_unchecked", out=self.root / ("b%s" % value))
            self.out = self.root / ("b%s" % value)
        e = entry()
        del e["envelope"]["recipient_basis"]["checked"]
        self.out = self.root / "babsent"
        self.refuse(mapping(e), "basis_unchecked", out=self.out)
        e = entry()
        e["envelope"]["recipient_basis"]["source_type"] = "source-header"
        self.out = self.root / "bhdr"
        self.refuse(mapping(e), "thread_basis_invalid", out=self.out)

    def test_unknown_agency_request_run_section_refuse(self):
        cases = (("agency_id", "agency-zzz", "agency_unknown"), ("request_id", "req-zzz", "request_unknown"),
                 ("request_id", "req-2", "request_agency_mismatch"), ("source_run_id", "run-zzz", "run_unknown"),
                 ("source_section", "Nope", "section_unknown"))
        for i, (field, value, code) in enumerate(cases):
            e = entry()
            e[field] = value
            self.out = self.root / ("u%d" % i)
            self.refuse(mapping(e), code, out=self.out)

    def test_unresolved_thread_and_recipient_mismatch_refuse(self):
        item = received_items.item_id(DOC)
        self.build_ledger()
        for i, (thread, to, code) in enumerate((("a" * 64, "records-desk@agency.example",
                                                 "import_refused:thread_provenance_unresolved"),
                                                (item, "other@agency.example", "import_refused:thread_provenance_contradiction"))):
            e = entry(subject="Re: Your request 4471")
            e["envelope"].update(to=to, thread_item_id=thread)
            e["envelope"]["recipient_basis"] = {"source_type": "source-header", "source_id": thread,
                                                "locator": "Reply-To", "checked": True}
            m = mapping(e)
            self.md.write_bytes(MD.replace("Records follow-up", "Re: Your request 4471").encode())
            self.out = self.root / ("t%d" % i)
            status, report = self.run_export(m, out=self.out)
            self.assertEqual(status, 1)
            self.assertEqual(len(report["refused"]), 1)
            if code:  # observed
                self.assertEqual(self.codes(report), [code])
            else:
                self.assertTrue(self.codes(report)[0].startswith("import_refused:"), report)
            self.assertEqual(list(self.out.glob("*")), [])

    def test_unlisted_and_unfound_placeholders_refuse(self):
        self.refuse(mapping(entry(placeholders=())), "placeholder_unlisted")
        self.out = self.root / "o2"
        self.refuse(mapping(entry(placeholders=("[EDIT ME: date]", "[EDIT ME: gone]"))), "placeholder_not_found",
                    out=self.out)

    def test_subject_mismatch_and_missing_subject(self):
        self.refuse(mapping(entry(subject="Different")), "subject_mismatch")
        self.out = self.root / "o2"
        self.md.write_bytes(MD.replace("> Subject: Records follow-up", "> Dear").encode())
        self.refuse(mapping(entry()), "subject_missing", out=self.out)

    def test_changing_raw_md_bytes_changes_digest(self):
        _, first = self.run_export(mapping(entry()))
        want = hashlib.sha256(self.md.read_bytes()).hexdigest()
        self.assertEqual(first["source_attachment_sha256"], want)
        self.assertEqual(json.loads(Path(first["files"][0]).read_text())["source_attachment_sha256"], want)
        self.md.write_bytes(self.md.read_bytes() + b" ")
        _, second = self.run_export(mapping(entry()), out=self.root / "out2")
        self.assertNotEqual(second["source_attachment_sha256"], want)
        self.assertEqual(second["source_attachment_sha256"], hashlib.sha256(self.md.read_bytes()).hexdigest())
        self.assertIn("not an agency attachment", second["source_digest_note"])

    def test_project_unchanged_never_applied_and_out_guards(self):
        before = tree(self.project)
        self.run_export(mapping(entry()))
        self.run_export(mapping(entry(subject="x")), out=self.root / "out2")
        self.assertEqual(tree(self.project), before)
        self.assertTrue(self.calls and all(c == {"apply": False} for c in self.calls))
        self.refuse_global(mapping(entry()), "out_inside_project", self.project / "sub")
        self.refuse_global(mapping(entry()), "out_not_empty", self.out)

    def refuse_global(self, m, code, out):
        status, report = self.run_export(m, out=out)
        self.assertEqual((status, self.codes(report)), (1, [code]))

    def test_symlink_oversize_and_duplicate_key_inputs_refuse(self):
        link = self.root / "link.md"
        link.symlink_to(self.md)
        status, report = self.run_export(mapping(entry()), md=link, out=self.root / "o1")
        self.assertEqual(self.codes(report), ["md_invalid"])
        big = self.root / "big.md"
        big.write_bytes(b"x" * (256 * 1024 + 1))
        self.assertEqual(self.codes(self.run_export(mapping(entry()), md=big, out=self.root / "o2")[1]), ["md_invalid"])
        dup = b'{"schema": "' + ade.MAPPING_SCHEMA.encode() + b'", "schema": "x"}'
        self.assertEqual(self.codes(self.run_export(None, raw_map=dup, out=self.root / "o3")[1]), ["mapping_invalid"])
        self.assertEqual(self.codes(self.run_export(None, raw_map=b"{}", out=self.root / "o4")[1]), ["mapping_invalid"])
        self.assertEqual(self.codes(self.run_export(mapping(entry(), entry()), out=self.root / "o5")[1]),
                         ["draft_key_duplicate"] * 2)


class LintReceiptExportTests(Base):
    """A receipt from the real agency-lint is verified against the exact body, rules and current inputs at export."""
    ASK = "1. Please send existing records."
    BODY = (ASK + "\n").encode()
    MD = MD.replace("> Second body.", "> " + ASK)

    def setUp(self):
        super().setUp()
        self.md, self.map, self.out = self.root / "next.md", self.root / "map.json", self.root / "out"
        self.md.write_bytes(self.MD.encode())
        self.inputs()

    def run_export(self, m, out=None):
        self.map.write_bytes(json.dumps(m).encode())
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = ade.main(["--md", str(self.md), "--mapping", str(self.map), "--project", str(self.project),
                             "--out", str(out or self.out)])
        return code, json.loads(buf.getvalue())

    def inputs(self):
        agency = self.root / "agency"
        (agency / "pages").mkdir(parents=True)
        (agency / "pages" / "D1_p001.txt").write_text("Unrelated page text.\n")
        for name, header in (("inventory.tsv", "doc_id\ttitle\trequest_ref"), ("asks.tsv", "request_ref\ttext\trating"),
                             ("events.tsv", "request_ref\tevent_type")):
            (agency / name).write_text(header + "\n")
        (self.root / "orig").mkdir()
        files = {"team_list": ("team.txt", "Jordan Testname\n"), "allow": ("allow.txt", "records@agency.example.org\n"),
                 "agencies": ("agencies.tsv", "agency\taliases\nAgency A\tA PD\nAgency B\tB PD\n"),
                 "quote_ledger": ("ledger.tsv", "\t".join("quote_id doc_id page original_sha256 method render_sha256 verdict checked_by checked_at".split()) + "\n"),
                 "inventory": ("pinv.tsv", "\t".join("doc_id title received_date source pages sha256 text_layer tier request_ref notes".split()) + "\n"),
                 "package": ("package.json", '{"records_law": {}}')}
        for name, text in files.values():
            (self.root / name).write_text(text)
        flags = {"team_list": "--team-list", "allow": "--allow", "agencies": "--agencies", "quote_ledger": "--quote-ledger",
                 "inventory": "--inventory", "package": "--package"}
        self.files = {role: self.root / files[role][0] for role in files}
        self.opts = ["--agency-dir", str(agency), "--agency", "Agency A", "--original-root", str(self.root / "orig")]
        for role, flag in flags.items():
            self.opts += [flag, str(self.files[role])]

    def without(self, *flags):
        out, skip = [], False
        for a in self.opts:
            if skip:
                skip = False
            elif a in flags:
                skip = True
            else:
                out.append(a)
        return out

    def lint(self, body=None, opts=None, name="r.json"):
        draft = self.root / "body.txt"
        draft.write_bytes(self.BODY if body is None else body)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = lint_all.main([str(draft), "--receipt", str(self.root / name), *(opts or self.opts)])
        self.assertTrue((self.root / name).exists(), out.getvalue())
        self.last_out = out.getvalue()
        return code, self.root / name

    def export(self, receipt, opts=None, section="second-letter", subject="Second subject", out=None):
        e = entry(section=section, subject=subject, placeholders=("[EDIT ME: date]",) if section == "sheriff-letter" else ())
        return self._run(mapping(e), receipt, opts, out)

    def _run(self, m, receipt, opts, out):
        self.map.write_bytes(json.dumps(m).encode())
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = ade.main(["--md", str(self.md), "--mapping", str(self.map), "--project", str(self.project),
                             "--out", str(out or self.out), "--lint-receipt", str(receipt), *(self.opts if opts is None else opts)])
        self.last_out = buf.getvalue()
        return code, json.loads(buf.getvalue())

    def refused(self, report):
        r = report["refused"][0]
        self.assertEqual(len(report["refused"]), 1)
        self.assertEqual(list(self.out.glob("*")) if self.out.exists() else [], [])
        return r["code"], r.get("reason")

    def why(self, report):
        return json.loads(Path(report["files"][0]).read_text("utf-8"))["why"]

    # Contract: a receipt for the exact body/rules/inputs exports as passed, shows its check time and still dry-run imports.
    # Fails if a stale-check bug rejects the control, the state is not carried into why, or a path/content leaks.
    def test_round_trip_exports_passed_and_imports(self):
        code, receipt = self.lint()
        self.assertEqual(code, 0, self.last_out)
        text = receipt.read_text("utf-8")
        doc = json.loads(text)
        before = tree(self.project)
        status, report = self.export(receipt)
        self.assertEqual((status, report["ok"]), (0, True), report)
        why = self.why(report)
        self.assertRegex(why, r"^agency lints: passed \(at export; checked %s\) \| Operator checked custodian$" % doc["created_at"])
        result = di.import_draft(self.project, report["files"][0], apply=False)
        self.assertEqual((result["action"], result["applied"]), ("would_queue", False))
        self.assertEqual(tree(self.project), before)
        for secret in (str(self.root / "agency"), "team.txt", "ledger.tsv", "package.json", "Jordan", "Please send", "Agency A"):
            self.assertNotIn(secret, text + self.last_out)

    # Contract: any change to one lint input, the body, the agency option or a rules version refuses with its fixed reason.
    # Fails if a hash is skipped or compared against the wrong role.
    def test_changed_body_inputs_and_config_refuse(self):
        code, receipt = self.lint()
        changes = {"team_list": "Other Name\n", "allow": "x@agency.example.org\n", "quote_ledger": "\n", "package": "{}"}
        for i, (role, text) in enumerate(changes.items()):
            saved = self.files[role].read_bytes()
            self.files[role].write_text(text if role != "quote_ledger" else saved.decode() + "\n")
            self.out = self.root / ("o%d" % i)
            with self.subTest(role=role):
                self.assertEqual(self.refused(self.export(receipt)[1]), ("LINT_RECEIPT_STALE", "input_changed:" + role))
            self.files[role].write_bytes(saved)
        (self.root / "agency" / "pages" / "D1_p001.txt").write_text("changed\n")
        self.out = self.root / "op"
        self.assertEqual(self.refused(self.export(receipt)[1]), ("LINT_RECEIPT_STALE", "input_changed:pages"))
        (self.root / "agency" / "pages" / "D1_p001.txt").write_text("Unrelated page text.\n")
        self.out = self.root / "oa"
        opts = [("Agency B" if a == "Agency A" else a) for a in self.opts]
        self.assertEqual(self.refused(self.export(receipt, opts)[1]), ("LINT_RECEIPT_STALE", "config_changed"))
        self.out = self.root / "ov"
        with mock.patch.object(lint_asks, "RULES_VERSION", "2"):
            self.assertEqual(self.refused(self.export(receipt)[1]), ("LINT_RECEIPT_STALE", "config_changed"))
        self.out = self.root / "ob"
        self.md.write_bytes(self.MD.replace(self.ASK, "1. Edited records.").encode())
        self.assertEqual(self.refused(self.export(receipt)[1]), ("LINT_RECEIPT_STALE", "body_changed"))
        self.md.write_bytes(self.MD.encode())
        self.out = self.root / "ok"
        self.assertEqual(self.export(receipt)[0], 0)  # control: everything restored

    # Contract: a role linted but absent now, or given now but absent at lint time, is stale (never silently ignored).
    def test_added_and_missing_roles_refuse(self):
        code, receipt = self.lint()
        self.out = self.root / "m"
        self.assertEqual(self.refused(self.export(receipt, self.without("--allow"))[1]), ("LINT_RECEIPT_STALE", "input_missing:allow"))
        code, bare = self.lint(opts=self.without("--allow"), name="bare.json")
        self.assertEqual(code, 3, self.last_out)
        self.out = self.root / "a"
        self.assertEqual(self.refused(self.export(bare)[1]), ("LINT_RECEIPT_STALE", "input_added:allow"))

    # Contract: a receipt that cannot be checked is never exported (unreadable input, unknown schema or lint, bad hash).
    def test_unverifiable_receipts_and_inputs_refuse(self):
        code, receipt = self.lint()
        good = json.loads(receipt.read_text("utf-8"))
        bad = {"schema": {**good, "schema": "records-agency-lint-receipt-v2"},
               "lint": {**good, "results": {**good["results"], "mystery": {"status": "pass"}}},
               "hash": {**good, "body_sha256": "xyz"}, "role": {**good, "inputs": {**good["inputs"], "extra_role": "not given"}}}
        paths = {}
        for name, doc in bad.items():
            paths[name] = self.root / (name + ".json")
            paths[name].write_text(json.dumps(doc))
        paths["dupe"] = self.root / "dupe.json"
        paths["dupe"].write_text('{"schema": "a", "schema": "b"}')
        for i, (name, path) in enumerate(paths.items()):
            self.out = self.root / ("u%d" % i)
            with self.subTest(name=name):
                self.assertEqual(self.refused(self.export(path)[1]), ("LINT_RECEIPT_UNVERIFIABLE", None))
        self.out = self.root / "ur"
        self.files["team_list"].unlink()
        status, report = self.export(receipt)
        self.assertEqual(self.refused(report), ("LINT_RECEIPT_UNVERIFIABLE", None))
        self.assertFalse(self.out.exists())
        self.assertNotIn(str(self.root), self.last_out)

    # Contract: skipped lints are listed, a failing receipt is stated (not hidden), and no receipt reads 'not run'.
    def test_incomplete_failed_and_not_run_are_stated(self):
        code, receipt = self.lint(opts=self.without("--allow", "--team-list", "--quote-ledger", "--inventory", "--original-root", "--package"))
        self.assertEqual(code, 3, self.last_out)
        status, report = self.export(receipt, self.without("--allow", "--team-list", "--quote-ledger", "--inventory", "--original-root", "--package"))
        self.assertEqual(status, 0, report)
        why = self.why(report)
        self.assertIn("agency lints: incomplete (at export; checked ", why)
        for item in ("cites no_package", "out no_team_list", "quotes q3_not_run"):
            self.assertIn(item, why)
        code, failing = self.lint(body=self.BODY + b"\nJordan Testname\n", name="f.json")
        self.assertEqual(code, 1)
        self.md.write_bytes(self.MD.replace("> " + self.ASK, "> " + self.ASK + "\n>\n> Jordan Testname").encode())
        self.out = self.root / "f"
        status, report = self.export(failing)
        self.assertEqual(status, 0, report)
        self.assertRegex(self.why(report), r"^agency lints: failed \(at export; checked [0-9TZ:-]+; failing: out 1 hits\)")
        self.out = self.root / "n"
        self.md.write_bytes(self.MD.encode())
        status, report = self.run_export(mapping(entry(section="second-letter", subject="Second subject", placeholders=())), out=self.out)
        self.assertEqual(status, 0)
        self.assertEqual(self.why(report), "agency lints: not run | Operator checked custodian")
