"""FA50: malformed OCR receipt status values are refused through the existing typed refusal.

_validate_receipt must require a str status before testing membership in STATES, so
list/object (unhashable) values raise ValueError("ocr_receipt_state_mismatch") instead of
TypeError, before any artifact directory traversal or read. All fixtures are synthetic and
are produced by the real OCR producer (extract_image_only_pages) with injected fake tools,
under fresh tempfile roots; nothing here reads records, runs OCR tools or uses the network.

A1  A1ProducerStatuses   every current status from real producer receipts, exact artifact sets
A2  A2MalformedStatus    list/object/null/bool/int/float/unknown status -> exact typed refusal
A3  A3PersistedReplay    persisted malformed status refused by _load_receipt, load_page_receipt,
                         reconcile_pages and a cached rerun, without publication or replacement
A4  A4GateControl        an isolated copy of the staged module with ONLY the new guard removed
                         must turn list/object refusals into TypeError (control detected)
A5  A5Portability        authored sources carry no machine paths; module identity comes from
                         one source tree; fixture roots stay below the active tempfile root;
                         the runner's child-command guard refuses the pinned inline -c child
                         shape and permits file/module children (inert: nothing is launched)
"""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from campaign_tool.records.extract import ocr
from tests.records import ocr_fixtures
from tests.records import test_ocr as pinned  # module import only: its OCRTests are not collected here

CODE = "ocr_receipt_state_mismatch"
FROZEN_STATES = {"blocked", "skipped_machine_text", "ocr_text_unreviewed", "visual_check_queued"}
# The exact guard text the product correction adds; A4 removes only this text in an isolated copy.
GUARD_TEXT = 'type(receipt["status"]) is not str or '
LIST_MARK = "fa50-list-status-marker"
OBJECT_MARK = "fa50-object-status-marker"
UNKNOWN_MARK = "fa50-unknown-status-marker"
# status kind -> (malformed value, payload marker that must never be reflected)
MALFORMED = {
    "list": ([LIST_MARK], LIST_MARK),
    "object": ({OBJECT_MARK: "blocked"}, OBJECT_MARK),
    "null": (None, None),
    "bool_true": (True, None),
    "bool_false": (False, None),
    "int_zero": (0, None),
    "int_one": (1, None),
    "float": (1.5, None),
    "unknown_string": (UNKNOWN_MARK, UNKNOWN_MARK),
    "case_variant": ("BLOCKED", None),
    "padded_state": ("blocked ", None),
}
UNHASHABLE = ("list", "object")
CONTROL_OUTCOMES = []  # read by tools/accept_fa50_ocr_state.py for the negative-control case


def canonical(value):
    """Independent statement of the receipt byte format (sorted keys, compact, ASCII, newline)."""
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode("ascii")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def tree(root):
    """Every regular file and directory below root with its bytes; no symlinks expected."""
    root = Path(root)
    found = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames:
            path = Path(dirpath) / name
            found[str(path.relative_to(root))] = "<dir>" if not path.is_symlink() else "<symlink>"
        for name in filenames:
            path = Path(dirpath) / name
            found[str(path.relative_to(root))] = "<symlink>" if path.is_symlink() else path.read_bytes()
    return found


class Producer:
    """The pinned test_ocr cache/reuse fixture (blobs + private-ocr + FakeRunner + stub-v1 calls)."""

    def __init__(self, testcase, pdf=None):
        fixture = pinned.OCRTests("test_low_confidence_receipt_index_and_idempotent_rerun")
        fixture.setUp()
        testcase.addCleanup(fixture.doCleanups)
        if pdf is not None:  # a second synthetic blob beside the fixture's own; nothing is removed
            fixture.raw = pdf
            fixture.sha = sha(pdf)
            (fixture.root / "blobs" / fixture.sha).write_bytes(pdf)
        self.fixture = fixture
        self.root = fixture.root
        self.output = fixture.output
        self.source = fixture.sha

    def call(self, runner, **kwargs):
        return self.fixture.call(runner, **kwargs)

    def final(self, receipt):
        return self.fixture.final(receipt)


