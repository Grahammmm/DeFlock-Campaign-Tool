"""Organizer-transcribed local law overlay (DF1952-01): law/local-rules.json, validated and cited by findings.

Synthetic data only; the state package is a synthetic copy under a temporary jurisdictions base (``law.JURISDICTIONS``
patched). Nothing is fetched. Software validation shows the shape of a transcription, never that a source is official,
that a rule applies or that anyone reviewed it. Run from the repository root:
python3 -B -m unittest tests.test_local_law -v
"""
import contextlib
import copy
import hashlib
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool import cli, finding_law, finding_packet, law
from campaign_tool.cli import read_config
from campaign_tool.content import ContentError, load_content
from campaign_tool.content_check import content_problems, problem_record
from campaign_tool.site import Site, build_site

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
SLUG = "cedar-policy-posted"
POLICE, SHERIFF, COUNCIL = "ca-cedar-police", "ca-cedar-county-sheriff", "ca-cedar-city-council"
LOCAL = "local-us-ca-cedar-retention"
OTHER = "local-us-ca-cedar-signage"
LABEL = "local rule, organizer-transcribed, not legally reviewed"
SOURCE = "https://library.example.org/cedar/municode/5-1"


def synthetic_package(status="reviewed"):
    package = copy.deepcopy(law.load_package("us-ca"))
    template = package["rules"][0]
    package["rules"] = [dict(copy.deepcopy(template), rule_id="ca-alpha", citation="Synthetic Code sec. 1 & 2",
                             effective_from="2020-01-01", effective_to=None)]
    for scope in package["request_scopes"]:
        scope["rule_ids"] = ["ca-alpha"]
    package.update(status=status, reviewed_by=["Synthetic Reviewer"] if status == "reviewed" else [],
                   reviewed_at="2026-02-03" if status == "reviewed" else None)
    return package


def rule(**changes):
    base = {"rule_id": LOCAL, "citation": "Cedar Municipal Code 5.1 (2020 text)", "duty": "Delete plate reads after 30 days.",
            "source_url": SOURCE, "checked_on": "2026-01-02", "effective_from": "2020-01-01",
            "effective_to": "2021-06-30", "agency_ids": [POLICE]}
    base.update(changes)
    return base


def sha(n):
    return hashlib.sha256(f"synthetic-df1952-01-{n}".encode()).hexdigest()


def tree(folder):
    return {p.relative_to(folder).as_posix(): p.read_bytes() for p in sorted(Path(folder).rglob("*")) if p.is_file()}


class LocalLawCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-local-law-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.base = self.tmp / "jurisdictions"
        (self.base / "us-ca").mkdir(parents=True)
        self.package_file = self.base / "us-ca" / "package.json"
        self.package_file.write_text(json.dumps(synthetic_package()), encoding="utf-8")
        self.root = self.tmp / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        patcher = mock.patch.object(law, "JURISDICTIONS", self.base)
        patcher.start()
        self.addCleanup(patcher.stop)

    def overlay(self, rules=None, **top):
        document = {"schema_version": 1, "jurisdiction": "us-ca", "rules": [rule()] if rules is None else rules}
        document.update(top)
        (self.root / "law").mkdir(exist_ok=True)
        (self.root / "law" / "local-rules.json").write_text(json.dumps(document), encoding="utf-8")

    def finding(self, **changes):
        path = EXAMPLE / "content" / "findings" / f"{SLUG}.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["sources"] = [{"sha256": sha(i), "locator": f"page:{i + 1}", "title": "Synthetic record",
                                "rule_id": None} for i in range(3)]
        document.update(event_date="2021-04-01")
        document.update(changes)
        (self.root / "content" / "findings" / f"{SLUG}.json").write_text(json.dumps(document), encoding="utf-8")

    def load_error(self):
        try:
            load_content(self.root)
        except ContentError as exc:
            return str(exc)
        return None

    def law_check(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = law.main(["check", "--directory", str(self.root)])
        return code, out.getvalue(), err.getvalue()

    def refused(self, needle):
        """Build, content-check and ``law check`` refuse with the same line, which contains ``needle``."""
        error = self.load_error()
        self.assertIsNotNone(error, "load_content accepted the overlay")
        self.assertIn(needle, error)
        self.assertIn(error, content_problems(self.root))
        code, stdout, stderr = self.law_check()
        self.assertEqual((code, stdout), (1, ""))
        if error.startswith("law/"):  # an agency-file refusal is not a law/ line, but is still shared
            self.assertEqual(stderr, f"Stopped: {error}\n")
        return error

    def page(self):
        self.assertTrue(build_site(self.root))
        return (self.root / "public" / "findings" / f"{SLUG}.html").read_text(encoding="utf-8")

    def section(self, html):
        start = html.index('<section class="legal-basis">')
        return html[start:html.index("</section>", start) + len("</section>")]


class ValidOverlayTests(LocalLawCase):
    def test_valid_overlay_and_scoped_event_versions(self):
        v1, v2 = rule(), rule(citation="Cedar Municipal Code 5.1 (2022 text)", effective_from="2021-07-01",
                              effective_to=None)
        sheriff = rule(rule_id=OTHER, citation="Cedar Sign Code 9", agency_ids=[SHERIFF, COUNCIL], effective_to=None)
        self.overlay([v2, sheriff, v1])
        package_bytes = self.package_file.read_bytes()
        package = law.load_package("us-ca")
        before = copy.deepcopy(package)
        loaded = law.load_local_rules(self.root, package, [{"agency_id": POLICE}, {"agency_id": SHERIFF},
                                                             {"agency_id": COUNCIL}])
        self.assertEqual([(r["rule_id"], r["effective_from"]) for r in loaded],
                         [(LOCAL, "2020-01-01"), (LOCAL, "2021-07-01"), (OTHER, "2020-01-01")])
        self.assertEqual(set(loaded[0]), {"rule_id", "citation", "duty", "source_url", "checked_on", "effective_from",
                                          "effective_to", "agency_ids"})
        # inclusive bounds, null end, scope isolation; the old two-argument result is unchanged
        def ids(day, agency=POLICE):
            return [(r["rule_id"], r["effective_from"])
                    for r in law.rules_in_force(package, day, local_rules=loaded, agency_id=agency)]
        self.assertEqual(ids("2021-06-30"), [("ca-alpha", "2020-01-01"), (LOCAL, "2020-01-01")])
        self.assertEqual(ids("2021-07-01"), [("ca-alpha", "2020-01-01"), (LOCAL, "2021-07-01")])
        self.assertEqual(ids("2019-12-31"), [])
        self.assertEqual(ids("2999-01-01"), [("ca-alpha", "2020-01-01"), (LOCAL, "2021-07-01")])
        self.assertEqual(ids("2021-06-30", SHERIFF), [("ca-alpha", "2020-01-01"), (OTHER, "2020-01-01")])
        self.assertEqual(ids("2021-06-30", "ca-elsewhere"), [("ca-alpha", "2020-01-01")])
        self.assertEqual(ids("2021-06-30", None), [("ca-alpha", "2020-01-01")])
        self.assertEqual(law.rules_in_force(package, "2021-06-30", local_rules=loaded), law.rules_in_force(package, "2021-06-30"))
        self.assertEqual(package, before)
        self.assertEqual(self.package_file.read_bytes(), package_bytes)
        self.assertEqual(self.law_check()[0], 0)
        self.assertEqual(json.loads(self.law_check()[1])["rule_ids"], [LOCAL, OTHER])

    def test_both_citation_forms_are_shown_once_with_the_label_and_state_status_is_kept(self):
        v2 = rule(citation="Cedar Municipal Code 5.1 & (2022 text)", effective_from="2021-07-01", effective_to=None)
        self.overlay([rule(), v2])
        self.finding(rule_ids=[LOCAL, "ca-alpha"])
        document = json.loads((self.root / "content" / "findings" / f"{SLUG}.json").read_text(encoding="utf-8"))
        document["sources"][1]["rule_id"] = document["sources"][2]["rule_id"] = LOCAL  # repeated sources, one citation
        (self.root / "content" / "findings" / f"{SLUG}.json").write_text(json.dumps(document), encoding="utf-8")
        part = self.section(self.page())
        self.assertEqual(part.count(LABEL), 1)
        self.assertEqual(part.count(f"<code>{LOCAL}</code>"), 1)
        self.assertIn(f"<li><code>{LOCAL}</code> — Cedar Municipal Code 5.1 (2020 text) ({LABEL})</li>", part)
        # the state item keeps its plain citation; the state package status is not applied to the local item
        self.assertIn("<li><code>ca-alpha</code> — Synthetic Code sec. 1 &amp; 2</li>", part)
        self.assertIn('<p class="notice">Law package status: reviewed on 2026-02-03.</p>', part)
        # a later event date selects the later version, escaped once
        self.finding(rule_ids=[LOCAL], event_date="2021-08-01")
        part = self.section(self.page())
        self.assertIn(f"Cedar Municipal Code 5.1 &amp; (2022 text) ({LABEL})</li>", part)
        self.assertNotIn("&amp;amp;", part)
        self.assertNotIn("Law package status", part)  # only local items cited: no package status is implied

    def test_a_local_only_citation_never_reads_the_state_package(self):
        self.overlay()
        self.finding(rule_ids=[LOCAL])
        self.package_file.unlink()
        self.assertIsNone(self.load_error())
        self.assertEqual(content_problems(self.root), [])
        self.assertIn(LABEL, self.section(self.page()))


class RefusalTests(LocalLawCase):
    def test_namespace_collision_source_and_window_refusals(self):
        sheet = "law/local-rules.json.rules[0]."
        cases = [
            ("state collision", rule(rule_id="ca-alpha"), sheet + "rule_id: 'ca-alpha' is a state rule id"),
            ("wrong state", rule(rule_id="local-us-tx-cedar"), sheet + "rule_id: must be a lowercase slug"),
            ("no prefix", rule(rule_id="cedar-retention"), sheet + "rule_id: must be a lowercase slug"),
            ("empty suffix", rule(rule_id="local-us-ca-"), sheet + "rule_id: must be a lowercase slug"),
            ("uppercase", rule(rule_id="local-us-ca-Cedar"), sheet + "rule_id: must be a lowercase slug"),
            ("rule_id type", rule(rule_id=7), sheet + "rule_id: must be a string"),
            ("source http", rule(source_url="http://library.example.org/x"), sheet + "source_url: must be an https:// URL"),
            ("source credentials", rule(source_url="https://user:pw@library.example.org/x"), sheet + "source_url:"),
            ("source control", rule(source_url="https://library.example.org/a\nb"), sheet + "source_url:"),
            ("source type", rule(source_url=None), sheet + "source_url:"),
            ("source empty", rule(source_url=""), sheet + "source_url:"),
            ("citation empty", rule(citation="  "), sheet + "citation: must be non-empty plain text"),
            ("citation markup", rule(citation="<b>x</b>"), sheet + "citation: must be non-empty plain text"),
            ("duty empty", rule(duty=""), sheet + "duty: must be non-empty plain text"),
            ("checked_on invalid", rule(checked_on="2026-13-01"), sheet + "checked_on: not a calendar date"),
            ("checked_on format", rule(checked_on="2026-1-2"), sheet + "checked_on: expected a YYYY-MM-DD date"),
            ("checked_on future", rule(checked_on="2999-01-01"), sheet + "checked_on: must not be after today"),
            ("from invalid", rule(effective_from="soon"), sheet + "effective_from: expected a YYYY-MM-DD date"),
            ("reversed", rule(effective_from="2021-07-01", effective_to="2021-06-30"), sheet + "effective_to: precedes"),
            ("agencies empty", rule(agency_ids=[]), sheet + "agency_ids: must be a list of 1 to 100"),
            ("agencies type", rule(agency_ids=POLICE), sheet + "agency_ids: must be a list of 1 to 100"),
            ("agency type", rule(agency_ids=[POLICE, 7]), sheet + "agency_ids[1]: must be a non-empty string"),
            ("agency duplicate", rule(agency_ids=[POLICE, POLICE]), sheet + "agency_ids[1]: duplicate agency id"),
            ("agency unknown", rule(agency_ids=[POLICE, "ca-nowhere"]), sheet + "agency_ids[1]: 'ca-nowhere' is not a loaded"),
        ]
        for name, document, needle in cases:
            with self.subTest(name):
                self.overlay([document])
                self.refused(needle)
        missing = rule()
        del missing["source_url"]
        self.overlay([missing])
        self.refused(sheet + "source_url: required field is missing")
        self.overlay(jurisdiction="us-tx")
        self.refused("law/local-rules.json.jurisdiction: 'us-tx' does not match the campaign jurisdiction 'us-ca'")
        self.overlay(jurisdiction=None)
        self.refused("law/local-rules.json.jurisdiction:")

    def test_window_overlaps_are_refused_at_the_later_effective_from(self):
        later = "law/local-rules.json.rules[1].effective_from: window overlaps rules[0]"
        for name, second in (("same day", rule(effective_from="2021-06-30", effective_to=None)),
                             ("inside", rule(effective_from="2020-06-01", effective_to="2020-06-02")),
                             ("other agencies", rule(effective_from="2021-01-01", effective_to=None,
                                                     agency_ids=[SHERIFF])),
                             ("identical", rule())):
            with self.subTest(name):
                self.overlay([rule(), second])
                self.refused(later)
        self.overlay([rule(effective_to=None), rule(effective_from="2030-01-01", effective_to=None)])
        self.refused(later)  # a null end is unbounded
        self.overlay([rule(effective_from="2021-07-01", effective_to=None), rule(effective_to=None)])
        self.refused("law/local-rules.json.rules[0].effective_from: window overlaps rules[1]")  # file order is irrelevant
        # different ids and adjacent windows coexist
        self.overlay([rule(), rule(effective_from="2021-07-01", effective_to=None), rule(rule_id=OTHER)])
        self.assertIsNone(self.load_error())

    def test_bad_file_and_unused_overlay_refused(self):
        path = self.root / "law" / "local-rules.json"
        (self.root / "law").mkdir()
        valid = json.dumps({"schema_version": 1, "jurisdiction": "us-ca", "rules": [rule()]})
        cases = [
            ("not json", b"{not json", "law/local-rules.json: not valid UTF-8 JSON"),
            ("bad utf8", b'{"schema_version": 1, "x": "\xff"}', "law/local-rules.json: not valid UTF-8 JSON"),
            ("bom", b"\xef\xbb\xbf" + valid.encode(), "law/local-rules.json: not valid UTF-8 JSON"),
            ("nan", valid.replace('"rules"', '"x": NaN, "rules"').encode(), "law/local-rules.json: NaN is not allowed"),
            ("duplicate key", valid.replace('"schema_version": 1', '"schema_version": 1, "schema_version": 1').encode(),
             "law/local-rules.json: duplicate key 'schema_version'"),
            ("duplicate rule key", valid.replace('"duty"', '"citation": "x", "duty"').encode(),
             "law/local-rules.json: duplicate key 'citation'"),
            ("not an object", b"[]", "law/local-rules.json: expected an object"),
            ("extra top key", json.dumps({"schema_version": 1, "jurisdiction": "us-ca", "rules": [], "status": "x"}).encode(),
             "law/local-rules.json: unknown field(s) ['status']"),
            ("missing top key", json.dumps({"schema_version": 1, "jurisdiction": "us-ca"}).encode(),
             "law/local-rules.json.rules: required field is missing"),
            ("bool version", json.dumps({"schema_version": True, "jurisdiction": "us-ca", "rules": []}).encode(),
             "law/local-rules.json.schema_version: must be the integer 1"),
            ("float version", json.dumps({"schema_version": 1.0, "jurisdiction": "us-ca", "rules": []}).encode(),
             "law/local-rules.json.schema_version: must be the integer 1"),
            ("string version", json.dumps({"schema_version": "1", "jurisdiction": "us-ca", "rules": []}).encode(),
             "law/local-rules.json.schema_version: must be the integer 1"),
            ("rules type", json.dumps({"schema_version": 1, "jurisdiction": "us-ca", "rules": {}}).encode(),
             "law/local-rules.json.rules: must be a list"),
            ("rule type", json.dumps({"schema_version": 1, "jurisdiction": "us-ca", "rules": ["x"]}).encode(),
             "law/local-rules.json.rules[0]: expected an object"),
            ("review field", json.dumps({"schema_version": 1, "jurisdiction": "us-ca",
                                         "rules": [rule(review="verified")]}).encode(),
             "law/local-rules.json.rules[0]: unknown field(s) ['review']"),
            ("status field", json.dumps({"schema_version": 1, "jurisdiction": "us-ca",
                                         "rules": [rule(status="reviewed")]}).encode(),
             "law/local-rules.json.rules[0]: unknown field(s) ['status']"),
            ("oversize", b" " * (law.LOCAL_RULES_MAX_BYTES + 1), "law/local-rules.json: file larger than 1048576 bytes"),
        ]
        for name, data, needle in cases:
            with self.subTest(name):
                path.write_bytes(data)
                self.assertIn(needle, self.refused(needle))
        path.write_bytes(b" " * law.LOCAL_RULES_MAX_BYTES + b"")  # exactly 1 MiB of blanks is size-ok but not JSON
        self.assertIn("not valid UTF-8 JSON", self.refused("law/local-rules.json:"))
        # no finding cites the overlay, yet a malformed one stops the build and leaves public/ alone
        path.write_bytes(b"{not json")
        with self.assertRaises(ContentError):
            build_site(self.root)
        self.assertFalse((self.root / "public").exists())

    def test_nonregular_symlink_dangling_and_missing_campaign_inputs_are_refused(self):
        path = self.root / "law" / "local-rules.json"
        (self.root / "law").mkdir()
        path.mkdir()
        self.refused("law/local-rules.json: must be a regular file")
        path.rmdir()
        real = self.tmp / "elsewhere.json"
        real.write_text(json.dumps({"schema_version": 1, "jurisdiction": "us-ca", "rules": []}), encoding="utf-8")
        os.symlink(real, path)
        self.refused("law/local-rules.json: must be a regular file")
        path.unlink()
        os.symlink(self.tmp / "no-such-file.json", path)  # dangling
        self.refused("law/local-rules.json: must be a regular file")
        path.unlink()
        (self.root / "law").rmdir()
        shutil.copytree(self.root / "content", self.tmp / "content-copy")  # a real folder to point at
        (self.tmp / "realdir").mkdir()
        (self.tmp / "realdir" / "local-rules.json").write_text("{}", encoding="utf-8")
        os.symlink(self.tmp / "realdir", self.root / "law")
        self.refused("law/: must be a plain folder")
        (self.root / "law").unlink()
        os.symlink(self.tmp / "nowhere", self.root / "law")  # dangling folder link
        self.refused("law/: must be a plain folder")
        (self.root / "law").unlink()
        (self.root / "law").write_text("x", encoding="utf-8")  # a file where the folder belongs
        self.refused("law/: must be a plain folder")
        (self.root / "law").unlink()
        self.overlay()
        (self.root / "campaign.json").unlink()
        error = self.load_error()
        self.assertEqual(error, "law/local-rules.json: needs a campaign.json to name the campaign's jurisdiction")
        self.assertIn(error, content_problems(self.root))
        self.assertEqual(self.law_check()[2], f"Stopped: {error}\n")

    def test_agency_scope_uses_loaded_agencies_and_a_refused_agency_file_is_named_once(self):
        self.overlay([rule(agency_ids=[POLICE, "ca-not-loaded"])])
        self.refused("law/local-rules.json.rules[0].agency_ids[1]: 'ca-not-loaded' is not a loaded campaign agency")
        agencies = self.root / "content" / "agencies.json"
        agencies.write_text("{broken", encoding="utf-8")
        problems = content_problems(self.root)
        self.assertEqual([line for line in problems if "agency_ids" in line], [])
        self.assertTrue(any(line.startswith("content/agencies.json") for line in problems), problems)
        self.assertTrue(self.load_error().startswith("agencies.json: "), self.load_error())


class LegacyBytesTests(LocalLawCase):
    def test_absent_empty_and_legacy_bytes(self):
        self.finding(rule_ids=["ca-alpha"])
        package = law.load_package("us-ca")
        self.assertEqual(law.load_local_rules(self.root, package, []), [])
        self.assertEqual(finding_law.check(self.root, load_content(self.root)["findings"]), ([], []))
        self.assertEqual(load_content(self.root)["local_rules"], [])
        absent = self.page()
        absent_tree = tree(self.root / "public")
        shutil.rmtree(self.root / "public")
        self.overlay([])
        self.assertEqual(self.law_check()[0], 0)
        self.assertEqual(json.loads(self.law_check()[1])["rule_count"], 0)
        self.assertEqual(self.page(), absent)
        self.assertEqual(tree(self.root / "public"), absent_tree)
        self.assertIn("<li><code>ca-alpha</code> — Synthetic Code sec. 1 &amp; 2</li>", absent)
        self.assertNotIn(LABEL, absent)
        # old call shapes keep their exact results
        self.assertEqual([r["rule_id"] for r in law.rules_in_force(package, "2021-01-01")], ["ca-alpha"])
        self.assertEqual(law.rules_in_force(package, "2019-12-31"), [])
        # law check on a campaign with no overlay: exit 0 and nothing written
        shutil.rmtree(self.root / "law")
        code, stdout, stderr = self.law_check()
        self.assertEqual((code, stderr, json.loads(stdout)["local_rules"]), (0, "", "absent"))

    def test_no_citation_and_no_overlay_never_reads_the_package_or_the_agencies(self):
        self.finding()
        with mock.patch.object(law, "load_package", side_effect=AssertionError("package read")) as package, \
                mock.patch.object(law, "load_local_rules", side_effect=AssertionError("overlay read")) as overlay:
            self.assertIsNone(self.load_error())
            self.assertEqual(content_problems(self.root), [])
            self.assertTrue(build_site(self.root))
        package.assert_not_called()
        overlay.assert_not_called()

    def test_legacy_law_cli_output_is_exact(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = law.main(["rules", "us-ca", "--base", str(self.base), "--on", "2021-01-01"])
        package = law.load_package("us-ca")
        expected = {"jurisdiction": "us-ca", "status": "reviewed", "on": "2021-01-01", "rule_count": 1,
                    "rules": [law._rule_view(package["rules"][0])], "note": law.HOLIDAY_NOTE}
        self.assertEqual((code, out.getvalue()), (0, json.dumps(expected, indent=2, ensure_ascii=False) + "\n"))
        with self.assertRaises(SystemExit) as raised, contextlib.redirect_stderr(io.StringIO()):
            law.main(["check"])  # argparse usage error
        self.assertEqual(raised.exception.code, 2)


class LabelTests(LocalLawCase):
    def test_local_labels_never_review_state(self):
        self.overlay([rule(), rule(rule_id=OTHER, agency_ids=[SHERIFF], effective_to=None)])
        self.finding(rule_ids=[LOCAL])
        html = self.page()
        part = self.section(html)
        self.assertEqual(part.count(LABEL), 1)
        self.assertNotIn("reviewed on", part)  # a reviewed package is not implied by a local-only citation
        self.assertNotIn("Law package status", part)
        for absent in ("Delete plate reads after 30 days.", SOURCE, "Duty:"):
            self.assertNotIn(absent, html)  # no duty is inferred from the transcription
        # hand-built model: the renderer escapes whatever it is given, once
        model = load_content(self.root)
        model["local_rules"] = [dict(model["local_rules"][0], citation='<b>Code</b> "5" & 6')]
        finding = dict(model["findings"][0])
        part = Site(self.root, read_config(self.root), model).legal_basis(finding)
        self.assertIn("&lt;b&gt;Code&lt;/b&gt; &quot;5&quot; &amp; 6", part)
        self.assertNotIn("<b>", part)
        self.assertEqual(part.count(LABEL), 1)

    def test_unknown_out_of_scope_and_out_of_force_local_citations_are_refused_everywhere(self):
        self.overlay()
        draft_path = self.tmp / "draft.json"
        cases = [
            ("unknown id", dict(rule_ids=["local-us-ca-ghost"]),
             f"findings/{SLUG}: local rule local-us-ca-ghost is not in law/local-rules.json"),
            ("other agency", dict(rule_ids=[LOCAL], agency_id=SHERIFF),
             f"findings/{SLUG}: local rule {LOCAL} does not apply to agency {SHERIFF}"),
            ("no agency", dict(rule_ids=[LOCAL], agency_id=None),
             f"findings/{SLUG}: local rule {LOCAL} does not apply to agency None"),
            ("expired", dict(rule_ids=[LOCAL], event_date="2021-07-01"),
             f"findings/{SLUG}: local rule {LOCAL} is not in force on 2021-07-01 (effective 2020-01-01 to 2021-06-30)"),
            ("not yet", dict(rule_ids=[LOCAL], event_date="2019-12-31"),
             f"findings/{SLUG}: local rule {LOCAL} is not in force on 2019-12-31 (effective 2020-01-01 to 2021-06-30)"),
            ("source form", dict(rule_ids=[], event_date="2021-07-01", cite_in_source=True),
             f"findings/{SLUG}: local rule {LOCAL} is not in force on 2021-07-01"),
        ]
        for name, changes, expected in cases:
            with self.subTest(name):
                cite = changes.pop("cite_in_source", False)
                self.finding(**changes)
                if cite:
                    document = json.loads((self.root / "content" / "findings" / f"{SLUG}.json").read_text(encoding="utf-8"))
                    document["sources"][0]["rule_id"] = LOCAL
                    (self.root / "content" / "findings" / f"{SLUG}.json").write_text(json.dumps(document), encoding="utf-8")
                self.assertTrue(self.load_error().startswith(expected), self.load_error())
                self.assertTrue(any(line.startswith(expected) for line in content_problems(self.root)))
                with self.assertRaises(ContentError):
                    build_site(self.root)
                self.assertFalse((self.root / "public").exists())
                finding = json.loads((self.root / "content" / "findings" / f"{SLUG}.json").read_text(encoding="utf-8"))
                self.assertTrue(finding_law.check_findings(self.root, [dict(finding, slug=SLUG)])[0].startswith(expected))
                self.finding()  # the published finding is fine; only the draft cites the local rule
                draft = dict(finding, slug="cedar-draft", id="fnd_00000000000000d1", state="draft", published_at=None)
                draft_path.write_text(json.dumps(draft), encoding="utf-8")
                refusal = "draft " + expected.replace(f"findings/{SLUG}", "findings/cedar-draft")
                for write in (lambda: cli.finding_preview(self.root, draft_path, self.tmp / "preview"),
                              lambda: finding_packet.build_packet(self.root, draft_path, self.tmp / "packet")):
                    with self.assertRaises(ValueError) as raised:
                        write()
                    self.assertTrue(str(raised.exception).startswith(refusal), raised.exception)
                self.assertFalse((self.tmp / "preview").exists() or (self.tmp / "packet").exists())

    def test_a_valid_local_citation_previews_with_its_label(self):
        self.overlay()
        self.finding(rule_ids=[])
        draft = json.loads((EXAMPLE / "content" / "findings" / f"{SLUG}.json").read_text(encoding="utf-8"))
        draft.update(slug="cedar-draft", id="fnd_00000000000000d1", state="draft", published_at=None,
                     event_date="2021-04-01", rule_ids=[LOCAL])
        path = self.tmp / "draft.json"
        path.write_text(json.dumps(draft), encoding="utf-8")
        cli.finding_preview(self.root, path, self.tmp / "preview")
        page = (self.tmp / "preview" / "findings" / "cedar-draft.html").read_text(encoding="utf-8")
        self.assertIn(f"<li><code>{LOCAL}</code> — Cedar Municipal Code 5.1 (2020 text) ({LABEL})</li>", page)


class CheckCommandTests(LocalLawCase):
    def test_check_read_only_and_handoff(self):
        self.overlay([rule(), rule(effective_from="2021-07-01", effective_to=None, citation="Cedar Code 5.1 (2022)")])
        self.finding(rule_ids=[LOCAL])
        before = {p: (d, os.stat(self.root / p).st_mtime_ns) for p, d in tree(self.root).items()}
        package_bytes = self.package_file.read_bytes()
        code, stdout, stderr = self.law_check()
        self.assertEqual((code, stderr), (0, ""))
        report = json.loads(stdout)
        self.assertEqual((report["local_rules"], report["rule_count"], report["label"]), ("valid", 2, LABEL))
        after = {p: (d, os.stat(self.root / p).st_mtime_ns) for p, d in tree(self.root).items()}
        self.assertEqual(before, after)  # no file added, removed, rewritten or touched
        self.assertFalse((self.root / "public").exists())
        self.assertEqual(self.package_file.read_bytes(), package_bytes)
        self.assertEqual(law.load_package("us-ca")["status"], "reviewed")
        # the handoff to DF1952-02: every version in the model, with exactly the validated fields
        model = load_content(self.root)
        self.assertEqual([r["effective_from"] for r in model["local_rules"]], ["2020-01-01", "2021-07-01"])
        self.assertTrue(all(set(r) == law.LOCAL_RULE_KEYS for r in model["local_rules"]))
        # file attribution and exit codes for invalid input and bad usage
        self.overlay([rule(source_url="http://x.example.org/a")])
        code, stdout, stderr = self.law_check()
        self.assertEqual((code, stdout), (1, ""))
        self.assertEqual(stderr, "Stopped: law/local-rules.json.rules[0].source_url: must be an https:// URL\n")
        self.assertEqual(problem_record(stderr[len("Stopped: "):].strip()),
                         {"file": "law/local-rules.json.rules[0].source_url", "message": "must be an https:// URL"})
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(law.main(["check", "--directory", str(self.tmp / "no-such-folder")]), 1)
        self.assertTrue(err.getvalue().startswith("Stopped: "), err.getvalue())

    def test_check_names_a_cited_local_rule_problem_only_through_content_check(self):
        self.overlay()
        self.finding(rule_ids=["local-us-ca-ghost"])
        self.assertEqual(self.law_check()[0], 0)  # the overlay itself is valid; citations belong to content-check
        self.assertEqual(len(content_problems(self.root)), 1)


if __name__ == "__main__":
    unittest.main()
