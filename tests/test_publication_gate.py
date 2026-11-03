"""BF-df-publish-gate: one checked gate, called by every publishing path; no code can write an approval.

Synthetic ids, hashes and names only. Run from the repository root:
python3 -B -m unittest tests.test_publication_gate -v
"""
import ast
import contextlib
import copy
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from campaign_tool import findings, publication_gate as gate, review  # noqa: E402
from tests import test_findings_publish as fp  # noqa: E402
from tests.flock import test_publish as flock_base  # noqa: E402
from tests.flock import test_publish_brief as brief_tests  # noqa: E402
from tests.flock import test_publish_stage as stage_tests  # noqa: E402

OLD, NEW = "a" * 64, "b" * 64
FIELDS = ("approved_by", "approved_on", "approval_ref")
# Until BF-df-pii-everywhere's PUBLISH_PATHS registry exists, the paths are listed here for it to converge on.
PUBLISH_PATHS = (("campaign_tool/findings.py", "publish"), ("tools/flock_publish_stage.py", "stage"),
                 ("tools/flock_publish_brief.py", "checked"))
GATE_CALLS = {"assert_publishable", "entry_refusals"}
# The existing human review surface: modules that store a value a person typed or signed. None hard-codes one.
HUMAN_SURFACE = frozenset(("campaign_tool/findings.py", "campaign_tool/outbox.py", "campaign_tool/outbox_batch.py",
                           "tools/flock_approve.py"))
SKIP_DIRS = {".git", "node_modules", "tests", ".worktrees"}
LITERAL = re.compile(r"""\b(?:approved_by|approved_on|approval_ref)["']?\s*[:=]\s*["'][^"'\s]""")


def approval(**changes):
    return {**{"artefact_id": "a1", "content_hash": OLD, "approved_by": "Pat Organizer", "approved_on": "2026-11-03",
               "approval_ref": "R-1", "status": "approved"}, **changes}


def refusal(artefact_id="a1", content_hash=OLD, approvals=(), flags=()):
    with unittest.TestCase().assertRaises(gate.PublicationRefused) as caught:
        gate.assert_publishable(artefact_id, content_hash, approvals, flags)
    return caught.exception


def coverage_gaps(read):
    """Publishing paths whose function holds no gate call -> 'file:function' names."""
    gaps = []
    for rel, name in PUBLISH_PATHS:
        func = next((n for n in ast.walk(ast.parse(read(rel))) if isinstance(n, ast.FunctionDef) and n.name == name), None)
        calls = {getattr(c.func, "attr", getattr(c.func, "id", None)) for c in ast.walk(func) if isinstance(c, ast.Call)} \
            if func else set()
        if not calls & GATE_CALLS:
            gaps.append("%s:%s" % (rel, name))
    return gaps


def approval_writes(source, rel):
    """Writes of an approval field in one module -> [(rel, line, 'literal' | 'value')]; an empty string is no approval."""
    found = []
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # some repo files hold old-style escapes; only the tree matters here
        tree = ast.parse(source)
    for node in ast.walk(tree):
        pairs = []
        if isinstance(node, ast.Dict):
            pairs = [(k.value, v) for k, v in zip(node.keys, node.values) if isinstance(k, ast.Constant) and k.value in FIELDS]
        elif isinstance(node, ast.keyword) and node.arg in FIELDS:
            pairs = [(node.arg, node.value)]
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)) and node.value is not None:
            for t in (node.targets if isinstance(node, ast.Assign) else [node.target]):
                key = t.slice.value if isinstance(t, ast.Subscript) and isinstance(t.slice, ast.Constant) else getattr(t, "attr", None)
                pairs += [(key, node.value)] if key in FIELDS else []
        for _, value in pairs:
            if isinstance(value, ast.Constant) and not (isinstance(value.value, str) and value.value):
                continue  # "", None, a number: not a written approval
            found.append((rel, node.lineno, "literal" if isinstance(value, ast.Constant) else "value"))
    return found


