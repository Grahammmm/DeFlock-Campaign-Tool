"""FC4-ROW-PAGE-STATE: explicit failed page states never yield complete rows.

Synthetic, in-memory only: no originals, stores, subprocesses, network or IO.
Each case drives the real ``rows.normalize`` through the existing
``tests.test_understanding_rows`` fixture/``args_for`` helpers, so every page
mutation is re-serialized and its derived receipt byte/hash binding and source
inventory are recomputed. The only thing that differs between a clear and a
not-clear input is therefore the page entry itself.

Page vocabulary comes from the producers, which stay read-only here:
``extraction_routes.page_manifest`` emits page rows whose ``status`` is one of
``ok``/``partial``/``blocked`` and which always carry ``needs_visual_review``;
``extraction_routes`` marks a PDF receipt ``partial`` when any page is not
``ok``. ``native_extract`` only consumes the receipt-level status.
"""
import copy
import unittest

from campaign_tool.records import extraction_routes
from campaign_tool.records.understanding import rows
from tests.test_understanding_rows import STAMP, args_for, fixture, reasons, records

PAGE_REASON = "upstream_pages_not_clear"
TOP_REASON = "upstream_not_clear"
CELLS = [["event-1", STAMP, "investigation", "user-1"]]
# Producer status vocabulary (extraction_routes.page_manifest / OCR overlay).
PRODUCER_NOT_OK = ("partial", "blocked")
# Declared JSON value classes a malformed field can take, other than the valid one.
JSON_CLASSES = {"null": None, "true": True, "false": False, "int_zero": 0, "int_one": 1,
                "float": 1.5, "empty_string": "", "string": "x", "list": [], "list_ok": ["ok"],
                "object": {}, "object_ok": {"status": "ok"}}


OMIT = object()


def page(status="ok", visual=False, **extra):
    """A producer-shaped page row; ``visual=OMIT`` leaves out needs_visual_review."""
    entry = {"page_no": 1, "method": "synthetic-parser", "method_version": "synthetic-1",
             "derivative_sha256": None, "confidence": None, "status": status, "reason": None}
    if visual is not OMIT:
        entry["needs_visual_review"] = visual
    entry.update(extra)
    return entry


def run(pages, *, status="complete", issues=None, kind="pdf_page"):
    """Normalize one synthetic receipt whose page inventory is ``pages``.

    The receipt is re-encoded through ``args_for`` so its derived hash and
    inventory are bound to exactly these bytes.
    """
    base, receipt = fixture(copy.deepcopy(CELLS), kind=kind)
    receipt["pages"] = copy.deepcopy(pages)
    receipt["status"] = status
    receipt["issues"] = [] if issues is None else copy.deepcopy(issues)
    args = args_for(receipt, base["mappings"][0])
    before = copy.deepcopy(args)
    output = rows.normalize(**args)
    return output, args, before


