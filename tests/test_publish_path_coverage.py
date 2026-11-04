"""BF-df-pii-everywhere: every registered publishing path runs the shared privacy lint, no emitting module is unregistered, and the
email, phone and plate patterns have one copy. Synthetic values only; no assertion or message prints one."""
import ast
import copy
import importlib.util
import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool import privacy_patterns, publish_paths, social
from campaign_tool.content import ContentError
from campaign_tool.newsletter import build_draft
from campaign_tool.newsletter.draft import DraftError
from campaign_tool.site import build_site
from tests.test_newsletter import MANIFEST, NOW

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "fictional-campaign"
VALUES = {"email": "jane.doe@example.invalid", "phone": "805-555-0142", "plate": "7ABC123"}
EXPECTED_EXCEPTIONS = [
    ("campaign_tool/newsletter/draft.py", "phone"), ("campaign_tool/digest/redact.py", "email"), ("campaign_tool/digest/redact.py", "phone"),
    ("campaign_tool/digest/redact.py", "plate"), ("tools/flock_email.py", "email"), ("campaign_tool/kit.py", "email"),
    ("workers/workspace/src/executors/social_adapter.ts", "all")]
# Older wording BLOCKERS (review-status / state tools) that predate the alert-only guard; pinned, reviewed, not new copies.
CLAIM_WORDING_BLOCKERS = {"campaign_tool/review.py", "tools/flock_dashboard.py", "tools/flock_state.py", "tools/run_report_review.py"}
# Modules the walk below flags that were read and emit nothing a member of the public could read (reason each).
REVIEWED_NOT_PUBLISHING = {
    "campaign_tool/content.py": "loads and validates content; its only writes are the scaffold helpers",
    "campaign_tool/content_init.py": "writes the starter content/ templates for the organizer to edit",
    "campaign_tool/finding_packet.py": "private reviewer packet; refuses to write inside public/ or content/",
    "campaign_tool/image_meta.py": "reads image metadata; the writer is the copy step in site.py",
    "campaign_tool/kit_calendar.py": "private .ics under kit/, refused inside public/",
    "campaign_tool/recent.py": "private recent-activity report under kit/",
    "campaign_tool/setup_cmd.py": "writes the starter config",
    "campaign_tool/site_check.py": "reads the built tree",
    "campaign_tool/correction_requests.py": "private log; the site build never reads it",
    "campaign_tool/newsletter/archive.py": "reads sent newsletters; the lint is draft.py's",
    "tools/flock_facts.py": "private fact tables", "tools/flock_monitor.py": "private monitor output",
    "tools/flock_region.py": "private region tables", "tools/records_search.py": "private search index",
    "tools/rehearse_localities.py": "rehearsal into a temp tree", "tools/rehearse_agency.py": "rehearsal into a temp tree",
    "tools/flock_deadlines.py": "private deadline report, linted by the shared patterns", "tools/collection_triage.py": "private triage report",
    "tools/custody_report.py": "private custody report", "tools/agency_verify.py": "private verification report",
    "tools/flock_redaction_record.py": "private redaction record", "tools/flock_privacy_scan.py": "the scanner itself",
    "tools/store_inventory.py": "private inventory", "tools/flock_privacy.py": "re-exports the shared patterns",
}
WRITE = {"write_text", "write_bytes", "replace", "copyfile", "copy", "copy2", "copytree", "move", "rename", "write", "_write",
         "_write_text", "_write_json", "_write_private", "_write_atomic"}
MARK = re.compile(r"^(public|feed\.xml|agency-cards\.js|social|briefs|cards)$|^(kit/social|public/)")
USES = ("privacy_patterns", "flock_privacy")
SKIP = re.compile(r"tools/(accept_|fc\d)|^campaign_tool/records/|^campaign_tool/(privacy_patterns|publish_paths)\.py$|^tools/(check_public_tree|ci_affected)")


def modules(root):
    for base in ("campaign_tool", "tools", "runner"):
        for path in sorted((root / base).rglob("*.py")):
            rel = path.relative_to(root).as_posix()
            if not SKIP.search(rel):
                yield rel, ast.parse(path.read_text(encoding="utf-8"))