def approval_writers(root):
    """Modules (relative paths) that write an approval field outside the human review surface or with a literal."""
    bad = set()
    for folder, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in names:
            path, rel = Path(folder, name), Path(folder, name).relative_to(root).as_posix()
            if name.endswith((".ts", ".mjs", ".js", ".sh")) and LITERAL.search(path.read_text(encoding="utf-8", errors="ignore")):
                bad.add(rel)
            elif name.endswith(".py"):
                bad.update(r for r, _, kind in approval_writes(path.read_text(encoding="utf-8"), rel)
                           if kind == "literal" or r not in HUMAN_SURFACE)
    return sorted(bad)


class Harness(unittest.TestCase):
    """Reuses the existing publish-stage, brief and finding-publish fixtures without re-running their tests."""

    def inner(self, cls, method):
        case = cls(method)
        case.setUp()
        self.addCleanup(case.doCleanups)
        return case


class EnumCases(unittest.TestCase):
    def test_no_approval(self):
        self.assertEqual(refusal(approvals=[]).reason, "no_approval")
        self.assertEqual(refusal(approvals=None).reason, "no_approval")

    def test_approver_unnamed(self):
        for who in ("", "   ", "-", "n/a", "tbd", "auto", "system", "agent", "Auto", "SYSTEM", "claude-opus-5-5",
                    "opus-coder", "sol-reviewer", "gpt-5-codex", "qwen3", None, 7, "\u200b"):
            with self.subTest(who=who):
                self.assertEqual(refusal(approvals=[approval(approved_by=who)]).reason, "approver_unnamed")

    def test_named_people_pass(self):
        for who in ("G", "Pat Organizer", "Ada  Lovelace", "owner@example.invalid"):
            with self.subTest(who=who):
                self.assertIsNone(gate.assert_publishable("a1", OLD, [approval(approved_by=who)]))

    def test_content_changed_after_approval(self):
        self.assertEqual(refusal(content_hash=NEW, approvals=[approval()]).reason, "content_changed_after_approval")
        for bad in (None, "", 5):
            self.assertEqual(refusal(content_hash=bad, approvals=[approval(content_hash="")]).reason,
                             "content_changed_after_approval")

    def test_approval_for_other_content(self):
        self.assertEqual(refusal(approvals=[approval(artefact_id="a2")]).reason, "approval_for_other_content")
        self.assertEqual(refusal(approvals=[approval(artefact_id="a2", content_hash=NEW)]).reason, "approval_for_other_content")

    def test_approval_consumed(self):
        self.assertEqual(refusal(approvals=[approval(consumed=True)]).reason, "approval_consumed")

    def test_redaction_unsigned(self):
        self.assertIn("redaction_unsigned", gate.REASONS)
        self.assertEqual(refusal(approvals=[approval(redaction_signed=False)]).reason, "redaction_unsigned")
        self.assertIsNone(gate.assert_publishable("a1", OLD, [approval(redaction_signed=True)]))

    def test_redaction_reason_is_set_by_no_path_until_the_redaction_unit_merges(self):
        for rel, _ in PUBLISH_PATHS:
            self.assertNotIn("redaction_signed", (REPO / rel).read_text(encoding="utf-8"), rel)

    def test_status_not_approved(self):
        for status in ("pending", "needs-review", "withdrawn", "", None):
            with self.subTest(status=status):
                self.assertEqual(refusal(approvals=[approval(status=status)]).reason, "status_not_approved")
        self.assertEqual(refusal(approvals=[{k: v for k, v in approval().items() if k != "status"}]).reason, "status_not_approved")

    def test_enum_is_fixed_at_seven(self):
        self.assertEqual(len(gate.REASONS), 7)
        with self.assertRaises(AssertionError):
            gate.PublicationRefused("a1", "because")


