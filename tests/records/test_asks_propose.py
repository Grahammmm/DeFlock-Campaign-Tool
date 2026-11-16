import contextlib
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

from campaign_tool.records import asks_propose, case_builder, cli
from campaign_tool.records.outcomes import received_items, register, response_items

RUN = "run1"
ASKED = "1. Policy documents; 2. Audit logs; 3. Contracts"
SHA_A, SHA_B = "a" * 64, "b" * 64


def put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def req(agency, rid, ledger, asked=ASKED, issues=()):
    return {"agency": agency.upper(), "agency_key": agency, "request_id": rid, "ledger_request_id": ledger,
            "asked": asked, "issues": list(issues)}


def item(iid, sha, agency, ledger, **extra):
    return {"item_id": iid, "original_sha256": sha, "agency_id": agency, "request_id": ledger, **extra}


def ask(n, text, rating, quotes=(), flags=()):
    return {"ask_id": "ask-%d" % n, "text": text, "rating": rating, "flags": list(flags),
            "evidence": [{"item_id": "i1", "page": 2, "quote": q} for q in quotes]}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name) / "proj"
        self.out = self.project / "run-outputs" / RUN
        self.texts = [a["text"] for a in response_items.split_asked(ASKED)]

    def stage(self, requests=None, received=None, response=None, count=None):
        requests = [req("alpha", "R-1", "L1")] if requests is None else requests
        put(self.out / "CURRENT-REQUEST-REGISTER.json", {
            "schema": register.SCHEMA, "as_of": "2026-01-02", "requests": requests,
            "request_count": len(requests) if count is None else count})
        put(self.out / "outcomes" / "received-items.json", {
            "schema": received_items.SCHEMA, "run_id": RUN,
            "items": [item("i1", SHA_A, "alpha", "L1")] if received is None else received})
        if response is not None:
            put(self.out / "outcomes" / "response-items.json", {
                "schema": response_items.SCHEMA, "run_id": RUN, "requests": response})

    def response(self, asks, **extra):
        return [{"agency_key": "alpha", "request_id": "R-1", "ledger_request_id": "L1", "flags": [],
                 "responses": ["i1"], "asks": asks, **extra}]

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = asks_propose.main(["--project", str(self.project), "--run-id", RUN, *args])
        return code, out.getvalue(), err.getvalue()

    def snapshot(self):
        result = {}
        for path in sorted(self.project.rglob("*")):
            info = path.lstat()
            result[str(path)] = (info.st_mode, info.st_mtime_ns,
                                 path.read_bytes() if path.is_file() else None)
        return result

    def rows(self, agency="alpha"):
        text = (self.project / "agency-asks" / (agency + ".proposed.tsv")).read_text(encoding="utf-8")
        return list(csv.DictReader(io.StringIO(text), delimiter="\t"))


class MappingTests(Base):
    def test_rows_match_register_ask_numbers_and_agencies(self):
        tricky = "1. Tab\tand \"quote\"\nnewline é; 2. Policy documents"
        self.stage([req("alpha", "R-1", "L1"), req("alpha", "R 2/x", "L2", tricky), req("beta", "B-1", None)])
        self.assertEqual(self.run_cli("--apply")[0], 0)
        rows = self.rows()
        self.assertEqual([(r["message"], r["item_no"]) for r in rows],
                         [("R 2/x", "1"), ("R 2/x", "2"), ("R-1", "1"), ("R-1", "2"), ("R-1", "3")])
        self.assertEqual(rows[0]["ask_id"], "R%202%2Fx#ask-1")
        self.assertEqual(rows[0]["item_text"], "Tab\tand \"quote\"\nnewline é")
        self.assertEqual([r["item_text"] for r in rows[2:]], self.texts)
        self.assertEqual({r["state"] for r in rows}, {"unknown"})
        self.assertEqual(len({r["ask_id"] for r in rows}), 5)
        self.assertEqual(len(self.rows("beta")), 3)
        header = (self.project / "agency-asks" / "alpha.proposed.tsv").read_text().splitlines()[0]
        self.assertEqual(header.split("\t"), case_builder.ASK_COLUMNS)

    def test_agency_filter_writes_only_that_file(self):
        self.stage([req("alpha", "R-1", "L1"), req("beta", "B-1", None)])
        code, out, _ = self.run_cli("--agency", "beta", "--apply")
        self.assertEqual(code, 0)
        self.assertEqual(os.listdir(self.project / "agency-asks"), ["beta.proposed.tsv"])
        self.assertEqual(json.loads(out)["agencies"], 1)
        code, out, err = self.run_cli("--agency", "nope")
        self.assertEqual((code, out, err.strip()), (2, "", "ASKS_AGENCY_UNKNOWN"))

    def test_no_requests_is_zero_and_writes_nothing(self):
        self.stage([])
        code, out, _ = self.run_cli("--apply")
        self.assertEqual(code, 0)
        self.assertEqual((json.loads(out)["rows"], json.loads(out)["written"]), (0, 0))
        self.assertFalse((self.project / "agency-asks").exists())


