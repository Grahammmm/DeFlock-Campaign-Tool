"""ADM-CFG-1: the tracked workspace deploy template has placeholders only and the safe shape.

Offline: reads files and asks git check-ignore; no network, no wrangler, no account.
"""
import json
import re
import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = REPO_ROOT / "workers" / "workspace"
TEMPLATE = WORKSPACE / "wrangler.deploy.example.jsonc"
REAL = "workers/workspace/wrangler.deploy.jsonc"


def strip_jsonc(text):
    """Remove // and /* */ comments outside strings and trailing commas."""
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            j = i + 1
            while text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            out.append(text[i:j + 1])
            i = j + 1
        elif text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
        elif text.startswith("/*", i):
            i = text.index("*/", i) + 2
        else:
            out.append(c)
            i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


class DeployTemplate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = TEMPLATE.read_text(encoding="utf-8")
        cls.cfg = json.loads(strip_jsonc(cls.raw))

    def test_only_placeholder_ids(self):
        d1 = self.cfg["d1_databases"]
        kv = self.cfg["kv_namespaces"]
        self.assertEqual(d1[0]["database_id"], "<D1_DATABASE_ID>")
        self.assertEqual(kv[0]["id"], "<KV_NAMESPACE_ID>")
        self.assertNotIn("account_id", self.cfg)
        ids = re.findall(r"\b[0-9a-f]{32}\b|\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", self.raw)
        self.assertEqual(ids, [])
        self.assertNotIn('"local"', self.raw)

    def test_bindings_exactly_db_and_cache(self):
        self.assertEqual([b["binding"] for b in self.cfg["d1_databases"]], ["DB"])
        self.assertEqual([b["binding"] for b in self.cfg["kv_namespaces"]], ["CACHE"])
        allowed = {"name", "main", "compatibility_date", "compatibility_flags", "workers_dev", "preview_urls",
                   "observability", "d1_databases", "kv_namespaces", "triggers"}
        self.assertEqual(set(self.cfg) - allowed, set())
        for absent in ("r2_buckets", "queues", "vars", "services", "durable_objects", "send_email"):
            self.assertNotIn(absent, self.cfg)

    def test_pinned_flags(self):
        self.assertEqual(self.cfg["triggers"]["crons"], [])
        self.assertIs(self.cfg["workers_dev"], True)
        self.assertIs(self.cfg["preview_urls"], False)
        self.assertIs(self.cfg["observability"]["logs"]["invocation_logs"], False)

    def test_matches_dev_config_identity(self):
        dev = json.loads(strip_jsonc((WORKSPACE / "wrangler.jsonc").read_text(encoding="utf-8")))
        for key in ("name", "main", "compatibility_date", "compatibility_flags"):
            self.assertEqual(self.cfg[key], dev[key], key)
        self.assertEqual(self.cfg["d1_databases"][0]["database_name"], dev["d1_databases"][0]["database_name"])
        self.assertEqual(self.cfg["d1_databases"][0]["migrations_dir"], dev["d1_databases"][0]["migrations_dir"])

    def test_real_deploy_config_is_git_ignored(self):
        r = subprocess.run(["git", "check-ignore", "-q", REAL], cwd=REPO_ROOT)
        self.assertEqual(r.returncode, 0, REAL + " must be git-ignored")
        r = subprocess.run(["git", "check-ignore", "-q", REAL.replace("wrangler.deploy.jsonc", "wrangler.deploy.example.jsonc")],
                           cwd=REPO_ROOT)
        self.assertEqual(r.returncode, 1, "the template must stay tracked")

    def test_doc_names_template_and_dash_c_rule(self):
        doc = (REPO_ROOT / "docs" / "ADMIN-DEPLOY.md").read_text(encoding="utf-8")
        self.assertIn("wrangler.deploy.example.jsonc", doc)
        self.assertIn("-c wrangler.deploy.jsonc", doc)


if __name__ == "__main__":
    unittest.main()
