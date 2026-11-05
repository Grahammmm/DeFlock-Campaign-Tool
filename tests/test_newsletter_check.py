"""DF1622-13: private newsletter draft provenance and the read-only ``--check``. Synthetic; nothing is sent."""
import hashlib
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool.newsletter.check import MATCH, NOT_CHECKED, check_draft  # noqa: E402
from campaign_tool.newsletter.draft import DraftError, build_draft, write_draft  # noqa: E402
from campaign_tool.newsletter.from_content import manifest_from_content  # noqa: E402
from tests.test_newsletter_from_content import SITE, finding, meeting  # noqa: E402

BASELINE = json.loads((REPO_ROOT / "tests" / "fixtures" / "newsletter_provenance" / "baseline.json").read_text())
FAR = datetime(2100, 1, 1, tzinfo=timezone.utc)
NOTE = "Fixed the policy date."


def snapshot(root):
    out = {}
    for path in sorted(Path(root).rglob("*")):
        info = path.lstat()
        out[str(path.relative_to(root))] = (info.st_mode, info.st_size, info.st_mtime_ns,
                                            hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None)
    return out


class CheckBase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-newsletter-check-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "init", "--directory", str(self.root), "--name",
                        "Synthetic", "--county", "Synthetic", "--state", "CA"], cwd=REPO_ROOT, check=True,
                       capture_output=True, timeout=30)
        self.kit = self.root / "kit"
        self.draft_path = self.kit / "newsletter-draft.json"

    def write_content(self, findings=(), meetings=()):
        content = self.root / "content"
        (content / "findings").mkdir(parents=True, exist_ok=True)
        (content / "meetings").mkdir(exist_ok=True)
        (content / "site.json").write_text(json.dumps(SITE))
        for item in findings:
            self.put_finding(item)
        for name, item in meetings:
            self.put_meeting(name, item)

    def put_finding(self, item):
        (self.root / "content" / "findings" / (item["slug"] + ".json")).write_text(json.dumps(item))

    def put_meeting(self, name, item):
        (self.root / "content" / "meetings" / (name + ".json")).write_text(json.dumps(item))

    def standard(self):
        self.write_content([finding("cedar-policy", "2026-02-01T18:00:00+00:00", [{"date": "2026-03-03", "note": NOTE}]),
                            finding("oak-retention", "2026-03-10T18:00:00+00:00")],
                           [("council-late", meeting("Synthetic Council Late", "2099-06-02T18:00:00-07:00")),
                            ("council-soon", meeting("Synthetic Council Soon", "2099-05-01T18:00:00-07:00"))])

    def cli(self, *extra, module="campaign_tool.newsletter"):
        return subprocess.run([sys.executable, "-B", "-m", module, "--directory", str(self.root), *extra],
                              cwd=REPO_ROOT, capture_output=True, text=True, timeout=30)

    def build(self, *extra):
        result = self.cli("--from-content", *extra)
        self.assertEqual(result.returncode, 0, result.stderr)

    def check(self, *extra):
        return self.cli("--check", *extra)

    def provenance(self):
        return json.loads(self.draft_path.read_text())["provenance"]

    def rebuild_command(self, since=None):
        return (f"python3 -m campaign_tool.newsletter --directory {shlex.quote(str(self.root.resolve()))} --from-content"
                + (f" --since {since}" if since else ""))

    def assert_different(self, *fragments, since="2026-03-01"):
        result = self.check()
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        for fragment in fragments:
            self.assertIn(fragment, result.stdout)
        for line in result.stdout.splitlines():
            self.assertTrue(line.endswith("rebuild: " + self.rebuild_command(since)), line)
        return result.stdout


