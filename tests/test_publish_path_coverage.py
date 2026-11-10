"""BF-df-pii-everywhere: every registered publishing path runs the shared privacy lint, no emitting module, function or Worker send is
unregistered, and the email, phone and plate patterns have one copy. Synthetic values only; no assertion or message prints one."""
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
from campaign_tool.site import _privacy_gate, build_site
from tests.test_newsletter import MANIFEST, NOW

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "fictional-campaign"
VALUES = {"email": "jane.doe@example.invalid", "phone": "805-555-0142", "plate": "7ABC123"}
EXPECTED_EXCEPTIONS = [
    ("campaign_tool/content.py", "email"),
    ("campaign_tool/newsletter/draft.py", "phone"), ("campaign_tool/digest/redact.py", "email"), ("campaign_tool/digest/redact.py", "phone"),
    ("campaign_tool/digest/redact.py", "plate"), ("tools/flock_email.py", "email"), ("campaign_tool/kit.py", "email"),
    ("workers/workspace/src/executors/social_adapter.ts", "all")]
# Older wording BLOCKERS (review-status / state tools) that predate the alert-only guard; pinned, reviewed, not new copies.
CLAIM_WORDING_BLOCKERS = {"campaign_tool/review.py", "tools/flock_dashboard.py", "tools/flock_state.py", "tools/run_report_review.py"}
# Modules the walk below flags that were read and emit nothing a member of the public could read (reason each).
REVIEWED_NOT_PUBLISHING = {
    "campaign_tool/body_schedule_cmd.py": "writes staged organizer schedule content only; explicit build retains the publication/privacy boundary",
    "campaign_tool/launch_check.py": "doctor --launch rehearsal build into a temp tree, removed in finally; nothing published",
    "campaign_tool/content.py": "loads and validates content; its only writes are the scaffold helpers",
    "campaign_tool/content_events.py": "validates content/events.json (shared privacy lint on every text field); it writes nothing",
    "campaign_tool/content_init.py": "writes the starter content/ templates for the organizer to edit",
    "campaign_tool/content_profile.py": "organizer content writer for content/agencies.json (shared lint on the new document and "
                                        "the site's privacy gate over the whole file before the write); only the next "
                                        "explicit site build publishes it",
    "campaign_tool/content_statements.py": "reads and validates content/statements.json (its lint runs in the build); it writes nothing",
    "campaign_tool/finding_packet.py": "private reviewer packet; refuses to write inside public/ or content/",
    "campaign_tool/image_meta.py": "reads image metadata; the writer is the copy step in site.py",
    "campaign_tool/kit_calendar.py": "private .ics under kit/, refused inside public/",
    "campaign_tool/recent.py": "private recent-activity report under kit/",
    "campaign_tool/finding_search_log.py": "composes a private draft from a verified summary; the only write is findings._write_draft into private/drafts",
    "campaign_tool/search_log.py": "read-only private stdout aggregates of a local search log; writes nothing",
    "campaign_tool/setup_cmd.py": "writes the starter config",
    "campaign_tool/site_check.py": "reads the built tree",
    "campaign_tool/correction_requests.py": "private log; the site build never reads it",
    "campaign_tool/officials_cmd.py": "stages the organizer's content/officials.json roster (shared lint on admitted fields); only the "
                                      "next explicit site build publishes it, through the build's strict officials loader",
    "campaign_tool/elsewhere_cmd.py": "maintains the organizer's content/elsewhere.json only (build loader and shared privacy lint "
                                      "on every field before storage); only the next explicit site build publishes it, gated in site.py",
    "campaign_tool/newsletter/archive.py": "reads sent newsletters; the lint is draft.py's",
    "campaign_tool/supporters.py": "writes the organizer's content/supporters.json and a private consent record; the public rows are "
                                   "linted by the site build's privacy gate",
    "tools/flock_facts.py": "private fact tables", "tools/flock_monitor.py": "private monitor output",
    "tools/flock_region.py": "private region tables", "tools/records_search.py": "private search index",
    "tools/rehearse_localities.py": "rehearsal into a temp tree", "tools/rehearse_agency.py": "rehearsal into a temp tree",
    "tools/flock_deadlines.py": "private deadline report, linted by the shared patterns", "tools/collection_triage.py": "private triage report",
    "tools/custody_report.py": "private custody report", "tools/agency_verify.py": "private verification report",
    "tools/flock_redaction_record.py": "private redaction record", "tools/flock_privacy_scan.py": "the scanner itself",
    "tools/store_inventory.py": "private inventory", "tools/flock_privacy.py": "re-exports the shared patterns",
    "tools/workspace_seed.py": "local seed.sql for the workspace D1; templated text only, backstop-screened, owner approves by hash",
}
# (module, function) writers inside a REGISTERED module that were read and publish nothing (reason each).
REVIEWED_FUNCTIONS = {
    ("campaign_tool/cli.py", "_store_bytes"): "private originals store", ("campaign_tool/cli.py", "backup_drill_cmd"): "private backup drill",
    ("campaign_tool/cli.py", "finding_preview"): "private draft preview, marked not published", ("campaign_tool/cli.py", "initialize"): "starter config",
    ("campaign_tool/site.py", "build_starter"): "starter content for the organizer to edit, never built into public/"}
