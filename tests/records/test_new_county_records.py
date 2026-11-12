"""DFGL-19: a fictional second county goes from `campaign_tool init` to a strict, request-tied e2e-proof PASS offline."""
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from campaign_tool.records import e2e_proof
from tests.records.test_e2e_proof import FIXTURE, HAVE_PARSERS, pdf_email, text_pdf

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "fictional-campaign" / "content"
REQUEST = "cedar-req-1"
SLO = (b"deflockslo", b"san luis obispo")


class NewCountyRecordsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="new-county-records-")
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.campaign, self.project, self.out = self.tmp / "cedar-demo", self.tmp / "records-project", self.tmp / "out"
        self.run_tool("campaign_tool", "init", "--directory", self.campaign, "--name", "Synthetic Cedar",
                      "--county", "Cedar", "--state", "CA")
        shutil.copytree(EXAMPLE, self.campaign / "content")  # the documented synthetic-preview step
        agencies = json.loads((self.campaign / "content" / "agencies.json").read_text(encoding="utf-8"))
        self.agency = next(a for a in agencies if a["agency_id"] == "ca-cedar-police")
        self.message = self.tmp / "m.eml"
        # DFGL-03's message builder with only its ids/names rewritten for the new county.
        self.message.write_bytes(pdf_email().replace(b"FV-2026-0001", b"CE-2026-0001")
                                 .replace(b"fictional-valley", b"cedar-police"))

    def run_tool(self, module, *args):
        done = subprocess.run([sys.executable, "-B", "-m", module, *map(str, args)], cwd=ROOT,
                              capture_output=True, text=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        return done

    def seed(self, agency_id, name):
        """DFGL-03's fixture seed with only ids/names rewritten; the fact sheet cites the built PDF's sha256."""
        text = (FIXTURE / "seed.json").read_text(encoding="utf-8")
        for old, new in (("@POLICY_PDF_SHA256@", hashlib.sha256(text_pdf()).hexdigest()),
                         ("ca-fictional-valley-pd", agency_id), ("Fictional Valley Police Department", name),
                         ("fv-req-1", REQUEST), ("FV-2026-0001", "CE-2026-0001")):
            text = text.replace(old, new)
        path = self.tmp / ("seed-%s.json" % agency_id)
        path.write_text(text, encoding="utf-8")
        return path

    def outcome(self, label, name):
        files = list((self.out / label / "project" / "run-outputs").glob("*/outcomes/" + name))
        self.assertEqual(len(files), 1)
        return json.loads(files[0].read_text(encoding="utf-8"))

    def probes(self, report):
        return next(s for s in report["stages"] if s["stage"] == "next_steps")["detail"]["queued_by_kind"]

    def assert_no_slo_text(self, *roots):
        for root in roots:
            for path in root.rglob("*"):
                if path.is_file() and not path.is_symlink():
                    data = path.read_bytes().lower()
                    for needle in SLO:
                        self.assertNotIn(needle, data, path)

    @unittest.skipUnless(HAVE_PARSERS, "UNSUPPORTED_RUNTIME: pypdf/openpyxl not in this interpreter")
    def test_second_county_init_project_init_and_seeded_replay_pass(self):
        self.run_tool("campaign_tool.records", "project-init", "--project", self.project,
                      "--campaign-root", self.campaign)
        # Layout only: DFGL-18's tests own project-init behaviour.
        for name in ("campaign.json", ".campaign-root", "processing-v1/ledger.sqlite", "portal-queue-v1/portal.sqlite"):
            self.assertTrue((self.project / name).is_file(), name)
        report = e2e_proof.replay(self.message, self.out, self.campaign, label="cedar",
                                  seed=self.seed(self.agency["agency_id"], self.agency["name"]))
        self.assertEqual((report["result"], report["manual_steps"]), ("PASS", []), report["stages"])
        seeded = next(s for s in report["stages"] if s["stage"] == "seed")
        self.assertEqual((seeded["status"], seeded["detail"]["agencies"], seeded["detail"]["requests"]), ("ok", 1, 1))
        items = self.outcome("cedar", "received-items.json")["items"]
        self.assertTrue(items)
        self.assertTrue(all(i.get("agency_id") == "ca-cedar-police" and i.get("request_id") == REQUEST
                            for i in items), items)
        queued = self.probes(report)
        self.assertGreaterEqual(queued["next-followup-probe"], 1)
        self.assertGreaterEqual(queued["next-social-post"], 1)  # the agency is in the new campaign's content
        self.assert_no_slo_text(self.out, self.project)

    @unittest.skipUnless(HAVE_PARSERS, "UNSUPPORTED_RUNTIME: pypdf/openpyxl not in this interpreter")
    def test_seed_agency_outside_the_new_campaign_gets_no_post_or_site_update(self):
        # The old county's fixture agency is not in Cedar's content: its items carry it, but next steps queue
        # no social post or site update for it (agency_not_in_content); only the request-tied PROBE remains.
        report = e2e_proof.replay(self.message, self.out, self.campaign, label="stale",
                                  seed=self.seed("ca-fictional-valley-pd", "Fictional Valley Police Department"))
        self.assertTrue(all(i.get("agency_id") == "ca-fictional-valley-pd"
                            for i in self.outcome("stale", "received-items.json")["items"]))
        queued = self.probes(report)
        self.assertEqual((queued["next-social-post"], queued["next-site-update"]), (0, 0), report["stages"])


if __name__ == "__main__":
    unittest.main()
