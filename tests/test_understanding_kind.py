"""Synthetic-only F-033 acceptance; no extraction/inference/real records.

Receipt wrapper: extraction_routes.py:433 and understanding/evidence.py:43-69.
Native units: intake/folder.py at baseline93995a1: csv_header at302 (string
array), docx_paragraph at348 (part/paragraph), workbook_row at363-365 (dict
cells in both JSON text and data.cells), text_line at386 (line). PDF uses page=1.
These synthetic fixtures are NOT workbook/preservation qualification. Native
XLSX's cached-values issue still holds other/low. Outputs are retained in a new
candidate out/test-kind-UUID on each run, never written under fixtures.
"""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch
import uuid

from campaign_tool.records.understanding import kind

REPO = Path(__file__).resolve().parents[1]
CLI = REPO / "tools/understanding_kind.py"
SUBJECT = hashlib.sha256(b"synthetic original identity, not a real record").hexdigest()
RUN_ID = "synthetic-producer-opaque-id"
# Independent expected vocabulary, not generated from the implementation.
POSITIVES = {
    "search-log": "Search log", "sharing-list": "Sharing list",
    "network-search": "Network search", "retention-setting": "Retention settings",
    "audit": "Audit log", "request": "Public records request",
    "training-roster": "Training roster", "policy": "ALPR policy",
    "contract": "Service agreement", "correspondence": "Email correspondence",
    "invoice": "Invoice", "denial-or-extension": "Records request denial",
}


def unit(text, *, page=1, kind_name="pdf_page", locator=None):
    return {"kind": kind_name, "locator": {"page": page} if locator is None else locator,
            "text": text, "data": {}}


def workbook(values, *, sheet="opaque", row=1, types=None):
    # Source-shaped synthetic cells, not a string-array workbook surrogate.
    types = types if types is not None else ["s" if type(v) is str else "n" for v in values]
    cells = [{"cell": chr(65 + i) + str(row), "value": v, "type": t}
             for i, (v, t) in enumerate(zip(values, types))]
    return {"kind": "workbook_row", "locator": {"sheet": sheet, "row": row},
            "text": json.dumps(cells, sort_keys=True, ensure_ascii=True, default=str),
            "data": {"cells": cells}}


def csv_header(values, *, record=1):
    return unit(json.dumps(values, sort_keys=True, ensure_ascii=True, default=str), kind_name="csv_header",
                locator={"record": record, "line_start": record, "line_end": record})


def paragraph(text, *, number=1, part="word/document.xml"):
    return unit(text, kind_name="docx_paragraph", locator={"part": part, "paragraph": number})


def native(*units, status="complete", issues=None):
    return {"schema": "format-extraction-v1", "original_sha256": SUBJECT,
            "original_changed": False, "review_status": "not_reviewed",
            "status": status, "units": list(units), "pages": [],
            "issues": [] if issues is None else issues,
            "route": {"route": "extract:pdf", "format": "pdf"},
            "parser_adapter": "synthetic-producer-not-qualified",
            "parser": "synthetic-fixture", "parser_version": "1"}


def args(receipt=None, **extra):
    receipt = native(unit("Search log\nSynthetic cells only")) if receipt is None else receipt
    raw = kind.canonical_bytes(receipt)
    return {"run_id": RUN_ID, "artifact_sha256": SUBJECT,
            "derived_receipt_sha256": hashlib.sha256(raw).hexdigest(),
            "derived_receipt": raw, **extra}