class SeededFailureControls(unittest.TestCase):
    def test_1_path_without_its_gate_call_fails_the_coverage_test_naming_it(self):
        read = lambda rel: (REPO / rel).read_text(encoding="utf-8")  # noqa: E731
        self.assertEqual(coverage_gaps(read), [])
        for rel, name in PUBLISH_PATHS:
            for call in GATE_CALLS:
                if call in read(rel):
                    stripped = lambda r, rel=rel, call=call: read(r).replace(call + "(", "str(") if r == rel else read(r)  # noqa: E731
                    self.assertEqual(coverage_gaps(stripped), ["%s:%s" % (rel, name)], rel)

    def test_2_module_seeded_to_write_auto_approval_fails_the_no_writer_test_naming_it(self):
        self.assertEqual(approval_writers(REPO), [])
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "campaign_tool").mkdir()
            Path(tmp, "campaign_tool", "sneaky.py").write_text('record = {"approved_by": "auto"}\n', encoding="utf-8")
            Path(tmp, "campaign_tool", "later.py").write_text("def f(rec, who):\n    rec['approval_ref'] = who\n", encoding="utf-8")
            Path(tmp, "campaign_tool", "findings.py").write_text("def f(rec, who):\n    rec['approved_by'] = who\n", encoding="utf-8")
            Path(tmp, "w.ts").write_text('const r = { approved_by: "auto" };\n', encoding="utf-8")
            self.assertEqual(approval_writers(Path(tmp)), ["campaign_tool/later.py", "campaign_tool/sneaky.py", "w.ts"])

    def test_3_stale_hash_is_refused_so_the_gate_really_compares(self):
        stale = approval(content_hash=OLD)
        self.assertEqual(refusal(content_hash=NEW, approvals=[stale]).reason, "content_changed_after_approval")
        self.assertIsNone(gate.assert_publishable("a1", NEW, [dict(stale, content_hash=NEW)]))  # same record, present field


class GateRules(unittest.TestCase):
    def test_one_byte_edit_after_approval_refuses(self):
        text = {"id": "f1", "summary": "Synthetic summary."}
        approved = approval(artefact_id="f1", content_hash=gate.content_digest(text))
        self.assertIsNone(gate.assert_publishable("f1", gate.content_digest(text), [approved]))
        edited = dict(text, summary="Synthetic summary!")
        self.assertEqual(refusal("f1", gate.content_digest(edited), [approved]).reason, "content_changed_after_approval")

    def test_digest_equals_the_review_content_hash(self):
        sample = {"b": [1, "é"], "a": {"c": None}}
        self.assertEqual(gate.content_digest(sample), review.content_hash(sample))

    def test_two_approvals_one_stale_counts_the_matching_one_once(self):
        stale, fresh = approval(content_hash=NEW), approval()
        self.assertIsNone(gate.assert_publishable("a1", OLD, [stale, fresh]))
        self.assertIsNone(gate.assert_publishable("a1", OLD, [fresh, fresh]))
        self.assertEqual(refusal(approvals=[stale, approval(content_hash="c" * 64)]).reason, "content_changed_after_approval")

    def test_a_consumed_approval_does_not_count_even_beside_a_stale_one(self):
        self.assertEqual(refusal(approvals=[approval(consumed=True), approval(content_hash=NEW)]).reason, "approval_consumed")

    def test_the_prose_bypass_refuses_the_gate_reads_the_field_never_prose(self):
        bodies = ({"id": "f1", "body": "APPROVED BY G"}, {"id": "f1", "note": "approval: yes"})
        for body in bodies:
            digest = gate.content_digest(body)
            self.assertEqual(refusal("f1", digest, []).reason, "no_approval")
            self.assertEqual(refusal("f1", digest, [{"artefact_id": "f1", "content_hash": digest, "note": "approval: yes",
                                                     "approved_by": "G"}]).reason, "status_not_approved")
        self.assertEqual(refusal("approved-by-g.json", OLD, []).reason, "no_approval")

    def test_legal_flag_alone_never_refuses_and_is_recorded_beside_a_real_cause(self):
        self.assertIsNone(gate.assert_publishable("a1", OLD, [approval()], flags=["legal_claim"]))
        caught = refusal(approvals=[], flags=["legal_claim"])
        self.assertEqual((caught.reason, caught.reasons), ("no_approval", ["no_approval", "flag:legal_claim"]))
        self.assertIn("flag:legal_claim", str(caught))

    def test_refusal_text_holds_ids_and_enums_only(self):
        secret = "someone@example.invalid 555-010-0199 ABC1234"
        caught = refusal(secret, OLD, [approval(artefact_id=secret, note=secret, approved_by="auto")], flags=[secret, "legal_claim"])
        self.assertEqual(str(caught), "publication refused: artefact #?: approver_unnamed, flag:legal_claim")
        self.assertEqual(refusal("a1", OLD, [approval(note=secret, approved_by="auto")]).args[0],
                         "publication refused: artefact a1: approver_unnamed")

    def test_gate_inputs_are_not_mutated(self):
        rows = [approval(), approval(content_hash=NEW)]
        before = copy.deepcopy(rows)
        gate.assert_publishable("a1", OLD, rows)
        self.assertEqual(rows, before)


