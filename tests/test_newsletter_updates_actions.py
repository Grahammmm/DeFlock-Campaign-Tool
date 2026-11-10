"""DF1109-06: newsletter --from-content reports public finding Updates and agency actions.

Synthetic only (a copy of examples/fictional-campaign); nothing is sent, archived or published. Drafts without the two
optional lists must keep main's exact bytes (pinned below as sha256 digests of write_draft output, taken on main).
"""
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool.newsletter.check import MATCH, check_draft  # noqa: E402
from campaign_tool.newsletter.draft import DraftError, build_draft, write_draft  # noqa: E402
from campaign_tool.newsletter.from_content import manifest_from_content  # noqa: E402

EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
NOW = datetime(2026, 11, 1, 20, 0, tzinfo=timezone.utc)
SINCE = "2026-10-01T00:00:00+00:00"
CFG = {"name": "Synthetic", "county": "Synthetic"}
CEDAR = "Cedar Police produced a written ALPR usage policy (fictional)"
LONG_NOTE = "the retention note continues. " * 10  # 300 characters: never cut at the 200-character correction limit
CANARY = "ZQ-PRIVATE-CANARY-7c1"
UPDATE_KEYS, ACTION_KEYS = {"title", "path", "date", "note"}, {"agency", "kind", "as_stated", "date", "source_url"}
SHERIFF_URL, POLICE_URL = "https://example.invalid/sheriff/sharing", "https://example.invalid/cedar/policy-v2+final:1"


def digest(blob):
    """The first 8 bytes of sha256 as an integer, so the credential scan's quoted-hex entropy rule has nothing to match."""
    return int.from_bytes(hashlib.sha256(blob).digest()[:8], "big")


def legacy_manifests():
    """Manifests without updates/actions: template, Worker-shaped and a --from-content-shaped explicit manifest."""
    template = {"schema_version": 1, "campaign": {"name": "Synthetic", "base_url": None, "county_name": "Synthetic"},
                "since": None, "findings": [], "meetings": []}
    worker = {"schema_version": 1, "campaign": {"name": "Synthetic", "base_url": "https://synthetic.example.invalid",
                                                "county_name": "Synthetic"}, "since": SINCE,
              "findings": [{"title": "Synthetic new", "summary": "A synthetic record.", "classification": "documented_fact",
                            "confidence": "verified", "path": "/findings/new.html", "published_at": "2026-10-20T18:00:00+00:00",
                            "corrected": False}],
              "meetings": [{"body": "Synthetic Council", "starts_at": "2026-11-20T18:00:00-08:00", "agenda_item": "Item",
                            "agenda_url": "https://synthetic.example.invalid/agenda", "relevance": None}]}
    shaped = json.loads(json.dumps(worker))
    shaped["findings"][0].update(id="fnd_new", slug="new")
    shaped["findings"].append({"title": "Synthetic old", "summary": "An older record.", "classification": "documented_fact",
                               "confidence": "verified", "path": "/findings/old.html", "corrected": True,
                               "published_at": "2026-02-01T18:00:00+00:00", "id": "fnd_old", "slug": "old",
                               "corrections": [{"date": "2026-10-05", "note": "fixed a date."}]})
    shaped["meetings"][0].update(id="mtg-1", participation=None)
    shaped["decisions"] = [{"body": "Synthetic Council", "on": "2026-10-10", "result": "approved", "vote": "4-1",
                            "source_url": "https://synthetic.example.invalid/minutes", "meeting_path": "/meetings/m.html"}]
    return {"template": (template, {}), "worker": (worker, {}),
            "manifest": (shaped, {"archive_public": True, "provenance": True})}


def draft_bytes(manifest, kwargs, folder):
    Path(folder).mkdir(parents=True, exist_ok=True)
    write_draft(folder, build_draft(manifest, NOW, **kwargs))
    return {ext: (Path(folder) / "kit" / f"newsletter-draft.{ext}").read_bytes() for ext in ("html", "txt", "json")}