class PageStateTests(unittest.TestCase):
    def assert_clear(self, output):
        self.assertTrue(output["normalized"]["complete"])
        self.assertEqual(output["upstream"], [])
        self.assertEqual(reasons(output), set())

    def assert_not_clear(self, output, args, extra=()):
        subject = args["receipts"][0]["artifact_sha256"]
        self.assertIs(output["normalized"]["complete"], False)
        expected = [{"artifact_sha256": subject, "reason": r} for r in sorted((PAGE_REASON,) + tuple(extra))]
        self.assertEqual(sorted(output["upstream"], key=lambda u: u["reason"]), expected)
        # Row-level mapping is untouched: the page gate is the only reason.
        self.assertEqual(reasons(output), set())

    def assert_limits(self, output):
        self.assertIs(output["review_required"], True)
        for field in ("qualified", "activation_allowed", "publication_ready"):
            self.assertIs(output[field], False)
        self.assertEqual(output["stage_promotions"], 0)

    # -------------------------------------------------------------- positives

    def test_ok_page_and_empty_page_inventory(self):
        cases = {"empty": [], "ok_false": [page()], "ok_omitted": [page(visual=OMIT)],
                 "ok_minimal": [{"status": "ok"}], "two_ok": [page(), page(page_no=2, visual=OMIT)]}
        outputs = {}
        for name, pages in cases.items():
            with self.subTest(case=name):
                output, args, before = run(pages)
                self.assert_clear(output)
                self.assert_limits(output)
                self.assertEqual(args, before)
                outputs[name] = output
        # Apart from receipt-bound identities, a clear page inventory changes nothing.
        def projection(output):
            return [i["record"] | {"original_sha256": None} for i in output["normalized"]["units"]], \
                [(d["status"], d["reasons"], d["cells"]) for d in output["diagnostics"]], \
                [e["identity"] for e in output["events"]]
        for name in cases:
            self.assertEqual(projection(outputs[name]), projection(outputs["empty"]))
        # Text receipts with clear pages behave like PDF receipts.
        output, _, _ = run([page()], kind="text_line")
        self.assert_clear(output)

    # -------------------------------------------------------------- not clear

    def test_partial_blocked_unknown_page_status(self):
        unknown = ("OK", "Ok", " ok", "ok ", "complete", "clear", "", "unknown", "not_reviewed")
        for status in PRODUCER_NOT_OK + unknown:
            with self.subTest(status=status):
                output, args, before = run([page(status)])
                self.assert_not_clear(output, args)
                self.assert_limits(output)
                self.assertEqual(args, before)
        # Real producer rows: page_manifest marks blank pages partial and errors blocked.
        units = [{"kind": "pdf_page", "locator": {"page": 1}, "text": "", "data": {}},
                 {"kind": "pdf_page", "locator": {"page": 2}, "text": "x", "data": {"error": "e"}}]
        produced = extraction_routes.page_manifest(units, 3, "synthetic-parser", "synthetic-1")
        self.assertEqual([p["status"] for p in produced], ["partial", "blocked", "blocked"])
        for entry in produced:
            with self.subTest(producer_status=entry["status"], page=entry["page_no"]):
                output, args, _ = run([entry])
                self.assert_not_clear(output, args)
                # Even with the producer's review flag lowered, a non-ok status stays not clear.
                output, args, _ = run([dict(entry, needs_visual_review=False)])
                self.assert_not_clear(output, args)

    def test_missing_and_container_page_status(self):
        missing = page()
        del missing["status"]
        self.assert_not_clear(*run([missing])[:2])
        self.assert_not_clear(*run([{"needs_visual_review": False}])[:2])
        self.assert_not_clear(*run([{}])[:2])
        for name, value in JSON_CLASSES.items():
            with self.subTest(status_class=name):
                output, args, before = run([page(value)])
                self.assert_not_clear(output, args)
                self.assertEqual(args, before)

    def test_visual_review_true_and_malformed(self):
        for name, value in JSON_CLASSES.items():
            if name == "false":
                continue  # The only accepted present value.
            with self.subTest(visual_class=name):
                output, args, before = run([page("ok", value)])
                self.assert_not_clear(output, args)
                self.assertEqual(args, before)
        self.assert_clear(run([page("ok", False)])[0])
        # Producer ok rows always carry needs_visual_review=True, so they are not clear.
        produced = extraction_routes.page_manifest(
            [{"kind": "pdf_page", "locator": {"page": 1}, "text": "x", "data": {}}],
            1, "synthetic-parser", "synthetic-1")
        self.assertEqual((produced[0]["status"], produced[0]["needs_visual_review"]), ("ok", True))
        self.assert_not_clear(*run(produced)[:2])
        self.assert_clear(run([dict(produced[0], needs_visual_review=False)])[0])

    def test_malformed_page_entry(self):
        for name, value in JSON_CLASSES.items():
            if name in ("object", "object_ok"):
                continue  # Objects are well-shaped entries, judged by their fields.
            for position in ("only", "after_ok", "before_ok"):
                pages = {"only": [value], "after_ok": [page(), value], "before_ok": [value, page()]}[position]
                with self.subTest(entry_class=name, position=position):
                    base, receipt = fixture(copy.deepcopy(CELLS), kind="pdf_page")
                    receipt["pages"] = pages
                    args = args_for(receipt, base["mappings"][0])
                    before = copy.deepcopy(args)
                    with self.assertRaisesRegex(rows.RowsError, "^receipt_page_shape$"):
                        rows.normalize(**args)
                    self.assertEqual(args, before)
        # A non-list pages container stays the pre-existing receipt shape refusal.
        base, receipt = fixture(copy.deepcopy(CELLS), kind="pdf_page")
        receipt["pages"] = {"status": "ok"}
        with self.assertRaisesRegex(rows.RowsError, "^receipt_status_shape$"):
            rows.normalize(**args_for(receipt, base["mappings"][0]))

    def test_multiple_pages_one_not_clear(self):
        bad_entries = {"partial": page("partial"), "blocked": page("blocked"), "visual": page("ok", True)}
        for name, bad in bad_entries.items():
            for index in range(3):
                pages = [page(page_no=n + 1) for n in range(3)]
                pages[index] = dict(bad, page_no=index + 1)
                with self.subTest(bad=name, index=index):
                    output, args, _ = run(pages)
                    self.assert_not_clear(output, args)
                    self.assertEqual(len(output["upstream"]), 1)  # One aggregate reason per artifact.
        output, args, _ = run([page("partial"), page("blocked"), page("ok", True)])
        self.assert_not_clear(output, args)
        self.assertEqual(len(output["upstream"]), 1)
        self.assert_clear(run([page(page_no=n + 1) for n in range(3)])[0])

    def test_top_level_hold_stays_hold(self):
        holds = {"partial": dict(status="partial"), "blocked": dict(status="blocked"),
                 "issues": dict(issues=[{"code": "ocr_needed"}])}
        for name, hold in holds.items():
            with self.subTest(hold=name, pages="clear"):
                output, args, _ = run([page()], **hold)
                subject = args["receipts"][0]["artifact_sha256"]
                self.assertIs(output["normalized"]["complete"], False)
                self.assertEqual(output["upstream"], [{"artifact_sha256": subject, "reason": TOP_REASON}])
            with self.subTest(hold=name, pages="empty"):
                output, args, _ = run([], **hold)
                self.assertIs(output["normalized"]["complete"], False)
                self.assertEqual([u["reason"] for u in output["upstream"]], [TOP_REASON])
            with self.subTest(hold=name, pages="blocked"):
                output, args, _ = run([page("blocked", True)], **hold)
                self.assert_not_clear(output, args, extra=(TOP_REASON,))

    def test_rows_retained_for_triage(self):
        clear, clear_args, _ = run([page()])
        for pages in ([page("blocked", True)], [page("partial")], [page("ok", True)]):
            with self.subTest(pages=pages[0]["status"]):
                output, args, _ = run(pages)
                self.assertIs(output["normalized"]["complete"], False)
                self.assertEqual(len(records(output)), 1)
                record = records(output)[0]
                self.assertEqual(record["record_type"], "search-log")
                self.assertEqual(record["event_id"], "event-1")
                self.assertEqual(record["event_at"], STAMP)
                self.assertEqual(record["fields"], records(clear)[0]["fields"])
                self.assertEqual([d["status"] for d in output["diagnostics"]], ["mapped"])
                self.assertEqual([d["cells"] for d in output["diagnostics"]],
                                 [d["cells"] for d in clear["diagnostics"]])
                self.assertEqual([(e["identity"], e["status"]) for e in output["events"]],
                                 [(e["identity"], e["status"]) for e in clear["events"]])
                self.assertEqual(output["source_bindings"], args["source_bindings"])

    def test_aggregate_reason_no_payload(self):
        marker = "page-text-must-not-leak"
        pages = [page("blocked", True, reason=marker, page_no=7, derivative_sha256=None),
                 page("partial", marker, note=marker)]
        output, args, _ = run(pages)
        self.assert_not_clear(output, args)
        entry = output["upstream"][0]
        self.assertEqual(set(entry), {"artifact_sha256", "reason"})
        self.assertEqual(entry["artifact_sha256"], args["receipts"][0]["artifact_sha256"])
        self.assertNotIn(marker, repr(output))
        self.assertNotIn("page_no", repr(output["upstream"]))
        self.assertEqual(output, rows.normalize(**args))  # Deterministic.

    def test_publication_stays_false(self):
        for pages in ([], [page()], [page("blocked", True)], [page("ok", True)], [page(None)]):
            with self.subTest(pages=pages):
                self.assert_limits(run(pages)[0])
        self.assert_limits(run([page("partial")], status="partial")[0])

    def test_inputs_unchanged(self):
        for pages in ([page()], [page("blocked", True)], [page("ok", 1)], [{"status": ["ok"]}]):
            with self.subTest(pages=pages):
                output, args, before = run(pages)
                self.assertEqual(args, before)
                raw = args["receipts"][0]["derived_receipt"]
                output["upstream"].append({"tamper": True})
                output["normalized"]["units"].clear()
                self.assertEqual(args, before)
                self.assertEqual(rows.normalize(**args)["upstream"], run(pages)[0]["upstream"])
                self.assertIs(args["receipts"][0]["derived_receipt"], raw)


if __name__ == "__main__":
    unittest.main()
