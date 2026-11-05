"""DF0022-06: `finding update ID --directory D --on D --note T` adds a dated Update to a published finding without touching
its reviewed content. Synthetic campaigns only; no network.

Run from the repository root: python3 -B -m unittest tests.test_finding_updates -v
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock
from xml.etree import ElementTree as ET

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import finding_updates, findings, law, publication_gate  # noqa: E402
from campaign_tool.content import ContentError, load_content, load_finding_preview  # noqa: E402
from campaign_tool.site import PUBLIC_FINDING_FIELDS, build_site  # noqa: E402
from tests import test_findings_publish as publish_tests  # noqa: E402  (module import: not re-collected here)

NOW, RECORDS, draft, sha = publish_tests.NOW, publish_tests.RECORDS, publish_tests.draft, publish_tests.sha
LATER = datetime(2026, 3, 10, 12, 0, 0, tzinfo=timezone.utc)
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
SCHEMA = json.loads((REPO_ROOT / "schemas" / "findings-open-data.schema.json").read_text(encoding="utf-8"))
A = "{http://www.w3.org/2005/Atom}"
S = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
PLAIN = "the agency answered our request by letter."


class Campaign:
    """The DF2237 synthetic publish campaign helpers, shared without re-running those tests."""
    _p = vars(publish_tests.FindingPublishTests)
    setUp_campaign, cli, register, write_draft, receipt, approve_all, log_lines = (
        _p["setUp"], _p["cli"], _p["register"], _p["write_draft"], _p["receipt"], _p["approve_all"], _p["log_lines"])


class FindingUpdateTests(Campaign, unittest.TestCase):
    def setUp(self):
        self.setUp_campaign()
        self.approve_all()
        findings.publish(self.root, self.draft, "Pat Organizer", now=NOW)
        self.path = self.content / "findings" / "synthetic-title.json"
        self.original = json.loads(self.path.read_text(encoding="utf-8"))

    def update(self, on="2026-03-05", note=PLAIN, approved_by="Pat Organizer", now=LATER, finding_id="syn-pub-1"):
        return findings.update(self.root, finding_id, on, note, now=now, approved_by=approved_by)

    def document(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def refused(self, needle, exc=ValueError, **kwargs):
        before = self.path.read_bytes() if self.path.exists() else None
        lines = self.log_lines()
        with self.assertRaises(exc) as caught:
            self.update(**kwargs)
        message = str(caught.exception)
        self.assertIn(needle, message)
        self.assertEqual(self.path.read_bytes() if self.path.exists() else None, before)
        self.assertEqual(self.log_lines(), lines)
        note = kwargs.get("note")
        if isinstance(note, str) and len(note) > 3:
            self.assertNotIn(note, message)  # a refusal never echoes the note
        return message

    def run_cli(self, *arguments):
        return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *map(str, arguments)], cwd=REPO_ROOT,
                              capture_output=True, text=True, timeout=30)

    # --- dates, shape and note privacy -------------------------------------------------------------------------

    def test_dates_shape_and_note_privacy(self):
        result = self.update(on="2026-03-04", note="  kept exactly as typed (3 pages).  ")  # publication day, verbatim note
        self.assertEqual(result, {"updated": "synthetic-title", "updates": 1})
        self.update(on="2026-03-10")  # today (UTC of the operation time)
        self.assertEqual([u["date"] for u in self.document()["updates"]], ["2026-03-04", "2026-03-10"])
        self.assertEqual(self.document()["updates"][0]["note"], "  kept exactly as typed (3 pages).  ")
        self.update(on="2026-03-05")  # backfill: stored order, never sorted
        self.assertEqual([u["date"] for u in self.document()["updates"]], ["2026-03-04", "2026-03-10", "2026-03-05"])
        self.update(on="2026-03-05")  # same day twice, no deduplication
        self.assertEqual(len(self.document()["updates"]), 4)
        for note in ("a", "a" * 400, "agency answered (see page 3) on 2026-03-05: ok & done"):
            self.update(note=note)
        self.assertEqual(self.document()["updates"][-3]["note"], "a")

        self.refused("--on: is in the future", on="2026-03-11")
        self.refused("--on: is before the publication date 2026-03-04", on="2026-03-03")
        for bad in ("2027-02-29", "2026-3-5", "05/03/2026", "", True, None, 20260305):
            with self.subTest(on=bad):
                self.refused("--on: must be a real YYYY-MM-DD date", on=bad)
        for bad in ("", "   ", "a" * 401, "two\nlines", "tab\there", "zero\u200bwidth", "line\u2028sep", "<b>x</b>", "a > b",
                    None, 5, True):
            with self.subTest(note=bad):
                self.refused("--note: update note must be 1-400 plain-text characters", note=bad)
        for bad in ("Plate 1ABC234 was mentioned.", "Officer Smith answered.", "Requester: Jane Doe replied.",
                    "Jane Doe replied by letter.", "José García wrote back.", "O'Brien-Smith Lopez attended.",
                    "reply came from someone@example.org", "call 805-555-0100 about it", "records at 12 Oak Street",
                    "Sheriff Office answered."):
            with self.subTest(note=bad):
                self.refused("--note: update note contains private information", note=bad)

    def test_load_updates_shape(self):
        published = "2026-03-04T05:06:07Z"
        load = lambda value, **kw: finding_updates.load_updates(value, published, label="L", **kw)  # noqa: E731
        today = datetime(2026, 3, 10).date()
        self.assertEqual(load([], today=today), [])
        self.assertEqual(load([{"date": "2026-03-04", "note": PLAIN}], today=today),
                         [{"date": "2026-03-04", "note": PLAIN}])
        for bad, needle in ((None, "L: must be a list"), ("x", "L: must be a list"), ({}, "L: must be a list"),
                            ([None], "L[0]: must be an object with exactly date and note"),
                            ([{"date": "2026-03-05"}], "L[0]: must be an object with exactly date and note"),
                            ([{"date": "2026-03-05", "note": PLAIN, "extra": 1}], "L[0]: must be an object"),
                            ([{"date": True, "note": PLAIN}], "L[0].date: must be a real"),
                            ([{"date": None, "note": PLAIN}], "L[0].date: must be a real"),
                            ([{"date": "2026-03-03", "note": PLAIN}], "L[0].date: is before the publication date"),
                            ([{"date": "2026-03-11", "note": PLAIN}], "L[0].date: is in the future"),
                            ([{"date": "2026-03-05", "note": PLAIN}, {"date": "2026-03-05", "note": "<i>"}],
                             "L[1].note: update note must be")):
            with self.subTest(bad=bad), self.assertRaises(ValueError) as caught:
                load(bad, today=today)
            self.assertIn(needle, str(caught.exception))

    def test_stored_updates_are_validated_when_the_site_loads(self):
        self.update()
        for updates, needle in ((None, "updates: must be a list"), ([{"date": "2026-03-05", "note": "x", "z": 1}], "updates[0]"),
                                ([{"date": "2999-01-01", "note": "x"}], "updates[0].date: is in the future"),
                                ([{"date": "2026-03-05", "note": "Jane Doe wrote."}], "updates[0].note: update note contains")):
            with self.subTest(updates=updates):
                self.path.write_text(json.dumps(dict(self.original, updates=updates)), encoding="utf-8")
                with self.assertRaises(ContentError) as caught:
                    load_content(self.root)
                self.assertIn(needle, str(caught.exception))

    # --- gate, seal and no partial write -----------------------------------------------------------------------

    def test_gate_seal_and_no_partial_write(self):
        for approver in (None, "", "   ", "bot", "claude-opus"):
            with self.subTest(approver=approver):
                self.refused("publication refused", approved_by=approver)
        self.refused("0 published findings have it", finding_id="syn-missing")
        duplicate = self.content / "findings" / "copy.json"
        shutil.copyfile(self.path, duplicate)
        self.refused("2 published findings have it")
        duplicate.unlink()

        self.path.write_text(json.dumps(dict(self.original, state="draft")), encoding="utf-8")
        self.refused("only a published finding takes an update")
        self.path.write_text(json.dumps(self.original), encoding="utf-8")

        real = findings._one_published  # the target changes between the first read and the gate
        calls = []

        def racing(folder, finding_id):
            calls.append(finding_id)
            if len(calls) == 2:
                self.path.write_text(json.dumps(dict(self.original, summary="Someone else changed this.")), encoding="utf-8")
            return real(folder, finding_id)
        lines = self.log_lines()
        with mock.patch.object(findings, "_one_published", racing), self.assertRaises(publication_gate.PublicationRefused) as caught:
            self.update()
        self.assertEqual(caught.exception.reason, "content_changed_after_approval")
        self.assertNotIn("updates", self.document())
        self.assertEqual(self.document()["summary"], "Someone else changed this.")
        self.assertEqual(self.log_lines(), lines)
        self.path.write_text(json.dumps(self.original), encoding="utf-8")

        outside = self.base / "outside.json"  # a symlinked finding file refuses and the target is untouched
        shutil.copyfile(self.path, outside)
        self.path.unlink()
        os.symlink(outside, self.path)
        before = outside.read_bytes()
        self.refused("symlink")
        self.assertEqual(outside.read_bytes(), before)
        self.path.unlink()
        shutil.copyfile(outside, self.path)

        saved = self.log.read_bytes()  # a bad log refuses before the finding is written
        self.log.unlink()
        os.symlink(self.base / "elsewhere", self.log)
        self.refused("cannot open publication log")
        self.log.unlink()
        self.log.mkdir()
        before = self.path.read_bytes()
        with self.assertRaises(ValueError) as caught:
            self.update()
        self.assertIn("publication log", str(caught.exception))
        self.assertEqual(self.path.read_bytes(), before)
        self.log.rmdir()
        self.log.write_bytes(saved)

        synthetic_note = "records arrived in two boxes"
        self.update(note=synthetic_note)
        entry = json.loads(self.log_lines()[-1])
        self.assertEqual(entry, {"at": "2026-03-10T12:00:00Z", "action": "update", "id": "syn-pub-1", "slug": "synthetic-title",
                                 "date": "2026-03-05", "content_sha256": findings.reviewed_hash(self.original), "updates": 1})
        self.assertNotIn(synthetic_note, self.log.read_text(encoding="utf-8"))

    def test_withdrawn_finding_takes_no_update(self):
        findings.withdraw(self.root, "syn-pub-1", "Synthetic reason.", now=LATER)
        withdrawn = self.root / "private" / "withdrawn" / "synthetic-title.json"
        before = withdrawn.read_bytes()
        lines = self.log_lines()
        self.refused("was withdrawn")
        shutil.copyfile(withdrawn, self.path)  # a leftover published copy does not revive it
        self.refused("was withdrawn")
        self.assertEqual(withdrawn.read_bytes(), before)
        self.assertEqual(self.log_lines(), lines)

    def test_every_note_gets_its_own_seal_and_named_approval(self):
        seen = []
        real = publication_gate.assert_publishable

        def spy(artefact_id, content_hash, approvals, flags=()):
            seen.append((content_hash, approvals))
            return real(artefact_id, content_hash, approvals, flags)
        digests = []
        for on, note in (("2026-03-05", "records arrived in two boxes"), ("2026-03-05", "records arrived in three boxes")):
            existing = self.document()
            with mock.patch.object(publication_gate, "assert_publishable", spy):
                self.update(on=on, note=note)
            written = self.document()
            digests.append(publication_gate.content_digest({
                "action": "update", "id": "syn-pub-1", "on": on, "note": note,
                "existing": publication_gate.content_digest(existing), "proposed": publication_gate.content_digest(written)}))
        self.assertEqual([hash_ for hash_, _ in seen], digests)
        self.assertEqual(len(set(digests)), 2)
        for (hash_, approvals) in seen:
            self.assertEqual(approvals[0]["content_hash"], hash_)
            self.assertEqual(approvals[0]["approved_by"], "Pat Organizer")
            self.assertNotEqual(hash_, findings.reviewed_hash(self.original))  # the receipt hash is not the operation seal

    # --- an update is not a correction -------------------------------------------------------------------------

    def test_update_is_not_a_correction(self):
        [before] = [r for r in findings.status_rows(self.root) if r["id"] == "syn-pub-1" and "draft_path" not in r][:1]
        self.update()
        document = self.document()
        self.assertEqual(document["updates"], [{"date": "2026-03-05", "note": PLAIN}])
        self.assertEqual(document["corrections"], [])
        self.assertEqual(document["updated_at"], "2026-03-10T12:00:00Z")
        for key in ("id", "slug", "published_at", "state"):
            self.assertEqual(document[key], self.original[key])
        self.assertEqual(findings.reviewed_view(document), findings.reviewed_view(self.original))
        self.assertEqual(findings.reviewed_hash(document), findings.reviewed_hash(self.original))
        self.assertEqual(findings.reviewed_hash(document), findings.reviewed_hash(draft()))
        [after] = [r for r in findings.status_rows(self.root) if r["id"] == "syn-pub-1" and "draft_path" not in r][:1]
        self.assertEqual((after["state"], after["receipts_current"], after["receipts_stale"], after["corrections"]),
                         (before["state"], before["receipts_current"], before["receipts_stale"], 0))
        self.assertEqual(after["receipts_current"], 3)

        findings.correct(self.root, "syn-pub-1", "2026-03-06", "Fixed the page number.", None, now=LATER,
                         approved_by="Pat Organizer")  # note-only correct keeps the history
        self.assertEqual(self.document()["updates"], document["updates"])

        changed = draft(summary="Corrected synthetic summary.")  # a text edit still needs fresh reviews
        path = self.base / "corrected.json"
        path.write_text(json.dumps(changed), encoding="utf-8")
        with self.assertRaises(ValueError) as caught:
            findings.correct(self.root, "syn-pub-1", "2026-03-07", "Reworded.", path, now=LATER, approved_by="Pat Organizer")
        self.assertIn("review blockers", str(caught.exception))
        self.assertNotIn("Corrected synthetic", self.path.read_text(encoding="utf-8"))
        self.approve_all(document=changed)
        findings.correct(self.root, "syn-pub-1", "2026-03-07", "Reworded.", path, now=LATER, approved_by="Pat Organizer")
        final = self.document()
        self.assertEqual(final["summary"], "Corrected synthetic summary.")
        self.assertEqual(final["updates"], document["updates"])  # the --from path keeps the history too
        self.assertNotEqual(findings.reviewed_hash(final), findings.reviewed_hash(self.original))

    def test_correct_cannot_replace_update_history(self):
        self.update()
        stored = self.document()["updates"]
        self.approve_all(document=draft(summary="Corrected synthetic summary."))  # updates are outside the reviewed hash
        for updates in ([], [{"date": "2026-03-06", "note": "another note"}], stored + stored, None):
            with self.subTest(updates=updates):
                changed = draft(summary="Corrected synthetic summary.", updates=updates)
                path = self.base / "corrected.json"
                path.write_text(json.dumps(changed), encoding="utf-8")
                before, lines = self.path.read_bytes(), self.log_lines()
                with self.assertRaises(ValueError) as caught:
                    findings.correct(self.root, "syn-pub-1", "2026-03-07", "Reworded.", path, now=LATER,
                                     approved_by="Pat Organizer")
                self.assertEqual(str(caught.exception), "update history cannot be replaced by finding correct")
                self.assertEqual((self.path.read_bytes(), self.log_lines()), (before, lines))
        same = draft(summary="Corrected synthetic summary.", updates=stored)  # an identical history is accepted
        path = self.base / "same.json"
        path.write_text(json.dumps(same), encoding="utf-8")
        findings.correct(self.root, "syn-pub-1", "2026-03-07", "Reworded.", path, now=LATER, approved_by="Pat Organizer")
        self.assertEqual(self.document()["updates"], stored)

    def test_cli_update_prints_advisories_without_the_note_and_has_no_from(self):
        result = self.run_cli("finding", "update", "syn-pub-1", "--directory", self.root, "--on", "2026-03-05",
                              "--note", "the county violated the retention rule.", "--approved-by", "Pat Organizer")
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(json.loads(lines[0]), {"updated": "synthetic-title", "updates": 1})
        self.assertIn("Not built or deployed.", lines[1])
        self.assertIn("review-required: --note: legal-claim wording (violation-verb)", result.stderr)
        self.assertNotIn("retention rule", result.stderr)
        quiet = self.run_cli("finding", "update", "syn-pub-1", "--directory", self.root, "--on", "2026-03-05",
                             "--note", PLAIN, "--approved-by", "Pat Organizer")
        self.assertEqual((quiet.returncode, quiet.stderr), (0, ""))
        self.assertEqual(len(self.document()["updates"]), 2)
        for extra in (["--from", "x.json"], ["--title", "x"], ["--body", "x"], ["--replace", "1"]):
            result = self.run_cli("finding", "update", "syn-pub-1", "--directory", self.root, "--on", "2026-03-05",
                                  "--note", PLAIN, "--approved-by", "Pat Organizer", *extra)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("unrecognized arguments", result.stderr)
        for missing in ("--on", "--note", "--directory"):
            arguments = {"--directory": self.root, "--on": "2026-03-05", "--note": PLAIN}
            del arguments[missing]
            result = self.run_cli("finding", "update", "syn-pub-1", *(x for kv in arguments.items() for x in kv))
            self.assertEqual(result.returncode, 2, result.stderr)
        result = self.run_cli("finding", "update", "syn-pub-1", "--directory", self.root, "--on", "2026-03-05",
                              "--note", PLAIN)  # no approver: the gate refuses
        self.assertEqual(result.returncode, 1)
        self.assertTrue(result.stderr.startswith("Stopped: "), result.stderr)
        self.assertIn("approver_unnamed", result.stderr)
        self.assertEqual(len(self.document()["updates"]), 2)


class DraftHistoryTests(Campaign, unittest.TestCase):
    def setUp(self):
        self.setUp_campaign()

    def test_draft_history_cannot_bypass_update_gate(self):
        self.approve_all(document=draft())  # updates are outside the reviewed hash, so one set of receipts covers every variant
        for updates in ([{"date": "2026-03-05", "note": PLAIN}], None, "x", {}):
            with self.subTest(updates=updates):
                self.write_draft(draft(updates=updates))
                with self.assertRaises(ValueError) as caught:
                    findings.publish(self.root, self.draft, "Pat Organizer", now=NOW)
                self.assertEqual(str(caught.exception), "updates require an already published finding")
                self.assertEqual(sorted(p.name for p in (self.content / "findings").iterdir()), [])
                self.assertEqual(self.log_lines(), [])
        result = self.cli("finding", "publish", self.draft, "--directory", self.root, "--approved-by", "Pat Organizer",
                          returncode=1)
        self.assertIn("updates require an already published finding", result.stderr)
        with self.assertRaises(ContentError):  # the preview never invents a history for a new draft
            load_finding_preview(draft(updates=[{"date": "2026-03-05", "note": PLAIN}]), "draft")
        self.assertNotIn("updates", load_finding_preview(draft(updates=[]), "draft"))
        self.assertNotIn("updates", load_finding_preview(draft(), "draft"))

        self.write_draft(draft(updates=[]))  # an empty list is no history: it publishes and leaves no key behind
        findings.publish(self.root, self.draft, "Pat Organizer", now=NOW)
        self.assertNotIn("updates", json.loads((self.content / "findings" / "synthetic-title.json").read_text(encoding="utf-8")))


# --- the built site ---------------------------------------------------------------------------------------------

class UpdateSiteTests(unittest.TestCase):
    SLUG = "cedar-policy-posted"

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-finding-updates-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.path = self.root / "content" / "findings" / f"{self.SLUG}.json"
        self.public = self.root / "public"

    def edit(self, **changes):
        data = json.loads(self.path.read_text(encoding="utf-8"))
        data.update(changes)
        self.path.write_text(json.dumps(data), encoding="utf-8")

    def build(self):
        build_site(self.root)
        return {p.relative_to(self.public).as_posix(): p.read_bytes() for p in sorted(self.public.rglob("*")) if p.is_file()}

    def text(self, name):
        return (self.public / name).read_text(encoding="utf-8")

    def test_render_order_export_and_feed(self):
        updates = [{"date": "2026-03-01", "note": "first *note* & `code` arrived"},
                   {"date": "2026-03-15", "note": "newest note"},
                   {"date": "2026-03-01", "note": "second note same day"},
                   {"date": "2026-02-10", "note": "backfilled note"}]
        self.edit(updates=updates)
        self.build()
        page = self.text(f"findings/{self.SLUG}.html")
        order = [page.index(n) for n in ("newest note", "second note same day", "first *note* &amp; `code` arrived", "backfilled note")]
        self.assertEqual(order, sorted(order))
        self.assertIn('<h3 id="updates">Updates</h3><ul>', page)
        self.assertIn('<li>Update · <time datetime="2026-03-15">2026-03-15</time>: newest note</li>', page)
        self.assertLess(page.index("The policy is a"), page.index('id="updates"'))  # after the body ...
        self.assertLess(page.index('id="updates"'), page.index("<h3>Sources</h3>"))  # ... before the sources
        self.assertNotIn("<em>note</em>", page)  # literal text, never Markdown
        self.assertEqual(page.count("Update ·"), 4)
        corrections = self.text("corrections.html")  # not listed as corrections
        for note in ("newest note", "backfilled note"):
            self.assertNotIn(note, corrections)
        self.assertIn("Corrected the policy date", corrections)

        data = json.loads(self.text("data/findings.json"))
        self.assertEqual(data["findings"][0]["updates"], updates)  # stored order
        self.assertEqual(len(data["findings"][0]["corrections"]), 1)
        law.validate(data, SCHEMA)
        self.assertEqual(set(SCHEMA["$defs"]["update"]["properties"]), {"date", "note"})
        self.assertIs(SCHEMA["$defs"]["update"]["additionalProperties"], False)
        self.assertNotIn("updates", SCHEMA["$defs"]["finding"]["required"])
        self.assertEqual(SCHEMA["$defs"]["update"]["properties"]["note"]["maxLength"], 400)
        for bad in (lambda d: d["findings"][0]["updates"][0].update(extra=1), lambda d: d["findings"][0]["updates"][0].pop("note"),
                    lambda d: d["findings"][0]["updates"][0].update(date="2026-3-1")):
            broken = json.loads(json.dumps(data))
            bad(broken)
            with self.assertRaises(ValueError):
                law.validate(broken, SCHEMA)

        feeds = [self.public / "feed.xml", *sorted((self.public / "findings" / "agency").glob("*.xml"))]
        self.assertGreaterEqual(len(feeds), 2)
        for feed_path in feeds:
            feed = ET.fromstring(feed_path.read_text(encoding="utf-8"))
            [entry] = feed.findall(f"{A}entry")  # one stable entry
            self.assertEqual(entry.findtext(f"{A}id"), "urn:deflock-finding:fnd_0000000000000001")
            self.assertEqual(entry.findtext(f"{A}published"), "2026-02-01T18:00:00+00:00")
            self.assertEqual(entry.findtext(f"{A}updated"), "2026-03-15T00:00:00+00:00")
            self.assertEqual(feed.findtext(f"{A}updated"), "2026-03-15T00:00:00+00:00")
            self.assertTrue(entry.findtext(f"{A}summary").startswith("Corrected 2026-02-03. "))  # lead = corrections only
        sitemap = ET.fromstring(self.text("sitemap.xml"))
        lastmod = {u.findtext(S + "loc"): u.findtext(S + "lastmod") for u in sitemap.findall(S + "url")}
        [loc] = [k for k in lastmod if k.endswith(f"/findings/{self.SLUG}.html")]
        self.assertEqual(lastmod[loc], "2026-03-15")

    def test_update_only_finding_has_no_corrected_lead_and_labels_override(self):
        self.edit(corrections=[], updates=[{"date": "2026-03-01", "note": "the agency answered"}], updated_at=None)
        (self.root / "content" / "labels.json").write_text(json.dumps({"schema_version": 1, "labels": {
            "finding.updates": "XUPDATESX", "finding.update": "XUPDATEX"}}), encoding="utf-8")
        self.build()
        page = self.text(f"findings/{self.SLUG}.html")
        self.assertIn('<h3 id="updates">XUPDATESX</h3>', page)
        self.assertIn("<li>XUPDATEX · ", page)
        entry = ET.fromstring(self.text("feed.xml")).find(f"{A}entry")
        self.assertEqual(entry.findtext(f"{A}updated"), "2026-03-01T00:00:00+00:00")
        self.assertFalse(entry.findtext(f"{A}summary").startswith("Corrected"))
        self.assertNotIn("the agency answered", self.text("corrections.html"))
        self.assertNotIn("Corrected", page.split('<div class="finding-meta">')[1].split("</div>")[0])

    def test_absent_updates_byte_identical(self):
        plain = self.build()
        self.assertNotIn('id="updates"', plain[f"findings/{self.SLUG}.html"].decode())
        self.assertNotIn("updates", json.loads(plain["data/findings.json"])["findings"][0])
        header = plain["data/findings.csv"].decode().splitlines()[0]
        self.assertEqual(header.split(","), list(PUBLIC_FINDING_FIELDS))  # no new CSV column
        before = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertNotIn("updates", before)
        self.edit(updates=[])  # a present empty list changes nothing but its own key in the JSON
        again = self.build()
        self.assertEqual(sorted(again), sorted(plain))
        self.assertEqual([k for k in plain if plain[k] != again[k]], ["data/findings.json"])
        old, new = json.loads(plain["data/findings.json"]), json.loads(again["data/findings.json"])
        self.assertEqual(new["findings"][0].pop("updates"), [])
        self.assertEqual(old, new)
        document = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(findings.reviewed_hash(document), findings.reviewed_hash(before))
        self.assertEqual(findings.reviewed_hash(dict(document, updates=[{"date": "2026-03-01", "note": "x"}])),
                         findings.reviewed_hash(before))
        self.assertEqual(findings.reviewed_hash(dict(document, summary="Another summary.")) ==
                         findings.reviewed_hash(before), False)

    def test_export_guards_fail_closed(self):
        self.build()
        before = {p.relative_to(self.public).as_posix(): p.read_bytes() for p in sorted(self.public.rglob("*")) if p.is_file()}
        for note in ("0123456789abcdef" * 3, "see " + "ab" * 32):  # plain text, but a full hash never publishes
            with self.subTest(note=note):
                self.edit(updates=[{"date": "2026-03-01", "note": note}])
                with self.assertRaises(ContentError):
                    build_site(self.root)
                after = {p.relative_to(self.public).as_posix(): p.read_bytes() for p in sorted(self.public.rglob("*")) if p.is_file()}
                self.assertEqual(after, before)
        for note in ("Jane Doe called", "reply to someone@example.org"):  # a hand-edited private note is refused too
            with self.subTest(note=note):
                self.edit(updates=[{"date": "2026-03-01", "note": note}])
                with self.assertRaises(ContentError):
                    build_site(self.root)


if __name__ == "__main__":
    unittest.main()