def missing_tools(command, **kwargs):
    raise FileNotFoundError(command[0])


def produce(testcase, status):
    """One real producer receipt with the requested status, plus its fixture."""
    if status == "skipped_machine_text":
        producer = Producer(testcase, ocr_fixtures.mixed_pdf())
        receipt = producer.call(pinned.FakeRunner(), pages=(1,))[0]
    elif status == "blocked":
        producer = Producer(testcase)
        receipt = producer.call(missing_tools, pages=(1,))[0]
    elif status == "ocr_text_unreviewed":
        producer = Producer(testcase)
        receipt = producer.call(pinned.FakeRunner("97"), pages=(1,))[0]
    elif status == "visual_check_queued":
        producer = Producer(testcase)
        receipt = producer.call(pinned.FakeRunner("42"), pages=(1,))[0]
    else:
        raise AssertionError("unknown fixture status " + repr(status))
    testcase.assertEqual(receipt["status"], status)
    return producer, receipt


EXPECTED = {  # status -> (artifact names, confidence, eligibility decision, blocked_reason)
    "blocked": ({"page.pdf"}, None, "ocr_required", "local_ocr_dependency_or_output_missing"),
    "skipped_machine_text": (set(), None, "native_sufficient", None),
    "ocr_text_unreviewed": (set(ocr.ARTIFACTS), 97.0, "ocr_required", None),
    "visual_check_queued": (set(ocr.ARTIFACTS), 42.0, "ocr_required", None),
}


def malformed_variant(receipt, value):
    """Same receipt with only the status replaced; the visual flag stays consistent with
    'not visual_check_queued' so no other state clause decides the refusal."""
    variant = copy.deepcopy(receipt)
    variant["status"] = value
    variant["visual_check_required"] = False
    return variant


def check_state_refusal(testcase, module, receipt, identity, directory, marker):
    """Shared A2/A4 assertion: exact typed refusal, no traversal/read, nothing changed."""
    before_tree = tree(directory)
    before_receipt = repr(receipt)
    real_iterdir = Path.iterdir
    with patch.object(module, "_read", wraps=module._read) as read_spy, \
            patch.object(module, "_directory", wraps=module._directory) as directory_spy, \
            patch.object(Path, "iterdir", autospec=True, side_effect=real_iterdir) as iterdir_spy, \
            patch.object(os, "listdir", wraps=os.listdir) as listdir_spy, \
            patch.object(os, "scandir", wraps=os.scandir) as scandir_spy:
        with testcase.assertRaises(ValueError) as caught:
            module._validate_receipt(receipt, identity, directory)
    error = caught.exception
    testcase.assertIs(type(error), ValueError)
    testcase.assertEqual(error.args, (CODE,))
    testcase.assertIsNone(error.__cause__)
    testcase.assertIsNone(error.__context__)
    if marker is not None:
        testcase.assertNotIn(marker, str(error))
        testcase.assertNotIn(marker, repr(error))
    testcase.assertEqual((read_spy.call_count, directory_spy.call_count, iterdir_spy.call_count,
                          listdir_spy.call_count, scandir_spy.call_count), (0, 0, 0, 0, 0))
    testcase.assertEqual(tree(directory), before_tree)
    testcase.assertEqual(repr(receipt), before_receipt)


