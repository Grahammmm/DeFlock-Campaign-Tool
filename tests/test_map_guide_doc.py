"""DF0237-16: docs/MAP-GUIDE.md. Local links and anchors, every documented command against the real parsers, and the
guide's own ```sh commands run in order in temporary campaign storage with outbound connections blocked. Synthetic data
only; nothing is fetched, sent, published or deployed.

Run from the repository root: python3 -B -m unittest tests.test_map_guide_doc -v

Conventions the guide keeps and these tests read: ```sh blocks are the main path, run in document order inside their
numbered task section; ```bash blocks are optional or online forms, checked against the real parser and never run. Every
$VARIABLE in a command must have a fixture value in Sandbox.mapping.
"""
import argparse
import contextlib
import csv
import io
import json
import re
import shlex
import shutil
import socket
import stat
import sys
import tempfile
import unittest
import urllib.request
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import cli, map_survey  # noqa: E402
from tools import fetch_osm_alprs as tool  # noqa: E402

GUIDE, README = REPO_ROOT / "docs" / "MAP-GUIDE.md", REPO_ROOT / "README.md"
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
NO_NETWORK = AssertionError("no network in these tests")
FENCE = re.compile(r"^```(\w*)\n(.*?)^```$", re.S | re.M)
LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)\)")
NODE = "https://www.openstreetmap.org/node/"
TASKS = ["1. Inspect the current map", "2. Preview a refresh", "3. Write a reviewed snapshot", "4. Plan a field survey",
         "5. Record sourced checks", "6. Validate, rebuild and inspect", "7. Help residents contribute"]
CSV_HEADER = "osm_url,latitude,longitude,manufacturer,operator,direction,city,check_status,checked_on"
CITY = "Synthetic Cedar"
SOURCE_URL = "https://records.example.invalid/survey-a"
LIBRARY_PREFIX = "cafecafecafe"  # sha256[:12] of the fictional example's first sources.json entry


class DocumentError(AssertionError):
    pass


# --- markdown helpers -------------------------------------------------------------------------------------------

def read(path):
    return Path(path).read_text(encoding="utf-8")


def slug(heading):
    heading = re.sub(r"[`*_]", "", heading.strip().lower())
    return re.sub(r"\s", "-", re.sub(r"[^\w\s-]", "", heading))


def anchors(text):
    """GitHub-style heading anchors of a Markdown text (outside code fences), duplicates suffixed -1, -2."""
    seen, found = {}, set()
    for line in FENCE.sub("", text).splitlines():
        match = re.match(r"#{1,6}\s+(.*)", line)
        if match:
            base = slug(match.group(1))
            found.add(base if base not in seen else f"{base}-{seen[base]}")
            seen[base] = seen.get(base, 0) + 1
    return found


def check_links(source, text):
    """Problems for every local link in ``text`` (as if it were the file ``source``): missing file or fragment."""
    problems = []
    for target in LINK.findall(FENCE.sub("", text)):
        if re.match(r"[a-z][a-z0-9+.-]*:", target):
            continue  # external: never fetched here
        name, _, fragment = target.partition("#")
        path = (Path(source).parent / name).resolve() if name else Path(source).resolve()
        if not path.exists():
            problems.append(f"{target}: no such file")
        elif fragment and (path.suffix != ".md" or fragment not in anchors(read(path))):
            problems.append(f"{target}: no such heading")
    return problems


def sections(text):
    """{heading: body} of the ``## `` sections."""
    parts = re.split(r"^## (.*)$", text, flags=re.M)
    return dict(zip(parts[1::2], parts[2::2]))


# --- command helpers --------------------------------------------------------------------------------------------

TOOL = ["python3", "tools/fetch_osm_alprs.py"]
CLI = ["python3", "-m", "campaign_tool"]


def command_lines(block):
    """Logical lines of a fence (backslash continuations joined)."""
    return [line.strip() for line in re.sub(r"\\\n\s*", " ", block).splitlines() if line.strip()]


