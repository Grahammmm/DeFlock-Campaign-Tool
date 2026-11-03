"""Runner handlers for newsletter_draft and backup (BF-df-runner-handlers). Synthetic campaign, offline, nothing sent."""
import ast
import contextlib
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import backup, cli  # noqa: E402
from campaign_tool.newsletter import build_draft, write_draft  # noqa: E402
from campaign_tool.newsletter.from_content import manifest_from_content  # noqa: E402
from campaign_tool.newsletter.sent import record_sent  # noqa: E402
from runner.client import FakeWorkspace  # noqa: E402
from runner.handlers import backup as backup_handler, newsletter_draft  # noqa: E402
from runner.loop import Runner, default_handlers  # noqa: E402
from tests.runner.helpers import Silent, context, job, settings  # noqa: E402

SECRETS = ("planted.person@example.invalid", "555-867-5309", "7ABC123", "SECRET-PLANTED-TEXT")
SITE = {"schema_version": 1, "tagline": "Synthetic records.", "about_md": "Synthetic campaign.",
        "base_url": "https://synthetic.example.invalid"}
BODY = "A synthetic record about cedar-policy."


def finding(slug, published_at):
    return {"id": "fnd_" + slug.replace("-", "_"), "title": "Synthetic finding " + slug, "slug": slug,
            "summary": BODY if slug == "cedar-policy" else "A synthetic record about " + slug + ".",
            "classification": "documented_fact", "confidence": "verified", "event_date": "2026-01-15",
            "sources": [{"sha256": "ab" * 32, "locator": "page:1", "title": "Synthetic record"}],
            "published_at": published_at, "corrections": [], "state": "published", "author": "synthetic-author",
            "limitations": ["Synthetic fixture."], "counterevidence": []}


def tree(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(Path(root).rglob("*")) if p.is_file()}