class EvidenceTests(Base):
    def states(self, asks, items=None, **extra):
        self.stage(response=self.response(asks, **extra), received=items)
        (self.project / "agency-asks" / "alpha.proposed.tsv").unlink(missing_ok=True)
        self.assertEqual(self.run_cli("--apply")[0], 0)
        return self.rows()

    def test_produced_pending_only_from_exact_evidence(self):
        t = self.texts
        rows = self.states([ask(1, t[0], "answered", ["Item 1 is attached."]),
                            ask(2, t[1], "answered", ["Item 2 will be provided by June."]),
                            ask(3, t[2], "answered", ["We will extend the deadline."])])
        self.assertEqual([r["state"] for r in rows], ["produced", "pending", "unknown"])
        quote_hash = hashlib.sha256(b"Item 1 is attached.").hexdigest()
        self.assertEqual(json.loads(rows[0]["response"]),
                         [{"item_id": "i1", "page": 2, "quote_sha256": quote_hash, "original_sha256": SHA_A}])
        self.assertEqual(rows[0]["held_doc_ids"], json.dumps([SHA_A]))
        self.assertEqual(rows[1]["held_doc_ids"], "[]")
        self.assertNotIn("attached", rows[0]["response"])

    def test_no_suppression_without_exact_support(self):
        t = self.texts
        cases = [("Item 2 is attached.", "answered", ()),            # wrong ask number
                 ("The records are attached.", "answered", ()),      # unnumbered
                 ("Item 1 is not attached.", "answered", ()),        # negated
                 ("Item 1 is attached and item 2 will be provided.", "answered", ()),  # competing
                 ("Item 1 is attached and will be provided.", "answered", ()),  # past/future conflict
                 ("Item 1 is attached.", "unclear", ()),             # rating not answered
                 ("Item 1 is attached.", "answered", ("text_unreviewed",))]
        for quote, rating, flags in cases:
            with self.subTest(quote=quote, rating=rating, flags=flags):
                rows = self.states([ask(1, t[0], rating, [quote], flags)])
                self.assertEqual(rows[0]["state"], "unknown")
        rows = self.states([ask(1, t[0], "answered", ["Item 1 is attached."])],
                           items=[item("i1", SHA_A, "alpha", "L1", needs_visual_review_unresolved=True)])
        self.assertEqual(rows[0]["state"], "unknown")
        rows = self.states([ask(1, t[0], "answered", ["Item 1 is attached."])], flags=["text_unreviewed"])
        self.assertEqual(rows[0]["state"], "unknown")

    def test_wrong_join_refuses_evidence_invalid(self):
        t = self.texts
        good = ask(1, t[0], "answered", ["Item 1 is attached."])
        bad = [
            (self.response([good], ledger_request_id="L9"), None),
            (self.response([good], agency_key="zzz"), None),
            (self.response([ask(1, "other text", "answered", ["Item 1 is attached."])]), None),
            (self.response([ask(9, t[0], "answered")]), None),
            (self.response([good]), [item("i1", SHA_A, "alpha", "L1"), item("i1", SHA_B, "alpha", "L1")]),
            (self.response([good]), [item("i1", SHA_A, "alpha", "L2")]),
            (self.response([good]), [item("i1", SHA_A, "other", "L1")]),
            (self.response([good]), [item("i1", "short", "alpha", "L1")]),
            (self.response([ask(1, t[0], "answered", ["Item 1 is attached.", "Item 1 is attached."])]), None),
        ]
        for response, items in bad:
            with self.subTest(response=response, items=items):
                self.stage(response=response, received=items)
                self.assertEqual(self.run_cli("--apply")[::2], (2, "ASKS_EVIDENCE_INVALID\n"))
        self.assertFalse((self.project / "agency-asks").exists())

    def test_stale_schemas_refuse_with_codes(self):
        self.stage(response=[])
        path = self.out / "outcomes" / "response-items.json"
        data = json.loads(path.read_text())
        put(path, {**data, "run_id": "older"})
        self.assertEqual(self.run_cli()[::2], (2, "ASKS_RESPONSE_INVALID\n"))
        self.stage(response=[])
        path = self.out / "outcomes" / "received-items.json"
        put(path, {**json.loads(path.read_text()), "schema": "old"})
        self.assertEqual(self.run_cli()[::2], (2, "ASKS_RECEIVED_INVALID\n"))
        path.unlink()
        self.assertEqual(self.run_cli()[::2], (2, "ASKS_INPUT_UNAVAILABLE\n"))


