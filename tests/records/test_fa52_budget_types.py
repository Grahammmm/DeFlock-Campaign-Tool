"""FA52: mail-wire receipt budget counters bind only as exact producer integers.

The unchanged bind compared ``receipt["budget"] == plan.budget``. Python equality
treats ``True == 1`` and ``1.0 == 1``, so a receipt could carry boolean or
integral-float counters and still bind; a missing budget raised ``KeyError``
instead of the fixed refusal. These regressions drive the real producer
(``eml_export.export_message`` -> ``native_receipt.export``), the real binder
(``mail_delta.bind_parts`` -> ``native_receipt.bind``) and the real importer
(``mail_delta.import_receipt``).

All fixtures are generated from the existing synthetic mail-wire producer
fixtures under ``tempfile`` roots; nothing reads received mail, a mailbox or a
live ledger. ``tools/accept_fa52_budget_types.py`` redirects ``tempfile`` to a
fresh per-invocation root before this module is imported.
"""
import functools
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from campaign_tool.records.intake import eml_export, folder, mail_delta, mail_wire
from tests.records import native_msg_fixtures as native
from tests.records.test_mail_wire_intake import binary, chain, leaf, mixed

COUNTERS = ("total_bytes", "mime_parts", "captures", "messages", "max_depth")
REFUSAL = ("mail_wire_receipt_mismatch",)
NATIVE_PRODUCERS = ("eml_with_native_msg", "msg_embedded")


def sha(data):
    return hashlib.sha256(data).hexdigest()


@functools.lru_cache(maxsize=None)
def producers():
    """name -> (raw bytes, original format), built from existing synthetic fixtures."""
    embedded = native.message(embedded=True)
    return {
        "nested_rfc822": (chain()[0], "eml"),
        "eml_with_native_msg": (mixed([leaf(), binary(embedded, b"outer.msg")]), "eml"),
        "msg_zero_one": (native.message(), "msg"),
        "msg_embedded": (embedded, "msg"),
    }


def set_counter(key, value):
    def mutate(receipt):
        receipt["budget"][key] = value
    return mutate


def drop_counter(key):
    def mutate(receipt):
        del receipt["budget"][key]
    return mutate


def set_budget(value):
    def mutate(receipt):
        receipt["budget"] = value
    return mutate


def drop_budget(receipt):
    del receipt["budget"]


def add_counter(receipt):
    receipt["budget"]["extra"] = 0


def tree(root):
    """relative path -> sha256 for every regular file below ``root``."""
    root = Path(root)
    return {str(path.relative_to(root)): sha(path.read_bytes())
            for path in sorted(root.rglob("*")) if path.is_file() and not path.is_symlink()}