def classify(argv, where):
    if argv[:2] == TOOL:
        return "tool"
    if argv[:3] == CLI:
        return "cli"
    raise DocumentError(f"{where}: not a documented command: {' '.join(argv)}")


def documented(text):
    """Every command in the text's sh and bash fences and inline code spans, as (kind, argv, fence kind). A fence line
    that is not one of the two commands is an error rather than silently skipped."""
    commands = []
    for fence, body in FENCE.findall(text):
        if fence in ("sh", "bash"):
            for line in command_lines(body):
                argv = shlex.split(line)
                commands.append((classify(argv, fence), argv, fence))
    for span in re.findall(r"`(python3 (?:-m campaign_tool|tools/fetch_osm_alprs\.py) [^`]*)`", FENCE.sub("", text)):
        argv = shlex.split(span)
        commands.append((classify(argv, "inline"), argv, "inline"))
    return commands


def substitute(argv, mapping):
    def one(token):
        return re.sub(r"\$([A-Z0-9_]+)", lambda m: str(mapping[m.group(1)]), token)
    return [one(token) for token in argv]


class Parsed(Exception):
    pass


def parse_only(kind, arguments):
    """The real parse of ``arguments`` (nothing runs); the Namespace, or DocumentError if the parser refuses."""
    original = argparse.ArgumentParser.parse_args

    def capture(self, args=None, namespace=None):
        raise Parsed(original(self, args, namespace))

    err = io.StringIO()
    with mock.patch.object(argparse.ArgumentParser, "parse_args", capture), contextlib.redirect_stderr(err):
        try:
            if kind == "cli":
                with mock.patch.object(sys, "argv", ["campaign_tool", *arguments]):
                    cli.main()
            else:
                tool.main(arguments)
        except Parsed as parsed:
            return parsed.args[0]
        except SystemExit:
            raise DocumentError(err.getvalue().strip().splitlines()[-1] if err.getvalue() else "refused") from None
    raise DocumentError("parser returned without parsing")


def run_cli(arguments):
    """The real CLI in process: (exit code, stdout, stderr)."""
    out = io.TextIOWrapper(io.BytesIO(), encoding="utf-8", newline="", write_through=True)
    err, code = io.StringIO(), 0
    with mock.patch.object(sys, "argv", ["campaign_tool", *map(str, arguments)]), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            cli.main()
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    return code, out.buffer.getvalue().decode("utf-8"), err.getvalue()


def run_tool(arguments):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = tool.main([str(a) for a in arguments])
    return code, out.getvalue(), err.getvalue()


def run_documented(kind, arguments):
    return run_tool(arguments) if kind == "tool" else run_cli(arguments)


def tree(root, top=None):
    root = Path(root)
    base = root / top if top else root
    return {str(p.relative_to(root)): (p.read_bytes() if p.is_file() else None) for p in sorted(base.rglob("*"))
            if not p.is_symlink()} if base.exists() else {}


# --- synthetic data ---------------------------------------------------------------------------------------------

def element(identifier, lat, lon, **tags):
    base = {"man_made": "surveillance", "surveillance:type": "ALPR"}
    return {"type": "node", "id": identifier, "lat": lat, "lon": lon, "tags": dict(base, **tags)}


def square(name, west, east):
    ring = [[west, 0.5], [east, 0.5], [east, 1.5], [west, 1.5], [west, 0.5]]
    return {"type": "Feature", "properties": {"NAME": name}, "geometry": {"type": "Polygon", "coordinates": [ring]}}


FIRST = {"osm3s": {"timestamp_osm_base": "2026-10-01T00:00:00Z"}, "elements": [
    element(7000001, 1.0, 0.6, manufacturer="Example Vendor", operator="Cedar PD", direction="N"),
    element(7000002, 1.0, 0.7),  # removed in the second answer
    element(7000003, 0.8, 0.9),  # moved ~55 m in the second answer
    element(7000004, 1.2, 0.8, operator="Cedar PD"),  # retagged in the second answer
    element(7000005, 0.9, 1.2)]}  # inside Synthetic Pine
