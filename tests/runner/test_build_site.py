"""build_site: manifest -> temp campaign -> build -> leak check -> public bucket -> deploy_site card."""
import base64
import hashlib
import json
import unittest
from pathlib import Path

from runner.client import FakeWorkspace
from runner.handlers import build_site
from tests.runner.helpers import REPO, context, job, tempdir

EXAMPLE = REPO / "examples" / "fictional-campaign"


def manifest_from_example():
    content = {}
    for path in sorted((EXAMPLE / "content").rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(EXAMPLE / "content").as_posix()
        if path.suffix in (".json", ".geojson"):
            content[rel] = json.loads(path.read_text())
        else:
            content[rel] = path.read_text()
    return {"version": "v2026-09-30.1", "campaign": json.loads((EXAMPLE / "campaign.json").read_text()), "content": content}


class BuildSiteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_builds_uploads_and_proposes_deploy(self):
        ws = FakeWorkspace()
        ctx = context(self.tmp.name, job("build_site", manifest_from_example()), ws)
        result = build_site.run(ctx)
        self.assertEqual(result.status, "done", result.error)
        paths = [m["path"] for m in result.outputs["manifest"]]
        for expected in ("index.html", "findings/index.html", "findings/cedar-policy-posted.html", "_headers", "style.css", "data/cameras.geojson", "sitemap.xml"):
            self.assertIn(expected, paths)
        self.assertEqual(result.outputs["version"], "v2026-09-30.1")
        for entry in result.outputs["manifest"]:
            data, media = ws.site_files["sites/v2026-09-30.1/" + entry["path"]]
            self.assertEqual(hashlib.sha256(data).hexdigest(), entry["sha256"])
            self.assertEqual(len(data), entry["bytes"])
        self.assertEqual(ws.site_files["sites/v2026-09-30.1/index.html"][1], "text/html; charset=utf-8")
        self.assertEqual(ws.site_files["sites/v2026-09-30.1/_headers"][1], "text/plain; charset=utf-8")
        self.assertEqual(len(ws.proposals), 1)
        action = next(iter(ws.proposals.values()))
        self.assertEqual(action["kind"], "deploy_site")
        self.assertEqual(action["proposal"]["site_version"], "v2026-09-30.1")
        self.assertEqual(action["proposal"]["manifest_sha256"], result.outputs["manifest_sha256"])
        # the temp campaign directory is private; the loop removes it after the job
        work = Path(self.tmp.name) / "work" / "job_0000000000000001"
        self.assertEqual(work.stat().st_mode & 0o777, 0o700)
        self.assertEqual((work / "campaign" / "campaign.json").stat().st_mode & 0o777, 0o600)
        ctx.cleanup()
        self.assertFalse(work.exists())

    def test_leak_in_content_blocks_upload(self):
        ws = FakeWorkspace()
        inputs = manifest_from_example()
        inputs["content"]["site.json"]["about_md"] += "\n\nkey: AKIA" + "ABCDEFGHIJKLMNOP"
        ctx = context(self.tmp.name, job("build_site", inputs), ws)
        result = build_site.run(ctx)
        self.assertEqual(result.status, "failed")
        self.assertIn("potential leak", result.error)
        self.assertEqual(ws.site_files, {})
        self.assertEqual(ws.proposals, {})

    def test_bad_inputs(self):
        ws = FakeWorkspace()
        inputs = manifest_from_example()
        for bad in ({**inputs, "version": "Bad Version!"}, {**inputs, "content": {}}, {**inputs, "content": {**inputs["content"], "../evil.json": {}}}):
            result = build_site.run(context(self.tmp.name, job("build_site", bad), ws))
            self.assertEqual(result.status, "failed", bad.get("version"))
        self.assertEqual(ws.site_files, {})
        # invalid content is refused by the content model before anything is written to the bucket
        inputs["content"]["findings/cedar-policy-posted.json"]["confidence"] = "needs_attorney_review"
        result = build_site.run(context(self.tmp.name, job("build_site", inputs), ws))
        self.assertEqual(result.status, "failed")
        self.assertIn("ContentError", result.error)

    def test_base64_manifest_entries(self):
        ws = FakeWorkspace()
        inputs = manifest_from_example()
        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
        inputs["content"]["public-assets/logo.png"] = {"base64": base64.b64encode(png).decode()}
        result = build_site.run(context(self.tmp.name, job("build_site", inputs), ws))
        self.assertEqual(result.status, "done", result.error)
        self.assertEqual(ws.site_files["sites/v2026-09-30.1/logo.png"][0], png)
        self.assertEqual(ws.site_files["sites/v2026-09-30.1/logo.png"][1], "image/png")


if __name__ == "__main__":
    unittest.main()