class Workspace:
    """One synthetic export root plus an owner-only intake ledger, all under tempfile."""

    def __init__(self, case, producer):
        temporary = tempfile.TemporaryDirectory(prefix="fa52-")
        case.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.base.chmod(0o700)
        self.mail = self.base / "mail"
        self.raw, self.form = producers()[producer]
        self.receipt_path = eml_export.export_message(
            self.raw, mail_root=self.mail, account="synthetic-account", mailbox="INBOX",
            uidvalidity=7, uid=1, original_format=self.form)
        self.receipt = json.loads(Path(self.receipt_path).read_bytes())
        self.outputs = 0
        self.tampers = 0

    def tampered(self, mutate, *, sort_keys=True):
        """Write a mutated copy of the exported receipt beside (not inside) the export root."""
        value = json.loads(Path(self.receipt_path).read_bytes())
        mutate(value)
        self.tampers += 1
        path = self.base / ("tampered-%03d.json" % self.tampers)
        path.write_bytes(json.dumps(value, sort_keys=sort_keys).encode())
        return path

    def fresh_output(self):
        self.outputs += 1
        out = self.base / ("bind-%03d" % self.outputs)
        (out / "blobs").mkdir(mode=0o700, parents=True)
        out.chmod(0o700)
        return out

    def bind(self, receipt_path, out):
        """Load, stage and bind exactly as ``import_receipt`` does, without a ledger."""
        receipt, _, items = mail_delta.load_receipt(receipt_path)
        if receipt.get("schema") != mail_wire.SCHEMA:
            raise AssertionError("fixture must produce a mail-wire-receipt-v2 receipt")
        for item in items:
            mail_delta.stage_source(item, self.mail, out)
        items[0].update(native_root=self.mail, native_output=out)
        return mail_delta.bind_parts(items, receipt)

    def intake(self):
        out = self.base / "intake"
        (out / "blobs").mkdir(mode=0o700, parents=True)
        out.chmod(0o700)
        path = out / "intake.sqlite"
        db = sqlite3.connect(path)
        try:
            db.executescript(folder.SCHEMA)
            db.execute("INSERT INTO runs VALUES(?,?,?,?,?,?)",
                       ("baseline", folder.now(), folder.now(), "inventory", "complete", "{}"))
            db.execute("INSERT INTO meta VALUES('inventory_run','baseline')")
            db.commit()
        finally:
            db.close()
        path.chmod(0o600)
        self.out = out
        return out

    def import_receipt(self, receipt_path):
        return mail_delta.import_receipt(receipt_path, self.mail, self.out)

    def ledger(self):
        db = sqlite3.connect("file:%s?mode=ro" % (self.out / "intake.sqlite"), uri=True)
        try:
            return list(db.iterdump())
        finally:
            db.close()

    def snapshot(self):
        lock = self.out / "writer.lock"
        out = tree(self.out)
        if "writer.lock" in out:
            # The importer's advisory lock file is not CAS, ledger or receipt state.
            if not lock.is_file() or lock.is_symlink() or lock.stat().st_size != 0:
                raise AssertionError("writer.lock must stay an empty regular file")
            del out["writer.lock"]
        return {"mail": tree(self.mail), "out": out, "ledger": self.ledger(),
                "pending": sorted(p.name for p in (self.out / "blobs").iterdir()
                                  if p.name.startswith(".pending-"))}


class BudgetCase(unittest.TestCase):
    def assertRefused(self, action):
        """Exact fixed refusal; any other outcome (acceptance or a stray error) fails."""
        try:
            action()
        except mail_delta.Rejected as error:
            self.assertEqual(error.args, REFUSAL)
            return
        except Exception as error:  # e.g. the unchanged bind's KeyError('budget')
            self.fail("expected Rejected%r, got %s: %r" % (REFUSAL, type(error).__name__, error))
        self.fail("tampered receipt budget was accepted by bind")

    def assertBindRefused(self, workspace, mutate):
        """Refusal at the budget gate: before any derived artifact verification or write."""
        path = workspace.tampered(mutate)
        out = workspace.fresh_output()
        with mock.patch.object(mail_delta, "_native_artifact",
                               wraps=mail_delta._native_artifact) as artifact:
            self.assertRefused(lambda: workspace.bind(path, out))
        self.assertEqual(artifact.call_count, 0)
        self.assertFalse((out / "native-items").exists())
        self.assertFalse((out / "native-streams").exists())

    def assertBinds(self, workspace, receipt_path):
        out = workspace.fresh_output()
        plan = workspace.bind(receipt_path, out)
        self.assertEqual(plan.budget, workspace.receipt["budget"])
        return plan, out