class ProvenanceTests(CheckBase):
    def test_fresh_content_round_trip_matches_text_and_json(self):
        self.standard()
        self.build("--since", "2026-03-01")
        provenance = self.provenance()
        self.assertEqual(set(provenance), {"schema_version", "built_at", "since", "findings", "meetings"})
        self.assertEqual((provenance["schema_version"], provenance["since"]), (1, "2026-03-01T00:00:00+00:00"))
        self.assertTrue(provenance["built_at"].endswith("+00:00"))
        self.assertEqual([(f["section"], f["slug"]) for f in provenance["findings"]],
                         [("new", "oak-retention"), ("correction", "cedar-policy")])
        self.assertEqual(provenance["findings"][1]["corrections"], [{"date": "2026-03-03", "note": NOTE}])
        self.assertEqual([(m["id"], m["agenda_url"]) for m in provenance["meetings"]],
                         [("council-soon", "https://synthetic.example.invalid/agenda"),
                          ("council-late", "https://synthetic.example.invalid/agenda")])
        before = snapshot(self.root)
        text = self.check()
        self.assertEqual((text.returncode, text.stdout.strip(), text.stderr), (0, MATCH, ""))
        data = self.check("--json")
        self.assertEqual((data.returncode, json.loads(data.stdout)),
                         (0, {"status": "match", "message": MATCH, "differences": []}))
        self.assertEqual(snapshot(self.root), before)

    def test_null_since_and_empty_content_round_trip(self):
        self.write_content()
        self.build()
        self.assertEqual(self.provenance()["findings"], [])
        self.assertIsNone(self.provenance()["since"])
        self.assertEqual(self.check().stdout.strip(), MATCH)
        self.standard()  # content now gains a finding: null since means every published finding is in the window
        self.assert_different("finding fnd_cedar_policy (cedar-policy): published in the draft's window", since=None)

    def test_all_counted_entries_survive_caps_and_correction_selection(self):
        long_note = "Long correction note. " * 20
        items = [finding(f"new-{n:02d}", f"2026-04-{n + 1:02d}T10:00:00+00:00") for n in range(14)]
        items.append(finding("old-corrected", "2026-01-01T10:00:00+00:00", [{"date": "2026-03-20", "note": long_note}]))
        items.append(finding("old-plain", "2026-01-02T10:00:00+00:00", [{"date": "2026-02-01", "note": "Before the window."}]))
        items.append(finding("new-corrected", "2026-04-20T10:00:00+00:00", [{"date": "2026-04-21", "note": "Also fixed."}]))
        self.write_content(items, [(f"m-{n:02d}", meeting(f"Body {n}", f"2099-05-{n + 1:02d}T18:00:00-07:00")) for n in range(10)])
        result = self.cli("--from-content", "--since", "2026-03-01")
        out = json.loads(result.stdout)
        self.assertEqual(out["counts"], {"findings": 15, "meetings": 10})
        provenance = self.provenance()
        news = [f for f in provenance["findings"] if f["section"] == "new"]
        corrections = [f for f in provenance["findings"] if f["section"] == "correction"]
        self.assertEqual((len(news), len(corrections), len(provenance["meetings"])), (15, 1, 10))
        self.assertEqual(len({f["id"] for f in provenance["findings"]}), 16)
        self.assertEqual(corrections[0]["slug"], "old-corrected")
        self.assertEqual(corrections[0]["corrections"], [{"date": "2026-03-20", "note": long_note}])
        self.assertEqual([f["corrections"] for f in news if f["slug"] == "new-corrected"], [[{"date": "2026-04-21", "note": "Also fixed."}]])
        self.assertNotIn("old-plain", {f["slug"] for f in provenance["findings"]})
        self.assertEqual(self.check().stdout.strip(), MATCH)

    def test_provenance_requires_identity_only_when_asked(self):
        manifest = {"schema_version": 1, "campaign": {"name": "S", "base_url": None, "county_name": "S"}, "since": None,
                    "findings": [{"title": "T", "summary": "S.", "classification": "documented_fact", "confidence": "verified",
                                  "path": "/findings/t.html", "published_at": "2026-04-01T00:00:00+00:00", "corrected": False}],
                    "meetings": []}
        self.assertNotIn("provenance", build_draft(manifest, FAR))
        with self.assertRaises(DraftError):
            build_draft(manifest, FAR, provenance=True)