# write_draft bytes of legacy_manifests() on main 45551bd9 (before DF1109-06); never regenerate these.
MAIN_DIGESTS = {
    "template": {"html": 9986792581858194093, "txt": 5204263887270599280, "json": 75387812258661670},
    "worker": {"html": 17862360603284059956, "txt": 1917102204566973324, "json": 523192304790066617},
    "manifest": {"html": 68226492169048104, "txt": 15756915808917987217, "json": 2327261668270416680},
    "content": {"html": 2753200710547519119, "txt": 1400532812653189135, "json": 14020634623946105475},
}


def make_campaign(base):
    root = Path(base) / "campaign"
    shutil.copytree(EXAMPLE, root, ignore=shutil.ignore_patterns("public"))
    return root


def content_draft(root, since=SINCE):
    return build_draft(manifest_from_content(root, CFG, since, now=NOW), NOW, archive_public=True, provenance=True)


class Base(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-df1109-06-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.root = make_campaign(self.tmp)
        self.cedar_path = self.root / "content" / "findings" / "cedar-policy-posted.json"

    def write(self, path, doc):
        path.write_text(json.dumps(doc), encoding="utf-8")

    def populate(self):
        cedar = json.loads(self.cedar_path.read_text(encoding="utf-8"))
        oak = {**cedar, "id": "fnd_0000000000000002", "slug": "oak-retention", "title": "Synthetic oak retention finding",
               "published_at": "2026-10-25T18:00:00+00:00", "corrections": [{"date": "2026-10-26", "note": "fixed a typo."}],
               "updates": [{"date": "2026-10-28", "note": "the agency posted a second version."}]}
        cedar["corrections"].append({"date": "2026-10-12", "note": "corrected the page count."})
        cedar["updates"] = [{"date": "2026-03-05", "note": "an earlier update outside the window."},
                            {"date": "2026-10-20", "note": "a second policy page was received."},
                            {"date": "2026-10-20", "note": "the same-day follow-up: the page count is now three."},
                            {"date": "2026-10-10", "note": "backfilled: a literal *star*, [bracket] and back\\slash stay text."},
                            {"date": "2026-10-05", "note": LONG_NOTE}]
        self.write(self.cedar_path, cedar)
        self.write(self.root / "content" / "findings" / "oak-retention.json", oak)
        row = {"agency_id": "ca-cedar-police", "on": "2026-10-15", "kind": "policy_adopted",
               "as_stated": "Council adopted the use policy [v2] *as amended*", "source_url": POLICE_URL,
               "checked_on": "2026-10-30"}
        self.write(self.root / "content" / "agency-actions.json", {"schema_version": 1, "actions": [
            row, {**row, "agency_id": "ca-cedar-county-sheriff", "kind": "sharing_ended",
                  "as_stated": "Sharing with outside agencies ended", "source_url": SHERIFF_URL},
            {**row, "agency_id": "ca-cedar-city-council", "on": "2026-09-01", "kind": "contract_signed",
             "as_stated": "Contract signed", "source_url": "https://example.invalid/council/contract"}]})

    def cli(self, *extra):
        return subprocess.run([sys.executable, "-B", "-m", "campaign_tool.newsletter", "--directory", str(self.root), *extra],
                              cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)


class ProjectionTests(Base):
    def test_public_model_projects_exact_optional_keys(self):
        plain = manifest_from_content(self.root, CFG, SINCE, now=NOW)
        self.assertFalse({"updates", "actions"} & set(plain))
        self.populate()
        manifest = manifest_from_content(self.root, CFG, SINCE, now=NOW)
        self.assertEqual(set(manifest) - set(plain), {"updates", "actions"})
        self.assertTrue(all(set(r) == UPDATE_KEYS for r in manifest["updates"]))
        self.assertTrue(all(set(r) == ACTION_KEYS for r in manifest["actions"]))
        self.assertEqual(manifest["actions"], [
            {"agency": "Cedar County Sheriff's Office (fictional)", "kind": "Sharing ended",
             "as_stated": "Sharing with outside agencies ended", "date": "2026-10-15", "source_url": SHERIFF_URL},
            {"agency": "Cedar Police Department (fictional)", "kind": "Policy adopted",
             "as_stated": "Council adopted the use policy [v2] *as amended*", "date": "2026-10-15", "source_url": POLICE_URL}])
        late = manifest_from_content(self.root, CFG, "2026-10-31T00:00:00+00:00", now=NOW)
        self.assertFalse({"updates", "actions"} & set(late), "everything outside the window adds no key")

    def test_from_content_reports_a_long_official_action_url(self):
        long_url = "https://a.co/" + "p" * 480
        self.populate()
        path = self.root / "content" / "agency-actions.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["actions"][0]["source_url"] = long_url
        doc["actions"][1]["source_url"] = "https://example.invalid?id=1"
        self.write(path, doc)
        draft = content_draft(self.root)
        self.assertIn(f"[Official source]({long_url})", draft["text"])
        self.assertIn("[Official source](https://example.invalid/?id=1)", draft["text"])
        self.assertIn(f'<a href="{long_url}" rel="noopener noreferrer">Official source</a>', draft["html"])

    def test_old_new_corrected_findings_each_keep_all_updates(self):
        self.populate()
        rows = manifest_from_content(self.root, CFG, SINCE, now=NOW)["updates"]
        self.assertEqual([(r["date"], r["path"]) for r in rows], [
            ("2026-10-28", "/findings/oak-retention.html"), ("2026-10-20", "/findings/cedar-policy-posted.html"),
            ("2026-10-20", "/findings/cedar-policy-posted.html"), ("2026-10-10", "/findings/cedar-policy-posted.html"),
            ("2026-10-05", "/findings/cedar-policy-posted.html")])
        self.assertEqual([r["note"] for r in rows[1:3]], ["a second policy page was received.",
                                                          "the same-day follow-up: the page count is now three."])
        self.assertEqual((rows[4]["note"], rows[1]["title"]), (LONG_NOTE, CEDAR))
        draft = content_draft(self.root)
        self.assertEqual((draft["counts"]["findings"], draft["counts"]["updates"], draft["corrections_listed"]), (1, 5, 1))
        self.assertIn(LONG_NOTE.strip(), draft["text"])
        self.assertNotIn("an earlier update outside the window", draft["text"])


class WindowTests(unittest.TestCase):
    def manifest(self, since, *days):
        return {"schema_version": 1, "campaign": {"name": "Synthetic", "base_url": None}, "since": since,
                "findings": [], "meetings": [],
                "updates": [{"title": f"Finding {i}", "path": f"/findings/f{i}.html", "date": d, "note": f"update {i}."}
                            for i, d in enumerate(days)],
                "actions": [{"agency": "Synthetic Agency", "kind": "Other", "as_stated": f"Action {i}", "date": d,
                             "source_url": "https://example.invalid/a"} for i, d in enumerate(days)]}

    def lines(self, draft, prefix):
        return [line for line in draft["text"].splitlines() if line.startswith(prefix)]

    def test_calendar_window_edges_and_future_rows(self):
        days = ("2026-09-30", "2026-10-01", "2026-11-01", "2026-11-02", "2026-10-15")
        draft = build_draft(self.manifest(SINCE, *days), NOW)
        self.assertEqual(self.lines(draft, "- **Finding"), [
            "- **Finding 2**, updated 2026-11-01 — update 2. /findings/f2.html",
            "- **Finding 4**, updated 2026-10-15 — update 4. /findings/f4.html",
            "- **Finding 1**, updated 2026-10-01 — update 1. /findings/f1.html"])
        self.assertEqual(self.lines(draft, "- **Synthetic Agency**"), [
            "- **Synthetic Agency**, 2026-11-01: Other — Action 2 [Official source](https://example.invalid/a)",
            "- **Synthetic Agency**, 2026-10-15: Other — Action 4 [Official source](https://example.invalid/a)",
            "- **Synthetic Agency**, 2026-10-01: Other — Action 1 [Official source](https://example.invalid/a)"])
        self.assertEqual((draft["counts"]["updates"], draft["counts"]["actions"]), (3, 3))
        zoned = build_draft(self.manifest("2026-10-01T23:30:00-07:00", "2026-10-01", "2026-09-30"), NOW)
        self.assertEqual(len(self.lines(zoned, "- **Finding")), 1, "the since day as written, not converted to UTC")
        leap = build_draft(self.manifest(None, "2024-02-29", "2026-11-02"), NOW)
        self.assertEqual(self.lines(leap, "- **Finding"), ["- **Finding 0**, updated 2024-02-29 — update 0. /findings/f0.html"])
        ties = self.manifest(None, "2026-10-01", "2026-10-01")
        ties["updates"][1]["title"] = ties["updates"][0]["title"]
        ties["updates"][1]["path"] = ties["updates"][0]["path"]
        self.assertEqual([line[-20:] for line in self.lines(build_draft(ties, NOW), "- **Finding")],
                         ["update 0. /findings/f0.html"[-20:], "update 1. /findings/f0.html"[-20:]])
        for bad in ("2026-02-30", "2026-1-05", "20261005"):
            with self.assertRaises(DraftError) as caught:
                build_draft(self.manifest(None, bad), NOW)
            self.assertEqual(str(caught.exception), "updates[0].date: real YYYY-MM-DD day required")


class RenderTests(Base):
    def test_sections_follow_corrections_in_html_text_and_archive(self):
        self.populate()
        draft = content_draft(self.root)
        text, html, body = draft["text"], draft["html"], draft["public_archive"]["body_md"]
        order = [text.index(h) for h in ("## New findings", "## Corrections", "## Updates to earlier findings",
                                         "## What agencies did", "Read everything")]
        self.assertEqual(order, sorted(order))
        everything = manifest_from_content(self.root, CFG, None, now=NOW)
        everything["requests"] = [{"agency": "Synthetic Agency", "topic": "Policy", "asked_on": "2026-10-02",
                                   "updated_on": None, "state": "awaiting_response", "sentence": "Asked 2026-10-02.",
                                   "path": "/agencies.html#synthetic"}]
        full = build_draft(everything, NOW)["text"]
        order = [full.index(h) for h in ("## Updates to earlier findings", "## What agencies did", "## Records requests",
                                         "## New in the records library")]
        self.assertEqual(order, sorted(order))
        self.assertTrue(text.startswith(body))
        self.assertEqual(set(draft["public_archive"]), {"schema_version", "title", "body_md"})
        self.assertIn("- **Synthetic oak retention finding**, updated 2026-10-28 — the agency posted a second version. "
                      "[Read the finding](https://cedar-records.example.invalid/findings/oak-retention.html)", body)
        self.assertIn(r"backfilled: a literal \*star\*, \[bracket\] and back\\slash stay text.", body)
        self.assertIn("- **Cedar Police Department (fictional)**, 2026-10-15: Policy adopted — Council adopted the use policy "
                      rf"\[v2\] \*as amended\* [Official source]({POLICE_URL})", body)
        self.assertIn("a literal &#42;star&#42;, &#91;bracket&#93; and back&#92;slash stay text.", html)
        self.assertIn(f'<a href="{POLICE_URL}" rel="noopener noreferrer">Official source</a>', html)
        self.assertNotIn("<strong>as amended", html)
        self.assertIn("<h3>Updates to earlier findings</h3>", html)
        no_corrections = json.loads(json.dumps(manifest_from_content(self.root, CFG, SINCE, now=NOW)))
        no_corrections["findings"] = [f for f in no_corrections["findings"] if f["slug"] == "oak-retention"]
        text = build_draft(no_corrections, NOW)["text"]
        self.assertLess(text.index("## New findings"), text.index("## Updates to earlier findings"))
        self.assertNotIn("## Corrections", text)
        no_base = manifest_from_content(self.root, CFG, SINCE, now=NOW)
        no_base["campaign"]["base_url"] = None
        self.assertIn("— the agency posted a second version. /findings/oak-retention.html\n", build_draft(no_base, NOW)["text"])

    def test_site_option_checks_update_finding_links(self):
        self.populate()
        public = self.tmp / "public"
        public.mkdir()
        result = self.cli("--from-content", "--since", "2026-10-01", "--site", str(public))
        self.assertEqual(result.returncode, 3, result.stderr)
        report = json.loads(result.stdout)["site_links"]
        self.assertIn("/findings/cedar-policy-posted.html", [m["link"] for m in report["missing"]])
        (public / "findings").mkdir()
        (public / "findings" / "cedar-policy-posted.html").write_text("<html></html>", encoding="utf-8")
        report = json.loads(self.cli("--from-content", "--since", "2026-10-01", "--site", str(public)).stdout)["site_links"]
        self.assertNotIn("/findings/cedar-policy-posted.html", [m["link"] for m in report["missing"]])


class ValidationTests(unittest.TestCase):
    UPDATE = {"title": "Synthetic finding", "path": "/findings/x.html", "date": "2026-10-10", "note": "a new page was added."}
    ACTION = {"agency": "Synthetic Agency", "kind": "Policy adopted", "as_stated": "Adopted", "date": "2026-10-10",
              "source_url": "https://example.invalid/a"}

    def refused(self, key, rows):
        manifest = {"schema_version": 1, "campaign": {"name": "Synthetic"}, "since": SINCE, key: rows}
        with self.assertRaises(DraftError) as caught:
            build_draft(manifest, NOW)
        self.assertNotIn(CANARY, str(caught.exception))
        return str(caught.exception)

    def test_new_manifest_validation_is_fail_closed(self):
        old = "2020-01-01"  # outside the window: still validated
        cases = [("updates", {"rows": True}, "updates must be a list"),
                 ("updates", [CANARY], "updates[0]: must be an object"),
                 ("updates", [{**self.UPDATE, "id": CANARY}], "updates[0]: must have exactly the keys"),
                 ("updates", [{k: v for k, v in self.UPDATE.items() if k != "note"}], "updates[0]: must have exactly"),
                 ("updates", [{**self.UPDATE, "date": CANARY}], "updates[0].date: real YYYY-MM-DD day required"),
                 ("updates", [{**self.UPDATE, "date": old, "path": "/../" + CANARY}], "updates[0].path: invalid public path"),
                 ("updates", [{**self.UPDATE, "path": "https://evil.example/" + CANARY}], "updates[0].path"),
                 ("updates", [{**self.UPDATE, "title": 7}], "updates[0].title"),
                 ("updates", [{**self.UPDATE, "date": old, "title": "<b>" + CANARY}], "updates[0].title"),
                 ("updates", [{**self.UPDATE, "note": "x" * 401}], "updates[0].note"),
                 ("updates", [{**self.UPDATE, "date": old, "note": "write to zq@example.invalid " + CANARY}], "updates[0].note"),
                 ("updates", [{**self.UPDATE, "note": "Jane Doe " + CANARY}], "updates[0].note: update note contains private"),
                 ("actions", "rows", "actions must be a list"),
                 ("actions", [{**self.ACTION, "agency_id": CANARY}], "actions[0]: must have exactly the keys"),
                 ("actions", [{**self.ACTION, "kind": "policy_adopted"}], "actions[0].kind: must be an agency action kind"),
                 ("actions", [{**self.ACTION, "date": old, "source_url": "http://example.invalid/" + CANARY}],
                  "actions[0].source_url: must be an https:// URL"),
                 ("actions", [{**self.ACTION, "source_url": "https://x.invalid/a)(" + CANARY}], "actions[0].source_url"),
                 ("actions", [{**self.ACTION, "as_stated": ""}], "actions[0].as_stated: required"),
                 ("actions", [{**self.ACTION, "date": old, "as_stated": "call 415-555-0100 " + CANARY}],
                  "privacy lint: actions[0].as_stated: phone"),
                 ("actions", [{**self.ACTION, "agency": "x" * 201}], "actions[0].agency: longer than 200")]
        for key, rows, message in cases:
            with self.subTest(message=message):
                self.assertIn(message, self.refused(key, rows))

    def test_every_content_valid_official_url_is_reported_and_linked(self):
        """DF1109-06 return 1: the draft accepts what content/agency_actions accept (500 characters; ``?``/``#`` right after the host)."""
        long_url = "https://a.co/" + "p" * 480  # path 481, URL 493
        edge = "https://a.co/" + "q" * 487  # exactly agency_actions.MAX_URL
        self.assertEqual((len(long_url), len(edge)), (493, 500))
        for url, linked in ((long_url, long_url), (edge, edge),
                            ("https://example.invalid?id=1", "https://example.invalid/?id=1"),
                            ("https://example.invalid#top", "https://example.invalid/#top"),
                            ("https://example.invalid:8443?a=b", "https://example.invalid:8443/?a=b"),
                            ("https://example.invalid/", "https://example.invalid/"),
                            ("https://example.invalid", "https://example.invalid")):
            with self.subTest(url=url[:60]):
                draft = build_draft({"schema_version": 1, "campaign": {"name": "Synthetic"}, "since": SINCE,
                                     "actions": [{**self.ACTION, "source_url": url}]}, NOW)
                self.assertIn(f"[Official source]({linked})", draft["text"])
                self.assertIn(f'<a href="{linked}" rel="noopener noreferrer">Official source</a>', draft["html"])

    def test_embedded_host_query_or_fragment_is_never_rewritten(self):
        """DF1109-06 return 2: only a bare leading authority gets ``/``; an embedded ``https://host?``/``#`` stays exact."""
        for url, linked in (
                ("https://city.example/records?ref=https://state.example?doc=1", None),
                ("https://city.example/records?ref=https://state.example#top", None),
                ("https://city.example/page#https://other.example?x=1", None),
                ("https://city.example:8443/a?b=https://c.example#d", None),
                ("https://city.example?ref=https://state.example?doc=1", "https://city.example/?ref=https://state.example?doc=1"),
                ("https://city.example#https://state.example?doc=1", "https://city.example/#https://state.example?doc=1"),
                ("https://city.example:8443?u=https://x.example#y", "https://city.example:8443/?u=https://x.example#y")):
            with self.subTest(url=url):
                draft = build_draft({"schema_version": 1, "campaign": {"name": "Synthetic"}, "since": SINCE,
                                     "actions": [{**self.ACTION, "source_url": url}]}, NOW)
                linked = linked or url
                self.assertIn(f"[Official source]({linked})", draft["text"])
                self.assertIn(f'<a href="{linked}" rel="noopener noreferrer">Official source</a>', draft["html"])

    def test_url_one_character_over_the_content_limit_is_refused(self):
        too_long = "https://a.co/" + "q" * 488
        self.assertEqual(len(too_long), 501)
        self.assertIn("actions[0].source_url", self.refused("actions", [{**self.ACTION, "source_url": too_long}]))

    def test_refused_manifest_leaves_the_existing_draft(self):
        with tempfile.TemporaryDirectory(prefix="synthetic-df1109-06-") as base:
            root = make_campaign(base)
            before = draft_bytes(*legacy_manifests()["worker"], root)
            bad = Path(base) / "bad.json"
            bad.write_text(json.dumps({**legacy_manifests()["worker"][0], "actions": [{**self.ACTION, "kind": CANARY}]}))
            result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool.newsletter", "--directory", str(root),
                                     "--manifest", str(bad)], cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 1)
            self.assertIn("Stopped: actions[0].kind", result.stderr)
            self.assertNotIn(CANARY, result.stderr + result.stdout)
            self.assertEqual(before, {e: (root / "kit" / f"newsletter-draft.{e}").read_bytes() for e in before})


