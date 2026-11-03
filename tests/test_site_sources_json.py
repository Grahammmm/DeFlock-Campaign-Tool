"""data/sources.json: the source library as open data (DF1822-14), built from a temp copy of the fictional example.

Synthetic content only; no network. Hex fixtures are generated at test time (no hash literals in this file).
"""
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool import law
from campaign_tool.content import ContentError
from campaign_tool.site import PUBLIC_LIBRARY_FIELDS, PUBLIC_LIBRARY_TOP_LEVEL, Site, build_site

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
SCHEMA = json.loads((REPO_ROOT / "schemas" / "sources-open-data.schema.json").read_text(encoding="utf-8"))
BASE = "https://cedar-records.example.invalid"
SLUG = "cedar-policy-posted"
F1, F2 = "fnd_0000000000000001", "fnd_0000000000000002"
NOTE = "Private synthetic custody note"
LINK = '<a href="/data/sources.json" type="application/json">'


def sha(label):
    return hashlib.sha256(f"synthetic-df1822-14-{label}".encode()).hexdigest()


A, B, C = sha("a"), sha("b"), sha("c")
LIBRARY = [
    {"sha256": A, "title": "Synthetic ALPR policy", "agency_id": "ca-cedar-police", "received_at": "2026-01-20T16:30:00+00:00",
     "public_url": "https://example.invalid/fixtures/policy", "fragment": "cedar-police-records", "pages": 2, "note": NOTE},
    {"sha256": B, "title": "Synthetic audit log", "agency_id": "ca-cedar-police", "received_at": "2026-01-22T09:00:00+00:00",
     "public_url": None, "fragment": "cedar-police-records"},
    {"sha256": C, "title": "Synthetic vendor letter", "agency_id": "ca-unconfigured-agency",
     "received_at": "2026-01-25T10:00:00Z", "public_url": None, "fragment": "unconfigured-records", "pages": 0},
]


class SourcesJsonTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="synthetic-sources-json-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.public = self.root / "public"
        self.content = self.root / "content"
        (self.content / "sources.json").write_text(json.dumps(LIBRARY), encoding="utf-8")
        self.finding(SLUG, lambda d: d["sources"][0].update(sha256=A))

    def finding(self, name, change):
        path = self.content / "findings" / f"{name}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        change(data)
        path.write_text(json.dumps(data), encoding="utf-8")

    def add_finding(self, slug, fid, shas, **changes):
        document = json.loads((self.content / "findings" / f"{SLUG}.json").read_text(encoding="utf-8"))
        document.update(slug=slug, id=fid, title=f"Synthetic finding {slug}", corrections=[], updated_at=None,
                        sources=[{"sha256": s, "locator": f"page:{i + 1}", "title": "Synthetic"} for i, s in enumerate(shas)],
                        **changes)
        (self.content / "findings" / f"{slug}.json").write_text(json.dumps(document), encoding="utf-8")

    def read(self, name):
        return (self.public / name).read_text(encoding="utf-8")

    def data(self):
        build_site(self.root)
        return json.loads(self.read("data/sources.json"))

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

    def test_three_sources_give_the_exact_document_and_validate(self):
        self.add_finding("second", F2, [A, B, A], published_at="2026-03-01T18:00:00+00:00")
        doc = self.data()
        police = "Cedar Police Department (fictional)"
        expected = {
            "schema_version": 1, "campaign": "Cedar County Records Project", "generated_from": "content/sources.json",
            "base_url": BASE, "url_kind": "absolute",
            "sources": [
                {"sha256_prefix": A[:12], "title": "Synthetic ALPR policy", "agency_id": "ca-cedar-police", "agency_name": police,
                 "received_at": "2026-01-20T16:30:00+00:00", "public_url": "https://example.invalid/fixtures/policy",
                 "pages": 2, "cited_by": [F2, F1], "public_copy": None},
                {"sha256_prefix": B[:12], "title": "Synthetic audit log", "agency_id": "ca-cedar-police", "agency_name": police,
                 "received_at": "2026-01-22T09:00:00+00:00", "public_url": None, "pages": None, "cited_by": [F2], "public_copy": None},
                {"sha256_prefix": C[:12], "title": "Synthetic vendor letter", "agency_id": "ca-unconfigured-agency",
                 "agency_name": None, "received_at": "2026-01-25T10:00:00Z", "public_url": None, "pages": 0, "cited_by": [],
                 "public_copy": None},
            ]}
        self.assertEqual(doc, expected)
        self.assertEqual(set(doc), set(PUBLIC_LIBRARY_TOP_LEVEL))
        law.validate(doc, SCHEMA)
        text = self.read("data/sources.json")
        self.assertEqual(text, json.dumps(doc, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
        self.edit_site(base_url=None)
        self.assertEqual({k: v for k, v in self.data().items() if k in ("base_url", "url_kind")},
                         {"base_url": None, "url_kind": "relative"})

    def edit_site(self, **changes):
        path = self.content / "site.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data.update(changes)
        path.write_text(json.dumps(data), encoding="utf-8")

    def test_unconfigured_agency_keeps_id_with_null_name(self):
        entry = self.data()["sources"][2]
        self.assertEqual((entry["agency_id"], entry["agency_name"]), ("ca-unconfigured-agency", None))
        self.assertIn("ca-unconfigured-agency", self.read("sources.html"))

    def test_cited_by_lists_each_published_finding_once_and_never_a_draft(self):
        self.add_finding("second", F2, [A, A], published_at="2026-03-01T18:00:00+00:00")
        drafts = self.root / "private" / "drafts"
        drafts.mkdir(parents=True)
        draft = json.loads((self.content / "findings" / f"{SLUG}.json").read_text(encoding="utf-8"))
        draft.update(id="fnd_draft00000003", slug="draft", state="draft",
                     sources=[{"sha256": s, "locator": "page:1", "title": "Synthetic"} for s in (A, C)])
        (drafts / "fnd_draft00000003.json").write_text(json.dumps(draft), encoding="utf-8")
        by_prefix = {s["sha256_prefix"]: s["cited_by"] for s in self.data()["sources"]}
        self.assertEqual(by_prefix, {A[:12]: [F2, F1], B[:12]: [], C[:12]: []})
        self.assertNotIn("fnd_draft00000003", self.read("data/sources.json"))
        self.finding("second", lambda d: d.update(published_at="2026-01-01T18:00:00+00:00"))
        self.assertEqual(self.data()["sources"][0]["cited_by"], [F1, F2])  # follows model order, not file order

    def test_note_fragment_and_full_hash_never_appear(self):
        doc = self.data()
        text = self.read("data/sources.json")
        for entry in doc["sources"]:
            self.assertEqual(set(entry), set(PUBLIC_LIBRARY_FIELDS))
            for key in ("note", "fragment", "sha256"):
                self.assertNotIn(key, entry)
        for leaked in (NOTE, "cedar-police-records", "unconfigured-records", '"note"', '"fragment"', '"sha256"', A, B, C):
            self.assertNotIn(leaked, text)
        self.assertIn(NOTE, self.read("sources.html"))  # the page still shows it; only the open data omits it

    def test_full_hash_in_a_title_is_refused_and_public_untouched(self):
        for plant in (f"Copy of {sha('planted')}", f"Upper {sha('planted')[:40].upper()}"):
            with self.subTest(plant=plant):
                (self.content / "sources.json").write_text(json.dumps(LIBRARY), encoding="utf-8")
                build_site(self.root)
                before = self.snapshot()
                library = json.loads(json.dumps(LIBRARY))
                library[1]["title"] = plant
                (self.content / "sources.json").write_text(json.dumps(library), encoding="utf-8")
                with self.assertRaisesRegex(ContentError, r"^data/sources\.json: a string holds a full hash"):
                    build_site(self.root)
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(sorted(p.name for p in self.root.iterdir() if p.name.startswith(".public")), [])
        library[1]["title"] = f"Short {sha('planted')[:39]}"  # 39 hex characters are not a full hash
        (self.content / "sources.json").write_text(json.dumps(library), encoding="utf-8")
        self.assertEqual(self.data()["sources"][1]["title"], library[1]["title"])

    def test_injected_key_is_refused_naming_sources_json(self):
        real = Site._public_library_entry
        leaks = {"x": lambda s: s.update(x=1), "note": lambda s: s.update(note=NOTE),
                 "fragment": lambda s: s.update(fragment="cedar-police-records"), "sha256": lambda s: s.update(sha256="short"),
                 "Title": lambda s: s.update(Title="case variant"),
                 "id": lambda s: s.update(cited_by=[{"id": F1}])}
        for key, leak in leaks.items():
            with self.subTest(key=key):
                def builder(self_, source, leak=leak):
                    value = real(self_, source)
                    leak(value)
                    return value
                self.assert_refused_and_untouched(f"^data/sources\\.json: non-public key {key}$",
                                                  mock.patch.object(Site, "_public_library_entry", builder))

    def test_empty_library_still_writes_and_links_and_starter_writes_none(self):
        (self.content / "sources.json").unlink()
        doc = self.data()
        self.assertEqual(doc["sources"], [])
        law.validate(doc, SCHEMA)
        self.assertIn(LINK + "Open data (JSON)</a>", self.read("sources.html"))
        shutil.rmtree(self.content)
        shutil.rmtree(self.public)
        written = {p.relative_to(self.public).as_posix() for p in build_site(self.root)}
        self.assertNotIn("data/sources.json", written)
        self.assertFalse((self.public / "data" / "sources.json").exists())

    def test_sources_page_and_about_link_the_file(self):
        written = {p.relative_to(self.public).as_posix() for p in build_site(self.root)}
        self.assertIn("data/sources.json", written)
        self.assertIn(LINK + "Open data (JSON)</a>", self.read("sources.html"))
        self.assertIn('<a href="/data/findings.json" type="application/json">data/findings.json</a> and '
                      + LINK + "data/sources.json</a>", self.read("about.html"))
        self.assertNotIn("sources.json", self.read("sitemap.xml"))

    def test_schema_matches_the_allowlist_and_rejects_drift(self):
        doc = self.data()
        entry = SCHEMA["$defs"]["source"]
        self.assertEqual(set(entry["properties"]), set(PUBLIC_LIBRARY_FIELDS))
        self.assertEqual(set(SCHEMA["properties"]), set(PUBLIC_LIBRARY_TOP_LEVEL))
        for where in (SCHEMA, entry):
            self.assertIs(where["additionalProperties"], False)
        bad = [lambda d: d.update(extra=1), lambda d: d["sources"][0].update(note="n"),
               lambda d: d["sources"][0].update(sha256_prefix=A[:13]),
               lambda d: d["sources"][0].update(sha256_prefix=A[:12].upper()),
               lambda d: d["sources"][0].update(cited_by=["bad id"]), lambda d: d["sources"][0].pop("cited_by"),
               lambda d: d.update(generated_from="content/findings")]
        for index, change in enumerate(bad):
            with self.subTest(index=index):
                broken = json.loads(json.dumps(doc))
                change(broken)
                with self.assertRaises(ValueError):
                    law.validate(broken, SCHEMA)


if __name__ == "__main__":
    unittest.main()