MODULE_WRITE = {"write_text", "write_bytes", "replace", "copyfile", "copy", "copy2", "copytree", "move", "rename", "write", "_write",
                "_write_text", "_write_json", "_write_private", "_write_atomic"}
FILE_WRITE = {"write_text", "write_bytes", "_write", "_write_text", "_write_json", "_write_private", "_write_atomic"}
PAIR_WRITE = {"copyfile", "copy", "copy2", "copytree", "move"}  # two arguments; a bare .copy() is dict.copy
WORKER_EMIT = re.compile(r'PUBLIC_BUCKET\.put\(|CACHE\.put\("site_version"|\.send\(|\bfetcher\(|"POST"|\bobject\.body\b')
WORKER_REVIEWED = ("workers/wizard/",)  # the setup wizard's Cloudflare provisioning calls carry no campaign text
MARK = re.compile(r"^(public|feed\.xml|agency-cards\.js|social|briefs|cards)$|^(kit/social|public/)")
USES = ("privacy_patterns", "flock_privacy")
SKIP = re.compile(r"tools/(accept_|fc\d)|^campaign_tool/records/|^campaign_tool/(privacy_patterns|publish_paths)\.py$|^tools/(check_public_tree|ci_affected)")
# What each site-driven entry is fed: the published field that reaches its artefact.
PROBES = {"site-pages": ("finding.summary", "site.tagline", "source.title", "agency.name", "statement.quote", "statement.context_note",
                          "elsewhere.place", "elsewhere.body", "elsewhere.as_stated",
                          "event.title", "event.venue", "event.description_md", "event.accessibility_note"), "atom-feed": ("finding.summary",),
          "events-calendar": ("event.title", "event.venue"), "agency-feeds": ("finding.summary",), "agency-meetings-calendars": ("agency.name",), "corrections-feed": ("finding.summary",), "open-graph-meta": ("finding.title",),
          "agency-cards": ("site.county_profile.summary", "agency.profile.summary"), "data-exports": ("source.title", "agency.name"),
          "map-geojson": ("map.cameras.geojson", "map.city-boundaries.geojson", "map.county.geojson")}
TEXT_ONLY = {"flock-page", "flock-publish-brief", "flock-publish-stage"}  # these tools refuse without printing the category per value


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
                writes = writes or name in MODULE_WRITE
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


def is_write(call):
    func = call.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
    receiver = ast.unparse(func.value) if isinstance(func, ast.Attribute) else ""
    if name in FILE_WRITE:
        return True
    if name in PAIR_WRITE:
        return len(call.args) >= 2
    if name == "rename":
        return len(call.args) == 1
    if name == "replace":  # Path.replace(target) takes one argument, os.replace(a, b) two, str.replace(a, b) is not os.
        return len(call.args) == 1 or receiver == "os"
    if name == "write":
        return not receiver.startswith(("sys.std", "stdout", "stderr"))
    return False