class Base(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-runner-handlers-")
        self.addCleanup(temporary.cleanup)
        self.tmp = temporary.name
        self.root = Path(self.tmp) / "campaign"
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", ["campaign_tool", "init", "--directory", str(self.root), "--name", "Synthetic",
                                              "--county", "Synthetic", "--state", "CA"]), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            cli.main()
        self.assertTrue((self.root / "campaign.json").is_file(), err.getvalue())
        (self.root / "kit").mkdir(exist_ok=True)
        (self.root / "kit" / "notes.txt").write_text(" ".join(SECRETS))
        self.ws, self.log, self.settings = FakeWorkspace(), Silent(), settings(self.tmp)
        patcher = mock.patch.dict(os.environ, {"CAMPAIGN_ROOT": str(self.root)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def content(self, findings=(finding("cedar-policy", "2026-02-01T18:00:00+00:00"),
                                finding("oak-retention", "2026-03-10T18:00:00+00:00"))):
        content = self.root / "content"
        (content / "findings").mkdir(parents=True, exist_ok=True)
        (content / "meetings").mkdir(exist_ok=True)
        (content / "site.json").write_text(json.dumps(SITE))
        for item in findings:
            (content / "findings" / (item["slug"] + ".json")).write_text(json.dumps(item))

    def step(self, kind, inputs=None):
        self.ws.enqueue(kind, inputs or {"n": len(self.ws.jobs)})
        status = Runner(self.ws, self.settings, handlers=default_handlers(), log=self.log, sleep=lambda s: None).step()
        return status, self.ws.results[-1]

    def direct(self, module, job_id="job_0000000000000009"):
        return module.run(context(self.tmp, job(module.__name__.rsplit(".", 1)[1], job_id=job_id)))

    def everything(self, result, extra=""):
        return json.dumps([result.outputs, result.receipt, result.error]) + "\n".join(self.log.lines) + extra

    def assert_clean(self, text):
        for secret in SECRETS + (BODY, "Synthetic finding cedar-policy"):
            self.assertNotIn(secret, text)


class NewsletterDraftTests(Base):
    def test_happy_path_writes_draft_and_touches_nothing_else(self):
        self.content()
        write_draft(self.root, build_draft(manifest_from_content(self.root, {"name": "Synthetic", "county": "Synthetic"})))
        record_sent(self.root, "2026-01-31", today=date(2026, 3, 1))
        before = tree(self.root)
        status, result = self.step("newsletter_draft")
        self.assertEqual((status, result["status"]), ("done", "done"))
        outputs = result["outputs"]
        self.assertEqual(outputs["draft"], "artifacts/job_0000000000000001/kit/newsletter-draft.html")
        self.assertEqual(outputs["findings"], 2)
        self.assertNotIn("already", outputs)
        draft = Path(self.settings.workdir) / outputs["draft"]
        self.assertIn(BODY, draft.read_text())  # the artefact exists and holds the text the receipt does not
        self.assertEqual(outputs["chars"], len(json.loads(draft.with_suffix(".json").read_text())["text"]))
        self.assertEqual(tree(self.root), before)  # campaign tree and sent.py state byte-identical
        self.assertEqual(stat.S_IMODE(draft.stat().st_mode), 0o600)
        self.assertEqual(result["error"], "draft only; sending needs approval")

    def test_rerun_same_job_returns_existing_draft_unchanged(self):
        self.content()
        first = self.direct(newsletter_draft)
        path = Path(self.settings.workdir) / first.outputs["draft"]
        body = path.read_bytes()
        self.content([finding("cedar-policy", "2026-02-01T18:00:00+00:00")])  # content moved on; the draft must not
        again = self.direct(newsletter_draft)
        self.assertEqual((again.status, again.outputs["already"], again.outputs["draft"]), ("done", True, first.outputs["draft"]))
        self.assertEqual(path.read_bytes(), body)
        self.assertEqual(again.receipt["draft_sha256"], first.receipt["draft_sha256"])
        other = self.direct(newsletter_draft, job_id="job_000000000000000a")  # a different job id gets its own draft
        self.assertNotIn("already", other.outputs)

    def test_named_failures_not_generic(self):
        cases = []
        with mock.patch.dict(os.environ):
            os.environ.pop("CAMPAIGN_ROOT")
            cases.append((self.direct(newsletter_draft, "job_00000000000000a1"), "campaign_root_missing"))
        with mock.patch.dict(os.environ, {"CAMPAIGN_ROOT": str(Path(self.tmp) / "absent")}):
            cases.append((self.direct(newsletter_draft, "job_00000000000000a2"), "campaign_root_missing"))
        cases.append((self.direct(newsletter_draft, "job_00000000000000a3"), "campaign_unreadable"))  # no content/ yet
        self.content([])
        cases.append((self.direct(newsletter_draft, "job_00000000000000a4"), "no_published_findings"))
        self.content([dict(finding("cedar-policy", "2026-02-01T18:00:00+00:00"), summary="Call planted.person@example.invalid")])
        cases.append((self.direct(newsletter_draft, "job_00000000000000a5"), "draft_refused"))
        self.content()
        cases.append((self.direct(newsletter_draft, "../evil"), "bad_job_id"))
        (Path(self.settings.workdir) / "artifacts").write_text("not a directory")
        cases.append((self.direct(newsletter_draft, "job_00000000000000a6"), "write_refused"))
        for result, reason in cases:
            self.assertEqual((result.status, result.error, result.outputs, result.receipt), ("failed", reason, {"reason": reason}, None))
        self.assertFalse((Path(self.tmp) / "evil").exists())

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores file modes")
    def test_unreadable_root_is_named(self):
        self.content()
        os.chmod(self.root / "campaign.json", 0)
        self.addCleanup(os.chmod, self.root / "campaign.json", 0o600)
        self.assertEqual(self.direct(newsletter_draft).error, "campaign_unreadable")

    def test_planted_text_never_reaches_receipt_result_or_logs(self):
        self.content()
        status, result = self.step("newsletter_draft")
        self.assertEqual(status, "done")
        self.assert_clean(json.dumps(result) + "\n".join(self.log.lines))

    def test_handler_exception_message_has_no_record_text(self):
        self.content()
        with mock.patch.object(newsletter_draft, "build_draft", side_effect=RuntimeError("SECRET-PLANTED-TEXT 7ABC123")):
            status, result = self.step("newsletter_draft")
        self.assertEqual((status, result["error"]), ("failed", "RuntimeError: handler failed"))
        self.assert_clean(json.dumps(result) + "\n".join(self.log.lines))


class BackupTests(Base):
    def test_happy_path_archive_private_and_manifest_hash_verifies(self):
        self.content()
        before = tree(self.root)
        status, result = self.step("backup")
        self.assertEqual((status, result["status"]), ("done", "done"))
        outputs = result["outputs"]
        archive = Path(self.settings.workdir) / outputs["archive"]
        self.assertRegex(outputs["archive"], r"^artifacts/job_0000000000000001/deflock-backup-\d{8}T\d{6}Z\.tar$")
        self.assertEqual(stat.S_IMODE(archive.stat().st_mode), 0o600)
        self.assertEqual(outputs["bytes"], archive.stat().st_size)
        self.assertTrue(backup.verify(archive, expected_manifest_sha256=outputs["manifest_sha256"])["ok"])
        self.assertEqual(result["outputs"]["receipt"]["manifest_sha256"], outputs["manifest_sha256"])
        self.assertEqual(tree(self.root), before)  # the campaign directory is only read

    def test_rerun_onto_existing_archive_is_named_failure_and_archive_untouched(self):
        self.content()
        first = self.direct(backup_handler)
        archive = Path(self.settings.workdir) / first.outputs["archive"]
        before = archive.read_bytes()
        again = self.direct(backup_handler)
        self.assertEqual((again.status, again.error, again.receipt), ("failed", "archive_exists", None))
        self.assertEqual(archive.read_bytes(), before)
        # a same-name collision inside the shipped exporter is named too, and the colliding file is kept
        out = Path(self.settings.workdir) / "artifacts" / "job_00000000000000b1"
        out.mkdir(parents=True, mode=0o700)
        (out / "collide.tar").write_bytes(b"keep me")
        with mock.patch.object(backup, "scheduled_name", return_value="collide.tar"):
            collided = self.direct(backup_handler, "job_00000000000000b1")
        self.assertEqual(collided.error, "archive_exists")
        self.assertEqual((out / "collide.tar").read_bytes(), b"keep me")

    def test_named_failures_not_generic(self):
        cases = []
        with mock.patch.dict(os.environ):
            os.environ.pop("CAMPAIGN_ROOT")
            cases.append((self.direct(backup_handler, "job_00000000000000a1"), "campaign_root_missing"))
        empty = Path(self.tmp) / "empty"
        empty.mkdir()
        with mock.patch.dict(os.environ, {"CAMPAIGN_ROOT": str(empty)}):
            cases.append((self.direct(backup_handler, "job_00000000000000a2"), "campaign_root_missing"))
        cases.append((self.direct(backup_handler, "../evil"), "bad_job_id"))
        (self.root / "private").rename(self.root / "private.bak")
        (self.root / "private").symlink_to(empty)  # the shipped exporter refuses a symlinked private/
        cases.append((self.direct(backup_handler, "job_00000000000000a3"), "backup_refused"))
        (self.root / "private").unlink()
        (self.root / "private.bak").rename(self.root / "private")
        cfg = json.loads((self.root / "campaign.json").read_text())
        (self.root / "campaign.json").write_text(json.dumps(dict(cfg, backup={"encrypt_to": "missing.asc"})))
        cases.append((self.direct(backup_handler, "job_00000000000000a4"), "backup_config_invalid"))
        (self.root / "campaign.json").write_text(json.dumps(cfg))
        shutil.rmtree(Path(self.settings.workdir) / "artifacts")
        (Path(self.settings.workdir) / "artifacts").write_text("not a directory")
        cases.append((self.direct(backup_handler, "job_00000000000000a5"), "write_refused"))
        for result, reason in cases:
            self.assertEqual((result.status, result.error, result.outputs, result.receipt), ("failed", reason, {"reason": reason}, None))
        self.assertFalse((Path(self.tmp) / "evil").exists())

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores file modes")
    def test_unreadable_root_is_named(self):
        os.chmod(self.root, 0o311)  # searchable, not readable
        self.addCleanup(os.chmod, self.root, 0o700)
        self.assertEqual(self.direct(backup_handler).error, "campaign_root_unreadable")

    def test_planted_text_never_reaches_receipt_result_or_logs(self):
        status, result = self.step("backup")
        self.assertEqual(status, "done")
        self.assert_clean(json.dumps(result) + "\n".join(self.log.lines))

    def test_handler_exception_message_has_no_record_text(self):
        with mock.patch.object(backup_handler, "export_into", side_effect=RuntimeError("SECRET-PLANTED-TEXT 7ABC123")):
            status, result = self.step("backup")
        self.assertEqual((status, result["error"]), ("failed", "RuntimeError: handler failed"))
        self.assert_clean(json.dumps(result) + "\n".join(self.log.lines))


class RegistrationTests(Base):
    def test_registered_and_unknown_or_send_request_answers_unchanged(self):
        handlers = default_handlers()
        self.assertIs(handlers["newsletter_draft"], newsletter_draft.run)
        self.assertIs(handlers["backup"], backup_handler.run)
        status, result = self.step("frobnicate")
        self.assertEqual((status, result["error"]), ("blocked", "no handler for kind 'frobnicate' in this runner version"))
        status, result = self.step("send_request")
        self.assertEqual((status, result["error"]), ("blocked", "send_request is not a runner job; it runs in the workspace after approval"))
        status, result = self.step("backup")  # and the two kinds are no longer answered blocked
        self.assertNotEqual(status, "blocked")

    def test_handlers_import_no_network_or_upload_client_and_name_no_remote(self):
        banned = {"smtplib", "imaplib", "socket", "ssl", "http", "urllib", "requests", "boto3", "ftplib", "paramiko", "subprocess"}
        for module in (newsletter_draft, backup_handler):
            source = Path(module.__file__).read_text()
            for node in ast.walk(ast.parse(source)):
                names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""] if isinstance(node, ast.ImportFrom) and not node.level else []
                self.assertFalse({n.split(".")[0] for n in names} & banned, (module.__name__, names))
            for marker in ("http://", "https://", "s3://", "sftp", "rsync", "scp ", "smtp"):
                self.assertNotIn(marker, source.lower())


if __name__ == "__main__":
    unittest.main()