SECOND = {"osm3s": {"timestamp_osm_base": "2026-10-20T00:00:00Z"}, "elements": [
    element(7000001, 1.0, 0.6, manufacturer="Example Vendor", operator="Cedar PD", direction="N"),
    element(7000003, 0.8, 0.9005),
    element(7000004, 1.2, 0.8, operator="Cedar Public Works"),
    element(7000005, 0.9, 1.2),
    element(7000006, 1.3, 0.55, manufacturer="Example Vendor")]}  # added


class Sandbox:
    """A synthetic campaign holding the first snapshot, and the guide's variables."""

    def __init__(self, case):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-map-guide-")
        case.addCleanup(temporary.cleanup)
        self.case, self.base = case, Path(temporary.name).resolve()
        self.root = self.base / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.map = self.root / "content" / "map"
        self.saved = self.base / "saved-second.json"
        self.filled = self.base / "filled.csv"
        self.write(self.base / "saved-first.json", FIRST)
        self.write(self.saved, SECOND)
        self.write(self.map / "city-boundaries.geojson", {"type": "FeatureCollection", "features": [
            square(CITY, 0.5, 1.0), square("Synthetic Pine", 1.0, 1.5)]})
        self.mapping = {"CAMPAIGN": self.root, "SAVED": self.saved, "FILLED": self.filled,
                        "NEW_CAMPAIGN": self.base / "fresh", "RELATION_ID": 12345,
                        "SOUTH": 0.5, "WEST": 0.5, "NORTH": 1.5, "EAST": 1.5}
        code, out, err = run_tool(["--directory", self.root, "--from-file", self.base / "saved-first.json"])
        case.assertEqual(code, 0, err)
        config = json.loads(read(self.map / "map-config.json"))
        self.write(self.map / "map-config.json", dict(config, initial_point_count=5))

    @staticmethod
    def write(path, value):
        Path(path).write_text(json.dumps(value), encoding="utf-8")

    def section_commands(self, number):
        """The ```sh commands of numbered task ``number``, substituted, as (kind, argv)."""
        body = sections(read(GUIDE))[TASKS[number - 1]]
        return [(kind, substitute(argv[(2 if kind == "tool" else 3):], self.mapping))
                for kind, argv, fence in documented(body) if fence == "sh"]

    def play(self, number):
        """Run task ``number``'s sh commands as written. A fetch ``--check`` exits 3 (the sample has a removal); every
        other command exits 0. Returns the outputs."""
        results = []
        for kind, arguments in self.section_commands(number):
            code, out, err = run_documented(kind, arguments)
            expected = 3 if kind == "tool" and "--check" in arguments else 0
            self.case.assertEqual(code, expected, f"{arguments}: {out}{err}")
            results.append(out)
        return results

    def sheet_csv(self):
        found = sorted((self.root / "kit" / "survey").glob("synthetic-cedar-*.csv"))
        self.case.assertEqual(len(found), 1, found)
        return found[0]

    def fill(self, rows, header_extra=None):
        """Write $FILLED: the sheet's CSV with {node: (status, checked_on, source, note)} in the four blank columns."""
        table = list(csv.reader(io.StringIO(self.sheet_csv().read_text(encoding="utf-8"), newline="")))
        for row in table[1:]:
            node = row[0].rsplit("/", 1)[1]
            if node in rows:
                row[10:14] = rows[node]
            if header_extra and node in header_extra:
                row[8] = header_extra[node]
        with self.filled.open("w", encoding="utf-8", newline="") as handle:
            csv.writer(handle).writerows(table)

    def importer(self, *extra):
        return run_cli(["map", "survey-import", "--csv", self.filled, "--directory", self.root, *extra])

    def checks(self):
        path = self.map / "point-checks.json"
        return json.loads(read(path))["checks"] if path.exists() else None

    def build(self):
        code, out, err = run_cli(["build", "--directory", self.root, "--check"])
        self.case.assertEqual(code, 0, out + err)
        return self.root / "public"