class A1ProducerStatuses(unittest.TestCase):
    """A1: all four statuses from actual producer receipts keep validating and replaying."""

    def test_a1_status_vocabulary_is_unchanged(self):
        self.assertEqual(ocr.STATES, FROZEN_STATES)

    def test_a1_every_producer_status_validates_with_its_exact_artifact_set(self):
        covered = set()
        for status in sorted(FROZEN_STATES):
            with self.subTest(status=status):
                producer, receipt = produce(self, status)
                final = producer.final(receipt)
                names, confidence, decision, reason = EXPECTED[status]
                receipt_bytes = (final / "receipt.json").read_bytes()
                manifest_bytes = (final / "manifest.json").read_bytes()
                self.assertEqual(receipt_bytes, canonical(receipt))
                self.assertEqual(set(receipt["artifact_sha256"]), names)
                self.assertEqual({p.name for p in final.iterdir()}, names | {"receipt.json", "manifest.json"})
                for name, digest in receipt["artifact_sha256"].items():
                    self.assertEqual(sha((final / name).read_bytes()), digest)
                self.assertEqual(receipt["confidence"], confidence)
                self.assertEqual(receipt["blocked_reason"], reason)
                self.assertIs(receipt["visual_check_required"], status == "visual_check_queued")
                self.assertEqual(receipt["review_status"], "not_reviewed")
                self.assertEqual(receipt["visual_check_status"], "not_done")
                self.assertEqual(receipt["fidelity_status"], "not_checked")
                eligibility = receipt["eligibility"]
                self.assertEqual((eligibility["source_sha256"], eligibility["page"], eligibility["decision"]),
                                 (producer.source, 1, decision))
                identity = receipt["identity"]
                self.assertEqual(ocr._validate_receipt(receipt, identity, final),
                                 (sha(canonical(identity)), sha(receipt_bytes)))
                self.assertEqual(ocr._load_receipt(final, identity),
                                 (receipt, sha(receipt_bytes), sha(manifest_bytes)))
                loaded = ocr.load_page_receipt(producer.output, producer.source, 1, receipt["receipt_id"])
                self.assertEqual((loaded["receipt"], loaded["receipt_bytes"]), (receipt, receipt_bytes))
                covered.add(status)
        self.assertEqual(covered, FROZEN_STATES)

    def test_a1_valid_receipts_replay_from_cache_with_unchanged_hashes(self):
        for status in sorted(FROZEN_STATES):
            with self.subTest(status=status):
                producer, receipt = produce(self, status)
                before = tree(producer.output)
                again = pinned.FakeRunner("97")
                pages = (1,)
                self.assertEqual(producer.call(again, pages=pages), [receipt])
                self.assertEqual(again.calls, [])
                self.assertEqual(tree(producer.output), before)


class A2MalformedStatus(unittest.TestCase):
    """A2: each malformed status on an otherwise-valid producer receipt is refused exactly."""

    def test_a2_malformed_status_is_refused_before_artifact_traversal(self):
        for status in sorted(FROZEN_STATES):
            producer, receipt = produce(self, status)
            final = producer.final(receipt)
            retained = tree(producer.output)
            for kind, (value, marker) in MALFORMED.items():
                with self.subTest(base=status, status_kind=kind):
                    check_state_refusal(self, ocr, malformed_variant(receipt, value), receipt["identity"],
                                        final, marker)
            self.assertEqual(tree(producer.output), retained)

    def test_a2_missing_status_stays_a_schema_mismatch(self):
        producer, receipt = produce(self, "blocked")
        variant = copy.deepcopy(receipt)
        del variant["status"]
        with patch.object(ocr, "_read", wraps=ocr._read) as read_spy, self.assertRaises(ValueError) as caught:
            ocr._validate_receipt(variant, receipt["identity"], producer.final(receipt))
        self.assertEqual(caught.exception.args, ("ocr_receipt_schema_mismatch",))
        self.assertEqual(read_spy.call_count, 0)

    def test_a2_unrelated_schema_and_binding_refusals_keep_their_order(self):
        producer, receipt = produce(self, "blocked")
        final = producer.final(receipt)
        extra = malformed_variant(receipt, [LIST_MARK])
        extra["unexpected_key"] = True
        with self.assertRaises(ValueError) as caught:
            ocr._validate_receipt(extra, receipt["identity"], final)
        self.assertEqual(caught.exception.args, ("ocr_receipt_schema_mismatch",))
        bad_binding = malformed_variant(receipt, {OBJECT_MARK: "blocked"})
        bad_binding["eligibility"] = dict(bad_binding["eligibility"], page=2)
        with self.assertRaises(ValueError) as caught:
            ocr._validate_receipt(bad_binding, receipt["identity"], final)
        self.assertEqual(caught.exception.args, ("ocr_eligibility_binding_mismatch",))
        reason = copy.deepcopy(receipt)
        reason["blocked_reason"] = None  # a valid status still reaches the later blocked-reason check
        with self.assertRaises(ValueError) as caught:
            ocr._validate_receipt(reason, receipt["identity"], final)
        self.assertEqual(caught.exception.args, ("ocr_receipt_blocked_reason_mismatch",))