def writers(tree):
    """Qualified names of the outermost functions whose body (nested functions included) writes a file."""
    found = []

    def visit(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if any(isinstance(n, ast.Call) and is_write(n) for n in ast.walk(child)):
                    found.append(prefix + child.name)
            elif isinstance(child, ast.ClassDef):
                visit(child, prefix + child.name + ".")
            else:
                visit(child, prefix)
    visit(tree, "")
    return found


def function_problems(root):
    """Assertion (b), Python half: every function that writes a file in a registered module is bound to a registry id."""
    ids = {entry["id"] for entry in publish_paths.PUBLISH_PATHS}
    bound = {}
    problems = ["%s: unknown registry id for %s:%s" % (i, m, f) for i, m, f in publish_paths.EMISSION_SITES if i not in ids]
    for _, module, function in publish_paths.EMISSION_SITES:
        bound.setdefault(module, set()).add(function)
    for module in sorted({e["module"] for e in publish_paths.PUBLISH_PATHS if e["module"].endswith(".py")} | set(bound)):
        if not (root / module).is_file():
            continue
        found = set(writers(ast.parse((root / module).read_text(encoding="utf-8"))))
        reviewed = {f for m, f in REVIEWED_FUNCTIONS if m == module}
        problems += ["%s:%s writes a file and is not bound to a registry entry" % (module, f) for f in sorted(found - bound.get(module, set()) - reviewed)]
        problems += ["%s:%s is bound but writes nothing now" % (module, f) for f in sorted(bound.get(module, set()) - found)]
    return problems


def worker_counts(root):
    found = {}
    for path in sorted((root / "workers").glob("*/src/**/*.ts")):
        rel = path.relative_to(root).as_posix()
        if "/generated/" in rel or rel.startswith(WORKER_REVIEWED):
            continue
        if count := len(WORKER_EMIT.findall(path.read_text(encoding="utf-8"))):
            found[rel] = count
    return found


def worker_problems(root):
    """Assertion (b), Worker half: every TypeScript file that sends, uploads or serves is pinned to a registry id and a match count."""
    ids = {entry["id"] for entry in publish_paths.PUBLISH_PATHS}
    problems = ["%s: unknown registry id for %s" % (i, f) for f, (i, _) in publish_paths.WORKER_EMISSIONS.items() if i not in ids]
    for rel, count in worker_counts(root).items():
        if rel not in publish_paths.WORKER_EMISSIONS:
            problems.append("%s sends, uploads or serves (%d sites) and is not registered" % (rel, count))
        elif publish_paths.WORKER_EMISSIONS[rel][1] != count:
            problems.append("%s now has %d emission sites, registered %d: review the new one" % (rel, count, publish_paths.WORKER_EMISSIONS[rel][1]))
    for rel, (_, count) in publish_paths.WORKER_EMISSIONS.items():
        if (root / rel).is_file() and rel not in worker_counts(root):
            problems.append("%s is registered but has no emission site now" % rel)
    return problems


def pattern_copies(root):
    """(module, category) for every module other than the shared home that defines an email, phone or plate pattern."""
    found = []
    trees = list(modules(root))
    public_scan = root / "campaign_tool/records/public_scan.py"
    if public_scan.is_file():
        trees.append(("campaign_tool/records/public_scan.py", ast.parse(public_scan.read_text(encoding="utf-8"))))
    for rel, tree in trees:
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and 6 < len(node.value) < 700:
                s = node.value
                if "@" in s and "\\." in s and "{2" in s:
                    found.append((rel, "email"))
                if "\\d{3}" in s and "\\d{4}" in s:
                    found.append((rel, "phone"))
                if ("[A-Z]{3}" in s or "[A-Za-z]{3}" in s) and ("\\d{3}" in s or "\\d{4}" in s):
                    found.append((rel, "plate"))
    return found


def claim_copies(root):
    return sorted(rel for rel, tree in modules(root) if rel != "campaign_tool/legal_claims.py" for node in ast.walk(tree)
                  if isinstance(node, ast.Constant) and isinstance(node.value, str) and "violat" in node.value and "unlawful" in node.value
                  and "illegal" in node.value and rel not in ("tools/flock_compare.py",))


def second_copies(root):
    allowed = {(m, c) for m, c, _ in privacy_patterns.PATTERN_EXCEPTIONS}
    allowed |= {(m, c) for m, c, _ in privacy_patterns.PATTERN_EXCEPTIONS if c == "all" for c in ("email", "phone", "plate")}
    found = pattern_copies(root)
    # An exception permits one definition, not unlimited additional regexes in that module.
    for pair in allowed:
        # The broad heuristic also counts the digest's SSN and plate regexes as phone-shaped (both contain 3/4-digit groups).
        count = 3 if pair == ("campaign_tool/digest/redact.py", "phone") else 1
        for _ in range(count):
            if pair in found:
                found.remove(pair)
    return sorted(found)


def tool(name):
    spec = importlib.util.spec_from_file_location("_t_" + name, ROOT / "tools" / (name + ".py"))
    spec.loader.exec_module(module := importlib.util.module_from_spec(spec))
    return module


def worker_patterns(root):
    """The three regular expressions the Worker's pre-post lint really ships, read from social_adapter.ts."""
    text = (root / "workers/workspace/src/executors/social_adapter.ts").read_text(encoding="utf-8")
    at, bdot = (re.search(r"const %s = String\.raw`([^`]*)`" % name, text).group(1) for name in ("AT", "BDOT"))
    email = re.search(r'\["email", new RegExp\(String\.raw`([^`]*)`, "i"\)\]', text).group(1).replace("${AT}", at).replace("${BDOT}", bdot)
    phone, plate = (re.search(r'\["%s", /(.*)/\]' % name, text).group(1) for name in ("phone", "plate"))
    return (("email", re.compile(email, re.I)), ("phone", re.compile(phone)), ("plate", re.compile(plate)))


class Drivers:
    """feed(entry, value, probe) -> the refusal text the path produced for a synthetic artefact holding value, or None when it did not."""
    def __init__(self, case, root=ROOT):
        self.case, self.root = case, root

    def campaign(self):
        folder = Path(tempfile.mkdtemp(prefix="synthetic-pii-"))
        self.case.addCleanup(shutil.rmtree, folder, True)
        shutil.copytree(EXAMPLE, folder / "c", ignore=shutil.ignore_patterns("public"))
        return folder / "c"

    def edit(self, root, probe, value):
        """Put value into the published field named by probe: finding.<key>, site.<dotted key>, source.title or agency.<dotted key>."""
        kind, _, key = probe.partition(".")
        if kind == "map":
            path = root / "content" / "map" / key
            original = path if path.exists() else path.with_name("city-boundaries.geojson")
            document = json.loads(original.read_text(encoding="utf-8"))
            document["features"][0].setdefault("properties", {})["how"] = value
            path.write_text(json.dumps(document), encoding="utf-8")
            return
        if kind == "elsewhere":  # DF1107-04: a one-entry content/elsewhere.json whose one field holds the value
            entry = {"place": "Fictional City", "state": "ZZ", "body": "Fictional City Council", "decided_on": "2026-01-02",
                     "kind": "paused", "as_stated": "Fictional statement.", "source_url": "https://records.example.test/item",
                     "checked_on": "2026-01-03", key: "Text %s end (fictional)" % value}
            (root / "content" / "elsewhere.json").write_text(json.dumps([entry]), encoding="utf-8")
            return
        if kind == "statement":  # DF1107-07: a roster body plus one statement whose public field carries the value
            from datetime import date
            day = date.today().isoformat()
            row = {"agency_id": "ca-cedar-city-council", "official": "Rowan Example", "said_on": day, "quote": "Plain words.",
                   "context": "meeting", "source_url": "https://cedar.example/minutes", "checked_on": day}
            row[key] = "Text %s end" % value
            (root / "content" / "officials.json").write_text(json.dumps({"schema_version": 1, "bodies": [{
                "agency_id": "ca-cedar-city-council", "officials": [{
                    "name": "Rowan Example", "role": "Member", "source_url": "https://cedar.example/council", "checked_on": day}]}]}),
                encoding="utf-8")
            (root / "content" / "statements.json").write_text(json.dumps([row]), encoding="utf-8")
            return
        if kind == "event":  # DF1107-08: one synthetic campaign event with the probed field replaced
            event = {"id": "forum", "title": "Teach-in", "starts_at": "2026-10-10T18:00:00Z", "ends_at": "2026-10-10T19:00:00Z",
                     "venue": "Cedar Library", "description_md": "A synthetic gathering."}
            event[key] = "Text %s end (fictional)" % value
            (root / "content" / "events.json").write_text(json.dumps([event]), encoding="utf-8")
            return
        path = {"finding": next((root / "content" / "findings").glob("*.json")), "site": root / "content" / "site.json",
                "source": root / "content" / "sources.json", "agency": root / "content" / "agencies.json"}[kind]
        document = json.loads(path.read_text(encoding="utf-8"))
        target = document[0] if kind == "source" else document[1] if kind == "agency" else document
        *parents, last = key.split(".")
        for part in parents:
            target = target[part]
        target[last] = "Text %s end (fictional)" % value if last != "name" else "Agency %s (fictional)" % value
        path.write_text(json.dumps(document), encoding="utf-8")

    def feed(self, entry, value, probe=None):
        try:
            return getattr(self, "drive_" + entry["driver"])(entry, value, probe)
        except (ContentError, DraftError, ValueError) as exc:
            return str(exc)

    def drive_site(self, entry, value, probe):
        root = self.campaign()
        self.edit(root, probe, value)
        try:
            build_site(root)
        finally:
            self.case.assertFalse((root / "public").exists(), "a refused build must write nothing")

    def drive_runner(self, entry, value, probe):
        from runner.client import FakeWorkspace
        from runner.handlers import build_site as handler
        from tests.runner.helpers import context, job, tempdir
        from tests.runner.test_build_site import manifest_from_example
        manifest, tmp, workspace = manifest_from_example(), tempdir(), FakeWorkspace()
        self.case.addCleanup(tmp.cleanup)
        manifest["content"][next(k for k in manifest["content"] if k.startswith("findings/"))]["summary"] = "Summary %s end." % value
        result = handler.run(context(tmp.name, job("build_site", manifest), workspace))
        return result.error if result.status == "failed" and not workspace.site_files else None

    def drive_newsletter(self, entry, value, probe):
        manifest = copy.deepcopy(MANIFEST)
        manifest["findings"][0]["summary"] = "Policy text " + value + " end."
        build_draft(manifest, now=NOW)

    def drive_social(self, entry, value, probe):
        root = self.campaign()
        site = json.loads((root / "content" / "site.json").read_text(encoding="utf-8"))
        (root / "content" / "site.json").write_text(json.dumps({**site, "base_url": "https://x.example.invalid"}), encoding="utf-8")
        self.edit(root, "finding.title", value)
        social.build_social(root)

    def drive_publish(self, entry, value, probe):
        mod = tool("flock_publish")
        finding = {"finding_id": "N1", "topic": "retention", "claim_class": next(iter(mod.compare.CLASSES)), "rating": "strong",
                   "text": "Reads are kept. " + value, "cites": [], "review": {"status": "pending"}}
        return "; ".join(mod.finding_errors(finding)) or None

    def drive_report(self, entry, value, probe):
        errs = []
        tool("flock_report").lint({"deny": []}, "Basis. " + value, "", {}, 0, [], {}, [], errs)
        return "; ".join(errs) or None

    def borrowed(self, module, name, method):
        """An existing flock test case, set up (its fixtures are the tool's own); cleaned up with this test."""
        case = getattr(importlib.import_module("tests.flock." + module), name)(method)
        case.setUp()
        self.case.addCleanup(case.doCleanups)
        return case

    def drive_page(self, entry, value, probe):
        case = self.borrowed("test_page", "FlockPageTest", "test_lint_rejects_common_phone_formats_without_echoing_them")
        story = json.loads(json.dumps(case.story))
        story["dek"] += " Note " + value
        code, out = case.lint(story)
        return out if code == 1 else None

    def drive_stage(self, entry, value, probe):
        module = importlib.import_module("tests.flock.test_publish_stage")
        case = self.borrowed("test_publish_stage", "PublishStageTest", "test_copy_refusals_are_named")
        case.approve("O1")
        n1 = module.block("search-purpose", classification="documented_fact")
        case.write_copy({"N1": {**n1, "author": "Saw " + value}, "O1": module.block("outside-access")})
        code, out = case.stage()
        return out if code == 1 else None

    def drive_brief(self, entry, value, probe):
        module = importlib.import_module("tests.flock.test_publish_brief")
        case = self.borrowed("test_publish_brief", "PublishBriefTest", "test_contact_details_never_in_the_brief")
        case.approve(module.A, "N1", ref="ask " + value)
        code, out = case.brief()
        return out if code == 1 and not case.brief_md.exists() else None

    def drive_agency_brief(self, entry, value, probe):
        from datetime import datetime, timezone
        from campaign_tool.agency_brief import build_brief
        root = self.campaign()
        path = root / "content" / "agencies.json"
        agencies = json.loads(path.read_text(encoding="utf-8"))
        agencies[1].update(verified=True, verification={"source_url": "https://example.invalid/verified", "checked_on": "2026-10-30"})
        agencies[1]["profile"]["contract"] = {"vendor": "Synthetic vendor", "start": "2026-01-01", "end": "2027-01-01",
                                              "renewal_note": "Note %s end" % value, "source_url": "https://example.invalid/c",
                                              "checked_on": "2026-10-30"}
        path.write_text(json.dumps(agencies), encoding="utf-8")
        try:
            build_brief(root, agencies[1]["agency_id"], now=datetime(2026, 11, 4, tzinfo=timezone.utc))
        finally:
            self.case.assertFalse((root / "kit").exists(), "a refused brief must write nothing")

    def drive_meeting_comment(self, entry, value, probe):
        """DF1407-06: the SAME tainted file's runtime digest is recorded first, so only the lint can refuse it."""
        from datetime import date
        from campaign_tool import meeting_comment_publish, meeting_comments
        root = self.campaign()
        folder = root / "content" / "meetings"
        (folder / "2026-10-cedar-council.json").rename(path := folder / "cedar-council-2026-10.json")
        letter = root.parent / "letter.txt"
        letter.write_text("Please note %s end" % value, encoding="utf-8")
        day = date(2026, 10, 20)
        meeting_comments.record(root, "cedar-council-2026-10", "2026-10-10", "email", letter, today=day)
        before = path.read_bytes()
        try:
            meeting_comment_publish.publish(root, "cedar-council-2026-10", letter, signer_public=True, today=day)
        except ValueError:
            self.case.assertEqual(path.read_bytes(), before, "a refused comment must write nothing")
            raise

    def drive_email(self, entry, value, probe):
        case = self.borrowed("test_email_lint", "EmailLintTests", "test_private_patterns_named_never_printed")
        case.edit("Please reference", value + " Please reference")
        rules = case.rules(case.lint())
        return "; ".join(rules) if any(rule.startswith("private") for rule in rules) else None

    def drive_readiness(self, entry, value, probe):
        from campaign_tool import cli
        with mock.patch.object(cli, "doctor_report", return_value={"next_actions": ["Check " + value]}):
            cli.readiness(Path("unused"), campaign_id="synthetic")

    def drive_worker(self, entry, value, probe):
        """The Worker runtime cannot run here, so: the executor still calls privacyHits before the platform call, and the patterns it
        ships (read from social_adapter.ts, which vitest drives for real) refuse the value."""
        text = (self.root / entry["module"]).read_text(encoding="utf-8")
        if "privacyHits(text)" not in text or text.find("privacyHits(text)") > text.find("adapter.post("):
            return None
        hits = [name for name, rx in worker_patterns(self.root) if rx.search(value)]
        return "privacy lint: " + ", ".join(hits) if hits else None


def source_problem(entry, root=ROOT):
    """The static half of assertion (a): the module still names its entry point and still carries every lint call."""
    path = root / ("campaign_tool/site.py" if entry["driver"] == "site" else entry["module"])
    if not path.is_file():
        return "%s: %s is gone" % (entry["id"], entry["module"])
    text = path.read_text(encoding="utf-8")
    if entry["entry_point"].split(".")[-1] not in (root / entry["module"]).read_text(encoding="utf-8"):
        return "%s: entry point %s not found in %s" % (entry["id"], entry["entry_point"], entry["module"])
    for marker in (entry["marker"],) if isinstance(entry["marker"], str) else entry["marker"]:
        if marker not in text:
            return "%s: %s lost its lint call %s" % (entry["id"], entry["module"], marker)
    return None


def lint_problems(drivers, entries, registry=None):
    """Assertion (a): problems naming the path, for every entry that does not refuse a synthetic email, phone and plate."""
    by_id = {entry["id"]: entry for entry in (registry or publish_paths.PUBLISH_PATHS)}
    problems = []
    for entry in entries:
        if problem := source_problem(entry, drivers.root):
            problems.append(problem)
        if entry["lint"] == "upstream":  # sends text another path linted: the chain must end at a path that refuses
            seen, step = [], entry
            while step["lint"] == "upstream" and step["id"] not in seen:
                seen.append(step["id"])
                step = by_id.get(step["inherits"], {"id": "missing", "lint": "upstream", "inherits": None})
            if step["lint"] == "upstream":
                problems.append("%s (%s): its text is not linted upstream" % (entry["id"], entry["module"]))
            continue
        for probe in PROBES.get(entry["id"], (None,)):
            for category, value in VALUES.items():
                refusal = drivers.feed(entry, value, probe)
                if refusal is None:
                    problems.append("%s (%s): %s not refused%s" % (entry["id"], entry["module"], category, " via " + probe if probe else ""))
                elif value in refusal or (entry["id"] not in TEXT_ONLY and category not in refusal.lower()):
                    problems.append("%s (%s): refusal does not name %s alone" % (entry["id"], entry["module"], category))
    return problems


class RegistryTests(unittest.TestCase):
    def test_a_every_registered_path_runs_the_lint(self):
        self.assertEqual(lint_problems(Drivers(self), publish_paths.PUBLISH_PATHS), [])

    def test_b_no_emitting_module_function_or_worker_send_is_unregistered(self):
        self.assertEqual(unregistered(ROOT), [])
        self.assertEqual(function_problems(ROOT), [])
        self.assertEqual(worker_problems(ROOT), [])

    def test_c_one_copy_of_the_patterns(self):
        self.assertEqual(second_copies(ROOT), [])
        self.assertEqual(sorted(set(claim_copies(ROOT)) - CLAIM_WORDING_BLOCKERS), [])  # a NEW copy of the claim words fails
        self.assertEqual([(m, c) for m, c, _ in privacy_patterns.PATTERN_EXCEPTIONS], EXPECTED_EXCEPTIONS)
        self.assertTrue(all(reason.strip() for _, _, reason in privacy_patterns.PATTERN_EXCEPTIONS))
        self.assertTrue((ROOT / "workers/workspace/src/executors/social_adapter.ts").is_file())

    def test_worker_patterns_match_the_shared_ones(self):
        samples = list(VALUES.values()) + ["Write records [at] example [dot] org now.", "meet at city hall", "record 4155550199",
                                           "Call (805) 555-0142", "plain text", "Car 7abc123", ""]
        for text in samples:
            self.assertEqual([name for name, rx in worker_patterns(ROOT) if rx.search(text)], privacy_patterns.categories(text))

    def test_registry_shape_and_audience(self):
        keys = {"id", "module", "entry_point", "artefact_kind", "audience", "lint", "redaction_required", "marker", "driver", "inherits", "note"}
        ids = [entry["id"] for entry in publish_paths.PUBLISH_PATHS]
        self.assertEqual(len(ids), len(set(ids)))
        for entry in publish_paths.PUBLISH_PATHS:
            self.assertEqual(set(entry), keys)
            self.assertIn(entry["audience"], ("public", "pre-public"))
            self.assertIn(entry["lint"], ("privacy", "privacy+legal", "upstream"))
            self.assertEqual(entry["lint"] == "upstream", entry["inherits"] is not None)
            self.assertTrue(hasattr(Drivers, "drive_" + entry["driver"]) or entry["lint"] == "upstream", entry["id"])
        by_id = {entry["id"]: entry for entry in publish_paths.PUBLISH_PATHS}
        for named in ("atom-feed", "agency-cards", "open-graph-meta", "newsletter-draft", "social-drafts"):
            self.assertIn(named, by_id)
        self.assertEqual(by_id["newsletter-draft"]["audience"], "pre-public")
        self.assertEqual(by_id["social-drafts"]["audience"], "pre-public")

    def test_public_artefacts_are_planned_under_the_gate(self):
        planned = Drivers(self).campaign()
        build_site(planned)
        names = {p.relative_to(planned / "public").as_posix() for p in (planned / "public").rglob("*") if p.is_file()}
        self.assertTrue({"feed.xml", "corrections.xml", "agency-cards.js", "data/findings.json"} <= names)
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
        _privacy_gate({"meetings": [{"participation": {"remote": {"how": VALUES["phone"]}}}]})
        with self.assertRaisesRegex(ContentError, "meetings.*phone"):
            _privacy_gate({"meetings": [{"title": VALUES["phone"]}]})

    def test_gate_covers_sources_agencies_site_and_campaign_fields(self):
        for category, value in VALUES.items():
            for model, cfg, where in [({"sources": [{"title": "T " + value}]}, None, "sources[0].title"),
                                      ({"sources": [{"public_url": "https://x.example/" + value}]}, None, "sources[0].public_url"),
                                      ({"agencies": [{"name": value}]}, None, "agencies[0].name"),
                                      ({"agencies": [{"profile": {"facts": ["x", value]}}]}, None, "agencies[0].profile.facts[1]"),
                                      ({"site": {"tagline": value}}, None, "site.tagline"),
                                      ({"site": {"county_profile": {"summary": value}}}, None, "site.county_profile.summary"),
                                      ({}, {"name": value}, "campaign.name"),
                                      ({"supporters": [{"name": value}]}, None, "supporters[0].name"),
                                      ({"events": [{"venue": value}]}, None, "events[0].venue")]:
                with self.assertRaises(ContentError) as raised:
                    _privacy_gate(model, cfg)
                self.assertEqual(str(raised.exception), "privacy lint: %s: %s" % (where, category))

    def test_label_overrides_are_refused_before_writing(self):
        for key in ("signup.placeholder", "signup.email"):
            for category, value in VALUES.items():
                with self.subTest(key=key, category=category):
                    root = Drivers(self).campaign()
                    (root / "content" / "labels.json").write_text(json.dumps({"schema_version": 1, "labels": {key: value}}), encoding="utf-8")
                    with self.assertRaisesRegex(ContentError, "labels.*" + category):
                        build_site(root)
                    self.assertFalse((root / "public").exists())

    def test_gate_leaves_intentional_contacts_and_private_fields_alone(self):
        email, phone, plate = VALUES["email"], VALUES["phone"], VALUES["plate"]
        _privacy_gate({"findings": [{"summary": "clean", "reviewer_email": email}]})  # an undeclared model key is never published
        _privacy_gate({"site": {"contact_email": email, "privacy_contact": email},
                       "agencies": [{"records_email": email, "notes": phone, "asked": [{"request": plate}]}],
                       "sources": [{"title": "t", "note": phone, "fragment": plate, "sha256": "a" * 64}]}, {"name": "Cedar"})

    def test_clean_build_is_byte_identical(self):
        first, second = Drivers(self).campaign(), Drivers(self).campaign()
        build_site(first)
        with mock.patch("campaign_tool.site._privacy_gate"):
            build_site(second)
        for path in sorted((first / "public").rglob("*")):
            if path.is_file():
                self.assertEqual(path.read_bytes(), (second / "public" / path.relative_to(first / "public")).read_bytes())


class SeededFailureControls(unittest.TestCase):
    def tree(self, *parts):
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder, True)
        for part in ("campaign_tool", "tools", "runner", "workers/workspace/src/executors"):
            (folder / part).mkdir(parents=True)
        for rel in parts:
            shutil.copyfile(ROOT / rel, folder / rel)
        return folder

    def test_control_1_a_registered_path_without_its_lint_fails_a_naming_it(self):
        entries = [e for e in publish_paths.PUBLISH_PATHS if e["id"] == "social-drafts"]
        with mock.patch.object(social, "refusal", lambda where, text: None):
            problems = lint_problems(Drivers(self), entries)
        self.assertTrue(problems and all(p.startswith("social-drafts (campaign_tool/social.py)") for p in problems))
        stripped = copy.deepcopy(entries[0])
        stripped["marker"] = "no_such_lint_call("
        self.assertIn("lost its lint call", source_problem(stripped))

    def test_control_1a_the_meeting_comment_path_without_its_lint_fails_a_naming_it(self):
        from campaign_tool import meeting_comment_publish
        entries = [e for e in publish_paths.PUBLISH_PATHS if e["id"] == "meeting-comment-publish"]
        self.assertEqual(lint_problems(Drivers(self), entries), [])
        from campaign_tool import content  # the loader re-checks the staged file, so both copies of the call are disabled
        with mock.patch.object(meeting_comment_publish, "comment_text_problems", lambda text: []), \
                mock.patch.object(content, "comment_text_problems", lambda text: []):
            problems = lint_problems(Drivers(self), entries)
        self.assertTrue(len(problems) >= 3 and all(p.startswith("meeting-comment-publish (campaign_tool/meeting_comment_publish.py)") for p in problems), problems)

    def test_control_1b_removing_one_category_check_from_a_tool_fails_a_naming_it(self):
        entry = next(e for e in publish_paths.PUBLISH_PATHS if e["id"] == "flock-publish-brief")
        folder = self.tree()
        text = (ROOT / entry["module"]).read_text(encoding="utf-8")
        (folder / entry["module"]).write_text(text.replace("publish.privacy.EMAIL.search(body)", "False"), encoding="utf-8")
        self.assertEqual(source_problem(entry, folder), "flock-publish-brief: tools/flock_publish_brief.py lost its lint call "
                         "publish.privacy.EMAIL.search(body)")
        self.assertIsNone(source_problem(entry))

    def test_control_1c_a_worker_executor_that_skips_privacy_hits_fails_a_naming_it(self):
        entry = next(e for e in publish_paths.PUBLISH_PATHS if e["id"] == "social-post")
        folder = self.tree("workers/workspace/src/executors/social_adapter.ts")
        text = (ROOT / entry["module"]).read_text(encoding="utf-8")
        (folder / entry["module"]).write_text(text.replace("privacyHits(text)", "[]"), encoding="utf-8")
        self.assertTrue(all(p.startswith("social-post") for p in lint_problems(Drivers(self, folder), [entry])))
        self.assertEqual(len(lint_problems(Drivers(self, folder), [entry])), 4)  # the lost call, then each category unrefused
        self.assertEqual(lint_problems(Drivers(self), [entry]), [])

    def test_control_1d_an_upstream_path_whose_source_is_not_linted_fails(self):
        entry = next(e for e in publish_paths.PUBLISH_PATHS if e["id"] == "worker-send-mail")
        broken = {**entry, "inherits": "no-such-path"}
        self.assertEqual(lint_problems(Drivers(self), [broken], [broken]), ["worker-send-mail (%s): its text is not linted upstream" % entry["module"]])

    def test_control_2_a_new_unregistered_emitter_fails_b_naming_the_file(self):
        folder = self.tree()
        (folder / "campaign_tool" / "new_page.py").write_text('def go(root):\n    (root / "public" / "x.html").write_text("hi")\n')
        (folder / "campaign_tool" / "quiet.py").write_text("def go():\n    return 1\n")
        self.assertEqual(unregistered(folder), ["campaign_tool/new_page.py"])

    def test_control_2b_a_new_unguarded_output_in_a_registered_module_fails_b_naming_the_function(self):
        folder = self.tree("campaign_tool/social.py")
        text = (ROOT / "campaign_tool/social.py").read_text(encoding="utf-8")
        (folder / "campaign_tool/social.py").write_text(text + '\n\ndef leak(path, text):\n    path.write_text(text)\n', encoding="utf-8")
        self.assertEqual(function_problems(folder), ["campaign_tool/social.py:leak writes a file and is not bound to a registry entry"])

    def test_control_2c_a_new_worker_emitter_fails_b_naming_the_file(self):
        folder = self.tree()
        (folder / "workers/workspace/src/executors/new_poster.ts").write_text(
            'export async function go(f) { await f("https://x.example", { method: "POST", body: "hi" }); }\n')
        self.assertEqual(worker_problems(folder), ["workers/workspace/src/executors/new_poster.ts sends, uploads or serves (1 sites) and is not registered"])

    def test_control_2d_a_second_send_in_a_registered_worker_file_fails_b_naming_the_file(self):
        rel = "workers/workspace/src/executors/send_mail.ts"
        folder = self.tree(rel)
        (folder / rel).write_text((ROOT / rel).read_text(encoding="utf-8") + '\nexport const extra = (s, m) => s.send(m);\n', encoding="utf-8")
        self.assertRegex(worker_problems(folder)[0], r"^%s now has \d+ emission sites, registered \d+" % re.escape(rel))

    def test_control_3_a_second_email_regex_fails_c_naming_the_module(self):
        folder = self.tree()
        (folder / "campaign_tool" / "weak_copy.py").write_text('import re\nMAIL = re.compile(r"[a-z]+@[a-z]+\\.[a-z]{2,}")\n')
        self.assertEqual(second_copies(folder), [("campaign_tool/weak_copy.py", "email")])
        for rel in ("campaign_tool/content.py", "campaign_tool/records/public_scan.py"):
            with self.subTest(module=rel):
                path = folder / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text((ROOT / rel).read_text(encoding="utf-8") +
                                '\nMAIL = re.compile(r"[a-z]+@[a-z]+\\.[a-z]{2,}")\n', encoding="utf-8")
                self.assertIn((rel, "email"), second_copies(folder))
                path.unlink()


class PublicTreeScanTests(unittest.TestCase):
    def test_public_tree_scan_still_refuses_a_seeded_denied_artefact(self):
        from campaign_tool.records.public_scan import violations
        self.assertEqual(violations("private/x.txt", b"fine")[0][2], "private_path")
        self.assertEqual(violations("index.html", b"fine"), [])


if __name__ == "__main__":
    unittest.main()