class A1PositiveBoundaryTests(BudgetCase):
    def test_a1_parser_closure_available(self):
        # Native MSG coverage is required here, never silently skipped.
        self.assertTrue(native.available(), "installed extract-msg/olefile closure is required")

    def test_a1_producer_budget_is_flat_exact_integer_counters(self):
        for name, (raw, form) in producers().items():
            with self.subTest(producer=name):
                workspace = Workspace(self, name)
                budget = workspace.receipt["budget"]
                self.assertEqual(workspace.receipt["schema"], mail_wire.SCHEMA)
                self.assertEqual(sorted(budget), sorted(COUNTERS))
                self.assertEqual({key: type(value) for key, value in budget.items()},
                                 dict.fromkeys(COUNTERS, int))
                self.assertEqual(budget, mail_wire.prepare(raw, form).budget)

    def test_a1_zero_and_one_integer_counters_bind(self):
        workspace = Workspace(self, "msg_zero_one")
        budget = workspace.receipt["budget"]
        self.assertEqual((budget["captures"], budget["messages"], budget["max_depth"]), (0, 1, 1))
        self.assertBinds(workspace, workspace.receipt_path)

    def test_a1_larger_counts_and_nested_receipts_bind(self):
        for name in ("nested_rfc822", "eml_with_native_msg", "msg_embedded"):
            with self.subTest(producer=name):
                workspace = Workspace(self, name)
                self.assertGreater(workspace.receipt["budget"]["mime_parts"], 1)
                self.assertGreater(workspace.receipt["budget"]["messages"], 1)
                plan, out = self.assertBinds(workspace, workspace.receipt_path)
                if name in NATIVE_PRODUCERS:
                    self.assertTrue(plan.native_items)
                    self.assertTrue((out / "native-items").is_dir())
                    self.assertTrue((out / "native-streams").is_dir())

    def test_a1_reordered_integer_budget_binds(self):
        for name in ("msg_zero_one", "eml_with_native_msg"):
            with self.subTest(producer=name):
                workspace = Workspace(self, name)
                reordered = list(reversed(COUNTERS))
                path = workspace.tampered(
                    set_budget({key: workspace.receipt["budget"][key] for key in reordered}),
                    sort_keys=False)
                receipt, _, _ = mail_delta.load_receipt(path)
                self.assertEqual(list(receipt["budget"]), reordered)
                self.assertNotEqual(list(workspace.receipt["budget"]), reordered)
                self.assertBinds(workspace, path)


class A2MalformedBudgetTests(BudgetCase):
    PRODUCERS = ("msg_zero_one", "eml_with_native_msg")

    def each_counter(self):
        for name in self.PRODUCERS:
            workspace = Workspace(self, name)
            # Every refusal below differs from this accepted receipt only in its budget.
            self.assertBinds(workspace, workspace.receipt_path)
            for key in COUNTERS:
                yield name, workspace, key, workspace.receipt["budget"][key]

    def test_a2_bool_counter_refused(self):
        equivalent = 0
        for name, workspace, key, value in self.each_counter():
            substitute = bool(value) if value in (0, 1) else True
            equivalent += substitute == value
            with self.subTest(producer=name, counter=key, value=substitute):
                self.assertBindRefused(workspace, set_counter(key, substitute))
        self.assertGreaterEqual(equivalent, 3, "fixtures must include equivalent bool counters")

    def test_a2_integral_float_counter_refused(self):
        for name, workspace, key, value in self.each_counter():
            with self.subTest(producer=name, counter=key, value=float(value)):
                self.assertBindRefused(workspace, set_counter(key, float(value)))

    def test_a2_null_negative_string_list_object_counter_refused(self):
        for name, workspace, key, value in self.each_counter():
            for substitute in (None, -1, -(value + 1), str(value), [value], {"value": value}):
                with self.subTest(producer=name, counter=key, value=substitute):
                    self.assertBindRefused(workspace, set_counter(key, substitute))

    def test_a2_missing_extra_and_changed_counter_refused(self):
        workspaces = {}
        for name, workspace, key, value in self.each_counter():
            workspaces[name] = workspace
            with self.subTest(producer=name, counter=key, change="missing"):
                self.assertBindRefused(workspace, drop_counter(key))
            with self.subTest(producer=name, counter=key, change="changed_integer"):
                self.assertBindRefused(workspace, set_counter(key, value + 1))
        for name, workspace in workspaces.items():
            with self.subTest(producer=name, change="extra_counter"):
                self.assertBindRefused(workspace, add_counter)

    def test_a2_missing_or_nonobject_budget_refused(self):
        for name in self.PRODUCERS:
            workspace = Workspace(self, name)
            budget = workspace.receipt["budget"]
            with self.subTest(producer=name, budget="missing"):
                self.assertBindRefused(workspace, drop_budget)
            for value in (None, [budget[key] for key in COUNTERS], [[k, v] for k, v in budget.items()],
                          json.dumps(budget), budget["total_bytes"], True):
                with self.subTest(producer=name, budget=type(value).__name__):
                    self.assertBindRefused(workspace, set_budget(value))