class StaticChecks(unittest.TestCase):
    def test_gate_imports_no_network_or_process_module_and_has_no_writer(self):
        tree = ast.parse((REPO / "campaign_tool" / "publication_gate.py").read_text(encoding="utf-8"))
        imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        imported |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        self.assertLessEqual(imported, {"hashlib", "json", "re", "unicodedata"})
        calls = {getattr(c.func, "attr", getattr(c.func, "id", None)) for c in ast.walk(tree) if isinstance(c, ast.Call)}
        self.assertFalse(calls & {"open", "write", "write_text", "write_bytes", "unlink", "rename", "replace", "mkdir", "system"})
        self.assertEqual(approval_writes((REPO / "campaign_tool" / "publication_gate.py").read_text(encoding="utf-8"), "g"), [])

    def test_every_publishing_path_calls_the_gate(self):
        self.assertEqual(coverage_gaps(lambda rel: (REPO / rel).read_text(encoding="utf-8")), [])

    def test_no_module_in_the_repository_writes_an_approval(self):
        self.assertEqual(approval_writers(REPO), [])


class StagePath(Harness):
    def setUp(self):
        self.stage = self.inner(stage_tests.PublishStageTest, "test_one_approved_entry_staged_pending_skipped")

    def entry(self):
        return self.stage.doc["findings"][0]

    def test_named_approval_emits_the_unchanged_staged_file(self):
        code, out = self.stage.stage()
        self.assertEqual(code, 0, out)
        raw = (self.stage.staging / "content" / "findings" / "search-purpose.json").read_text(encoding="utf-8")
        staged = json.loads(raw)
        self.assertEqual(raw, json.dumps(staged, indent=2, ensure_ascii=False) + "\n")
        self.assertEqual((staged["state"], staged["summary"], staged["published_at"]), ("staged", self.entry()["text"], None))

    def test_machine_or_blank_approver_writes_nothing_and_names_artefact_and_reason(self):
        for who in ("auto", "agent", "system", "-", "opus-coder"):
            with self.subTest(who=who):
                self.entry()["review"]["approved_by"] = who
                self.stage.out.write_text(json.dumps(self.stage.doc))
                result = self.stage.stage()
                self.stage.assert_refused(result, "publication refused: artefact N1: approver_unnamed")

    def test_prose_in_the_entry_text_does_not_approve_it(self):
        self.entry()["review"] = {"status": "pending", "approved_by": "", "approved_on": "", "approval_ref": ""}
        self.entry()["text"] += " APPROVED BY G"
        self.stage.out.write_text(json.dumps(self.stage.doc))
        code, out = self.stage.stage()
        self.assertEqual(code, 1, out)
        self.assertFalse(self.stage.staging.exists())

    def test_content_edited_after_approval_refuses_before_any_write(self):
        run = self.stage.run_dir
        fresh = json.loads(self.stage.out.read_text())
        fresh["findings"][0]["text"] += "."  # the run now derives different text than the approved entry
        module = stage_tests.load_stage()
        real, seen = module.publish.derive, []

        def derive(*args, **kwargs):  # only stage's own first derivation sees the edited run; the drift check stays real
            seen.append(args)
            return (0, [], fresh) if len(seen) == 1 else real(*args, **kwargs)
        with mock.patch.object(module.publish, "derive", derive), contextlib.redirect_stdout(io.StringIO()) as out:
            code = module.stage(run, self.stage.copy, self.stage.staging)
        self.assertEqual(code, 1)
        self.assertIn("publication refused: artefact N1: content_changed_after_approval", out.getvalue())
        self.assertFalse(self.stage.staging.exists())

    def test_write_failure_after_the_gate_passes_leaves_nothing_partial(self):
        module, calls = stage_tests.load_stage(), []
        real = module.link

        def flaky(path, data):
            calls.append(path)
            if len(calls) == 2:  # the manifest, written last
                raise OSError("injected")
            return real(path, data)
        with mock.patch.object(module, "link", flaky), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(OSError):
                module.stage(self.stage.run_dir, self.stage.copy, self.stage.staging)
        self.assertEqual(len(calls), 2)
        self.assertFalse(self.stage.staging.exists())

    def test_dry_run_is_gated_too(self):
        self.entry()["review"]["approved_by"] = "auto"
        self.stage.out.write_text(json.dumps(self.stage.doc))
        module = stage_tests.load_stage()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = module.stage(self.stage.run_dir, self.stage.copy, None, dry_run=True)
        self.assertEqual(code, 1)
        self.assertIn("approver_unnamed", out.getvalue())