def emitters(root):
    """Module paths that write to a public output root, the feed, the cards or kit/social, or that load the shared lint."""
    found = set()
    for rel, tree in modules(root):
        writes = marks = uses = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
                writes = writes or name in WRITE
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                marks = marks or bool(MARK.search(node.value))
                uses = uses or node.value in USES or node.value.endswith(("flock_privacy.py", "privacy_patterns.py"))
            if isinstance(node, (ast.ImportFrom, ast.Import)):
                uses = uses or any(part in USES for part in re.split(r"[ .]", ast.unparse(node)))
        if (writes and marks) or uses:
            found.add(rel)
    return found


def unregistered(root):
    named = {entry["module"] for entry in publish_paths.PUBLISH_PATHS}
    return sorted(emitters(root) - named - set(REVIEWED_NOT_PUBLISHING))


def category_of(value):
    return "email" if "@" in value else "phone" if re.fullmatch(r"[\d-]+", value) else "plate"


def pattern_copies(root):
    """(module, category) for every module other than the shared home that defines an email, phone or plate pattern."""
    found = set()
    for rel, tree in modules(root):
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and 6 < len(node.value) < 700:
                s = node.value
                if "@" in s and "\\." in s and "{2" in s:
                    found.add((rel, "email"))
                if "\\d{3}" in s and "\\d{4}" in s:
                    found.add((rel, "phone"))
                if ("[A-Z]{3}" in s or "[A-Za-z]{3}" in s) and ("\\d{3}" in s or "\\d{4}" in s):
                    found.add((rel, "plate"))
    return found


def claim_copies(root):
    return sorted(rel for rel, tree in modules(root) if rel != "campaign_tool/legal_claims.py" for node in ast.walk(tree)
                  if isinstance(node, ast.Constant) and isinstance(node.value, str) and "violat" in node.value and "unlawful" in node.value
                  and "illegal" in node.value and rel not in ("tools/flock_compare.py",))


def second_copies(root):
    allowed = {(m, c) for m, c, _ in privacy_patterns.PATTERN_EXCEPTIONS}
    allowed |= {(m, c) for m, c, _ in privacy_patterns.PATTERN_EXCEPTIONS if c == "all" for c in ("email", "phone", "plate")}
    return sorted(pair for pair in pattern_copies(root) - allowed
                  if pair[0] not in {"campaign_tool/content.py", "campaign_tool/records/public_scan.py"} or pair[1] != "email")


def tool(name):
    spec = importlib.util.spec_from_file_location("_t_" + name, ROOT / "tools" / (name + ".py"))
    spec.loader.exec_module(module := importlib.util.module_from_spec(spec))
    return module


class Drivers:
    """feed(entry, value) -> the refusal text the path produced for a synthetic artefact holding value, or None when it did not refuse."""
    def __init__(self, case):
        self.case = case

    def campaign(self):
        folder = Path(tempfile.mkdtemp(prefix="synthetic-pii-"))
        self.case.addCleanup(shutil.rmtree, folder, True)
        shutil.copytree(EXAMPLE, folder / "c", ignore=shutil.ignore_patterns("public"))
        return folder / "c"

    def edit_finding(self, root, key, value):
        path = next((root / "content" / "findings").glob("*.json"))
        document = json.loads(path.read_text(encoding="utf-8"))
        document[key] = value
        path.write_text(json.dumps(document), encoding="utf-8")

    def feed(self, entry, value):
        try:
            return getattr(self, "drive_" + entry["driver"])(entry, value)
        except (ContentError, DraftError, ValueError) as exc:
            return str(exc)

    def drive_site(self, entry, value):
        root = self.campaign()
        self.edit_finding(root, "summary", "Summary text " + value + " end.")
        build_site(root)

    def drive_newsletter(self, entry, value):
        manifest = copy.deepcopy(MANIFEST)
        manifest["findings"][0]["summary"] = "Policy text " + value + " end."
        build_draft(manifest, now=NOW)

    def drive_social(self, entry, value):
        root = self.campaign()
        site = json.loads((root / "content" / "site.json").read_text(encoding="utf-8"))
        (root / "content" / "site.json").write_text(json.dumps({**site, "base_url": "https://x.example.invalid"}), encoding="utf-8")
        self.edit_finding(root, "title", "Title " + value + " end (fictional)")
        social.build_social(root)

    def drive_publish(self, entry, value):
        mod = tool("flock_publish")
        finding = {"finding_id": "N1", "topic": "retention", "claim_class": next(iter(mod.compare.CLASSES)), "rating": "strong",
                   "text": "Reads are kept. " + value, "cites": [], "review": {"status": "pending"}}
        return "; ".join(mod.finding_errors(finding)) or None

    def drive_report(self, entry, value):
        errs = []
        tool("flock_report").lint({"deny": []}, "Basis. " + value, "", {}, 0, [], {}, [], errs)
        return "; ".join(errs) or None


