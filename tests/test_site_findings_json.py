"""data/findings.json open data for published findings (DF2237-09), built from a temp copy of the fictional example.

Synthetic content only; no network. Hex fixtures are generated at test time (no hash literals in this file).
"""
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool import law, site as site_module
from campaign_tool.content import CLASSIFICATIONS, CONFIDENCE, ContentError, load_content
from campaign_tool.site import (PUBLIC_CORRECTION_FIELDS, PUBLIC_FINDING_FIELDS, PUBLIC_SOURCE_FIELDS, Site,
                                build_site, short_hash)

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
SCHEMA = json.loads((REPO_ROOT / "schemas" / "findings-open-data.schema.json").read_text(encoding="utf-8"))
BASE = "https://cedar-records.example.invalid"
SLUG = "cedar-policy-posted"
NEVER = ("author", "state", "rule_ids", "duty", "rule_version", "exceptions", "sha256")


def hexes(n):
    return (hashlib.sha256(b"synthetic-df2237-09").hexdigest() * 2)[:n]


class FindingsJsonTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="synthetic-findings-json-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.public = self.root / "public"
        self.content = self.root / "content"

    def edit(self, name, change):
        path = self.content / name
        data = json.loads(path.read_text(encoding="utf-8"))
        change(data)
        path.write_text(json.dumps(data), encoding="utf-8")

    def add_finding(self, slug, fid, **changes):
        document = json.loads((self.content / "findings" / f"{SLUG}.json").read_text(encoding="utf-8"))
        document.update(slug=slug, id=fid, title=f"Synthetic finding {slug}", corrections=[], updated_at=None, **changes)
        (self.content / "findings" / f"{slug}.json").write_text(json.dumps(document), encoding="utf-8")

    def read(self, name):
        return (self.public / name).read_text(encoding="utf-8")

    def data(self):
        build_site(self.root)
        return json.loads(self.read("data/findings.json"))

    def snapshot(self):
        return {p.relative_to(self.public).as_posix(): p.read_bytes() for p in self.public.rglob("*") if p.is_file()}

    def assert_refused_and_untouched(self, message, patcher=None):
        build_site(self.root)
        before = self.snapshot()
        with self.assertRaisesRegex(ContentError, message):
            if patcher is None:
                build_site(self.root)
            else:
                with patcher:
                    build_site(self.root)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(sorted(p.name for p in self.root.iterdir() if p.name.startswith(".public")), [])

    def test_example_emits_only_public_fields_and_page_prefixes(self):
        doc = self.data()
        self.assertEqual(set(doc), {"schema_version", "campaign", "generated_from", "base_url", "url_kind", "findings"})
        self.assertEqual((doc["schema_version"], doc["campaign"], doc["generated_from"]),
                         (1, "Cedar County Records Project", "content/findings"))
        self.assertEqual(len(doc["findings"]), 1)
        finding = doc["findings"][0]
        self.assertEqual(set(finding), set(PUBLIC_FINDING_FIELDS))
        for key in NEVER:
            self.assertNotIn(key, finding)
        page = self.read(f"findings/{SLUG}.html")
        source_doc = json.loads((self.content / "findings" / f"{SLUG}.json").read_text(encoding="utf-8"))
        self.assertEqual(len(finding["sources"]), len(source_doc["sources"]))
        for source, original in zip(finding["sources"], source_doc["sources"]):
            self.assertEqual(set(source), set(PUBLIC_SOURCE_FIELDS))
            self.assertRegex(source["sha256_prefix"], r"^[0-9a-f]{12}$")
            self.assertEqual(source["sha256_prefix"], short_hash(original["sha256"]))
            self.assertIn(f"sha256 {source['sha256_prefix']}", page)
        text = self.read("data/findings.json")
        self.assertIsNone(re.search(r"[0-9a-fA-F]{40,}", text))
        self.assertNotIn("synthetic-author", text)
        self.assertEqual(text, json.dumps(doc, ensure_ascii=False, sort_keys=True, indent=2) + "\n")

    def test_urls_absolute_only_with_base_url(self):
        doc = self.data()
        self.assertEqual((doc["url_kind"], doc["base_url"]), ("absolute", BASE))
        self.assertEqual([f["url"] for f in doc["findings"]], [f"{BASE}/findings/{SLUG}.html"])
        self.edit("site.json", lambda d: d.update(base_url=BASE + "/"))
        doc = self.data()
        self.assertEqual((doc["base_url"], doc["findings"][0]["url"]), (BASE, f"{BASE}/findings/{SLUG}.html"))
        self.edit("site.json", lambda d: d.update(base_url=None))
        doc = self.data()
        self.assertEqual((doc["url_kind"], doc["base_url"]), ("relative", None))
        for finding in doc["findings"]:
            self.assertTrue(finding["url"].startswith("/findings/"), finding["url"])

    def test_corrections_match_page_and_uncorrected_is_empty(self):
        self.edit(f"findings/{SLUG}.json", lambda d: d.update(updated_at="2026-02-03T18:00:00+00:00"))
        self.add_finding("uncorrected", "fnd_0000000000000009")
        doc = self.data()
        ids = [f["id"] for f in load_content(self.root)["findings"]]
        self.assertEqual([f["id"] for f in doc["findings"]], ids)  # model order
        by_slug = {f["slug"]: f for f in doc["findings"]}
        corrected = by_slug[SLUG]
        page = self.read(f"findings/{SLUG}.html").split('id="corrections"')[1].split("</ul>")[0]
        on_page = re.findall(r'<time datetime="([^"]+)">[^<]+</time>: ([^<]+)</li>', page)
        self.assertTrue(on_page)
        self.assertEqual([(c["date"], c["note"]) for c in corrected["corrections"]], on_page)
        for correction in corrected["corrections"]:
            self.assertEqual(set(correction), set(PUBLIC_CORRECTION_FIELDS))
        self.assertEqual(corrected["updated_at"], "2026-02-03T18:00:00+00:00")
        self.assertEqual((by_slug["uncorrected"]["corrections"], by_slug["uncorrected"]["updated_at"]), ([], None))

    def test_no_published_findings_still_writes_valid_empty_file(self):
        (self.content / "findings" / f"{SLUG}.json").unlink()
        doc = self.data()
        self.assertEqual(doc["findings"], [])
        law.validate(doc, SCHEMA)
        self.assertIn('href="/data/findings.json"', self.read("findings/index.html"))

    def test_extra_model_key_is_never_emitted(self):
        real = site_module.load_content

        def leaky(root):
            model = real(root)
            for finding in model["findings"]:
                finding.update(reviewer_email="reviewer@example.invalid", author="synthetic-author", state="published",
                               rule_ids=["r1"], duty="d", rule_version="v", exceptions=[])
                for source in finding["sources"]:
                    source["private_path"] = "private/records/x.pdf"
                for correction in finding["corrections"]:
                    correction["editor"] = "synthetic-editor"
            return model
        with mock.patch.object(site_module, "load_content", leaky):
            build_site(self.root)
        text = self.read("data/findings.json")
        for leaked in ("reviewer_email", "reviewer@example.invalid", "private_path", "editor", "rule_ids", '"author"'):
            self.assertNotIn(leaked, text)
        law.validate(json.loads(text), SCHEMA)

    def test_leaking_builder_is_refused_and_public_untouched(self):
        real = Site._public_finding
        leaks = {
            "reviewer_email": lambda f: f.update(reviewer_email="reviewer@example.invalid"),
            "author": lambda f: f.update(author="synthetic-author"),
            "Title": lambda f: f.update(Title="case variant"),
            "sha256": lambda f: f["sources"][0].update(sha256="short"),
            "editor": lambda f: f.update(corrections=[{"date": "2026-02-03", "note": "n", "editor": "x"}]),
            "note": lambda f: f.update(limitations=[{"note": "a dict where a string belongs"}]),
        }
        for key, leak in leaks.items():
            with self.subTest(key=key):
                def builder(self_, finding, leak=leak):
                    value = real(self_, finding)
                    leak(value)
                    return value
                self.assert_refused_and_untouched(f"data/findings.json: non-public key {key}$",
                                                  mock.patch.object(Site, "_public_finding", builder))

    def test_full_hash_in_any_text_is_refused(self):
        original = json.loads((self.content / "findings" / f"{SLUG}.json").read_text(encoding="utf-8"))
        plants = {"summary": f"Record {hexes(64)} cited.", "body_md": f"See {hexes(40)}.",
                  "title": f"Upper {hexes(64).upper()}", "limitations": [f"x{hexes(41)}x"]}
        for key, value in plants.items():
            with self.subTest(key=key):
                (self.content / "findings" / f"{SLUG}.json").write_text(json.dumps(original), encoding="utf-8")
                build_site(self.root)
                self.edit(f"findings/{SLUG}.json", lambda d, key=key, value=value: d.update({key: value}))
                before = self.snapshot()
                with self.assertRaisesRegex(ContentError, "full hash"):
                    build_site(self.root)
                self.assertEqual(self.snapshot(), before)

    def test_39_hex_characters_are_not_a_full_hash(self):
        self.edit(f"findings/{SLUG}.json", lambda d: d.update(summary=f"Short {hexes(39)} id."))
        self.assertIn(hexes(39), self.data()["findings"][0]["summary"])

    def test_output_validates_against_schema_and_schema_rejects_drift(self):
        doc = self.data()
        law.validate(doc, SCHEMA)
        finding = SCHEMA["$defs"]["finding"]
        self.assertEqual(sorted(finding["properties"]["classification"]["enum"]), sorted(CLASSIFICATIONS))
        self.assertEqual(sorted(finding["properties"]["confidence"]["enum"]),
                         sorted(set(CONFIDENCE) - {"needs_attorney_review"}))
        self.assertEqual(set(finding["properties"]), set(PUBLIC_FINDING_FIELDS))
        self.assertEqual(set(SCHEMA["$defs"]["source"]["properties"]), set(PUBLIC_SOURCE_FIELDS))
        self.assertEqual(set(SCHEMA["$defs"]["correction"]["properties"]), set(PUBLIC_CORRECTION_FIELDS))
        for where in (SCHEMA, finding, SCHEMA["$defs"]["source"], SCHEMA["$defs"]["correction"]):
            self.assertIs(where["additionalProperties"], False)
        bad = [lambda d: d.update(extra=1), lambda d: d["findings"][0].update(author="a"),
               lambda d: d["findings"][0]["sources"][0].update(sha256_prefix=hexes(13)),
               lambda d: d["findings"][0]["sources"][0].update(sha256_prefix=hexes(12).upper()),
               lambda d: d["findings"][0].pop("url"), lambda d: d.update(url_kind="mixed")]
        for index, change in enumerate(bad):
            with self.subTest(index=index):
                broken = json.loads(json.dumps(doc))
                change(broken)
                with self.assertRaises(ValueError):
                    law.validate(broken, SCHEMA)

    def test_index_and_about_link_the_file_and_build_check_passes(self):
        written = {p.relative_to(self.public).as_posix() for p in build_site(self.root)}
        self.assertIn("data/findings.json", written)
        self.assertIn('<a href="/feed.xml">Atom feed</a> · <a href="/data/findings.json" type="application/json">'
                      'Open data (JSON)</a>', self.read("findings/index.html"))
        self.assertIn('<a href="/data/findings.json" type="application/json">data/findings.json</a>', self.read("about.html"))
        result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "build", "--directory", str(self.root), "--check"],
                                capture_output=True, text=True, cwd=REPO_ROOT, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("data/findings.json", result.stdout)


if __name__ == "__main__":
    unittest.main()
