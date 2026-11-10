"""RF0725-5: kit-asked refuses an incomplete scope-title projection before changing public asked items (synthetic only)."""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import kit, kit_asked  # noqa: E402
from campaign_tool.content import ContentError  # noqa: E402
from tests.test_kit_asked import A, B, EXAMPLE, POLICE, SHERIFF, TITLE, TODAY, SECRET, run, step  # noqa: E402

FIXED = "kit-asked: cannot resolve all recorded scope titles from the law package; nothing proposed"
UNKNOWN = "synthetic-retired-scope-9917"
OLD_TOPIC = "Organizer confirmed original scope title"
CANARIES = (UNKNOWN, *SECRET, "No law package", "package.json")


class ScopeTitleTests(unittest.TestCase):
    def setUp(self):
        for target in ("socket.socket", "socket.create_connection", "urllib.request.urlopen"):
            patch = mock.patch(target, side_effect=AssertionError("network used"))
            patch.start()
            self.addCleanup(patch.stop)
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-kit-asked-titles-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.root = self.tmp / "campaign"
        shutil.copytree(EXAMPLE, self.root)
        self.path = self.root / "content" / "agencies.json"
        self.sent_path = self.root / "kit" / "sent.json"
        self.agencies = json.loads(self.path.read_text(encoding="utf-8"))

    # --- fixtures -----------------------------------------------------------------------------------------------
    def link(self, agency, request, **extra):
        """Give an agency one linked asked item so an overwrite would be visible."""
        entry = self.agencies[[a["agency_id"] for a in self.agencies].index(agency)]
        entry.setdefault("asked", []).append({"topic": OLD_TOPIC, "asked_on": "2026-03-02", "state": "awaiting_response",
                                              "request": request, **extra})
        self.path.write_text(json.dumps(self.agencies, indent=2) + "\n", encoding="utf-8")

    def sent(self, *entries):
        """entries: (agency, request, steps, scopes or None[, extra])."""
        requests = {}
        for agency, request, steps, scopes, *extra in entries:
            key = agency if request == "main" else f"{agency}#{request}"
            stem = agency if request == "main" else f"{agency}--{request}"
            requests[key] = {"sent_on": "2026-03-02", "channel": "email", "draft": f"requests/{stem}.md",
                             "draft_sha256": "a" * 64, "recorded_at": "2026-03-02T10:00:00+00:00", "steps": steps,
                             **({"scopes": scopes} if scopes else {}), **(extra[0] if extra else {})}
        self.sent_path.parent.mkdir(exist_ok=True)
        self.sent_path.write_text(json.dumps({"schema_version": 1, "requests": requests}), encoding="utf-8")

    def state(self, code):
        """Point the synthetic campaign at a jurisdiction that has no law package."""
        config = self.root / "campaign.json"
        data = json.loads(config.read_text(encoding="utf-8"))
        data["state"] = code
        config.write_text(json.dumps(data), encoding="utf-8")

    def snapshot(self):
        files = {}
        for path in sorted(self.root.rglob("*")):
            info = path.stat()
            files[str(path.relative_to(self.root))] = (path.read_bytes() if path.is_file() else None,
                                                       info.st_mode, info.st_ino, info.st_mtime_ns)
        return files, sorted(p.name for p in self.tmp.iterdir())

    def refused(self, *flags):
        """Run the real CLI; return nothing changed, exit 3, empty stdout, the fixed diagnostic and no payload."""
        before = self.snapshot()
        with mock.patch.object(kit, "_write_json", side_effect=AssertionError("writer reached")), \
                mock.patch.object(kit_asked.os, "chmod", side_effect=AssertionError("chmod reached")):
            code, out, err = run("kit-asked", "--directory", self.root, *flags)
        self.assertEqual((code, out, err), (3, "", f"Stopped: {FIXED}\n"), flags)
        for canary in CANARIES:
            self.assertNotIn(canary, out + err)
        self.assertEqual(self.snapshot(), before)

    def both_modes_refuse(self):
        self.refused()
        self.refused("--apply")
        with self.assertRaises(ContentError) as caught:
            kit_asked.propose(self.root, TODAY)
        self.assertEqual(str(caught.exception), FIXED)

    def asked(self, agency=POLICE):
        entries = json.loads(self.path.read_text(encoding="utf-8"))
        return next(a for a in entries if a["agency_id"] == agency).get("asked", [])

    # --- refusals -----------------------------------------------------------------------------------------------
    def test_mixed_known_and_unknown_recorded_scopes_refuse_whole_proposal(self):
        self.sent((POLICE, "main", [], [A, UNKNOWN]))
        self.both_modes_refuse()  # a new item: neither the partial known topic nor the generic one is installed
        self.link(POLICE, "main")
        self.both_modes_refuse()  # an existing linked item keeps its topic
        self.assertEqual(self.asked()[0]["topic"], OLD_TOPIC)

    def test_all_unknown_and_missing_package_never_overwrite_linked_topic(self):
        self.link(POLICE, "main", note="Keep linked note")
        self.sent((POLICE, "main", [], [UNKNOWN]))
        self.both_modes_refuse()
        self.sent((POLICE, "main", [], [A, B]))
        self.state("ZZ")  # a valid state code with no law package
        self.both_modes_refuse()
        item = self.asked()[0]
        self.assertEqual((item["topic"], item["note"]), (OLD_TOPIC, "Keep linked note"))
        # direct propose with an external missing and a malformed package base
        self.state("CA")
        missing = self.tmp / "no-laws"
        broken = self.tmp / "broken-laws"
        (broken / "us-ca").mkdir(parents=True)
        (broken / "us-ca" / "package.json").write_text("{" + SECRET[0], encoding="utf-8")
        for base in (missing, broken):
            with self.assertRaises(ContentError) as caught:
                kit_asked.propose(self.root, TODAY, law_base=base)
            self.assertEqual(str(caught.exception), FIXED)
            for canary in CANARIES:
                self.assertNotIn(canary, str(caught.exception))

    def test_unknown_after_truncation_boundary_still_refuses(self):
        titles = {"s1": "x" * 130, "s2": "y" * 20}
        entry = {"sent_on": "2026-03-02", "steps": [], "scopes": ["s1", "s2"]}
        # all known: the existing boundary is retained (the first title alone is cut with an ellipsis)
        self.assertEqual(kit_asked._proposal(entry, titles)["topic"], "x" * 117 + "...")
        with self.assertRaises(ContentError) as caught:
            kit_asked._proposal({**entry, "scopes": ["s1", "s2", UNKNOWN]}, titles)
        self.assertEqual(str(caught.exception), FIXED)
        # real package: known titles already beyond the display budget, then an unresolved ID
        package = kit_asked.law.load_package("us-ca")["request_scopes"]
        known = [scope["scope_id"] for scope in package]
        self.assertGreater(len("; ".join(scope["title"] for scope in package)), kit_asked.TOPIC_MAX)
        self.sent((POLICE, "main", [], known + [UNKNOWN]))
        self.both_modes_refuse()
        self.sent((POLICE, "main", [], known))
        code, out, err = run("kit-asked", "--directory", self.root, "--apply")
        self.assertEqual((code, err), (0, ""), err)
        topic = self.asked()[0]["topic"]
        self.assertEqual(topic, kit_asked.topic_text([scope["title"] for scope in package]))
        self.assertLessEqual(len(topic), kit_asked.TOPIC_MAX)

    def test_late_bad_request_refuses_before_any_change_lines_or_writes(self):
        self.link(POLICE, "main")
        good = (POLICE, "main", [], [A, B])
        self.sent(good, (POLICE, "later", [], [A, UNKNOWN]))
        self.both_modes_refuse()
        self.sent(good, (SHERIFF, "main", [], [UNKNOWN]))
        self.both_modes_refuse()

    # --- eligibility --------------------------------------------------------------------------------------------
    def test_absent_deselected_and_closed_without_production_need_no_titles(self):
        closed = [step("2026-03-09", "acknowledged"), step("2026-03-20", "closed")]
        self.agencies[[a["agency_id"] for a in self.agencies].index(SHERIFF)]["selected"] = False
        self.path.write_text(json.dumps(self.agencies, indent=2) + "\n", encoding="utf-8")
        self.sent((SHERIFF, "main", [], [UNKNOWN]), ("ca-not-in-content", "main", [], [UNKNOWN]),
                  (POLICE, "closed", closed, [UNKNOWN]))
        for state in ("CA", "ZZ"):
            self.state(state)
            before = self.snapshot()
            code, out, err = run("kit-asked", "--directory", self.root, "--apply")
            self.assertEqual((code, err), (0, ""), out)
            self.assertIn("agency not in content/agencies.json; skipped", out)
            self.assertIn("closed without production; organizer to choose", out)
            self.assertEqual(self.snapshot(), before)
        # a mixed eligible known request still updates normally
        self.state("CA")
        self.sent((SHERIFF, "main", [], [UNKNOWN]), (POLICE, "closed", closed, [UNKNOWN]), (POLICE, "main", [], [A]))
        code, out, err = run("kit-asked", "--directory", self.root, "--apply")
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual([i["topic"] for i in self.asked() if i["request"] == "main"], [TITLE[A]])
        # a malformed skipped history still fails the earlier history validator
        self.sent((SHERIFF, "main", [step("2026-03-09", "acknowledged"), step("2026-03-08", "acknowledged")],
                   [UNKNOWN]))
        code, out, err = run("kit-asked", "--directory", self.root)
        self.assertEqual((code, out), (3, ""))
        self.assertNotIn(FIXED, err)

    def test_withdrawn_and_no_step_items_require_complete_recorded_titles(self):
        withdrawn = [step("2026-03-10", "withdrawn", note=SECRET[0])]
        for steps in (withdrawn, []):
            self.sent((POLICE, "main", steps, [A, UNKNOWN]))
            self.both_modes_refuse()
            self.state("ZZ")
            self.both_modes_refuse()
            self.state("CA")
        self.sent((POLICE, "main", withdrawn, [A]))
        code, out, err = run("kit-asked", "--directory", self.root, "--apply")
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(self.asked()[0], {"topic": TITLE[A], "asked_on": "2026-03-02", "state": "withdrawn",
                                           "request": "main", "updated_on": "2026-03-10"})
        self.assertNotIn(SECRET[0], self.path.read_text(encoding="utf-8"))

    def test_unrecorded_scopes_keep_generic_topic_without_package(self):
        self.state("ZZ")
        self.sent((POLICE, "main", [], None), (SHERIFF, "main", [step("2026-03-09", "acknowledged")], None))
        code, out, err = run("kit-asked", "--directory", self.root, "--apply")
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual([i["topic"] for i in self.asked(POLICE) + self.asked(SHERIFF)], ["Records request"] * 2)
        # an empty source has no eligible request: a byte-for-byte no-op
        self.sent()
        before = self.snapshot()
        code, out, err = run("kit-asked", "--directory", self.root, "--apply")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Nothing written.", out)
        self.assertEqual(self.snapshot(), before)

    # --- preserved behaviour ------------------------------------------------------------------------------------
    def test_known_scope_order_apply_repeat_and_organizer_preservation(self):
        self.link(POLICE, "main", note="Keep linked note")
        organizer = {"topic": "Organizer item", "asked_on": "2026-02-01", "state": "acknowledged"}
        self.agencies[[a["agency_id"] for a in self.agencies].index(POLICE)]["asked"].append(organizer)
        self.path.write_text(json.dumps(self.agencies, indent=2) + "\n", encoding="utf-8")
        mode = self.path.stat().st_mode & 0o777
        self.sent((POLICE, "main", [], [B, A], {"note": SECRET[0], "basis": SECRET[1]}))
        stores = {p: p.read_bytes() for p in (self.sent_path, self.root / "campaign.json")}
        code, out, err = run("kit-asked", "--directory", self.root)
        self.assertEqual((code, err), (0, ""))
        self.assertIn(f"{POLICE}.asked[0].topic: \"{OLD_TOPIC}\" -> \"{TITLE[B]}; {TITLE[A]}\"", out)
        self.assertIn("Dry run: nothing written.", out)
        self.assertEqual(self.asked()[0]["topic"], OLD_TOPIC)
        code, out, err = run("kit-asked", "--directory", self.root, "--apply")
        self.assertEqual((code, err), (0, ""))
        written = self.path.read_bytes()
        self.assertEqual(json.loads(written)[[a["agency_id"] for a in self.agencies].index(POLICE)]["asked"],
                         [{"topic": f"{TITLE[B]}; {TITLE[A]}", "asked_on": "2026-03-02", "state": "awaiting_response",
                           "request": "main", "note": "Keep linked note"}, organizer])
        self.assertEqual(written, (json.dumps(json.loads(written), indent=2, ensure_ascii=False) + "\n").encode())
        self.assertEqual(self.path.stat().st_mode & 0o777, mode)
        code, out, err = run("kit-asked", "--directory", self.root, "--apply")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Nothing written.", out)
        self.assertEqual(self.path.read_bytes(), written)
        for path, content in stores.items():
            self.assertEqual(path.read_bytes(), content)
        self.assertNotIn(SECRET[0], written.decode())

    def test_title_refusals_are_offline_prewrite_and_payload_free(self):
        self.sent((POLICE, "main", [], [A, UNKNOWN], {"note": SECRET[0], "basis": SECRET[1], "reference": SECRET[2]}))
        self.link(POLICE, "main")
        with mock.patch.object(kit_asked, "load_agencies", side_effect=AssertionError("validated after refusal")):
            self.both_modes_refuse()
        self.assertEqual([p for p in os.listdir(self.root / "content") if p.endswith(".tmp")], [])


if __name__ == "__main__":
    unittest.main()
