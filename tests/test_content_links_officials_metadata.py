"""RF1651-1: officials link citations name the stored bodies[B].officials[P] coordinate. Synthetic content, no network."""
import json
import socket
import unittest
from unittest import mock

from campaign_tool import content_links
from tests import test_content_links as base

REFUSAL = "content/officials.json: cannot resolve stored citation locations"


def person(name, profile, source, checked="2026-10-20"):
    return {"name": name, "role": "Chief", "profile_url": profile, "source_url": source, "checked_on": checked}


SHERIFF = {"agency_id": "ca-cedar-sheriff", "officials": [
    person("Sam Sheriff", "https://official.example.org/sheriff", "https://official.example.org/sheriff-source"),
    person("Dee Deputy", None, "https://official.example.org/deputy-source")]}
POLICE = {"agency_id": "ca-cedar-police", "officials": [
    person("Pat Example", "https://county.example.org/chief", "https://county.example.org/staff")]}


def doc(*bodies):
    return {"schema_version": 1, "bodies": list(bodies)}


class OfficialsMetadataTests(base.LinkCheckBase):
    def setUp(self):
        super().setUp()
        self.path = self.root / "content" / "officials.json"

    def write(self, document):
        self.path.write_text(json.dumps(document), encoding="utf-8")

    def citations(self):
        return {url: group["citations"] for url, group in self.by_url().items()
                if any("officials.json" in c for c in group["citations"])}

    def collect_after(self, replace):
        real_load = content_links.load_content

        def load_then_replace(*args, **kwargs):
            model = real_load(*args, **kwargs)
            replace()
            return model
        with mock.patch.object(content_links, "load_content", side_effect=load_then_replace):
            return content_links.collect_links(self.root, today=base.TODAY)

    def test_official_citations_round_trip_reversed_body_order(self):
        for bodies in ((SHERIFF, POLICE), (POLICE, SHERIFF)):
            self.write(doc(*bodies))
            stored = json.loads(self.path.read_text(encoding="utf-8"))
            found = self.citations()
            self.assertEqual(len(found), 5)  # the null profile is omitted
            for url, locators in found.items():
                self.assertEqual(len(locators), 1)
                prefix, _, rest = locators[0].partition(":")
                self.assertEqual(prefix, "content/officials.json")
                self.assertTrue(rest.startswith("bodies["), rest)
                b = int(rest[len("bodies["):rest.index("]")])
                p = int(rest[rest.index("officials[") + len("officials["):rest.index("]", rest.index("officials["))])
                field = rest.rsplit(".", 1)[1]
                self.assertEqual(stored["bodies"][b]["officials"][p][field], url)
        # independent literal for the reversed order: police is now bodies[0], sheriff bodies[1]
        self.assertEqual(self.citations()["https://official.example.org/deputy-source"],
                         ["content/officials.json:bodies[1].officials[1].source_url"])
        self.assertEqual(self.citations()["https://county.example.org/chief"],
                         ["content/officials.json:bodies[0].officials[0].profile_url"])

    def test_shared_url_keeps_every_occurrence(self):
        shared = "https://shared.example.org/page"
        self.write(doc(
            {"agency_id": "ca-cedar-sheriff", "officials": [person("Same Name", shared, shared), person("Same Name", None, shared)]},
            {"agency_id": "ca-cedar-police", "officials": [person("Same Name", shared, "https://county.example.org/staff")]}))
        self.assertEqual(self.citations()[shared], [
            "content/officials.json:bodies[0].officials[0].profile_url",
            "content/officials.json:bodies[0].officials[0].source_url",
            "content/officials.json:bodies[0].officials[1].source_url",
            "content/officials.json:bodies[1].officials[0].profile_url"])

    def test_metadata_changed_after_load_refuses_before_transport(self):
        self.write(doc(SHERIFF, POLICE))
        changes = {
            "url": doc(SHERIFF, {**POLICE, "officials": [person("Pat Example", "https://other.example.org/x", "https://county.example.org/staff")]}),
            "person order": doc({**SHERIFF, "officials": SHERIFF["officials"][::-1]}, POLICE),
            "checked_on": doc(SHERIFF, {**POLICE, "officials": [person("Pat Example", "https://county.example.org/chief", "https://county.example.org/staff", "2026-10-21")]}),
            "removed body": doc(SHERIFF),
            "added body": doc(SHERIFF, POLICE, {"agency_id": "unknown", "officials": POLICE["officials"]}),
            "duplicate body": doc(SHERIFF, POLICE, POLICE),
        }
        for label, document in changes.items():
            with self.subTest(label), mock.patch.object(content_links, "real_opener", side_effect=AssertionError("opener")):
                with self.assertRaises(content_links.LinkCheckError) as caught:
                    self.collect_after(lambda d=document: self.write(d))
                self.assertEqual(str(caught.exception), REFUSAL)
            self.write(doc(SHERIFF, POLICE))
        # a body-only reordering is still the same roster and yields the new true indexes
        groups, _ = self.collect_after(lambda: self.write(doc(POLICE, SHERIFF)))
        locators = {g["url"]: g["citations"] for g in groups}
        self.assertEqual(locators["https://county.example.org/chief"], ["content/officials.json:bodies[0].officials[0].profile_url"])
        self.assertEqual(locators["https://official.example.org/sheriff"], ["content/officials.json:bodies[1].officials[0].profile_url"])

    def test_unsafe_unreadable_or_malformed_is_value_free(self):
        self.write(doc(SHERIFF, POLICE))
        outside = self.root / "outside.json"
        outside.write_text(json.dumps(doc(SHERIFF, POLICE)), encoding="utf-8")

        def swap(action):
            def run():
                self.path.unlink()
                action()
            return run
        cases = {
            "missing": swap(lambda: None),
            "symlink": swap(lambda: self.path.symlink_to(outside)),
            "dangling": swap(lambda: self.path.symlink_to(self.root / "nowhere.json")),
            "directory": swap(lambda: self.path.mkdir()),
            "bad utf8": swap(lambda: self.path.write_bytes(b"\xff\xfe")),
            "bad json": swap(lambda: self.path.write_text("{private-sentinel", encoding="utf-8")),
            "not object": swap(lambda: self.path.write_text("[]", encoding="utf-8")),
            "oversized": swap(lambda: self.path.write_bytes(b" " * (content_links.MAX_JSON_BYTES + 1))),
            "deep": swap(lambda: self.path.write_text("[" * 100000, encoding="utf-8")),
            "validator error": swap(lambda: self.write({"schema_version": 1, "bodies": "private-sentinel"})),
        }
        for label, replace in cases.items():
            with self.subTest(label):
                with self.assertRaises(content_links.LinkCheckError) as caught:
                    self.collect_after(replace)
                self.assertEqual(str(caught.exception), REFUSAL)
                self.assertNotIn("private-sentinel", str(caught.exception))
                if self.path.is_dir() and not self.path.is_symlink():
                    self.path.rmdir()
                elif self.path.is_symlink() or self.path.exists():
                    self.path.unlink()
                self.write(doc(SHERIFF, POLICE))
        # unsafe content directory: the roster read refuses a symlinked content directory
        real = self.root / "content"
        moved = self.root / "content-real"
        real.rename(moved)
        real.symlink_to(moved)
        try:
            with self.assertRaises(content_links.LinkCheckError):
                content_links._stored_official_positions(real, [], {"x": []}, base.TODAY)
        finally:
            real.unlink()
            moved.rename(real)

    def test_read_is_bounded_to_cap_plus_one(self):
        self.write(doc(SHERIFF, POLICE))
        sizes = []
        real_open = content_links.os.fdopen

        def spy(descriptor, *args, **kwargs):
            handle = real_open(descriptor, *args, **kwargs)
            real_read = handle.read
            return mock.Mock(__enter__=lambda s: s, __exit__=lambda s, *a: handle.close(),
                             read=lambda n=-1: (sizes.append(n), real_read(n))[1])
        with mock.patch.object(content_links.os, "fdopen", side_effect=spy):
            content_links._stored_official_positions(self.root / "content", content_links.load_content(self.root, today=base.TODAY)["agencies"],
                                                     content_links.load_content(self.root, today=base.TODAY)["officials"], base.TODAY)
        self.assertEqual(sizes, [content_links.MAX_JSON_BYTES + 1])

    def test_optional_empty_roster_has_no_secondary_read(self):
        for case in ("absent", "empty", "populated-after-load"):
            with self.subTest(case):
                self.path.unlink(missing_ok=True)
                if case == "empty":
                    self.write(doc())
                real_read = content_links._read_metadata
                with mock.patch.object(content_links, "_read_metadata", side_effect=real_read) as spy:
                    real_load = content_links.load_content

                    def load_then_add(*args, **kwargs):
                        model = real_load(*args, **kwargs)
                        if case == "populated-after-load":
                            self.write(doc(SHERIFF, POLICE))
                        return model
                    with mock.patch.object(content_links, "load_content", side_effect=load_then_add):
                        groups, _ = content_links.collect_links(self.root, today=base.TODAY)
                # other metadata families may read; officials never does for an empty load
                self.assertFalse([c for c in spy.call_args_list if c.args[1:] == ("officials.json",)])
                self.assertFalse([g for g in groups if any("officials.json" in c for c in g["citations"])])

    def test_effective_day_and_exception_locality(self):
        self.write(doc(SHERIFF, POLICE))
        days = []
        real_officials = content_links.load_officials

        def record(document, agencies, today, **kwargs):
            days.append(today)
            return real_officials(document, agencies, today, **kwargs)
        with mock.patch.object(content_links, "load_officials", side_effect=record):
            content_links.collect_links(self.root, today=base.TODAY)
        self.assertEqual(days, [base.TODAY])
        # an unsupplied day is resolved once and passed on to the load and the revalidation
        seen = []
        real_load = content_links.load_content
        with mock.patch.object(content_links, "load_content", side_effect=lambda *a, **k: (seen.append(k["today"]), real_load(*a, **k))[1]), \
                mock.patch.object(content_links, "load_officials", side_effect=record):
            days.clear()
            content_links.collect_links(self.root)
        self.assertEqual(seen, days)
        # TypeError/KeyError/RecursionError at the secondary seam become the fixed refusal; others keep identity
        # (content_links.load_officials is only the secondary seam; the initial load uses content's own reference)
        for error in (TypeError("private-sentinel"), KeyError("private-sentinel"), RecursionError("private-sentinel")):
            with self.subTest(error=type(error).__name__), \
                    mock.patch.object(content_links, "load_officials", side_effect=error):
                with self.assertRaises(content_links.LinkCheckError) as caught:
                    content_links.collect_links(self.root, today=base.TODAY)
                self.assertEqual(str(caught.exception), REFUSAL)
                self.assertNotIn("private-sentinel", str(caught.exception))
        for error in (RuntimeError("x"), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__), \
                    mock.patch.object(content_links, "load_officials", side_effect=error):
                with self.assertRaises(type(error)) as caught:
                    content_links.collect_links(self.root, today=base.TODAY)
                self.assertIs(caught.exception, error)

    def test_actual_cli_corrected_locators_and_refusal_are_read_only(self):
        self.write(doc(SHERIFF, POLICE))
        before = base.snapshot(self.root)
        out = []
        with mock.patch.object(socket.socket, "connect", side_effect=AssertionError("socket")), \
                mock.patch.object(socket, "getaddrinfo", side_effect=AssertionError("dns")):
            text, code = content_links.run_cli(self.root, today=base.TODAY)
        self.assertEqual(code, 0)
        self.assertIn("content/officials.json:bodies[0].officials[0].profile_url", text)
        self.assertIn("content/officials.json:bodies[1].officials[0].source_url", text)
        self.assertNotIn("ca-cedar-police.officials", text)
        self.assertEqual(base.snapshot(self.root), before)
        # refusal: no partial report, no opener, nothing written
        real_load = content_links.load_content

        def load_then_break(*a, **k):
            model = real_load(*a, **k)
            self.path.write_text("{", encoding="utf-8")
            return model
        broken = None
        with mock.patch.object(content_links, "load_content", side_effect=load_then_break), \
                mock.patch.object(content_links, "real_opener", side_effect=AssertionError("opener")):
            with self.assertRaises(content_links.LinkCheckError) as caught:
                content_links.run_cli(self.root, online=True, today=base.TODAY)
        self.assertEqual(str(caught.exception), REFUSAL)
        broken = base.snapshot(self.root)
        self.assertEqual({k: v for k, v in broken.items() if k != "content/officials.json"},
                         {k: v for k, v in before.items() if k != "content/officials.json"})
        self.write(doc(SHERIFF, POLICE))
        self.assertEqual(content_links.run_cli(self.root, today=base.TODAY)[1], 0)
        del out

    def test_nonofficial_locators_remain_compatible(self):
        self.write(doc(SHERIFF, POLICE))
        urls = self.by_url()
        self.assertEqual(urls["https://county.example.org/report"]["citations"],
                         ["content/agencies.json:ca-cedar-sheriff.profile.reports[0].source_url"])
        self.assertEqual(urls["https://pages.example.org/only"]["citations"],
                         ["content/pages/links.json:body_md[anchor 1]", "content/pages/links.json:body_md[anchor 2]"])
        self.assertEqual(urls["https://county.example.org/records"]["citations"].__len__(), 2)


if __name__ == "__main__":
    unittest.main()