class MalformedRegisterTests(Base):
    def test_named_codes_empty_stdout_no_writes(self):
        long = "; ".join("%d. item number %d" % (i, i) for i in range(1, 201))
        cases = {
            "ASKS_REGISTER_INVALID": [
                dict(requests=[req("alpha", "R-1", "L1"), req("alpha", "R-1", "L1")]),
                dict(requests=[req("a/b", "R-1", "L1")]),
                dict(requests=[req("alpha", "", "L1")]),
                dict(requests=[req("alpha", "R-1", "L1", asked="")]),
                dict(requests=[req("alpha", "R-1", "L1", asked=response_items.UNKNOWN_SCOPE)]),
                dict(requests=[req("alpha", "R-1", "L1", issues=["missing_date"])]),
                dict(count=7),
            ],
            "ASKS_SCOPE_INCOMPLETE": [
                dict(requests=[req("alpha", "R-1", "L1", asked=long)]),
                dict(requests=[req("alpha", "R-1", "L1", asked="1. " + "x" * 600)]),
            ],
        }
        for code, variants in cases.items():
            for variant in variants:
                with self.subTest(code=code, variant=str(variant)[:60]):
                    self.stage(**variant)
                    self.assertEqual(self.run_cli("--apply"), (2, "", code + "\n"))
                    self.assertFalse((self.project / "agency-asks").exists())
        self.stage()
        (self.out / "CURRENT-REQUEST-REGISTER.json").write_text('{"schema": "x", "schema": "y"}')
        self.assertEqual(self.run_cli("--apply"), (2, "", "ASKS_REGISTER_INVALID\n"))
        (self.out / "CURRENT-REQUEST-REGISTER.json").write_text("{")
        self.assertEqual(self.run_cli()[::2], (2, "ASKS_REGISTER_INVALID\n"))

    def test_symlinked_input_and_unsafe_run_refuse(self):
        self.stage()
        reg = self.out / "CURRENT-REQUEST-REGISTER.json"
        real = reg.with_name("real.json")
        reg.rename(real)
        reg.symlink_to(real)
        self.assertEqual(self.run_cli()[::2], (2, "ASKS_REGISTER_INVALID\n"))
        with self.assertRaises(asks_propose.ProposalError) as raised:
            asks_propose.propose(self.project, "../x")
        self.assertEqual(raised.exception.code, "ASKS_RUN_INVALID")


