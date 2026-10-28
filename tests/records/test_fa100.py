"""FA100: OCR receipt page identities must be exact positive integers.

identity.page, locator.page and (when present) eligibility.page were compared by ordinary
equality, so a canonically rehashed receipt whose page was ``true`` or ``1.0`` was accepted
as page 1. The correction requires ``type(page) is int and page >= 1`` before any numeric
comparison or artifact read, with the existing refusal codes:

  load_page_receipt, malformed identity.page     -> ocr_receipt_identity_mismatch
  _validate_receipt/_load_receipt identity/locator -> ocr_receipt_state_mismatch
  eligibility.page                                -> ocr_eligibility_binding_mismatch

Every fixture is a real producer receipt (extract_image_only_pages with an injected fake
runner) under a fresh tempfile root; a variant copies that output, changes ONE page field
and recomputes the canonical receipt/manifest bytes (and, for identity, receipt_id and the
directory name). Nothing here runs OCR tools, reads records or uses the network.

Behavior     A1: producer statuses, aliases at every entry point, restart/cache replay
GateControl  A3: an in-memory copy of the staged module with ONLY the exact-page predicate
             reverted to equality-only must accept true/1.0 aliases (regression fails)
             while the corrected module refuses them; valid producer receipts pass both.
"""
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import types
import unittest
from unittest.mock import patch

from campaign_tool.records.extract import ocr
from tests.records import ocr_fixtures

IDENTITY = "ocr_receipt_identity_mismatch"
STATE = "ocr_receipt_state_mismatch"
ELIGIBILITY = "ocr_eligibility_binding_mismatch"
# The exact predicate the correction adds; GateControl reverts only this line in a copy.
GUARD_TEXT = "    return type(value) is int and value >= 1\n"
CONTROL_TEXT = "    return True\n"
CONTROL_OUTCOMES = []  # read by tools/accept_fa100.py for the negative-control case

# base name -> (pdf kind, page, runner kind, fidelity holds, expected status, eligibility present)
BASES = {
    "blocked": ("blank", 1, "missing", (), "blocked", True),
    "skipped_machine_text": ("mixed", 1, "fake97", (), "skipped_machine_text", True),
    "ocr_text_unreviewed": ("blank", 1, "fake97", (), "ocr_text_unreviewed", True),
    "visual_check_queued": ("blank", 1, "fake42", (), "visual_check_queued", True),
    "legacy_hold_no_eligibility": ("blank", 1, "fake97", (1,), "blocked", False),
    "later_page_unreviewed": ("blank", 2, "fake97", (), "ocr_text_unreviewed", True),
}
ALIASES = ("bool_true", "float_same")


def bad_values(page):
    """Malformed page values for a receipt whose true page is ``page``."""
    return {"bool_true": True, "bool_false": False, "float_same": float(page), "null": None,
            "string": str(page), "list": [page], "object": {"page": page}, "zero": 0, "negative": -1}


def canonical(value):
    """Independent statement of the receipt byte format (sorted keys, compact, ASCII, newline)."""
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode("ascii")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def tree(root):
    root = Path(root)
    found = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames:
            found[str((Path(dirpath) / name).relative_to(root))] = "<dir>"
        for name in filenames:
            path = Path(dirpath) / name
            found[str(path.relative_to(root))] = "<symlink>" if path.is_symlink() else path.read_bytes()
    return found


def blank_pdf():
    from pypdf import PdfWriter
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.add_blank_page(width=72, height=72)
    stream = io.BytesIO()
    writer.write(stream)
    return stream.getvalue()


def missing_tools(command, **kwargs):
    raise FileNotFoundError(command[0])


def runner_for(kind):
    if kind == "missing":
        return missing_tools
    return ocr_fixtures.FakeOCRRunner(confidence="97" if kind == "fake97" else "42")