class GuideTests(unittest.TestCase):
    def setUp(self):
        patches = [mock.patch.object(socket, "socket", side_effect=NO_NETWORK),
                   mock.patch.object(socket, "create_connection", side_effect=NO_NETWORK),
                   mock.patch.object(urllib.request, "urlopen", side_effect=NO_NETWORK)]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    # --- links, anchors, structure
    def test_links_anchors_readme_and_required_sections(self):
        text = read(GUIDE)
        self.assertTrue(text.startswith("# Volunteer camera map guide\n"))
        self.assertEqual(check_links(GUIDE, text), [])
        self.assertEqual(check_links(README, read(README)), [])
        headings = re.findall(r"^## (\d+\. .*)$", text, re.M)
        self.assertEqual(headings, TASKS)  # exact order
        self.assertLess(text.index("## Before you start"), text.index("## " + TASKS[0]))
        self.assertGreater(text.index("## Troubleshooting"), text.index("## " + TASKS[-1]))
        reference = sections(text)
        for heading in TASKS:
            line = [part for part in reference[heading].splitlines() if part.startswith("Reference:")]
            self.assertEqual(len(line), 1, heading)
            self.assertRegex(line[0], r"\]\(SITE-CONTENT\.md#[a-z0-9-]+\)", heading)
        self.assertIn("SITE-CONTENT.md#help-map-page-starter", reference[TASKS[6]])
        self.assertIn("(SITE-CONTENT.md#map-data-and-maplibre)", text)
        self.assertEqual(read(README).count("[camera map guide](docs/MAP-GUIDE.md)"), 1)
        self.assertIn("](docs/REVIEW-GUIDE.md)", read(README))  # the older guide links stay
        self.assertIn("](docs/FINDINGS-GUIDE.md)", read(README))
        start = re.search(r"^## Start locally\s*$(.*?)(?=^## |\Z)", read(README), re.M | re.S).group(1)
        self.assertNotIn("MAP-GUIDE", start)
        lower = text.lower()
        for phrase in ("edits no openstreetmap data", "only when the organizer runs", "ownership",
                       "map data (c) openstreetmap contributors, odbl 1.0", "https://www.openstreetmap.org/copyright",
                       "deflock", "share-alike", "not a blanket licence", "never supplies, defaults, copies or infers a source",
                       "stay on public property, do not touch equipment, and never photograph people or license plates.",
                       "not a reviewed finding", "not one transaction across files", "exit 3"):
            self.assertIn(phrase, lower)
        for label, mutated in {
                "missing file": text.replace("(MAP-CONTROLLER.md)", "(NO-SUCH-FILE.md)"),
                "missing fragment": text.replace("#help-map-page-starter", "#no-such-heading", 1),
                "link to a missing guide": text + "\n[x](NO-SUCH-GUIDE.md)\n"}.items():
            with self.subTest(label):
                self.assertEqual(len(check_links(GUIDE, mutated)), 1)
        self.assertEqual(len(check_links(README, read(README).replace("(docs/MAP-GUIDE.md)", "(docs/MAP-GUIDES.md)"))), 1)

    def test_documented_commands_use_merged_parsers(self):
        mapping = Sandbox(self).mapping
        commands = documented(read(GUIDE))
        self.assertTrue(commands)
        seen, scopes = set(), set()
        for kind, argv, fence in commands:
            args = parse_only(kind, substitute(argv[(2 if kind == "tool" else 3):], mapping))
            if kind == "cli":
                seen.add((args.command, getattr(args, "map_command", None) or getattr(args, "content_command", None)))
            else:
                scopes.add(next(name for name in ("bbox", "relation", "from_file") if getattr(args, name)))
        self.assertEqual(seen, {("content-check", None), ("build", None), ("map", "survey-sheet"),
                                ("map", "survey-import"), ("content", "init"), ("content", "add-starter")})
        self.assertEqual(scopes, {"bbox", "relation", "from_file"})  # every scope alternative is shown
        city = [argv for kind, argv, fence in commands if "survey-sheet" in argv and fence == "sh"][0]
        self.assertEqual(city[city.index("--city") + 1], CITY)  # the quoted city survives shlex as one argument
        text = read(GUIDE)
        self.assertTrue(any("--move-metres" in argv and "--check" in argv for kind, argv, f in commands))
        sample = [argv for kind, argv, f in commands if kind == "cli" and "survey-import" in argv][0][3:]
        tool_sample = [argv for kind, argv, f in commands if kind == "tool" and "--check" in argv][0][2:]
        for label, kind, bad in (
                ("invented flag", "cli", [*sample, "--upload"]),
                ("invented subcommand", "cli", ["map", "survey-upload", "--directory", "$CAMPAIGN"]),
                ("unknown top-level command", "cli", ["map-init", "--directory", "$CAMPAIGN"]),
                ("missing required option", "cli", [t for i, t in enumerate(sample) if t != "--csv" and sample[i - 1] != "--csv"]),
                ("combined scopes", "tool", [*tool_sample, "--relation", "1"]),
                ("invented tool flag", "tool", [*tool_sample, "--refresh"])):
            with self.subTest(label), self.assertRaises(DocumentError):
                parse_only(kind, substitute(bad, mapping))
        with self.assertRaises(DocumentError):
            documented("```sh\nrm -rf x\n```\n")
        with self.assertRaises(DocumentError):
            documented("```sh\npython3 -m campaign_tool build --directory D --check\ncurl https://example.invalid\n```\n")
        self.assertIn("`--bbox`", text)

    # --- refresh
    def test_offline_refresh_preview_write_history(self):
        box = Sandbox(self)
        box.play(1)
        before = tree(box.root)
        previews = box.play(2)  # --check, then --check --move-metres
        self.assertEqual(tree(box.root), before)  # the preview writes nothing: not the map, snapshot or history
        first = previews[0].splitlines()[0]
        self.assertEqual(first, "added=1 removed=1 moved=1 retagged=1 unchanged=2 unkeyed_old=0")
        for kind, node in (("added", 7000006), ("removed", 7000002), ("moved", 7000003), ("retagged", 7000004)):
            self.assertRegex(previews[0], rf"{kind}:\n(  https://\S+\n)*  {re.escape(NODE)}{node}\n")
        code, out, err = run_tool(["--directory", box.root, "--from-file", box.saved, "--check", "--move-metres", "100"])
        self.assertEqual((code, out.splitlines()[0]), (3, "added=1 removed=1 moved=0 retagged=1 unchanged=3 unkeyed_old=0"))
        box.play(3)
        written = json.loads(read(box.map / "cameras.geojson"))
        self.assertEqual(sorted(f["properties"]["url"][len(NODE):] for f in written["features"]),
                         ["7000001", "7000003", "7000004", "7000005", "7000006"])
        self.assertEqual(json.loads(read(box.map / "snapshot.json"))["osm_base"], "2026-10-20T00:00:00Z")
        history = json.loads(read(box.map / "history.json"))["records"]
        self.assertEqual([(r["recorded_on"], r["added"], r["removed"]) for r in history],
                         [("2026-10-01", 5, 0), ("2026-10-20", 1, 1)])
        self.assertEqual(history[-1]["by_city"], {CITY: 4, "Synthetic Pine": 1})
        self.assertEqual(set(history[-1]), {"recorded_on", "osm_base", "source", "total", "by_city", "added", "removed"})
        self.assertFalse({"moved", "retagged"} & set(history[-1]))  # counts of added and removed only
        self.assertIn("Map data (c) OpenStreetMap contributors, ODbL 1.0", read(box.map / "attribution.txt"))

    def test_refresh_refusals_and_partial_history_write(self):
        box = Sandbox(self)
        # an empty answer never replaces a map with points: exit 3, bytes unchanged
        empty = box.base / "empty.json"
        box.write(empty, {"elements": []})
        cameras = (box.map / "cameras.geojson").read_bytes()
        code, out, err = run_tool(["--directory", box.root, "--from-file", empty])
        self.assertEqual((code, (box.map / "cameras.geojson").read_bytes()), (3, cameras))
        self.assertIn("current map kept", err)
        # no campaign.json: exit 2 before any read
        bare = box.base / "typo"
        bare.mkdir()
        code, out, err = run_tool(["--directory", bare, "--from-file", box.saved])
        self.assertEqual((code, list(bare.iterdir())), (2, []))
        # --check on a damaged map: exit 2, nothing written
        (box.map / "cameras.geojson").write_text("{", encoding="utf-8")
        before = tree(box.root)
        code, out, err = run_tool(["--directory", box.root, "--from-file", box.saved, "--check"])
        self.assertEqual((code, tree(box.root)), (2, before))
        self.assertIn("not readable GeoJSON", err)
        (box.map / "cameras.geojson").write_bytes(cameras)
        # a map day older than the last record: exit 2, the new map IS written, the history is not
        old = box.base / "old.json"
        box.write(old, dict(SECOND, osm3s={"timestamp_osm_base": "2026-09-01T00:00:00Z"}))
        history = (box.map / "history.json").read_bytes()
        code, out, err = run_tool(["--directory", box.root, "--from-file", old])
        self.assertEqual(code, 2)
        self.assertIn("history not updated", err)
        self.assertEqual((box.map / "history.json").read_bytes(), history)
        self.assertNotEqual((box.map / "cameras.geojson").read_bytes(), cameras)
        # a history file the build would refuse is never overwritten; the new map is written all the same
        (box.map / "history.json").write_text("{\"schema_version\": 2}", encoding="utf-8")
        code, out, err = run_tool(["--directory", box.root, "--from-file", box.saved])
        self.assertEqual((code, (box.map / "history.json").read_text(encoding="utf-8")), (2, "{\"schema_version\": 2}"))
        self.assertIn("history not updated", err)
        self.assertIn("7000006", read(box.map / "cameras.geojson"))
        # a symlinked output is refused
        link = box.map / "attribution.txt"
        link.unlink()
        link.symlink_to(box.base / "elsewhere.txt")
        (box.map / "history.json").unlink()
        code, out, err = run_tool(["--directory", box.root, "--from-file", box.saved])
        self.assertEqual(code, 2)
        self.assertIn("symlink", err)
        self.assertFalse((box.base / "elsewhere.txt").exists())

    # --- survey
    def test_survey_blank_preview_apply_roundtrip(self):
        box = Sandbox(self)
        box.play(3)
        self.assertEqual(box.play(4)[0].count("kit/survey/synthetic-cedar-"), 2)
        sheet = box.sheet_csv()
        self.assertEqual(stat.S_IMODE(sheet.stat().st_mode), 0o600)
        table = list(csv.reader(io.StringIO(sheet.read_text(encoding="utf-8"), newline="")))
        self.assertEqual(table[0][:10], ["node_url", "latitude", "longitude", "manufacturer", "operator", "direction",
                                         "latest_status", "latest_checked_on", "latest_source", "latest_note"])
        self.assertEqual(table[0][10:], ["status", "checked_on", "source", "note"])
        self.assertEqual([row[0] for row in table[1:]],
                         [NODE + n for n in ("7000001", "7000003", "7000004", "7000006")])  # Pine's point is not listed
        page = sheet.with_suffix(".html").read_text(encoding="utf-8")
        self.assertIn("Stay on public property", page)
        # a blank sheet adds nothing, even with --apply
        box.fill({})
        for extra in ((), ("--apply",)):
            code, out, err = box.importer(*extra)
            self.assertEqual(code, 0, err)
            self.assertEqual(json.loads(out)["added"], 0)
        self.assertIsNone(box.checks())
        # volunteered evidence: an https source, then a unique library prefix
        today = date.today().isoformat()
        box.fill({"7000001": ("seen_in_person", "2026-10-30", SOURCE_URL, "Pole at the corner"),
                  "7000003": ("in_agency_record", today, LIBRARY_PREFIX, "")})
        before = tree(box.root)
        preview, applied = box.play(5)
        self.assertEqual(json.loads(preview)["added"], 2)
        self.assertEqual(json.loads(applied)["applied"], True)
        changed = {name for name in tree(box.root) if tree(box.root).get(name) != before.get(name)}
        self.assertEqual(changed, {"content/map/point-checks.json"})  # only the checks file; preview wrote nothing first
        self.assertEqual([(c["node"], c["status"], c["source"]) for c in box.checks()],
                         [(7000001, "seen_in_person", SOURCE_URL), (7000003, "in_agency_record", LIBRARY_PREFIX)])
        box.play(6)
        public = box.root / "public"
        first = (public / "data" / "cameras.csv").read_bytes()
        self.assertEqual(first.decode("utf-8").split("\r\n")[0], CSV_HEADER)
        rows = list(csv.reader(io.StringIO(first.decode("utf-8"), newline="")))[1:]
        self.assertEqual([(r[0][len(NODE):], r[7], r[8]) for r in rows],
                         [("7000001", "seen_in_person", "2026-10-30"), ("7000003", "in_agency_record", today),
                          ("7000004", "", ""), ("7000005", "", ""), ("7000006", "", "")])
        self.assertIn("Organizer check", read(public / "cameras.html"))
        self.assertIn("cameras.csv", read(public / "data.html"))
        self.assertIn("ODbL", read(public / "data.html"))
        # two builds give identical bytes
        shutil.rmtree(public)
        self.assertEqual(box.build().joinpath("data", "cameras.csv").read_bytes(), first)
        # the private sheet never reaches the public tree
        for path in public.rglob("*"):
            if path.is_file():
                self.assertNotIn(b"Private worksheet", path.read_bytes(), path)
        self.assertFalse((public / "kit").exists())
        # the next sheet shows the newest check, agreeing with the CSV and the built page
        later = map_survey.survey_rows(box.root, CITY, today=date.today() + timedelta(days=1))[0]
        newest = {row["node_url"][len(NODE):]: (row["latest_status"], row["latest_checked_on"]) for row in later}
        self.assertEqual(newest["7000001"], ("seen_in_person", "2026-10-30"))
        self.assertEqual(newest["7000003"], ("in_agency_record", today))
        self.assertEqual(newest["7000004"], ("", ""))

    def test_import_source_and_identity_refusals_preserve_bytes(self):
        box = Sandbox(self)
        box.play(3)
        box.play(4)
        today = date.today()
        tomorrow = (today + timedelta(days=1)).isoformat()
        good = ("seen_in_person", "2026-10-30", SOURCE_URL, "")
        canary = "records.example.invalid"

        def refused(rows, line, header_extra=None, planted=None):
            box.fill(rows, header_extra)
            if planted is not None:
                (box.map / "point-checks.json").write_text(json.dumps(planted), encoding="utf-8")
            before = (tree(box.root, "content"), tree(box.root, "public"))
            for extra in ((), ("--apply",)):
                code, out, err = box.importer(*extra)
                self.assertEqual(code, 1, out + err)
                self.assertIn(line, err)
                self.assertNotIn(canary, out + err)
                self.assertEqual((tree(box.root, "content"), tree(box.root, "public")), before)

        refused({"7000001": ("seen_in_person", "2026-10-30", "", "")}, "row 2 source: required")
        refused({"7000001": ("", "2026-10-30", SOURCE_URL, "")}, "row 2 status: required")
        refused({"7000001": ("seen_in_person", "", SOURCE_URL, "")}, "row 2 checked_on: required")
        for label, bad in (("plain http", "http://records.example.invalid/a"),
                           ("userinfo", "https://" + "user" + ":" + "pw" + "@records.example.invalid/a"),
                           ("too long", "https://records.example.invalid/" + "a" * 500),
                           ("no library match", "0123456789ab"),
                           ("not a prefix", "records")):
            with self.subTest(label):
                refused({"7000001": ("seen_in_person", "2026-10-30", bad, "")}, "row 2 source: ")
        # a displayed old check's source or the node link is never copied into a new check
        refused({"7000001": ("seen_in_person", "2026-10-30", "", "")}, "row 2 source: required",
                header_extra={"7000001": SOURCE_URL})
        refused({"7000001": ("seen_in_person", tomorrow, SOURCE_URL, "")}, "row 2 checked_on: ")
        refused({"7000001": ("seen_in_person", "2026-02-30", SOURCE_URL, "")}, "row 2 checked_on: ")
        refused({"7000001": ("seen_in_person", "2026-10-30", SOURCE_URL, "Call 555 123 4567 first")}, "row 2 note: ")
        refused({"7000001": ("looks_fine", "2026-10-30", SOURCE_URL, "")}, "row 2 status: ")
        # an edited or unknown node link
        box.fill({"7000001": good})
        text = box.filled.read_text(encoding="utf-8")
        for label, replacement, line in (("edited", NODE + "7000001/", "row 2 node_url: edited"),
                                         ("unknown", NODE + "7999999", "row 2 node_url: not a mapped point")):
            with self.subTest(label):
                box.filled.write_text(text.replace(NODE + "7000001", replacement, 1), encoding="utf-8")
                before = tree(box.root, "content")
                code, out, err = box.importer("--apply")
                self.assertEqual(code, 1)
                self.assertIn(line, err)
                self.assertEqual(tree(box.root, "content"), before)
        # one good row beside one bad row: the whole import is refused
        refused({"7000001": good, "7000003": ("seen_in_person", "2026-10-30", "", "")}, "row 3 source: required")
        self.assertIsNone(box.checks())
        # a duplicate of an existing check
        box.fill({"7000001": good})
        self.assertEqual(box.importer("--apply")[0], 0)
        refused({"7000001": good}, "row 2: duplicate of an existing check")
        # a sixth check for one node
        five = [{"node": 7000001, "status": "seen_in_person", "checked_on": f"2026-10-0{n}", "source": SOURCE_URL}
                for n in range(1, 6)]
        refused({"7000001": ("duplicate", "2026-10-30", SOURCE_URL, "")}, "row 2 ",
                planted={"schema_version": 1, "checks": five})

    def test_optional_map_and_help_starter(self):
        box = Sandbox(self)
        fresh = box.mapping["NEW_CAMPAIGN"]
        self.assertEqual(run_cli(["init", "--directory", fresh, "--name", "Synthetic", "--county", "Synthetic County",
                                  "--state", "ca"])[0], 0)
        box.play(7)
        page = fresh / "content" / "pages" / "help-map.json"
        # the existing-campaign recipe ran as written: $CAMPAIGN had no pages/, now holds the same packaged bytes
        self.assertEqual((box.root / "content" / "pages" / "help-map.json").read_bytes(), page.read_bytes())
        document = json.loads(read(page))
        self.assertEqual(document, json.loads(read(REPO_ROOT / "examples" / "pages" / "help-map.json")))
        self.assertGreaterEqual(document["body_md"].count("[EDIT ME: verify for this campaign]"), 6)
        code, out, err = run_cli(["content-check", "--directory", fresh])
        self.assertEqual(code, 0, out + err)
        code, out, err = run_cli(["build", "--directory", fresh, "--check"])
        self.assertEqual(code, 0, out + err)
        public = fresh / "public"
        html = read(public / "help-map.html")
        self.assertIn("Help map the cameras", html)
        self.assertIn("[EDIT ME: verify for this campaign]", html)
        self.assertIn("ownership unconfirmed", html.lower())
        self.assertNotIn("/cameras.html", html)  # a fresh tree has no map, so the page links no map page
        self.assertFalse((public / "data" / "cameras.csv").exists())  # no map, no cameras.csv
        # an existing content/ is refused and left alone
        before = tree(fresh, "content")
        code, out, err = run_cli(box.section_commands(7)[0][1])
        self.assertEqual(code, 1)
        self.assertIn("content/ already exists", err)
        self.assertEqual(tree(fresh, "content"), before)
        # the guide's own words match the packaged starter's claims
        text = read(GUIDE)
        self.assertIn("[EDIT ME: verify for this campaign]", text)
        self.assertIn("content/pages/help-map.json", text)
        self.assertIn("/help-map.html", text)


if __name__ == "__main__":
    unittest.main()