def source_problem(entry, root=ROOT):
    """The static half of assertion (a): the module still names its entry point and still carries the lint call."""
    path = root / ("campaign_tool/site.py" if entry["driver"] == "site" else entry["module"])
    if not path.is_file():
        return "%s: %s is gone" % (entry["id"], entry["module"])
    text = path.read_text(encoding="utf-8")
    if entry["entry_point"].split(".")[-1] not in text:
        return "%s: entry point %s not found in %s" % (entry["id"], entry["entry_point"], entry["module"])
    if entry["marker"] not in text:
        return "%s: %s lost its lint call %s" % (entry["id"], entry["module"], entry["marker"])
    return None


def lint_problems(drivers, entries):
    """Assertion (a): problems naming the path, for every entry that does not refuse a synthetic email, phone and plate."""
    problems = []
    for entry in entries:
        if problem := source_problem(entry):
            problems.append(problem)
        if entry["driver"] == "static":
            continue
        for category, value in VALUES.items():
            refusal = drivers.feed(entry, value)
            if refusal is None:
                problems.append("%s (%s): %s not refused" % (entry["id"], entry["module"], category))
            elif category not in refusal.lower() or value in refusal:
                problems.append("%s (%s): refusal does not name %s alone" % (entry["id"], entry["module"], category))
    return problems


class RegistryTests(unittest.TestCase):
    def test_a_every_registered_path_runs_the_lint(self):
        self.assertEqual(lint_problems(Drivers(self), publish_paths.PUBLISH_PATHS), [])

    def test_b_no_emitting_module_is_unregistered(self):
        self.assertEqual(unregistered(ROOT), [])

    def test_c_one_copy_of_the_patterns(self):
        self.assertEqual(second_copies(ROOT), [])
        self.assertEqual(sorted(set(claim_copies(ROOT)) - CLAIM_WORDING_BLOCKERS), [])  # a NEW copy of the claim words fails
        self.assertEqual([(m, c) for m, c, _ in privacy_patterns.PATTERN_EXCEPTIONS], EXPECTED_EXCEPTIONS)
        self.assertTrue(all(reason.strip() for _, _, reason in privacy_patterns.PATTERN_EXCEPTIONS))
        self.assertTrue((ROOT / "workers/workspace/src/executors/social_adapter.ts").is_file())

    def test_registry_shape_and_audience(self):
        keys = {"id", "module", "entry_point", "artefact_kind", "audience", "lint", "redaction_required", "marker", "driver", "note"}
        ids = [entry["id"] for entry in publish_paths.PUBLISH_PATHS]
        self.assertEqual(len(ids), len(set(ids)))
        for entry in publish_paths.PUBLISH_PATHS:
            self.assertEqual(set(entry), keys)
            self.assertIn(entry["audience"], ("public", "pre-public"))
            self.assertIn(entry["lint"], ("privacy", "privacy+legal"))
        by_id = {entry["id"]: entry for entry in publish_paths.PUBLISH_PATHS}
        for named in ("atom-feed", "agency-cards", "open-graph-meta", "newsletter-draft", "social-drafts"):
            self.assertIn(named, by_id)
        self.assertEqual(by_id["newsletter-draft"]["audience"], "pre-public")
        self.assertEqual(by_id["social-drafts"]["audience"], "pre-public")

    def test_public_artefacts_are_planned_under_the_gate(self):
        planned = Drivers(self).campaign()
        build_site(planned)
        names = {p.relative_to(planned / "public").as_posix() for p in (planned / "public").rglob("*") if p.is_file()}
        self.assertTrue({"feed.xml", "agency-cards.js", "data/findings.json"} <= names)
        self.assertTrue(any(n.startswith("findings/agency/") and n.endswith(".xml") for n in names))
        self.assertIn('property="og:', (next((planned / "public" / "findings").glob("cedar-*.html"))).read_text(encoding="utf-8"))

    def test_empty_and_clean_artefacts_are_still_linted(self):
        with mock.patch.object(social, "refusal", wraps=social.refusal) as spy:
            root = Drivers(self).campaign()
            site = json.loads((root / "content" / "site.json").read_text(encoding="utf-8"))
            (root / "content" / "site.json").write_text(json.dumps({**site, "base_url": "https://x.example.invalid"}), encoding="utf-8")
            social.build_social(root)
        self.assertTrue(spy.call_count >= 1)
        self.assertIsNone(privacy_patterns.refusal("x", ""))
        with mock.patch.object(social, "refusal", wraps=social.refusal) as spy:
            social._write_drafts(Path(tempfile.mkdtemp(dir=Drivers(self).campaign().parent)) / "s", Path("unused"), {"empty": ""})
        spy.assert_called_once()

    def test_legal_flag_never_refuses(self):
        manifest = copy.deepcopy(MANIFEST)
        manifest["findings"][0]["summary"] = "This policy violates state law and is unlawful."
        draft = build_draft(manifest, now=NOW)  # flagged, recorded and returned for the human gate; never a refusal
        self.assertTrue(draft["review_flags"])
        self.assertIsNone(privacy_patterns.refusal("x", manifest["findings"][0]["summary"]))

    def test_refusals_name_category_and_place_never_the_value(self):
        for category, value in VALUES.items():
            note = privacy_patterns.refusal("findings[0].summary", "x " + value)
            self.assertEqual(note, "privacy lint: findings[0].summary: " + category)
            self.assertNotIn(value, note)

    def test_meeting_contact_how_text_is_not_scanned(self):
        from campaign_tool.site import _privacy_gate
        _privacy_gate({"meetings": [{"participation": {"remote": {"how": VALUES["phone"]}}}]})
        with self.assertRaisesRegex(ContentError, "meetings.*phone"):
            _privacy_gate({"meetings": [{"title": VALUES["phone"]}]})

    def test_clean_build_is_byte_identical(self):
        first, second = Drivers(self).campaign(), Drivers(self).campaign()
        build_site(first)
        with mock.patch("campaign_tool.site._privacy_gate"):
            build_site(second)
        for path in sorted((first / "public").rglob("*")):
            if path.is_file():
                self.assertEqual(path.read_bytes(), (second / "public" / path.relative_to(first / "public")).read_bytes())


