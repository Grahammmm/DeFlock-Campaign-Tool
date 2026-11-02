"""DF1822-06: meetings-stage --finding picks the cited findings; the default is agency first, newest event_date.

Synthetic only: temp copies of examples/fictional-campaign with fixture findings; nothing is fetched or sent.
"""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from campaign_tool.meeting_stage import MeetingStageError, stage, stale_kits

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
MID = "legistar-examplecity-3001"
POLICE, SHERIFF = "ca-cedar-police", "ca-cedar-county-sheriff"


def fid(n):
    return f"fnd_{n:016d}"


def run(*args):
    return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *args], cwd=REPO_ROOT,
                          capture_output=True, text=True, timeout=60)


def bullets(kit):
    return [line for line in kit.split("## Two-minute comment\n", 1)[1].split("\n\n", 1)[0].splitlines()
            if line.startswith("- ")]


def source_slugs(kit):
    return [line.split("/findings/")[1].split(".html")[0] for line in kit.split("## Sources\n", 1)[1].splitlines()
            if line.startswith("- **")]


class MeetingStageFindingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-meeting-stage-findings-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.findings = self.root / "content" / "findings"
        self.template = json.loads((self.findings / "cedar-policy-posted.json").read_text(encoding="utf-8"))
        (self.findings / "cedar-policy-posted.json").unlink()
        (self.root / "kit").mkdir(exist_ok=True)
        (self.root / "kit" / "meetings.json").write_text(json.dumps({"schema_version": 1, "meetings": [
            {"id": MID, "body": "Example City Council", "starts_at": "2026-11-18T18:00:00-08:00"}]}), encoding="utf-8")
        self.target = self.root / "content" / "meetings" / f"{MID}.json"

    def add(self, n, event_date, agency=POLICE, published_at="2026-02-01T18:00:00+00:00", corrections=()):
        document = dict(self.template, id=fid(n), slug=f"syn-{n}", title=f"Synthetic finding {n}",
                        summary=f"Synthetic summary {n}.", event_date=event_date, agency_id=agency,
                        published_at=published_at, corrections=list(corrections))
        (self.findings / f"syn-{n}.json").write_text(json.dumps(document), encoding="utf-8")

    def kit(self):
        return json.loads(self.target.read_text(encoding="utf-8"))["comment_kit_md"]

    def cli(self, *extra):
        return run("meetings-stage", "--directory", str(self.root), "--id", MID, *extra)

    # 1. chosen ids in reverse date order keep the given order; output lists them
    def test_chosen_ids_are_cited_in_the_given_order(self):
        for n, day in ((1, "2026-01-05"), (2, "2026-03-05"), (3, "2026-05-05")):
            self.add(n, day)
        result = self.cli("--finding", fid(1), "--finding", fid(3))
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual((report["findings"], report["findings_cited"]), ([fid(1), fid(3)], 2))
        kit = self.kit()
        self.assertEqual([line.split(" (")[0] for line in bullets(kit)],
                         ["- Synthetic summary 1.", "- Synthetic summary 3."])
        self.assertEqual(source_slugs(kit), ["syn-1", "syn-3"])
        self.assertIn("published 2 reviewed findings", kit)

    def test_agency_id_does_not_filter_chosen_ids(self):
        self.add(1, "2026-01-05", agency=SHERIFF)
        report = stage(self.root, MID, agency_id=POLICE, finding_ids=[fid(1)])
        self.assertEqual(report["findings"], [fid(1)])
        self.assertEqual(json.loads(self.target.read_text(encoding="utf-8"))["agency_id"], POLICE)

    # 2. unknown, draft, duplicate, too many: refused, nothing written, existing kit untouched
    def test_refusals_write_nothing(self):
        for n in range(1, 5):
            self.add(n, f"2026-0{n}-01")
        drafts = self.root / "private" / "drafts"
        drafts.mkdir(parents=True, exist_ok=True)
        (drafts / f"{fid(9)}.json").write_text(json.dumps(dict(self.template, id=fid(9), state="draft")))
        cases = (([fid(7)], f"--finding {fid(7)}: not a published finding"),
                 ([fid(9)], f"--finding {fid(9)}: not a published finding"),
                 ([fid(1), fid(2), fid(1)], f"--finding {fid(1)}: given more than once"),
                 ([fid(n) for n in range(1, 5)], "--finding: give 1 to 3 ids (got 4)"))
        for ids, needle in cases:
            with self.assertRaises(MeetingStageError) as caught:
                stage(self.root, MID, finding_ids=ids)
            self.assertEqual(str(caught.exception), needle)
            self.assertFalse(self.target.exists())
        with self.assertRaises(MeetingStageError):
            stage(self.root, MID, finding_ids=[])
        stage(self.root, MID, finding_ids=[fid(2)])
        before = self.target.read_bytes()
        for ids, needle in cases:
            result = self.cli("--replace", *[part for i in ids for part in ("--finding", i)])
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertIn("Stopped:", result.stderr)
            self.assertIn(needle, result.stderr)
            self.assertEqual(self.target.read_bytes(), before)

    # 3. default: the meeting agency's findings first (newest first), then the newest other one
    def test_default_cites_agency_findings_first_then_newest_other(self):
        self.add(1, "2026-01-01", agency=SHERIFF)
        self.add(2, "2026-02-01")
        self.add(3, "2026-09-01", agency=SHERIFF)
        self.add(4, "2026-04-01")
        self.add(5, "2026-06-01", agency=SHERIFF)
        report = stage(self.root, MID, agency_id=POLICE)
        self.assertEqual(report["findings"], [fid(4), fid(2), fid(3)])
        self.assertEqual(report["findings_cited"], 3)
        self.assertEqual(source_slugs(self.kit()), ["syn-4", "syn-2", "syn-3"])

    def test_default_agency_without_findings_cites_the_newest_others(self):
        self.add(1, "2026-01-01", agency=SHERIFF)
        self.add(2, "2026-02-01", agency=SHERIFF)
        report = stage(self.root, MID, agency_id="ca-cedar-city-council")
        self.assertEqual(report["findings"], [fid(2), fid(1)])

    # 4. default without agency: 3 newest by event_date, ties by id, independent of load/file order
    def test_default_without_agency_is_newest_with_ties_by_id(self):
        days = {1: "2026-03-01", 2: "2026-05-01", 3: "2026-05-01", 4: "2026-01-01", 5: "2026-04-01"}
        results = []
        for flip in (False, True):
            for n, day in days.items():
                hour = (n if flip else 9 - n)
                self.add(n, day, agency=POLICE if n % 2 else SHERIFF, published_at=f"2026-02-01T0{hour}:00:00+00:00")
            results.append(stage(self.root, MID, replace=flip)["findings"])
        self.assertEqual(results, [[fid(2), fid(3), fid(5)]] * 2)

    # 5. no published findings: the records-request text is unchanged
    def test_no_published_findings_keeps_the_request_text(self):
        report = stage(self.root, MID, agency_id=POLICE)
        self.assertEqual((report["findings"], report["findings_cited"]), ([], 0))
        kit = self.kit()
        self.assertIn("We have requested the policy, the vendor agreement and the sharing records", kit)
        self.assertIn("No published findings yet", kit)
        with self.assertRaisesRegex(MeetingStageError, "not a published finding"):
            stage(self.root, MID, replace=True, finding_ids=[fid(1)])

    # 6. stale_kits still sees a chosen finding that is corrected later
    def test_stale_kits_detects_a_chosen_corrected_finding(self):
        for n in range(1, 6):
            self.add(n, f"2026-0{n}-01")
        stage(self.root, MID, finding_ids=[fid(1)])  # the oldest: never in the default choice
        self.assertEqual(stale_kits(self.root), [])
        self.add(1, "2026-01-01", corrections=[{"date": "2026-10-15", "note": "Synthetic fix."}])
        self.assertEqual(stale_kits(self.root), [{"meeting": MID, "finding": "syn-1", "latest_correction": "2026-10-15"}])
        stage(self.root, MID, replace=True, finding_ids=[fid(1)])
        self.assertEqual(stale_kits(self.root), [])
        self.assertIn("/findings/syn-1.html (corrected 2026-10-15;", self.kit())

    # 7. --replace with a new choice replaces the kit; without --replace an existing kit is refused
    def test_replace_with_a_new_choice(self):
        for n in range(1, 4):
            self.add(n, f"2026-0{n}-01")
        self.assertEqual(stage(self.root, MID, finding_ids=[fid(1)])["findings"], [fid(1)])
        before = self.target.read_bytes()
        result = self.cli("--finding", fid(2))
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("exists; pass --replace", result.stderr)
        self.assertEqual(self.target.read_bytes(), before)
        result = self.cli("--finding", fid(2), "--finding", fid(3), "--replace")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((json.loads(result.stdout)["findings"], json.loads(result.stdout)["replaced"]),
                         ([fid(2), fid(3)], True))
        self.assertEqual(source_slugs(self.kit()), ["syn-2", "syn-3"])


if __name__ == "__main__":
    unittest.main()