class Fixture:
    """A fresh synthetic intake root with one preserved blob and a private OCR output."""

    def __init__(self, testcase, base):
        self.base = base
        pdf_kind, self.page, self.runner_kind, self.holds, self.status, self.eligible = BASES[base]
        self.temp = tempfile.TemporaryDirectory()
        testcase.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "blobs").mkdir()
        raw = ocr_fixtures.mixed_pdf() if pdf_kind == "mixed" else blank_pdf()
        self.source = sha(raw)
        (self.root / "blobs" / self.source).write_bytes(raw)
        self.output = self.root / "private-ocr"
        self.copies = 0
        self.receipt = None  # set by produce()

    def call(self, runner, output=None):
        return ocr.extract_image_only_pages(self.root, self.source, output or self.output,
                                            tool_signature="stub-v1", tools=ocr_fixtures.TOOLS,
                                            runner=runner, pages=(self.page,), fidelity_holds=self.holds)

    def directory(self, output, receipt_id):
        return Path(output) / self.source / f"page-{self.page:06d}" / receipt_id

    def variant(self, field, value):
        """Copy the producer output, change ONE page field and rewrite canonical bytes."""
        self.copies += 1
        output = self.root / ("variant-%03d" % self.copies)
        shutil.copytree(self.output, output, symlinks=True)
        directory = self.directory(output, self.receipt["receipt_id"])
        receipt = json.loads((directory / "receipt.json").read_bytes())
        if field == "identity":
            receipt["identity"] = dict(receipt["identity"], page=value)
            receipt["receipt_id"] = sha(canonical(receipt["identity"]))
            renamed = directory.parent / receipt["receipt_id"]
            os.rename(directory, renamed)
            directory = renamed
        elif field == "locator":
            receipt["locator"] = {"page": value}
        elif field == "eligibility":
            receipt["eligibility"] = dict(receipt["eligibility"], page=value)
        elif field == "page_count":
            receipt["page_count"] = value
        else:
            raise AssertionError("unknown field " + field)
        receipt_bytes = canonical(receipt)
        manifest = {"schema_version": ocr.VERSION, "receipt_sha256": sha(receipt_bytes),
                    "artifact_sha256": receipt["artifact_sha256"]}
        (directory / "receipt.json").write_bytes(receipt_bytes)
        (directory / "manifest.json").write_bytes(canonical(manifest))
        return output, directory, receipt


def produce(testcase, base):
    fixture = Fixture(testcase, base)
    receipts = fixture.call(runner_for(fixture.runner_kind))
    testcase.assertEqual(len(receipts), 1)
    receipt = receipts[0]
    testcase.assertEqual(receipt["status"], fixture.status)
    testcase.assertEqual(receipt["locator"], {"page": fixture.page})
    testcase.assertIs(type(receipt["identity"]["page"]), int)
    testcase.assertEqual("eligibility" in receipt, fixture.eligible)
    fixture.receipt = receipt
    return fixture, receipt


def fields_for(fixture):
    return ("identity", "locator", "eligibility") if fixture.eligible else ("identity", "locator")


class Spy:
    """Records every _read leaf name and Path.iterdir call made during one call."""

    def __init__(self, module):
        self.module = module
        self.reads = []
        self.iterdirs = 0

    def __enter__(self):
        original_read = self.module._read
        original_iterdir = Path.iterdir

        def read(path, *args, **kwargs):
            self.reads.append(Path(path).name)
            return original_read(path, *args, **kwargs)

        def iterdir(path):
            self.iterdirs += 1
            return original_iterdir(path)

        self.patches = [patch.object(self.module, "_read", side_effect=read),
                        patch.object(Path, "iterdir", autospec=True, side_effect=iterdir)]
        for item in self.patches:
            item.start()
        return self

    def __exit__(self, *exc):
        for item in reversed(self.patches):
            item.stop()
        return False


def refuse(testcase, module, code, call, allowed_reads):
    """Exact ValueError(code), no artifact read or directory traversal, nothing changed."""
    with Spy(module) as spy:
        with testcase.assertRaises(ValueError) as caught:
            call()
    testcase.assertIs(type(caught.exception), ValueError)
    testcase.assertEqual(caught.exception.args, (code,))
    testcase.assertEqual(spy.iterdirs, 0)
    testcase.assertEqual(spy.reads, list(allowed_reads))
    return spy