class KindTests(unittest.TestCase):
    def error(self, expected, values):
        with self.assertRaises(kind.KindError) as raised:
            kind.classify(**values)
        self.assertEqual(str(raised.exception), expected)

    def test_every_existing_kind_positive_and_exact_identity(self):
        self.assertEqual(kind.KINDS, set(POSITIVES) | {"other"})
        for expected, title in POSITIVES.items():
            with self.subTest(kind=expected):
                values = args(native(unit(title + "\nSYNTHETIC")), filename=expected + ".pdf", mime="application/pdf")
                output = kind.classify(**values)
                self.assertEqual(output["kind"], expected)
                # Filename aliases may differ, but first-page explicit title suffices.
                self.assertIn(output["confidence"], ("medium", "high"))
                self.assertEqual(output["reason"], "explicit_cues")
                self.assertEqual(output["artifact_sha256"], SUBJECT)
                self.assertEqual(output["derived_receipt_sha256"], values["derived_receipt_sha256"])
                self.assertEqual(output["run_id"], RUN_ID)
                cue = next(c for c in output["matched_cues"] if c["channel"] == "first_page")
                self.assertEqual(cue["unit_sha256"], hashlib.sha256(kind.canonical_bytes(native(unit(title + "\nSYNTHETIC"))["units"][0])).hexdigest())
                self.assertTrue(output["review_required"])
                for flag in ("qualified", "activation_allowed", "publication_ready"):
                    self.assertIs(output[flag], False)
                self.assertEqual(output["stage_promotions"], 0)
                self.assertEqual(output["semantic_assessment"], "NOT_ASSESSED")

    def test_detector_vocabulary_compatible_without_importing_pipeline(self):
        # Source-only check avoids any protected adapter imports/defaults.
        import ast
        source = (REPO / "campaign_tool/records/detectors/core.py").read_text()
        assignment = next(n for n in ast.parse(source).body if isinstance(n, ast.Assign)
                          and any(isinstance(t, ast.Name) and t.id == "TYPES" for t in n.targets))
        types = set().union(*ast.literal_eval(assignment.value).values())
        self.assertTrue(types <= kind.KINDS)
        self.assertEqual(types, {"search-log", "sharing-list", "network-search", "retention-setting", "audit", "request"})
        pipeline = ast.parse((REPO / "campaign_tool/records/detectors/pipeline.py").read_text())
        assignment = next(n for n in pipeline.body if isinstance(n, ast.Assign)
                          and any(isinstance(t, ast.Name) and t.id == "KNOWN_TYPES" for t in n.targets))
        # Inspect the native union AST without importing/evaluating its code.
        self.assertIsInstance(assignment.value, ast.BinOp)
        self.assertIsInstance(assignment.value.op, ast.BitOr)
        self.assertEqual(ast.dump(assignment.value.left),
                         ast.dump(ast.parse("set().union(*TYPES.values())", mode="eval").body))
        self.assertEqual(kind.KINDS, types | ast.literal_eval(assignment.value.right))

    def test_conflicting_channels_and_mixed_documents_are_other(self):
        cases = [args(filename="policy.pdf"),
                 args(native(unit("Search log"), unit("Policy"))),
                 args(native(unit("Policy")), mime="message/rfc822"),
                 args(native(unit("Search log"), unit("", kind_name="workbook_sheet", locator={"sheet": "Training roster"})))]
        for values in cases:
            with self.subTest(values=values):
                output = kind.classify(**values)
                self.assertEqual((output["kind"], output["confidence"], output["reason"]),
                                 ("other", "low", "conflicting_cues"))

    def test_unknown_ambiguous_body_mentions_and_substrings_not_usage(self):
        for text in ("", "Search", "Research log", "Not a search log", "Example search log",
                     "An explanatory memo\nThis mentions search log and policy.",
                     "Search log / Policy", "Policy discussion", "search logger"):
            output = kind.classify(**args(native(unit(text))))
            self.assertEqual((output["kind"], output["confidence"]), ("other", "low"))
        output = kind.classify(**args(native(unit("Search log", page=2))))
        self.assertEqual(output["reason"], "no_decisive_cues")

    def test_filename_and_mime_are_bound_but_insufficient(self):
        for name, mime in (("search-log.csv", "text/csv"), ("policy.pdf", "application/pdf"),
                           ("opaque.eml", "message/rfc822"), ("unknown.txt", "text/plain")):
            values = args(native(), filename=name, mime=mime)
            output = kind.classify(**values)
            self.assertEqual((output["kind"], output["confidence"]), ("other", "low"))
            self.assertEqual(output["metadata_sha256"], hashlib.sha256(kind.canonical_bytes({"filename": name, "mime": mime})).hexdigest())
        values = args(native(unit("Correspondence")), mime="message/rfc822; charset=utf-8")
        self.assertEqual(kind.classify(**values)["confidence"], "high")

    def test_sheet_and_native_header_rules(self):
        headers = ["event_id", "searched_at", "purpose"]
        row = workbook(headers, sheet="Search log")
        output = kind.classify(**args(native(row)))
        self.assertEqual((output["kind"], output["confidence"]), ("search-log", "high"))
        self.assertEqual({m["channel"] for m in output["matched_cues"]}, {"sheet", "header"})
        for names, expected in ((["agency_id", "retention_days"], "retention-setting"),
                                (["user_id", "training_completed_at"], "training-roster")):
            test = workbook(names)
            self.assertEqual(kind.classify(**args(native(test)))["kind"], expected)
        # Header cue does not arise from values or generic partial columns.
        for cells, row_index in ((headers, 2), (["event_id", "purpose"], 1)):
            test = workbook(cells, row=row_index)
            self.assertEqual(kind.classify(**args(native(test)))["kind"], "other")

    def test_all_header_signatures_and_near_miss_controls(self):
        signatures = {
            "search-log": ["event_id", "searched_at", "purpose"],
            "sharing-list": ["agency_id", "recipient_agency_id", "sharing_enabled"],
            "network-search": ["event_id", "network_id", "searched_at"],
            "retention-setting": ["agency_id", "retention_days"],
            "audit": ["event_id", "audit_action", "actor_id"],
            "request": ["request_id", "received_at", "request_status"],
            "training-roster": ["user_id", "training_completed_at"],
        }
        for expected, names in signatures.items():
            for permutation in (names, list(reversed(names))):
                for test in (workbook(permutation), csv_header(permutation)):
                    output = kind.classify(**args(native(test)))
                    self.assertEqual((output["kind"], output["confidence"]), (expected, "medium"))
            for removed in names:
                for test in (workbook([n for n in names if n != removed]),
                             csv_header([n for n in names if n != removed])):
                    self.assertEqual(kind.classify(**args(native(test)))["kind"], "other")

    def test_explicit_confidence_levels_and_semantic_gate_control(self):
        self.assertEqual(kind.classify(**args())["confidence"], "medium")
        self.assertEqual(kind.classify(**args(filename="search-log.pdf"))["confidence"], "high")
        # Removing the actual recognition rule loses the known synthetic positive;
        # no other default or filename/MIME guess may conceal that regression.
        with patch.object(kind, "TITLES", {}):
            output = kind.classify(**args())
            self.assertEqual((output["kind"], output["confidence"]), ("other", "low"))
        self.assertEqual(kind.classify(**args())["kind"], "search-log")

    def test_native_text_and_docx_opening_titles(self):
        for expected, title in POSITIVES.items():
            for opening in (unit(title, kind_name="text_line", locator={"line": 1}),
                            paragraph(title)):
                with self.subTest(expected=expected, kind=opening["kind"]):
                    output = kind.classify(**args(native(opening)))
                    self.assertEqual((output["kind"], output["confidence"], output["reason"]),
                                     (expected, "medium", "explicit_cues"))
                    self.assertEqual(output["matched_cues"], [{
                        "kind": expected, "channel": "first_page",
                        "rule": title.casefold().replace(" ", "-"),
                        "unit_sha256": hashlib.sha256(kind.canonical_bytes(opening)).hexdigest()}])

    def test_native_text_docx_negatives_and_non_native_text(self):
        for opening in (unit("Search log", kind_name="text_line", locator={"line": 2}),
                        unit("Search log", kind_name="text_line", locator={"line": True}),
                        unit("Search log", kind_name="text_line", locator={"line": "1"}),
                        unit("Memo about search log", kind_name="text_line", locator={"line": 1}),
                        paragraph("Policy discussion"),
                        paragraph("Policy", part="word/header1.xml"),
                        paragraph("Policy", part="word/comments.xml"),
                        paragraph("Policy", number=True),
                        unit("Policy", kind_name="text")):
            output = kind.classify(**args(native(opening)))
            self.assertEqual((output["kind"], output["confidence"], output["reason"]),
                             ("other", "low", "no_decisive_cues"))
        # A later body title cannot override an ordinary opening paragraph.
        result = kind.classify(**args(native(paragraph("Memo"), paragraph("Policy", number=2))))
        self.assertEqual(result["kind"], "other")

    def test_first_emitted_docx_paragraph_uses_main_part_locator_not_order(self):
        opening = paragraph("ALPR policy", number=3)  # Empty paragraphs 1-2 are not emitted.
        later = paragraph("Search log", number=8)
        header = paragraph("Contract", part="word/header1.xml")
        a = kind.classify(**args(native(later, opening, header)))
        b = kind.classify(**args(native(header, opening, later)))
        self.assertEqual((a["kind"], a["confidence"]), ("policy", "medium"))
        self.assertEqual(a["matched_cues"], b["matched_cues"])

    def test_ordinary_workbook_row_one_is_no_header_not_refusal(self):
        signature = ["event_id", "searched_at", "purpose"]
        cases = [[], [42, 3.5], [None], [True, False], ["ordinary", "data"],
                 signature + [42], signature + [None], signature + [False]]
        for values in cases:
            for sheet in ("opaque", "Search log"):
                output = kind.classify(**args(native(workbook(values, sheet=sheet))))
                self.assertEqual(output["kind"], "other" if sheet == "opaque" else "search-log")
                self.assertNotIn("header", {c["channel"] for c in output["matched_cues"]})
        for types in (["f", "s", "s"], ["e", "s", "s"], ["n", "s", "s"]):
            output = kind.classify(**args(native(workbook(signature, types=types))))
            self.assertEqual(output["reason"], "no_decisive_cues")

    def test_malformed_native_headers_fail_closed(self):
        for text in ('{}', '["event_id","searched_at","purpose"]', '[{}]',
                     '[{"cell":"A1","value":[],"type":"s"}]',
                     '[{"cell":null,"value":"event_id","type":"s"}]'):
            test = unit(text, kind_name="workbook_row", locator={"sheet": "opaque", "row": 1})
            self.error("header_shape", args(native(test)))
        for text in ('{}', '[null]', '[1]', '[{"value":"event_id"}]'):
            test = unit(text, kind_name="csv_header", locator={"record": 1})
            self.error("header_shape", args(native(test)))

    def test_csv_header_locator_conflict_and_disabled_rule_controls(self):
        names = ["event_id", "searched_at", "purpose"]
        for values, record in ((names, 2), (names, True), ([], 1), (["event_id", "purpose"], 1)):
            output = kind.classify(**args(native(csv_header(values, record=record))))
            self.assertEqual((output["kind"], output["reason"]), ("other", "no_decisive_cues"))
        output = kind.classify(**args(native(csv_header(names + ["network_id"]))))
        self.assertEqual((output["kind"], output["reason"]), ("other", "conflicting_cues"))
        for header in (csv_header(names), workbook(names)):
            values = args(native(header))
            self.assertEqual(kind.classify(**values)["kind"], "search-log")
            with patch.object(kind, "HEADERS", {}):
                self.assertEqual(kind.classify(**values)["reason"], "no_decisive_cues")
            self.assertEqual(kind.classify(**values)["kind"], "search-log")

    def test_native_workbook_upstream_issue_keeps_cues_informational(self):
        row = workbook(["event_id", "searched_at", "purpose"], sheet="Search log")
        sheet = {"kind": "workbook_sheet", "locator": {"sheet": "Search log"}, "text": "",
                 "data": {"state": "visible", "declared_rows": 1, "declared_columns": 3}}
        receipt = native(sheet, row, status="partial", issues=[{
            "code": "workbook_cached_values_and_objects_not_reviewed"}])
        receipt["route"] = {"route": "extract:xlsx", "format": "xlsx"}
        output = kind.classify(**args(receipt))
        self.assertEqual((output["kind"], output["confidence"], output["reason"]),
                         ("other", "low", "upstream_not_clear"))
        self.assertEqual({c["channel"] for c in output["matched_cues"]}, {"sheet", "header"})
        for flag in ("qualified", "activation_allowed", "publication_ready"):
            self.assertIs(output[flag], False)
        self.assertIs(output["review_required"], True)

    def test_mixed_header_signatures_are_ambiguous(self):
        cells = ["event_id", "searched_at", "purpose", "network_id"]
        test = workbook(cells)
        output = kind.classify(**args(native(test)))
        self.assertEqual((output["kind"], output["reason"]), ("other", "conflicting_cues"))

    def test_partial_blocked_and_issues_never_clear_upstream_hold(self):
        for status, issues in (("partial", []), ("blocked", [{"code": "preserve_pending"}]),
                               ("complete", [{"code": "visual_review_pending"}])):
            values = args(native(unit("Search log"), status=status, issues=issues))
            output = kind.classify(**values)
            self.assertEqual((output["kind"], output["confidence"], output["reason"]),
                             ("other", "low", "upstream_not_clear"))
            self.assertEqual(output["upstream_status"], status)

    def test_missing_malformed_and_mismatched_hashes_fail_closed(self):
        for field, code in (("artifact_sha256", "artifact_hash_required"),
                            ("derived_receipt_sha256", "derived_receipt_hash_required")):
            for value in (None, "", "f" * 63, "F" * 64, True, "f" * 64 + "\n"):
                values = args(); values[field] = value
                self.error(code, values)
        values = args(); values["artifact_sha256"] = "a" * 64
        self.error("artifact_receipt_mismatch", values)
        values = args(); values["derived_receipt_sha256"] = "b" * 64
        self.error("derived_receipt_hash_mismatch", values)

    def test_byte_hash_gate_isolated_negative_and_disabled_control(self):
        values = args()
        values["derived_receipt"] += b" "  # Otherwise-identical valid JSON.
        self.error("derived_receipt_hash_mismatch", values)
        actual_hash = kind.sha256
        altered = values["derived_receipt"]
        def disabled_only_for_altered(raw):
            return values["derived_receipt_sha256"] if raw == altered else actual_hash(raw)
        with patch.object(kind, "sha256", side_effect=disabled_only_for_altered):
            self.assertEqual(kind.classify(**values)["kind"], "search-log")
        self.assertEqual(kind.classify(**args())["kind"], "search-log")

    def test_schema_shape_and_review_claim_refusals(self):
        cases = [("schema", "v2", "derived_receipt_schema"),
                 ("original_changed", True, "artifact_receipt_mismatch"),
                 ("review_status", "approved", "receipt_review_claim"),
                 ("status", "done", "upstream_status_invalid"),
                 ("units", None, "receipt_units_shape"),
                 ("pages", None, "receipt_units_shape"),
                 ("issues", None, "receipt_units_shape")]
        for field, value, code in cases:
            test = native(unit("Search log")); test[field] = value
            self.error(code, args(test))
        for raw, code in ((b'{"schema":"a","schema":"b"}', "duplicate_json_key"),
                          (b'{"value":NaN}', "nonfinite_json"), (b'not-json', "invalid_json")):
            values = args(); values.update(derived_receipt=raw, derived_receipt_sha256=kind.sha256(raw))
            self.error(code, values)

    def test_bounds_and_metadata_validation(self):
        for field, value, code in (("run_id", " ", "run_id_required"),
                                   ("filename", "../policy.pdf", "filename_invalid"),
                                   ("filename", "x\npolicy.pdf", "filename_invalid"),
                                   ("mime", "text/plain\nInjected", "mime_invalid"),
                                   ("derived_receipt", "not bytes", "derived_receipt_bound")):
            values = args(); values[field] = value
            self.error(code, values)
        with patch.object(kind, "MAX_RECEIPT_BYTES", 10):
            self.error("derived_receipt_bound", args())
        with patch.object(kind, "MAX_UNITS", 0):
            self.error("receipt_units_shape", args())
        with patch.object(kind, "MAX_TEXT_BYTES", 1):
            self.error("unit_text_bound", args())
        for test, code in (({"kind": "text"}, "unit_shape"),
                           (unit("x", locator=None) | {"data": []}, "unit_payload_shape"),
                           (unit("[]", kind_name="workbook_row", locator={"sheet": None, "row": 1}), "sheet_locator_invalid"),
                           (unit('{}', kind_name="workbook_row", locator={"sheet": "x", "row": 1}), "header_shape")):
            self.error(code, args(native(test)))

    def test_reordered_identical_jobs_repeat_exact_receipts(self):
        jobs = [args(native(unit(title)), filename=label + ".pdf") for label, title in POSITIVES.items()]
        jobs.extend(args(native(opening)) for opening in (
            workbook(["event_id", "searched_at", "purpose"]),
            csv_header(["event_id", "searched_at", "purpose"]),
            unit("Search log", kind_name="text_line", locator={"line": 1}), paragraph("ALPR policy")))
        def process(values):
            return sorted(kind.canonical_bytes(kind.classify(**item)) for item in values)
        self.assertEqual(process(jobs), process(list(reversed(jobs))))
        self.assertEqual(process(jobs), process(jobs))
        # Caller input is untouched. Exact original receipt bytes remain bound;
        # reserializing/reordering a receipt CHANGES its hash and cannot be hidden.
        snapshot = copy.deepcopy(jobs)
        process(jobs)
        self.assertEqual(jobs, snapshot)
        raw = kind.canonical_bytes(kind.classify(**jobs[0]))
        self.assertTrue(raw.endswith(b"\n") and not raw.endswith(b"\n\n"))
        self.assertNotIn(b"Synthetic cells", raw)

    def test_matched_cues_deduplicate_and_are_order_stable(self):
        first, second = unit("Search log"), unit("Query log")
        a = kind.classify(**args(native(first, second, first)))
        b = kind.classify(**args(native(second, first, first)))
        self.assertEqual(a["matched_cues"], b["matched_cues"])
        self.assertEqual(len(a["matched_cues"]), 2)
        self.assertNotEqual(a["derived_receipt_sha256"], b["derived_receipt_sha256"])