class A3ImporterReplayTests(BudgetCase):
    def test_a3_importer_round_trip_and_replay(self):
        for name in producers():
            with self.subTest(producer=name):
                workspace = Workspace(self, name)
                workspace.intake()
                first = workspace.import_receipt(workspace.receipt_path)
                self.assertEqual(first["status"], "imported")
                self.assertGreaterEqual(first["added_occurrences"], 1)
                after = workspace.snapshot()
                self.assertEqual(after["out"]["blobs/" + sha(workspace.raw)], sha(workspace.raw))
                self.assertEqual((workspace.out / "blobs" / sha(workspace.raw)).read_bytes(), workspace.raw)
                if name in NATIVE_PRODUCERS:
                    self.assertTrue((workspace.out / "native-items").is_dir())
                replay = workspace.import_receipt(workspace.receipt_path)
                self.assertEqual(replay["status"], "replay")
                self.assertEqual(replay["run_id"], first["run_id"])
                self.assertEqual(workspace.snapshot(), after)

    def tampers(self, workspace):
        budget = workspace.receipt["budget"]
        zero_one = [key for key in COUNTERS if budget[key] in (0, 1)]
        yield "missing_budget", drop_budget
        yield "null_budget", set_budget(None)
        yield "extra_counter", add_counter
        yield "float_counter", set_counter("captures", float(budget["captures"]))
        for key in zero_one:
            yield "bool_" + key, set_counter(key, bool(budget[key]))

    def test_a3_tampered_budget_never_publishes_or_mutates(self):
        for name in ("msg_zero_one", "eml_with_native_msg"):
            with self.subTest(producer=name):
                self.tamper_round(name)

    def tamper_round(self, name):
        workspace = Workspace(self, name)
        workspace.intake()
        receipt_bytes = Path(workspace.receipt_path).read_bytes()
        before = workspace.snapshot()
        for label, mutate in self.tampers(workspace):
            with self.subTest(producer=name, phase="first_import", tamper=label):
                self.assertRefused(lambda: workspace.import_receipt(workspace.tampered(mutate)))
                self.assertEqual(workspace.snapshot(), before)
                self.assertFalse((workspace.out / "native-items").exists())
                self.assertFalse((workspace.out / "native-streams").exists())
        first = workspace.import_receipt(workspace.receipt_path)
        self.assertEqual(first["status"], "imported")
        imported = workspace.snapshot()
        self.assertNotEqual(imported["ledger"], before["ledger"])
        for label, mutate in self.tampers(workspace):
            with self.subTest(producer=name, phase="replay", tamper=label):
                self.assertRefused(lambda: workspace.import_receipt(workspace.tampered(mutate)))
                self.assertEqual(workspace.snapshot(), imported)
        replay = workspace.import_receipt(workspace.receipt_path)
        self.assertEqual(replay["status"], "replay")
        self.assertEqual(replay["run_id"], first["run_id"])
        self.assertEqual(Path(workspace.receipt_path).read_bytes(), receipt_bytes)
        self.assertEqual(workspace.snapshot(), imported)


if __name__ == "__main__":
    unittest.main()
