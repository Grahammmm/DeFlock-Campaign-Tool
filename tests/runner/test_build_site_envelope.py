"""Fail-closed contract checks; the separate seam suite obtains real Worker jobs."""
import copy
import hashlib
import json
import unittest

from runner.client import FakeWorkspace
from runner.handlers import build_site
from tests.runner.helpers import context, job, tempdir


def seal(manifest):
    body = {k: v for k, v in manifest.items() if k not in ("site_version", "manifest_sha256")}
    digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    manifest.update(manifest_sha256=digest, site_version="m" + digest[:16])
    return {"schema_version": 1, "site_version": manifest["site_version"], "reason": "synthetic rebuild", "manifest": manifest}


def envelope():
    return seal({"schema_version": 2,
                 "campaign": {"schema_version": 1, "name": "Synthetic Records", "county": "Example", "state": "CA", "country": "US"},
                 "site": {"schema_version": 1, "tagline": "Synthetic records", "about_md": "Reviewed synthetic records."},
                 "findings": [], "meetings": [], "sources": [], "agencies": [], "map": None})


class BuildEnvelopeTests(unittest.TestCase):
    def test_versioned_build_binds_input_and_output_without_another_deploy_card(self):
        inputs = envelope()
        with tempdir() as tmp:
            ws = FakeWorkspace()
            ctx = context(tmp, job("build_site", inputs), ws)
            result = build_site.run(ctx)
            self.assertEqual(result.status, "done", result.error)
            self.assertEqual(result.outputs["input_manifest_sha256"], inputs["manifest"]["manifest_sha256"])
            self.assertEqual(result.receipt["input_manifest_sha256"], inputs["manifest"]["manifest_sha256"])
            self.assertEqual(result.outputs["build_job_id"], ctx.job_id)
            self.assertEqual(ws.proposals, {})
            self.assertTrue(ws.site_files)
            first_files = copy.deepcopy(ws.site_files)
            ctx.cleanup()
            again = build_site.run(context(tmp, job("build_site", inputs), ws))
            self.assertEqual(again.status, "done", again.error)
            self.assertEqual(again.outputs, result.outputs)
            self.assertEqual(ws.site_files, first_files)
            self.assertEqual(ws.proposals, {})

    def test_invalid_envelope_fails_before_any_write_or_upload(self):
        cases = []
        for key, value in (("schema_version", 2), ("schema_version", True), ("schema_version", None),
                           ("site_version", "m" + "0" * 16), ("site_version", []), ("manifest", []),
                           ("reason", {}), ("version", "legacy"), ("content", {})):
            cases.append({**envelope(), key: value})
        cases.extend(([], "bad", None, {"site_version": "legacy", "manifest": {}}))
        for key, value in (("schema_version", 1), ("schema_version", True), ("campaign", None),
                           ("site", []), ("findings", {}), ("meetings", None), ("sources", {}),
                           ("agencies", {}), ("map", {}), ("extra", "unknown")):
            item = envelope()["manifest"]
            item[key] = value
            cases.append(seal(item))
        for key, value in (("state", "California"), ("name", ""), ("country", "us"),
                           ("schema_version", True), ("extra", "private setting")):
            item = envelope()["manifest"]
            item["campaign"][key] = value
            cases.append(seal(item))
        for number in (0.5, 1e-7, 9007199254740992):
            item = envelope()["manifest"]
            item["site"]["schema_version"] = number
            cases.append(seal(item))
        tampered = envelope()
        tampered["manifest"]["site"]["tagline"] = "changed after hashing"
        cases.append(tampered)
        for values in ([{"slug": "../escape"}], [{"slug": "same"}, {"slug": "same"}]):
            item = envelope()["manifest"]
            item["findings"] = values
            cases.append(seal(item))
        for values in ([{"id": "../escape"}], [{"id": "same"}, {"id": "same"}]):
            item = envelope()["manifest"]
            item["meetings"] = values
            cases.append(seal(item))
        for index, inputs in enumerate(cases):
            with self.subTest(case=index), tempdir() as tmp:
                ws = FakeWorkspace()
                ctx = context(tmp, {**job("build_site"), "inputs": inputs}, ws)
                result = build_site.run(ctx)
                self.assertEqual(result.status, "failed", inputs)
                self.assertEqual(ws.site_files, {})
                self.assertEqual(ws.proposals, {})
                self.assertFalse((ctx.dir / "campaign").exists())


if __name__ == "__main__":
    unittest.main()
