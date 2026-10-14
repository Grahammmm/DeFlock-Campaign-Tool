"""Real Worker producer/D1/API → Python Workspace/runner contracts, synthetic only.

Requires the checked-in Workers npm dependencies (`cd workers && npm ci`) and
Node >=22. Python-only installations explicitly skip this cross-runtime suite.
The Worker handlers run in Node; Miniflare supplies ephemeral D1/R2/KV. Requests
cross a JSON subprocess boundary through the actual Python Workspace client.
"""
import base64
import copy
import io
import json
import selectors
import shutil
import subprocess
import tempfile
import unittest
import urllib.error
import urllib.parse

from runner.client import Workspace, WorkspaceError
from runner.handlers import build_site
from runner.loop import run_job
from tests.runner.helpers import REPO, Silent, context, settings


class WorkerBridge:
    """urllib opener backed by actual Worker routes and local D1/R2 bindings."""

    def __init__(self):
        self.stderr = tempfile.TemporaryFile(mode="w+")
        self.process = subprocess.Popen(
            ["node", str(REPO / "workers/workspace/test/build-contract-bridge.mjs")],
            cwd=REPO, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=self.stderr, text=True, bufsize=1,
        )
        try:
            if self.read() != {"ready": True}:
                raise RuntimeError("Worker bridge did not initialize")
        except Exception:
            self.close()
            raise
        self.put_count = 0
        self.fail_put_at = None

    def read(self):
        with selectors.DefaultSelector() as selector:
            selector.register(self.process.stdout, selectors.EVENT_READ)
            if not selector.select(30):
                raise RuntimeError("Worker bridge response timed out")
        line = self.process.stdout.readline()
        if not line:
            self.stderr.seek(0)
            raise RuntimeError("Worker bridge exited: " + self.stderr.read()[-4000:])
        response = json.loads(line)
        if "error" in response:
            raise RuntimeError(response["error"])
        return response

    def request(self, method, path, body=None, headers=None):
        wire = {"method": method, "path": path, "headers": headers or {},
                "body": None if body is None else base64.b64encode(body).decode()}
        self.process.stdin.write(json.dumps(wire) + "\n")
        self.process.stdin.flush()
        response = self.read()
        return response["status"], response["headers"], base64.b64decode(response["body"])

    def fixture(self, operation):
        status, _, body = self.request("POST", "/__fixture/" + operation)
        if status != 200:
            raise AssertionError(f"fixture {operation}: HTTP {status}: {body.decode()}")
        return json.loads(body)

    def __call__(self, request, timeout=None):
        if request.method == "PUT":
            self.put_count += 1
            if self.put_count == self.fail_put_at:
                raise urllib.error.HTTPError(request.full_url, 503, "Synthetic staging outage", {}, io.BytesIO(b"retry"))
        parsed = urllib.parse.urlsplit(request.full_url)
        path = parsed.path + ("?" + parsed.query if parsed.query else "")
        status, headers, body = self.request(request.method, path, request.data, dict(request.header_items()))
        if status >= 400:
            raise urllib.error.HTTPError(request.full_url, status, "Worker refused", headers, io.BytesIO(body))
        response = io.BytesIO(body)
        response.status = status
        response.headers = headers
        return response

    def close(self):
        self.process.stdin.close()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        self.process.stdout.close()
        self.stderr.close()


class BuildSiteWorkerContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("node") or not (REPO / "workers/node_modules/esbuild/package.json").is_file():
            raise unittest.SkipTest("Worker contract requires Node >=22 and `cd workers && npm ci`")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bridge = WorkerBridge()
        self.addCleanup(self.bridge.close)
        self.workspace = Workspace("http://localhost", "synthetic-build-contract-token", opener=self.bridge, max_retries=0)
        self.assertEqual(self.bridge.fixture("seed")["finding"]["state"], "ready")

    def lease_build(self):
        # Publication also queues a newsletter draft. Do not rewrite queue rows;
        # consume that unrelated job through the actual result route if encountered.
        for _ in range(5):
            leased = self.workspace.lease_job()
            self.assertIsNotNone(leased)
            if leased["kind"] == "build_site":
                return leased
            self.workspace.post_result(leased["job_id"], "blocked", error="Unrelated synthetic fixture job")
        self.fail("build job not leased")

    def consume(self, leased):
        return run_job(self.workspace, settings(self.tmp.name), leased, Silent(), {"build_site": build_site.run})

    def assert_unpublished(self, expected_actions):
        snapshot = self.bridge.fixture("snapshot")
        deploys = [action for action in snapshot["actions"] if action["kind"] == "deploy_site"]
        self.assertEqual(len(deploys), expected_actions)
        self.assertTrue(all(action["state"] == "proposed" and action["approved_by"] is None for action in deploys))
        self.assertIsNone(snapshot["site_version"])
        return snapshot

    def test_publish_correction_withdrawal_cross_actual_queue_and_consumer(self):
        versions = []
        published = self.bridge.fixture("publish")
        self.assertEqual(published["unapproved_status"], 409)
        self.assertTrue(published["repeat_receipt"])
        for index, operation in enumerate(("publish", "correct", "withdraw"), 1):
            with self.subTest(operation=operation):
                changed = published if operation == "publish" else self.bridge.fixture(operation)
                leased = self.lease_build()
                self.assertEqual(leased["job_id"], changed["build_job_id"])
                inputs = leased["inputs"]
                self.assertEqual(set(inputs), {"schema_version", "site_version", "reason", "manifest"})
                self.assertEqual(inputs["schema_version"], 1)
                self.assertEqual(inputs["manifest"]["schema_version"], 2)
                self.assertEqual(inputs["manifest"]["campaign"], {"schema_version": 1, "name": "Synthetic County Records",
                                                                     "county": "Synthetic", "state": "CA", "country": "US"})
                result = self.consume(leased)
                self.assertEqual(result.status, "done", result.error)
                self.assertEqual(result.outputs["build_job_id"], leased["job_id"])
                version = result.outputs["version"]
                self.assertEqual(version, inputs["site_version"])
                versions.append(version)
                snapshot = self.assert_unpublished(index)
                files = {item["key"]: item["text"] for item in snapshot["files"] if item["key"].startswith(f"sites/{version}/")}
                self.assertIn(f"sites/{version}/index.html", files)
                finding_path = f"sites/{version}/findings/synthetic-policy.html"
                if operation == "withdraw":
                    self.assertNotIn(finding_path, files)
                    self.assertEqual(inputs["manifest"]["findings"], [])
                else:
                    self.assertIn(finding_path, files)
                    self.assertIn("Synthetic policy produced", files[finding_path])
                    if operation == "correct":
                        self.assertIn("Corrected.", files[finding_path])
                self.assertNotIn("Internal synthetic", "\n".join(files.values()))
                repeated = self.bridge.fixture("rebuild")
                self.assertEqual(repeated["job_id"], leased["job_id"])
                self.assertFalse(repeated["job_created"])
                self.assertFalse(repeated["action_created"])
                self.assert_unpublished(index)
        self.assertEqual(len(set(versions)), 3)

    def test_retry_reuses_same_job_version_files_and_approval_card(self):
        self.bridge.fixture("publish")
        leased = self.lease_build()
        # One file really reaches R2 before a transport failure. The normal runner
        # posts failure, D1 requeues, and the next attempt overwrites the same keys.
        self.bridge.fail_put_at = 2
        failed = self.consume(leased)
        self.assertEqual(failed.status, "failed")
        self.assertIn("503", failed.error)
        partial = self.assert_unpublished(1)
        self.assertEqual(len(partial["files"]), 1)
        self.assertEqual(next(job for job in partial["jobs"] if job["job_id"] == leased["job_id"])["state"], "queued")
        retried = self.lease_build()
        self.assertEqual(retried["job_id"], leased["job_id"])
        self.assertEqual(retried["inputs"], leased["inputs"])
        self.assertEqual(retried["attempt"], 2)
        result = self.consume(retried)
        self.assertEqual(result.status, "done", result.error)
        snapshot = self.assert_unpublished(1)
        self.assertEqual(len(snapshot["files"]), len(result.outputs["manifest"]))
        self.assertTrue(all(item["key"].startswith("sites/" + leased["inputs"]["site_version"] + "/") for item in snapshot["files"]))
        with self.assertRaises(WorkspaceError) as duplicate:
            self.workspace.post_result(leased["job_id"], "done", result.outputs)
        self.assertEqual(duplicate.exception.status, 409)

    def test_malformed_or_version_mismatched_worker_payload_never_stages(self):
        self.bridge.fixture("publish")
        leased = self.lease_build()
        mutations = [
            lambda value: value.update(schema_version=99),
            lambda value: value["manifest"].update(schema_version=99),
            lambda value: value.update(site_version="m" + "0" * 16),
            lambda value: value["manifest"]["campaign"].update(name="Changed after hashing"),
            lambda value: value.update(version="legacy", campaign={}, content={}),
        ]
        for mutate in mutations:
            bad = copy.deepcopy(leased)
            mutate(bad["inputs"])
            ctx = context(self.tmp.name, bad, self.workspace)
            try:
                result = build_site.run(ctx)
                self.assertEqual(result.status, "failed")
            finally:
                ctx.cleanup()
        self.assertEqual(self.assert_unpublished(1)["files"], [])

    def test_unsupported_map_leaves_tracked_publish_and_withdrawal_failures(self):
        self.bridge.fixture("unsupported-map")
        published = self.bridge.fixture("publish")
        snapshot = self.assert_unpublished(1)
        self.assertEqual(snapshot["finding"]["state"], "published")
        queued = next(job for job in snapshot["jobs"] if job["job_id"] == published["build_job_id"])
        self.assertEqual(queued["state"], "queued")
        # Decimal map coordinates cannot cross the contract's canonical numeric
        # domain. Keep a tracked failed job, including each bounded retry; a
        # producer exception after publishing would instead lose the build job.
        for attempt in range(1, queued["max_attempts"] + 1):
            leased = self.lease_build()
            self.assertEqual(leased["job_id"], published["build_job_id"])
            self.assertEqual(leased["attempt"], attempt)
            self.assertEqual(leased["inputs"]["manifest"]["map"]["center"], [-120.75, 35.25])
            self.assertEqual(len(leased["inputs"]["manifest"]["findings"]), 1)
            result = self.consume(leased)
            self.assertEqual(result.status, "failed")
        before = self.assert_unpublished(1)
        self.assertEqual(before["files"], [])
        self.assertEqual(next(job for job in before["jobs"] if job["job_id"] == published["build_job_id"])["state"], "failed")

        withdrawn = self.bridge.fixture("withdraw")
        self.assertNotEqual(withdrawn["build_job_id"], published["build_job_id"])
        snapshot = self.assert_unpublished(2)
        self.assertEqual(snapshot["finding"]["state"], "withdrawn")
        queued = next(job for job in snapshot["jobs"] if job["job_id"] == withdrawn["build_job_id"])
        self.assertEqual(queued["state"], "queued")
        leased = self.lease_build()
        self.assertEqual(leased["job_id"], withdrawn["build_job_id"])
        self.assertEqual(leased["inputs"]["manifest"]["findings"], [])
        result = self.consume(leased)
        self.assertEqual(result.status, "failed")
        final = self.assert_unpublished(2)
        self.assertEqual(final["files"], [])
        removal = next(job for job in final["jobs"] if job["job_id"] == withdrawn["build_job_id"])
        self.assertEqual(removal["state"], "queued")
        self.assertEqual(removal["attempt"], 1)
        self.assertTrue(removal["error"])

    def test_runner_bearer_is_required_before_queue_or_staging(self):
        self.bridge.fixture("publish")
        unauthorized = Workspace("http://localhost", "incorrect-synthetic-token", opener=self.bridge, max_retries=0)
        for operation in (unauthorized.lease_job, lambda: unauthorized.put_site_file("synthetic", "index.html", b"synthetic", "text/html")):
            with self.assertRaises(WorkspaceError) as caught:
                operation()
            self.assertEqual(caught.exception.status, 401)
        snapshot = self.assert_unpublished(1)
        self.assertEqual(snapshot["files"], [])
        self.assertTrue(all(job["attempt"] == 0 for job in snapshot["jobs"]))


if __name__ == "__main__":
    unittest.main()