class CliTests(unittest.TestCase):
    def setUp(self):
        self.root = REPO.parent / "out" / ("test-kind-" + uuid.uuid4().hex)
        self.source = self.root / "inputs"
        self.output = self.root / "outputs"
        self.source.mkdir(parents=True)
        self.output.mkdir()
        self.values = args()
        self.receipt = self.source / "extraction.json"
        self.receipt.write_bytes(self.values["derived_receipt"])

    def command(self, **changes):
        options = {"input-root": str(self.source), "receipt": "extraction.json",
                   "derived-receipt-sha256": self.values["derived_receipt_sha256"],
                   "artifact-sha256": SUBJECT, "run-id": RUN_ID,
                   "filename": "search-log.pdf", "mime": "application/pdf",
                   "output-root": str(self.output), **changes}
        argv = [sys.executable, str(CLI), "--copies-only"]
        for name, value in options.items():
            argv += ["--" + name, value]
        return argv

    def invoke(self, **changes):
        result = subprocess.run(self.command(**changes), cwd=REPO, capture_output=True, text=True, timeout=15)
        return result, json.loads(result.stdout)

    def test_cli_actual_save_repeat_no_overwrite_and_unchanged_input(self):
        before = self.receipt.read_bytes()
        first, ref = self.invoke()
        self.assertEqual((first.returncode, first.stderr), (0, ""))
        saved = (self.output / ref["path"]).read_bytes()
        self.assertEqual(hashlib.sha256(saved).hexdigest(), ref["sha256"])
        expected = kind.classify(**{**self.values, "filename": "search-log.pdf", "mime": "application/pdf"})
        self.assertEqual(saved, kind.canonical_bytes(expected))
        second, replay = self.invoke()
        self.assertEqual(second.returncode, 0)
        self.assertNotEqual(ref["path"], replay["path"])
        self.assertEqual((self.output / replay["path"]).read_bytes(), saved)
        self.assertEqual((self.output / ref["path"]).read_bytes(), saved)
        self.assertEqual(self.receipt.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in self.source.iterdir()), ["extraction.json"])

    def test_cli_native_units_saved_receipts(self):
        for opening, expected, mime in (
            (workbook(["event_id", "searched_at", "purpose"]), "search-log", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
            (csv_header(["event_id", "searched_at", "purpose"]), "search-log", "text/csv"),
            (unit("Search log", kind_name="text_line", locator={"line": 1}), "search-log", "text/plain"),
            (paragraph("ALPR policy"), "policy", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")):
            self.values = args(native(opening))
            before = self.values["derived_receipt"]
            self.receipt.write_bytes(before)
            result, ref = self.invoke(filename="opaque", mime=mime)
            self.assertEqual((result.returncode, result.stderr), (0, ""))
            saved = (self.output / ref["path"]).read_bytes()
            output = json.loads(saved)
            self.assertEqual((output["kind"], output["confidence"]), (expected, "medium"))
            self.assertEqual(output["derived_receipt_sha256"], self.values["derived_receipt_sha256"])
            self.assertEqual(hashlib.sha256(saved).hexdigest(), ref["sha256"])
            self.assertEqual(self.receipt.read_bytes(), before)

    def test_hash_failure_creates_no_output(self):
        result, body = self.invoke(**{"derived-receipt-sha256": "0" * 64})
        self.assertEqual((result.returncode, body["error"]), (2, "derived_receipt_hash_mismatch"))
        self.assertEqual(list(self.output.iterdir()), [])

    def test_unsafe_relative_paths_and_overlap_create_no_output(self):
        for name in ("../extraction.json", "/extraction.json", "a//b", "a\\b", "%2e%2e/x", "./extraction.json"):
            result, body = self.invoke(receipt=name)
            self.assertEqual((result.returncode, body["error"]), (2, "receipt_path_invalid"))
        result, body = self.invoke(**{"output-root": str(self.source)})
        self.assertEqual((result.returncode, body["error"]), (2, "input_output_overlap"))
        self.assertEqual(list(self.output.iterdir()), [])

    def test_symlink_leaf_parent_roots_and_nonregular_refused(self):
        (self.source / "alias.json").symlink_to(self.receipt)
        (self.source / "alias-dir").symlink_to(self.source, target_is_directory=True)
        (self.root / "linked-input").symlink_to(self.source, target_is_directory=True)
        (self.root / "linked-output").symlink_to(self.output, target_is_directory=True)
        (self.source / "directory.json").mkdir()
        for changes, code in (({"receipt": "alias.json"}, "receipt_path_not_regular"),
                              ({"receipt": "alias-dir/extraction.json"}, "receipt_path_not_regular"),
                              ({"receipt": "directory.json"}, "receipt_path_not_regular"),
                              ({"input-root": str(self.root / "linked-input")}, "root_not_real_directory"),
                              ({"output-root": str(self.root / "linked-output")}, "root_not_real_directory")):
            result, body = self.invoke(**changes)
            self.assertEqual((result.returncode, body["error"]), (2, code))
        self.assertEqual(list(self.output.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