class PrivacyTests(Base):
    def test_no_private_canary_in_any_public_projection(self):
        self.populate()
        agencies_path = self.root / "content" / "agencies.json"
        agencies = json.loads(agencies_path.read_text(encoding="utf-8"))
        agencies[1]["profile"]["summary"] += " " + CANARY  # an agency field the projection must not copy
        self.write(agencies_path, agencies)
        (self.root / "kit").mkdir(exist_ok=True)
        self.write(self.root / "kit" / "draft-finding.json", {"updates": [{"date": "2026-10-20", "note": CANARY}]})
        manifest = manifest_from_content(self.root, CFG, SINCE, now=NOW)
        draft = build_draft(manifest, NOW, archive_public=True, provenance=True)
        public = json.dumps([manifest["updates"], manifest["actions"], draft["public_archive"], draft["html"], draft["text"]])
        for secret in (CANARY, "ca-cedar-police", "2026-10-30", "checked_on", "agency_id", "cafecafe"):
            self.assertNotIn(secret, public)
        for private in ("provenance", "TEMPLATE CONSENT", "nsubscribe", "review_flags"):
            self.assertNotIn(private, json.dumps(draft["public_archive"]))


class CompatibilityTests(Base):
    def legacy(self):
        out = {}
        for name, (manifest, kwargs) in legacy_manifests().items():
            out[name] = draft_bytes(manifest, kwargs, self.tmp / name)
        content = content_draft(self.root)
        (self.tmp / "content").mkdir()
        write_draft(self.tmp / "content", content)
        out["content"] = {e: (self.tmp / "content" / "kit" / f"newsletter-draft.{e}").read_bytes() for e in ("html", "txt", "json")}
        return out

    def test_legacy_draft_files_are_byte_identical(self):
        legacy = self.legacy()
        self.assertEqual({n: {e: digest(b) for e, b in files.items()} for n, files in legacy.items()}, MAIN_DIGESTS)
        def rows(day):
            return {"updates": [{"title": "Old", "path": "/findings/o.html", "date": day, "note": "old."}],
                    "actions": [{"agency": "Agency", "kind": "Other", "as_stated": "Later", "date": day,
                                 "source_url": "https://example.invalid/a"}]}
        for name, (manifest, kwargs) in legacy_manifests().items():
            extras = [{"updates": None, "actions": None}, {"updates": [], "actions": []}, rows("2026-11-02")]
            extras += [rows("2026-09-30")] if manifest["since"] else []  # the day before since
            for number, extra in enumerate(extras):
                with self.subTest(name=name, variant=number):
                    self.assertEqual(draft_bytes({**manifest, **extra}, kwargs, self.tmp / "variant"), legacy[name])