def persist_status(final, value):
    """Rewrite receipt.json + manifest.json so ONLY the status (and its consistent visual flag)
    differs from the producer's otherwise-valid canonical bytes."""
    receipt = json.loads((final / "receipt.json").read_bytes())
    variant = malformed_variant(receipt, value)
    receipt_bytes = canonical(variant)
    manifest = {"schema_version": ocr.VERSION, "receipt_sha256": sha(receipt_bytes),
                "artifact_sha256": variant["artifact_sha256"]}
    (final / "receipt.json").write_bytes(receipt_bytes)
    (final / "manifest.json").write_bytes(canonical(manifest))
    return variant


class A3PersistedReplay(unittest.TestCase):
    """A3: real consumers refuse a persisted malformed status; nothing is published or replaced."""

    def assert_refused(self, call):
        with self.assertRaises(ValueError) as caught:
            call()
        self.assertIs(type(caught.exception), ValueError)
        self.assertEqual(caught.exception.args, (CODE,))

    def test_a3_persisted_malformed_status_is_refused_by_every_consumer(self):
        for base in ("blocked", "visual_check_queued"):
            for kind in ("list", "object", "null", "bool_true", "int_one", "float", "unknown_string"):
                value, _ = MALFORMED[kind]
                with self.subTest(base=base, status_kind=kind):
                    producer, receipt = produce(self, base)
                    final = producer.final(receipt)
                    variant = persist_status(final, value)
                    retained = tree(producer.output)
                    identity = receipt["identity"]
                    self.assert_refused(lambda: ocr._load_receipt(final, identity))
                    self.assert_refused(lambda: ocr.load_page_receipt(
                        producer.output, producer.source, 1, receipt["receipt_id"]))
                    self.assert_refused(lambda: ocr.reconcile_pages(producer.output, producer.source))
                    rerun = pinned.FakeRunner("97")
                    self.assert_refused(lambda: producer.call(rerun, pages=(1,)))
                    self.assertEqual(rerun.calls, [])
                    after = tree(producer.output)
                    self.assertEqual(after, retained)
                    self.assertEqual(after[str((final / "receipt.json").relative_to(producer.output))],
                                     canonical(variant))

    def test_a3_valid_receipt_cache_reuse_still_works(self):
        for base in ("blocked", "visual_check_queued"):
            with self.subTest(base=base):
                producer, receipt = produce(self, base)
                retained = tree(producer.output)
                rerun = pinned.FakeRunner("97")
                self.assertEqual(producer.call(rerun, pages=(1,)), [receipt])
                self.assertEqual(rerun.calls, [])
                self.assertEqual(ocr.reconcile_pages(producer.output, producer.source)["published_receipts"], 1)
                self.assertEqual(tree(producer.output), retained)


def control_module():
    """The complete staged ocr.py with ONLY the FA50 guard text removed, as an isolated module."""
    source = Path(ocr.__file__).read_text(encoding="utf-8")
    if source.count(GUARD_TEXT) != 1:
        raise AssertionError("FA50 guard text present %d times in the staged module" % source.count(GUARD_TEXT))
    broken = source.replace(GUARD_TEXT, "", 1)
    if len(source) - len(broken) != len(GUARD_TEXT):
        raise AssertionError("control removed more than the guard")
    module = types.ModuleType("fa50_control_ocr")
    module.__file__ = ocr.__file__
    exec(compile(broken, "<fa50-control " + ocr.__file__ + ">", "exec"), module.__dict__)
    return module