class WriteTests(Base):
    def test_dry_run_changes_nothing(self):
        self.stage()
        (self.project / "agency-asks").mkdir()
        (self.project / "agency-asks" / "alpha.tsv").write_text("confirmed")
        before = self.snapshot()
        code, out, _ = self.run_cli()
        self.assertEqual(code, 0)
        summary = json.loads(out)
        self.assertEqual((summary["dry_run"], summary["rows"], summary["written"]), (True, 3, 0))
        self.assertEqual(self.snapshot(), before)
        fresh = self.project / "agency-asks"
        for child in fresh.iterdir():
            child.unlink()
        fresh.rmdir()
        before = self.snapshot()
        self.run_cli()
        self.assertEqual(self.snapshot(), before)

    def test_apply_private_noclobber_and_confirmed_untouched(self):
        self.stage()
        directory = self.project / "agency-asks"
        directory.mkdir()
        os.chmod(directory, 0o755)
        (directory / "alpha.tsv").write_text("confirmed")
        os.chmod(directory, 0o700)
        mask = os.umask(0o022)
        os.umask(mask)
        code, out, _ = self.run_cli("--apply")
        self.assertEqual((code, json.loads(out)["written"]), (0, 1))
        self.assertEqual(os.umask(mask), mask)
        target = directory / "alpha.proposed.tsv"
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertEqual((directory / "alpha.tsv").read_text(), "confirmed")
        self.assertEqual(sorted(os.listdir(directory)), ["alpha.proposed.tsv", "alpha.tsv"])
        replay = json.loads(self.run_cli("--apply")[1])
        self.assertEqual((replay["written"], replay["present"]), (0, 1))
        target.write_text("edited by operator")
        self.assertEqual(self.run_cli("--apply")[::2], (2, "ASKS_PROPOSAL_EXISTS\n"))
        self.assertEqual(target.read_text(), "edited by operator")

    def test_new_directory_is_private(self):
        self.stage()
        self.run_cli("--apply")
        self.assertEqual(stat.S_IMODE((self.project / "agency-asks").stat().st_mode), 0o700)

    def test_conflict_in_second_agency_blocks_first_write(self):
        self.stage([req("alpha", "R-1", "L1"), req("beta", "B-1", None)])
        directory = self.project / "agency-asks"
        directory.mkdir()
        (directory / "beta.proposed.tsv").write_text("different")
        self.assertEqual(self.run_cli("--apply")[::2], (2, "ASKS_PROPOSAL_EXISTS\n"))
        self.assertEqual(os.listdir(directory), ["beta.proposed.tsv"])

    def test_symlink_target_and_directory_refuse(self):
        self.stage()
        directory = self.project / "agency-asks"
        directory.mkdir()
        outside = Path(self.tmp.name) / "outside"
        outside.write_text("x")
        (directory / "alpha.proposed.tsv").symlink_to(outside)
        self.assertEqual(self.run_cli("--apply")[::2], (2, "ASKS_TARGET_UNSAFE\n"))
        self.assertEqual(outside.read_text(), "x")
        (directory / "alpha.proposed.tsv").unlink()
        directory.rmdir()
        directory.symlink_to(Path(self.tmp.name))
        self.assertEqual(self.run_cli("--apply")[::2], (2, "ASKS_TARGET_UNSAFE\n"))

    def test_later_write_failure_rolls_back_without_leaking(self):
        self.stage([req("alpha", "R-1", "L1"), req("beta", "B-1", None)])
        real_link = os.link
        calls = []

        def flaky(src, dst, *a, **k):
            calls.append(dst)
            if len(calls) == 2:
                raise OSError("SECRET-PRIVATE-PATH")
            return real_link(src, dst, *a, **k)
        mask = os.umask(0o022)
        os.umask(mask)
        with mock.patch.object(asks_propose.os, "link", flaky):
            code, out, err = self.run_cli("--apply")
        self.assertEqual((code, out, err), (2, "", "ASKS_WRITE_FAILED\n"))
        self.assertFalse((self.project / "agency-asks").exists())
        self.assertEqual(os.umask(mask), mask)


class ConfirmBoundaryTests(Base):
    def test_case_builder_ignores_proposal_until_renamed(self):
        self.stage(response=self.response([
            ask(1, self.texts[0], "answered", ["Item 1 is attached."]),
            ask(2, self.texts[1], "answered", ["Item 2 will be provided."])]))
        self.assertEqual(self.run_cli("--apply")[0], 0)
        self.assertIsNone(case_builder._asks(self.project, "alpha"))
        directory = self.project / "agency-asks"
        (directory / "alpha.proposed.tsv").rename(directory / "alpha.tsv")
        rows = case_builder._asks(self.project, "alpha")
        self.assertEqual([r["state"] for r in rows], ["produced", "pending", "unknown"])
        self.assertEqual(rows[0]["item_text"], self.texts[0])

    def test_no_network_or_send_surface(self):
        source = Path(asks_propose.__file__).read_text(encoding="utf-8")
        for word in ("socket", "smtplib", "subprocess", "urlopen", "mail_send", "http"):
            self.assertNotIn(word, source)


class CliAndDocTests(unittest.TestCase):
    def test_registered_command(self):
        self.assertEqual(cli.COMMANDS["asks-propose"], ("asks_propose", "main"))

    def test_doc_documents_confirm_step(self):
        doc = (Path(__file__).resolve().parents[2] / "docs" / "RECORDS-FOLLOWUP-ENGINE.md").read_text(encoding="utf-8")
        for needle in ("records asks-propose", "--apply", ".proposed.tsv", "rename", "never overwrite",
                       "R1/R7", "never reads a `.proposed.tsv`"):
            self.assertTrue(needle in doc, needle)


if __name__ == "__main__":
    unittest.main()