class SeededFailureControls(unittest.TestCase):
    def test_control_1_a_registered_path_without_its_lint_fails_a_naming_it(self):
        entries = [e for e in publish_paths.PUBLISH_PATHS if e["id"] == "social-drafts"]
        with mock.patch.object(social, "refusal", lambda where, text: None):
            problems = lint_problems(Drivers(self), entries)
        self.assertTrue(problems and all(p.startswith("social-drafts (campaign_tool/social.py)") for p in problems))
        stripped = copy.deepcopy(entries[0])
        stripped["marker"] = "no_such_lint_call("
        self.assertIn("lost its lint call", source_problem(stripped))

    def test_control_2_a_new_unregistered_emitter_fails_b_naming_the_file(self):
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder, True)
        (folder / "campaign_tool").mkdir()
        (folder / "tools").mkdir()
        (folder / "runner").mkdir()
        (folder / "campaign_tool" / "new_page.py").write_text('def go(root):\n    (root / "public" / "x.html").write_text("hi")\n')
        (folder / "campaign_tool" / "quiet.py").write_text("def go():\n    return 1\n")
        self.assertEqual(unregistered(folder), ["campaign_tool/new_page.py"])

    def test_control_3_a_second_email_regex_fails_c_naming_the_module(self):
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder, True)
        for part in ("campaign_tool", "tools", "runner"):
            (folder / part).mkdir()
        (folder / "campaign_tool" / "weak_copy.py").write_text('import re\nMAIL = re.compile(r"[a-z]+@[a-z]+\\.[a-z]{2,}")\n')
        self.assertEqual(second_copies(folder), [("campaign_tool/weak_copy.py", "email")])


class PublicTreeScanTests(unittest.TestCase):
    def test_public_tree_scan_still_refuses_a_seeded_denied_artefact(self):
        from campaign_tool.records.public_scan import violations
        self.assertEqual(violations("private/x.txt", b"fine")[0][2], "private_path")
        self.assertEqual(violations("index.html", b"fine"), [])


if __name__ == "__main__":
    unittest.main()