class CheckDriftTests(CheckBase):
    def setUp(self):
        super().setUp()
        self.standard()
        self.build("--since", "2026-03-01")

    def test_correction_added_removed_same_date_note_and_order_changed(self):
        cedar = finding("cedar-policy", "2026-02-01T18:00:00+00:00", [{"date": "2026-03-03", "note": NOTE}])
        oak = finding("oak-retention", "2026-03-10T18:00:00+00:00", [{"date": "2026-03-12", "note": "Secret wording."}])
        self.put_finding(oak)
        out = self.assert_different("finding fnd_oak_retention (oak-retention): corrections changed (added 2026-03-12)")
        self.assertNotIn("Secret wording", out)
        self.put_finding(finding("oak-retention", "2026-03-10T18:00:00+00:00"))
        self.put_finding({**cedar, "corrections": [{"date": "2026-03-03", "note": "Different SECRET note."}]})
        out = self.assert_different("dates unchanged; correction details changed")
        self.assertNotIn("SECRET", out)
        self.put_finding({**cedar, "corrections": []})
        self.assert_different("corrections changed (removed 2026-03-03)")
        self.put_finding({**cedar, "corrections": [{"date": "2026-03-03", "note": NOTE}, {"date": "2026-03-02", "note": "x"}]})
        self.assert_different("added 2026-03-02")

    def test_listed_finding_withdrawn_or_unpublished(self):
        (self.root / "content" / "findings" / "oak-retention.json").unlink()
        self.assert_different("finding fnd_oak_retention (oak-retention): withdrawn or unpublished")
        data = json.loads(self.check("--json").stdout)
        self.assertEqual((data["status"], [(d["kind"], d["id"], d["reasons"]) for d in data["differences"]]),
                         ("different", [("finding", "fnd_oak_retention", ["withdrawn or unpublished"])]))
        self.assertEqual(data["differences"][0]["rebuild_command"], self.rebuild_command("2026-03-01"))
        self.put_finding({**finding("oak-retention", "2026-03-10T18:00:00+00:00"), "state": "withdrawn"})
        result = self.check()  # a non-published file left in content/findings is a content error, as for the loader
        self.assertEqual(result.returncode, 1)
        self.assertIn("Stopped: ", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_meeting_cancelled_rescheduled_removed_started_agenda_changed(self):
        self.put_meeting("council-soon", meeting("Synthetic Council Soon", "2099-05-01T18:00:00-07:00", status="cancelled"))
        self.assert_different("meeting council-soon: cancelled")
        self.put_meeting("council-soon", meeting("Synthetic Council Soon", "2099-05-02T18:00:00-07:00"))
        self.assert_different("meeting council-soon: rescheduled from 2099-05-01T18:00:00-07:00 to 2099-05-02T18:00:00-07:00")
        self.put_meeting("council-soon", {**meeting("Synthetic Council Soon", "2099-05-01T18:00:00-07:00"), "agenda_url": None})
        self.assert_different("meeting council-soon: agenda link changed")
        self.put_meeting("council-soon", meeting("Synthetic Council Soon", "2099-05-01T18:00:00-07:00"))
        (self.root / "content" / "meetings" / "council-late.json").unlink()
        self.assert_different("meeting council-late: removed")
        self.put_meeting("council-late", meeting("Synthetic Council Late", "2099-06-02T18:00:00-07:00"))
        self.assertEqual(check_draft(self.root)["status"], "match")
        report = check_draft(self.root, now=FAR)  # both listed meetings have started by then
        self.assertEqual([(d["id"], d["reasons"]) for d in report["differences"] if d["kind"] == "meeting"],
                         [("council-late", ["already started"]), ("council-soon", ["already started"])])
        # several reasons at once, and the start instant boundary (>= now still counts as upcoming)
        self.put_meeting("council-soon", {**meeting("Synthetic Council Soon", "2099-05-03T18:00:00-07:00", status="cancelled"),
                                          "agenda_url": None})
        reasons = [d["reasons"] for d in check_draft(self.root)["differences"]]
        self.assertEqual(len(reasons[0]), 3)
        self.put_meeting("council-soon", meeting("Synthetic Council Soon", "2099-05-01T18:00:00-07:00"))
        at = datetime(2099, 5, 2, 1, 0, tzinfo=timezone.utc)  # exactly the start instant of council-soon
        self.assertEqual(check_draft(self.root, now=at)["status"], "match")

    def test_new_content_missing_from_draft(self):
        self.put_finding(finding("elm-budget", "2026-04-01T09:00:00+00:00"))
        self.put_finding(finding("ash-old", "2025-12-01T09:00:00+00:00"))  # before the window, uncorrected: not reported
        self.put_finding(finding("fir-fixed", "2025-12-02T09:00:00+00:00", [{"date": "2026-04-02", "note": "n"}]))
        self.put_meeting("council-extra", meeting("Synthetic Extra", "2099-07-01T18:00:00-07:00"))
        self.put_meeting("council-cancelled", meeting("Synthetic Cancelled", "2099-07-02T18:00:00-07:00", status="cancelled"))
        self.put_meeting("council-past", meeting("Synthetic Past", "2000-07-02T18:00:00-07:00"))
        out = self.assert_different("finding fnd_elm_budget (elm-budget): published in the draft's window but not in the draft",
                                    "finding fnd_fir_fixed (fir-fixed): corrected in the draft's window but not in the draft",
                                    "meeting council-extra: upcoming but not in the draft")
        for absent in ("ash-old", "council-cancelled", "council-past"):
            self.assertNotIn(absent, out)

    def test_unlisted_finding_is_found_by_id_not_file_order(self):
        data = json.loads(self.check("--json").stdout)
        self.assertEqual(data["status"], "match")
        (self.root / "content" / "findings" / "cedar-policy.json").rename(self.root / "content" / "findings" / "zzz-cedar.json")
        self.assertEqual(self.check().returncode, 0)  # a renamed file keeping slug and id changes nothing


class LegacyTests(CheckBase):
    def not_checked(self):
        for extra in ((), ("--json",)):
            result = self.check(*extra)
            self.assertEqual((result.returncode, result.stderr), (3, ""))
            self.assertIn(NOT_CHECKED, result.stdout)
            self.assertNotIn("draft matches", result.stdout)
        self.assertEqual(json.loads(self.check("--json").stdout),
                         {"status": "not_checked", "message": NOT_CHECKED, "differences": []})

    def test_legacy_manifest_template_and_unknown_schema_not_checked(self):
        self.standard()
        manifest = self.root / "manifest.json"
        manifest.write_text(json.dumps(BASELINE["cases"]["manifest"]["manifest"]))
        self.assertEqual(self.cli("--manifest", str(manifest)).returncode, 0)
        self.assertNotIn("provenance", json.loads(self.draft_path.read_text()))
        self.not_checked()
        self.assertEqual(self.cli().returncode, 0)  # the template draft
        self.not_checked()
        self.build("--since", "2026-03-01")
        draft = json.loads(self.draft_path.read_text())
        for version in (2, True, "1", None, 1.5):
            draft["provenance"]["schema_version"] = version
            self.draft_path.write_text(json.dumps(draft))
            self.not_checked()
        del draft["provenance"]
        self.draft_path.write_text(json.dumps(draft))
        self.not_checked()

    def test_invalid_draft_content_and_flag_matrix(self):
        self.standard()
        result = self.check()
        self.assertEqual((result.returncode, result.stdout), (1, ""))  # no kit, no draft
        self.assertIn("Stopped: ", result.stderr)
        self.assertFalse(self.kit.exists() and self.draft_path.exists())
        self.build("--since", "2026-03-01")
        good = self.draft_path.read_text()
        draft = json.loads(good)

        def stopped(text=None):
            if text is not None:
                self.draft_path.write_text(text)
            result = self.check()
            self.assertEqual((result.returncode, result.stdout), (1, ""), result.stderr)
            self.assertTrue(result.stderr.startswith("Stopped: "))
            self.assertNotIn("Traceback", result.stderr)

        stopped("{not json")
        stopped('{"provenance": {"schema_version": 1}, "provenance": {}}')  # duplicate key
        stopped("[]")
        bad = json.loads(good)
        bad["provenance"]["findings"][0]["section"] = "other"
        stopped(json.dumps(bad))
        bad = json.loads(good)
        bad["provenance"]["findings"].append(dict(bad["provenance"]["findings"][0]))
        stopped(json.dumps(bad))
        bad = json.loads(good)
        bad["provenance"]["built_at"] = "2026-05-01T00:00:00"
        stopped(json.dumps(bad))
        bad = json.loads(good)
        bad["provenance"]["findings"][0]["corrections"] = [{"date": "yesterday", "note": "n"}]
        stopped(json.dumps(bad))
        stopped(" " * (4 * 1024 * 1024 + 1))
        self.draft_path.unlink()
        stopped("")  # empty file is not JSON
        self.draft_path.unlink()
        os.symlink(self.root / "nowhere.json", self.draft_path)  # dangling link
        stopped()
        self.draft_path.unlink()
        self.draft_path.mkdir()
        stopped()
        self.draft_path.rmdir()
        self.draft_path.write_text(good)
        moved = self.root / "kit-real"
        self.kit.rename(moved)
        os.symlink(moved, self.kit)
        stopped()
        self.kit.unlink()
        moved.rename(self.kit)
        # unreadable content
        import shutil
        shutil.move(str(self.root / "content"), str(self.root / "content-off"))
        stopped()
        shutil.move(str(self.root / "content-off"), str(self.root / "content"))
        (self.root / "content" / "site.json").write_text("{")
        stopped()
        (self.root / "content" / "site.json").write_text(json.dumps(SITE))
        self.assertEqual(self.check().returncode, 0)
        if hasattr(os, "mkfifo"):
            self.draft_path.unlink()
            os.mkfifo(self.draft_path)
            stopped()  # refused without opening it, so it cannot block
            self.draft_path.unlink()
            self.draft_path.write_text(good)
        # flags: usage errors exit 2 before any file access
        for extra in (("--manifest", "x.json"), ("--from-content",), ("--since", "2026-03-01"), ("--site", "pub"),
                      ("--record-sent", "2026-05-01"), ("--date", "2026-05-01"), ("--publish",), ("sent",), ("archive",)):
            result = self.check(*extra)
            self.assertEqual((result.returncode, result.stdout), (2, ""), extra)
        result = self.cli("--json")
        self.assertEqual((result.returncode, result.stdout), (2, ""))
        self.assertIn("--json belongs to --check", result.stderr)

    def test_documented_messages_are_pinned(self):
        text = (REPO_ROOT / "docs" / "NEWSLETTER.md").read_text()
        for needle in (MATCH, NOT_CHECKED, "`provenance`", "--check"):
            self.assertIn(needle, text)


class CompatibilityTests(CheckBase):
    def test_default_callers_match_prechange_bytes(self):
        now = datetime.fromisoformat(BASELINE["now"])
        for name, case in BASELINE["cases"].items():
            with self.subTest(case=name):
                draft = build_draft(case["manifest"], now, archive_public=case["archive_public"])
                self.assertEqual(sorted(draft), case["keys"])
                with tempfile.TemporaryDirectory() as tmp:
                    written = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in write_draft(tmp, draft)}
                self.assertEqual(written, {k: v.replace("-", "") for k, v in case["files"].items()})
        # ids and slug on a content manifest never render and change nothing without the opt-in
        content = BASELINE["cases"]["content"]
        with_ids = json.loads(json.dumps(content["manifest"]))
        for index, row in enumerate(with_ids["findings"]):
            row.update(id=f"fnd_secret_{index}", slug=f"secret-slug-{index}")
        for index, row in enumerate(with_ids["meetings"]):
            row["id"] = f"secret-meeting-{index}"
        draft = build_draft(with_ids, now, archive_public=True)
        self.assertNotIn("provenance", draft)
        with tempfile.TemporaryDirectory() as tmp:
            written = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in write_draft(tmp, draft)}
        self.assertEqual(written, {k: v.replace("-", "") for k, v in content["files"].items()})
        opted = build_draft(with_ids, now, archive_public=True, provenance=True)
        self.assertEqual({k: v for k, v in opted.items() if k != "provenance"}, draft)
        self.assertNotIn("secret", opted["html"] + opted["text"] + json.dumps(opted["public_archive"]))

    def test_content_manifest_passes_ids_through(self):
        self.standard()
        manifest = manifest_from_content(self.root, {"name": "Synthetic", "county": "Synthetic"})
        self.assertEqual(sorted(f["id"] for f in manifest["findings"]), ["fnd_cedar_policy", "fnd_oak_retention"])
        self.assertEqual(sorted(m["id"] for m in manifest["meetings"]), ["council-late", "council-soon"])
        self.assertEqual(sorted(f["slug"] for f in manifest["findings"]), ["cedar-policy", "oak-retention"])

    def test_public_archive_and_rendered_outputs_unchanged(self):
        self.standard()
        self.build("--since", "2026-03-01")
        saved = json.loads(self.draft_path.read_text())
        html, txt = (self.kit / "newsletter-draft.html").read_text(), (self.kit / "newsletter-draft.txt").read_text()
        canaries = [saved["provenance"]["built_at"], "fnd_cedar_policy", "fnd_oak_retention", "council-soon", "council-late",
                    "provenance"]
        for blob in (html, txt, saved["subject"], json.dumps(saved["public_archive"])):
            for canary in canaries:
                self.assertNotIn(canary, blob)
        # same inputs without provenance: only the private key differs
        from campaign_tool.cli import read_config
        manifest = manifest_from_content(self.root, read_config(self.root), "2026-03-01T00:00:00+00:00")
        now = datetime.fromisoformat(saved["provenance"]["built_at"])
        plain = build_draft(manifest, now, archive_public=True)
        withp = build_draft(manifest, now, archive_public=True, provenance=True)
        self.assertEqual({k: v for k, v in withp.items() if k != "provenance"}, plain)
        self.assertEqual(withp["provenance"]["built_at"], saved["provenance"]["built_at"])
        # record the issue, then project it publicly: provenance stays only in the private kept copy
        recorded = self.cli("sent", "--date", "2026-05-01")
        self.assertEqual(recorded.returncode, 0, recorded.stderr)
        kept = self.kit / "newsletter-issues" / "2026-05-01.json"
        self.assertIn("provenance", json.loads(kept.read_text()))
        published = self.cli("archive", "--date", "2026-05-01", "--publish")
        self.assertEqual(published.returncode, 0, published.stderr)
        public = self.root / "content" / "newsletters" / "2026-05-01.json"
        self.assertEqual(set(json.loads(public.read_text())), {"schema_version", "sent_on", "title", "body_md"})
        for canary in canaries:
            self.assertNotIn(canary, public.read_text() + published.stdout)

    def test_check_never_mutates_on_any_result(self):
        self.standard()
        self.build("--since", "2026-03-01")
        from unittest import mock
        import campaign_tool.newsletter.__main__ as entry
        import socket
        for label, mutate in (("match", lambda: None),
                              ("drift", lambda: (self.root / "content" / "findings" / "oak-retention.json").unlink()),
                              ("error", lambda: (self.root / "content" / "site.json").write_text("{"))):
            mutate()
            before = snapshot(self.root)
            with mock.patch.object(socket.socket, "connect", side_effect=AssertionError("network")), \
                    mock.patch("campaign_tool.newsletter.draft.write_draft", side_effect=AssertionError("write")), \
                    mock.patch("campaign_tool.newsletter.__main__.write_draft", side_effect=AssertionError("write")), \
                    mock.patch("campaign_tool.newsletter.__main__.record_sent", side_effect=AssertionError("record")), \
                    mock.patch("campaign_tool.newsletter.__main__.publish_issue", side_effect=AssertionError("publish")), \
                    mock.patch("campaign_tool.newsletter.__main__.last_sent", side_effect=AssertionError("sent")), \
                    mock.patch("campaign_tool.newsletter.__main__.read_config", side_effect=AssertionError("config")):
                code = entry.main(["--directory", str(self.root), "--check"])
            self.assertEqual(code, {"match": 0, "drift": 3, "error": 1}[label])
            self.assertEqual(snapshot(self.root), before, label)
        # an empty directory is not created into by a check
        empty = Path(tempfile.mkdtemp(prefix="synthetic-empty-"))
        self.addCleanup(empty.rmdir)
        self.assertEqual(entry.main(["--directory", str(empty), "--check"]), 1)
        self.assertEqual(list(empty.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