class A4GateControl(unittest.TestCase):
    """A4: disabling only the new guard must make list/object fail with TypeError."""

    def outcome(self, module, receipt, identity, directory, marker):
        try:
            check_state_refusal(self, module, receipt, identity, directory, marker)
        except AssertionError:
            return "assertion_failed"
        except TypeError:
            return "TypeError"
        return "passed"

    def test_a4_disabled_guard_is_detected_and_valid_control_still_passes(self):
        control = control_module()
        self.assertIsNot(control, ocr)
        self.assertIsNot(control._validate_receipt, ocr._validate_receipt)
        producer, receipt = produce(self, "blocked")
        final = producer.final(receipt)
        identity = receipt["identity"]
        retained = tree(producer.output)
        expected_valid = ocr._validate_receipt(copy.deepcopy(receipt), identity, final)
        for module_name, module in (("control", control), ("staged", ocr)):
            for kind in UNHASHABLE:
                value, marker = MALFORMED[kind]
                result = self.outcome(module, malformed_variant(receipt, value), identity, final, marker)
                CONTROL_OUTCOMES.append({"module": module_name, "case": kind, "outcome": result})
            reads = []
            original = module._read
            with patch.object(module, "_read", side_effect=lambda *a, **k: reads.append(a) or original(*a, **k)):
                try:
                    valid = module._validate_receipt(copy.deepcopy(receipt), identity, final)
                    result = "passed" if valid == expected_valid else "mismatch"
                except Exception as error:  # recorded, then asserted below
                    result = type(error).__name__
            CONTROL_OUTCOMES.append({"module": module_name, "case": "valid_blocked", "outcome": result,
                                     "artifact_reads": len(reads)})
        by_case = {(o["module"], o["case"]): o for o in CONTROL_OUTCOMES[-6:]}
        for kind in UNHASHABLE:
            self.assertEqual(by_case[("control", kind)]["outcome"], "TypeError")
            self.assertEqual(by_case[("staged", kind)]["outcome"], "passed")
        for module_name in ("control", "staged"):
            self.assertEqual(by_case[(module_name, "valid_blocked")]["outcome"], "passed")
            self.assertEqual(by_case[(module_name, "valid_blocked")]["artifact_reads"], 1)
        self.assertEqual(tree(producer.output), retained)


AUTHORED = ("campaign_tool/records/extract/ocr.py", "tests/records/test_fa50_ocr_state.py",
            "tools/accept_fa50_ocr_state.py")
MACHINE_PATH_TOKENS = ("/ho" + "me/", "agent" + "-work", "/Use" + "rs/", ".her" + "mes")