LOADER_READS = {  # entry point -> field -> reads expected before the refusal
    "load_page_receipt": {"identity": ("receipt.json",),
                          "locator": ("receipt.json", "manifest.json", "receipt.json"),
                          "eligibility": ("receipt.json", "manifest.json", "receipt.json")},
    "_load_receipt": {"identity": ("manifest.json", "receipt.json"),
                      "locator": ("manifest.json", "receipt.json"),
                      "eligibility": ("manifest.json", "receipt.json")},
}
PUBLIC_CODE = {"identity": IDENTITY, "locator": STATE, "eligibility": ELIGIBILITY}
INTERNAL_CODE = {"identity": STATE, "locator": STATE, "eligibility": ELIGIBILITY}


class Behavior(unittest.TestCase):
    """A1: exact positive-int page identities at every receipt entry point."""

    def test_valid_producer_receipts_validate_and_load_byte_identical(self):
        covered = set()
        for base in BASES:
            with self.subTest(base=base):
                fixture, receipt = produce(self, base)
                final = fixture.directory(fixture.output, receipt["receipt_id"])
                receipt_bytes = (final / "receipt.json").read_bytes()
                manifest_bytes = (final / "manifest.json").read_bytes()
                self.assertEqual(receipt_bytes, canonical(receipt))
                identity = receipt["identity"]
                before = tree(fixture.output)
                self.assertEqual(ocr._validate_receipt(receipt, identity, final),
                                 (sha(canonical(identity)), sha(receipt_bytes)))
                self.assertEqual(ocr._load_receipt(final, identity),
                                 (receipt, sha(receipt_bytes), sha(manifest_bytes)))
                loaded = ocr.load_page_receipt(fixture.output, fixture.source, fixture.page, receipt["receipt_id"])
                self.assertEqual(loaded["receipt"], receipt)
                self.assertEqual(loaded["receipt_bytes"], receipt_bytes)
                self.assertEqual((loaded["receipt_sha256"], loaded["manifest_sha256"]),
                                 (sha(receipt_bytes), sha(manifest_bytes)))
                self.assertEqual(loaded["sidecar"] is None, "sidecar.txt" not in receipt["artifact_sha256"])
                self.assertEqual((receipt["review_status"], receipt["visual_check_status"],
                                  receipt["fidelity_status"]), ("not_reviewed", "not_done", "not_checked"))
                self.assertEqual(tree(fixture.output), before)
                covered.add(receipt["status"])
        self.assertEqual(covered, ocr.STATES)

    def test_identity_page_malformed_is_refused_by_public_loader(self):
        for base in BASES:
            fixture, receipt = produce(self, base)
            original = tree(fixture.output)
            for kind, value in bad_values(fixture.page).items():
                with self.subTest(base=base, value=kind):
                    output, directory, variant = fixture.variant("identity", value)
                    before = tree(output)
                    refuse(self, ocr, IDENTITY, lambda: ocr.load_page_receipt(
                        output, fixture.source, fixture.page, variant["receipt_id"]),
                        LOADER_READS["load_page_receipt"]["identity"])
                    self.assertEqual(tree(output), before)
            self.assertEqual(tree(fixture.output), original)

    def test_identity_page_malformed_is_refused_by_validator_and_internal_loader(self):
        for base in BASES:
            fixture, receipt = produce(self, base)
            for kind, value in bad_values(fixture.page).items():
                with self.subTest(base=base, value=kind):
                    output, directory, variant = fixture.variant("identity", value)
                    before = tree(output)
                    for identity in (variant["identity"], receipt["identity"]):
                        refuse(self, ocr, STATE, lambda: ocr._validate_receipt(
                            copy.deepcopy(variant), identity, directory), ())
                    refuse(self, ocr, STATE, lambda: ocr._load_receipt(directory, variant["identity"]),
                           LOADER_READS["_load_receipt"]["identity"])
                    self.assertEqual(tree(output), before)

    def test_locator_page_malformed_is_refused_at_every_entry_point(self):
        for base in BASES:
            fixture, receipt = produce(self, base)
            for kind, value in bad_values(fixture.page).items():
                with self.subTest(base=base, value=kind):
                    output, directory, variant = fixture.variant("locator", value)
                    before = tree(output)
                    identity = receipt["identity"]
                    refuse(self, ocr, STATE, lambda: ocr._validate_receipt(
                        copy.deepcopy(variant), identity, directory), ())
                    refuse(self, ocr, STATE, lambda: ocr._load_receipt(directory, identity),
                           LOADER_READS["_load_receipt"]["locator"])
                    refuse(self, ocr, STATE, lambda: ocr.load_page_receipt(
                        output, fixture.source, fixture.page, receipt["receipt_id"]),
                        LOADER_READS["load_page_receipt"]["locator"])
                    self.assertEqual(tree(output), before)

    def test_eligibility_page_malformed_is_refused_when_eligibility_exists(self):
        checked = 0
        for base in BASES:
            fixture, receipt = produce(self, base)
            if not fixture.eligible:
                self.assertNotIn("eligibility", receipt)
                continue
            for kind, value in bad_values(fixture.page).items():
                with self.subTest(base=base, value=kind):
                    output, directory, variant = fixture.variant("eligibility", value)
                    before = tree(output)
                    identity = receipt["identity"]
                    refuse(self, ocr, ELIGIBILITY, lambda: ocr._validate_receipt(
                        copy.deepcopy(variant), identity, directory), ())
                    refuse(self, ocr, ELIGIBILITY, lambda: ocr._load_receipt(directory, identity),
                           LOADER_READS["_load_receipt"]["eligibility"])
                    refuse(self, ocr, ELIGIBILITY, lambda: ocr.load_page_receipt(
                        output, fixture.source, fixture.page, receipt["receipt_id"]),
                        LOADER_READS["load_page_receipt"]["eligibility"])
                    self.assertEqual(tree(output), before)
                    checked += 1
        self.assertEqual(checked, 5 * len(bad_values(1)))

    def test_later_positive_page_passes_and_its_float_alias_is_refused(self):
        fixture, receipt = produce(self, "later_page_unreviewed")
        self.assertEqual(receipt["locator"], {"page": 2})
        loaded = ocr.load_page_receipt(fixture.output, fixture.source, 2, receipt["receipt_id"])
        self.assertEqual(loaded["receipt"], receipt)
        for field in fields_for(fixture):
            with self.subTest(field=field):
                output, directory, variant = fixture.variant(field, 2.0)
                refuse(self, ocr, PUBLIC_CODE[field], lambda: ocr.load_page_receipt(
                    output, fixture.source, 2, variant["receipt_id"]), LOADER_READS["load_page_receipt"][field])

    def test_restart_and_cache_replay_refuse_persisted_aliases(self):
        for base in ("blocked", "skipped_machine_text", "visual_check_queued", "legacy_hold_no_eligibility"):
            fixture, receipt = produce(self, base)
            for field in fields_for(fixture):
                for kind in ALIASES:
                    value = bad_values(fixture.page)[kind]
                    with self.subTest(base=base, field=field, value=kind):
                        output, directory, variant = fixture.variant(field, value)
                        before = tree(output)
                        with self.assertRaises(ValueError) as caught:
                            ocr.reconcile_pages(output, fixture.source)
                        self.assertEqual(caught.exception.args, (INTERNAL_CODE[field],))
                        rerun = ocr_fixtures.FakeOCRRunner(confidence="97")
                        with self.assertRaises(ValueError) as caught:
                            fixture.call(rerun, output)
                        self.assertEqual(caught.exception.args, (INTERNAL_CODE[field],))
                        self.assertEqual(rerun.calls, [])
                        self.assertEqual(tree(output), before)

    def test_valid_receipts_replay_from_cache_unchanged(self):
        for base in BASES:
            with self.subTest(base=base):
                fixture, receipt = produce(self, base)
                before = tree(fixture.output)
                rerun = ocr_fixtures.FakeOCRRunner(confidence="97")
                self.assertEqual(fixture.call(rerun), [receipt])
                self.assertEqual(rerun.calls, [])
                self.assertEqual(ocr.reconcile_pages(fixture.output, fixture.source)["published_receipts"], 1)
                self.assertEqual(tree(fixture.output), before)

    def test_page_count_exact_integer_bound_is_unchanged(self):
        fixture, receipt = produce(self, "later_page_unreviewed")
        for value in (True, 2.0, 1, None):
            with self.subTest(page_count=value):
                output, directory, variant = fixture.variant("page_count", value)
                refuse(self, ocr, STATE, lambda: ocr._validate_receipt(
                    copy.deepcopy(variant), receipt["identity"], directory), ())

    def test_authored_sources_contain_no_machine_paths(self):
        root = Path(__file__).resolve().parents[2]
        tokens = ("/ho" + "me/", "agent" + "-work", "/Use" + "rs/", ".her" + "mes")
        for relative in ("tests/records/test_fa100.py", "tools/accept_fa100.py"):
            text = (root / relative).read_text(encoding="utf-8")
            for token in tokens:
                with self.subTest(file=relative, token=token):
                    self.assertNotIn(token, text)