class CheckScopeTests(Base):
    def test_provenance_and_check_scope_stay_exact(self):
        self.populate()
        manifest = manifest_from_content(self.root, CFG, SINCE, now=NOW)
        plain = {k: v for k, v in manifest.items() if k not in ("updates", "actions")}
        self.assertEqual(build_draft(manifest, NOW, provenance=True)["provenance"],
                         build_draft(plain, NOW, provenance=True)["provenance"])
        built = self.cli("--from-content", "--since", "2026-10-01")
        self.assertEqual(built.returncode, 0, built.stderr)
        self.assertIn("## What agencies did", (self.root / "kit" / "newsletter-draft.txt").read_text(encoding="utf-8"))
        cedar = json.loads(self.cedar_path.read_text(encoding="utf-8"))
        cedar["updates"].append({"date": "2026-10-21", "note": "an update added after the draft."})
        self.write(self.cedar_path, cedar)
        actions_path = self.root / "content" / "agency-actions.json"
        actions = json.loads(actions_path.read_text(encoding="utf-8"))
        actions["actions"][0]["as_stated"] = "Council adopted a different policy"
        self.write(actions_path, actions)
        report = check_draft(self.root)
        self.assertEqual((report["status"], report["message"]), ("match", MATCH), "updates and actions are not checked")
        self.assertEqual(MATCH, "draft matches current content (findings, corrections and meetings; "
                                "requests, records, map and decisions not checked)")
        (self.root / "content" / "findings" / "oak-retention.json").unlink()
        self.assertEqual(check_draft(self.root)["status"], "different")


if __name__ == "__main__":
    unittest.main()