class A5Portability(unittest.TestCase):
    """A5 (in-module half): portable sources and one coherent source tree."""

    def source_root(self):
        return Path(__file__).resolve().parents[2]

    def test_a5_authored_sources_contain_no_machine_paths(self):
        for relative in AUTHORED:
            text = (self.source_root() / relative).read_text(encoding="utf-8")
            for token in MACHINE_PATH_TOKENS:
                with self.subTest(file=relative, token=token):
                    self.assertNotIn(token, text)

    def test_a5_application_and_tests_come_from_the_same_source_tree(self):
        root = self.source_root()
        for module in (ocr, ocr_fixtures, pinned):
            with self.subTest(module=module.__name__):
                self.assertEqual(Path(module.__file__).resolve().relative_to(root).parts[0],
                                 module.__name__.split(".")[0])

    def test_a5_fixture_roots_stay_below_the_active_temporary_root(self):
        producer, receipt = produce(self, "visual_check_queued")
        active = Path(tempfile.gettempdir()).resolve()
        self.assertTrue(producer.root.resolve().is_relative_to(active))
        self.assertTrue(producer.final(receipt).resolve().is_relative_to(active))

    # Inert coverage of the runner's child-command guard: argv lists are classified and fed to a
    # detached Guard instance directly; nothing here launches a process.
    def runner(self):
        path = self.source_root() / "tools" / "accept_fa50_ocr_state.py"
        spec = importlib.util.spec_from_file_location("fa50_acceptance_runner", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def detached_guard(self, runner, mode):
        guard = runner.Guard(mode, [tempfile.gettempdir()], [])
        guard.active = True
        guard.current_test = self.id()
        return guard

    def test_a5_runner_refuses_the_pinned_inline_interpreter_child_shape(self):
        runner = self.runner()
        pinned_text = (self.source_root() / "tests/records/test_ocr.py").read_text(encoding="utf-8")
        self.assertIn('subprocess.run([sys.executable, "-c", code]', pinned_text)  # the actual shape
        shapes = {
            "pinned_inline_source": [sys.executable, "-c", "import resource"],
            "bundled_inline_source": [sys.executable, "-Bc", "pass"],
            "after_value_option": [sys.executable, "-W", "ignore", "-c", "pass"],
            "stdin_dash": [sys.executable, "-"],
            "stdin_no_script": [sys.executable, "-B"],
            "env_wrapped": ["/usr/bin/env", "python3", "-c", "pass"],
            "shell_true": ["/bin/sh", "-c", "true"],
            "shell_bundled": ["bash", "-ec", "true"],
            "shell_stdin": ["bash"],
            "shell_string": "true",
        }
        for name, argv in shapes.items():
            with self.subTest(shape=name):
                self.assertIsNotNone(runner.child_command_violation(argv))
                guard = self.detached_guard(runner, "compat")
                for event, args in (("subprocess.Popen", ("x", argv, None, None)),
                                    ("os.posix_spawn", ("x", argv, {})), ("os.exec", ("x", argv, {}))):
                    with self.assertRaises(PermissionError):
                        guard(event, args)
                self.assertEqual([r["event"] for r in guard.refusals], [runner.FORBIDDEN_CHILD] * 3)
                self.assertTrue(all(r["test"] == self.id() for r in guard.refusals))
        guard = self.detached_guard(runner, "compat")
        with self.assertRaises(PermissionError):
            guard("os.system", ("true",))
        self.assertEqual(runner.child_command_violation(shapes["pinned_inline_source"]),
                         "interpreter_inline_source")

    def test_a5_runner_permits_file_and_module_children(self):
        runner = self.runner()
        stage_text = (self.source_root() / "tests/records/test_ocr_stage.py").read_text(encoding="utf-8")
        self.assertIn('[sys.executable, "-B", "-m", "campaign_tool.records.ocr_stage"', stage_text)
        permitted = {
            "runner_child": [sys.executable, "-B", "tools/accept_fa50_ocr_state.py", "--limit-probe"],
            "stage_module": [sys.executable, "-B", "-m", "campaign_tool.records.ocr_stage", "transcribe-png"],
            "value_options": [sys.executable, "-W", "ignore", "-X", "dev", "script.py"],
            "double_dash": [sys.executable, "--", "script.py"],
            "tool": ["pdftoppm", "-r", "300", "-singlefile", "in.pdf", "page"],
            "script_file": ["fixture-script.sh"],
            "shell_script": ["bash", "fixture-script.sh"],
        }
        for name, argv in permitted.items():
            with self.subTest(shape=name):
                self.assertIsNone(runner.child_command_violation(argv))
                compat = self.detached_guard(runner, "compat")
                compat("subprocess.Popen", ("x", argv, None, None))
                self.assertEqual(compat.refusals, [])
                self.assertEqual(compat.children[-1]["verdict"], "permitted_file_or_module_child")
                strict = self.detached_guard(runner, "strict")
                with self.assertRaises(PermissionError):
                    strict("subprocess.Popen", ("x", argv, None, None))


if __name__ == "__main__":
    unittest.main()