def control_module():
    """The complete staged ocr.py with ONLY the exact-page predicate reverted, in memory."""
    source = Path(ocr.__file__).read_text(encoding="utf-8")
    if source.count(GUARD_TEXT) != 1:
        raise AssertionError("FA100 predicate present %d times in the staged module" % source.count(GUARD_TEXT))
    reverted = source.replace(GUARD_TEXT, CONTROL_TEXT, 1)
    module = types.ModuleType("fa100_control_ocr")
    module.__file__ = ocr.__file__
    exec(compile(reverted, "<fa100-control>", "exec"), module.__dict__)
    return module


class GateControl(unittest.TestCase):
    """A3: equality-only page validation must accept true/1.0 aliases; the fix refuses them."""

    def regression(self, module, fixture, field, value):
        output, directory, variant = fixture.variant(field, value)
        try:
            refuse(self, module, PUBLIC_CODE[field], lambda: module.load_page_receipt(
                output, fixture.source, fixture.page, variant["receipt_id"]),
                LOADER_READS["load_page_receipt"][field])
        except AssertionError as error:
            accepted = None
            try:
                accepted = module.load_page_receipt(output, fixture.source, fixture.page, variant["receipt_id"])
            except ValueError:
                pass
            return ("regression_failed_alias_accepted" if accepted is not None
                    and accepted["receipt"]["identity"] == variant["identity"] else "regression_failed_other"), \
                str(error)[:300]
        return "refused", ""

    def test_equality_only_control_accepts_aliases_and_corrected_module_refuses(self):
        control = control_module()
        self.assertIsNot(control.load_page_receipt, ocr.load_page_receipt)
        fixture, receipt = produce(self, "visual_check_queued")
        retained = tree(fixture.output)
        start = len(CONTROL_OUTCOMES)
        for name, module in (("control", control), ("staged", ocr)):
            for field in ("identity", "locator", "eligibility"):
                for kind in ALIASES:
                    outcome, detail = self.regression(module, fixture, field, bad_values(1)[kind])
                    CONTROL_OUTCOMES.append({"module": name, "case": field + ":" + kind,
                                             "outcome": outcome, "detail": detail})
            try:
                loaded = module.load_page_receipt(fixture.output, fixture.source, 1, receipt["receipt_id"])
                outcome = "passed" if loaded["receipt"] == receipt else "mismatch"
            except Exception as error:  # recorded, then asserted below
                outcome = type(error).__name__
            CONTROL_OUTCOMES.append({"module": name, "case": "valid_producer", "outcome": outcome, "detail": ""})
        observed = {(o["module"], o["case"]): o["outcome"] for o in CONTROL_OUTCOMES[start:]}
        for field in ("identity", "locator", "eligibility"):
            for kind in ALIASES:
                self.assertEqual(observed[("control", field + ":" + kind)], "regression_failed_alias_accepted")
                self.assertEqual(observed[("staged", field + ":" + kind)], "refused")
        self.assertEqual(observed[("control", "valid_producer")], "passed")
        self.assertEqual(observed[("staged", "valid_producer")], "passed")
        self.assertEqual(tree(fixture.output), retained)


if __name__ == "__main__":
    unittest.main()