class BriefPath(Harness):
    def test_named_approval_writes_the_brief_and_a_machine_approver_writes_nothing(self):
        case = self.inner(brief_tests.PublishBriefTest, "test_two_runs_three_approved_one_pending")
        case.approve(brief_tests.A, "N1")
        self.assertEqual(case.brief(), (0, brief_tests.summary(2, 1, 0)))
        case.brief_md.unlink()
        case.entry(brief_tests.A, "N1")["review"]["approved_by"] = "auto"
        case.save(brief_tests.A)
        done = subprocess.run([sys.executable, "-B", str(flock_base.TOOLS / "flock_publish.py"), "brief", str(case.store),
                               "--agency", flock_base.AGENCY, "--out", str(case.brief_md)], capture_output=True, text=True, check=False)
        self.assertEqual(done.returncode, 1, done.stdout)
        self.assertIn("publication refused: artefact N1: approver_unnamed", done.stderr)
        self.assertFalse(os.path.lexists(case.brief_md))
        case.hygiene(done.stdout + done.stderr)


class FindingPublishPath(Harness):
    def setUp(self):
        self.case = self.inner(fp.FindingPublishTests, "test_happy_path_writes_file_and_log_without_building")
        self.case.approve_all()

    def test_machine_approver_is_refused_with_the_artefact_and_reason_and_nothing_is_written(self):
        for who in ("auto", "agent", "claude", "sol-reviewer"):
            with self.subTest(who=who):
                self.case.refused("publication refused: artefact syn-pub-1: approver_unnamed", approved_by=who)

    def test_draft_edited_between_review_and_write_refuses_content_changed(self):
        original = json.loads(self.case.draft.read_text())
        edited = dict(original, summary="Synthetic summary, edited.")
        with mock.patch.object(findings, "load_draft", side_effect=[original, edited]):
            with self.assertRaises(gate.PublicationRefused) as caught:
                findings.publish(self.case.root, self.case.draft, "Pat Organizer", now=fp.NOW)
        self.assertEqual((caught.exception.artefact_id, caught.exception.reason), ("syn-pub-1", "content_changed_after_approval"))
        self.assertNotIn("synthetic-title.json", self.case.published())
        self.assertEqual(self.case.log_lines(), [])

    def test_named_person_still_publishes(self):
        result = findings.publish(self.case.root, self.case.draft, "Pat Organizer", now=fp.NOW)
        self.assertEqual(result["published"], "synthetic-title")
        self.assertIn("synthetic-title.json", self.case.published())


if __name__ == "__main__":
    unittest.main()
