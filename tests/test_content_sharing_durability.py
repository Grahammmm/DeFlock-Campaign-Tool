"""RF0540-4: ``content agency-sharing`` flushes the admitted content directory after replacing agencies.json.

Synthetic fictional campaigns only. Wrappers forward the real ``fsync``/``replace``/``open``/``close`` calls and classify
descriptors with the real ``fstat``; a directory-only EIO is injected after a real replacement. No network or service.
"""
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

from campaign_tool import cli
from campaign_tool.content import ContentError, load_agencies
from campaign_tool.content_sharing import SharingImportError, import_sharing

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
TODAY = date(2026, 11, 1)
AGENCY = "ca-cedar-police"
URL = "https://transparency.flocksafety.com/cedar-police"
CHECKED = "2026-10-31"
FIRST = "name,state,kind\nCedar Hills PD,CA,local\nReno Test PD,NV,local\nFictional Federal Office,US,federal\n"
SECOND = "\ufeffname,state,kind\nZürich Test PD,NV,local\nAlpha Test PD,CA,state\n"
SECRET = "sk-synthetic-canary-0123"
MESSAGE = ("content/agencies.json: the new sharing data was written, but content/ could not be flushed to disk; "
           "crash durability is unconfirmed. Inspect the file before retrying.")


class Probe:
    """Records the operation order and every descriptor, forwarding to the real calls."""

    def __init__(self, content_dir, fail_dir=None, fail_file=None, fail_replace=None, fail_write=None):
        self.content_id = (os.stat(content_dir).st_dev, os.stat(content_dir).st_ino)
        self.events, self.opened, self.content_opens, self.dir_fsync_fds, self.content_fd = [], set(), [], [], None
        self.fail_dir, self.fail_file, self.fail_replace, self.fail_write = fail_dir, fail_file, fail_replace, fail_write
        self.real_open, self.real_close, self.real_fsync = os.open, os.close, os.fsync
        self.real_replace, self.real_write = os.replace, os.write

    def open(self, path, *args, **kwargs):
        fd = self.real_open(path, *args, **kwargs)
        self.opened.add(fd)
        if path == "content" and kwargs.get("dir_fd") is not None:
            self.content_opens.append(fd)
        elif kwargs.get("dir_fd") is None and Path(str(path)).name == "content":
            self.content_opens.append(fd)
        return fd

    def close(self, fd):
        self.opened.discard(fd)
        self.real_close(fd)

    def fsync(self, fd):
        info = os.fstat(fd)
        if stat.S_ISDIR(info.st_mode) and (info.st_dev, info.st_ino) == self.content_id:
            self.events.append("fsync:content-dir")
            self.dir_fsync_fds.append(fd)
            if self.fail_dir:
                raise OSError(5, f"Input/output error {self.fail_dir}")
            return self.real_fsync(fd)
        self.events.append("fsync:file" if stat.S_ISREG(info.st_mode) else "fsync:other")
        if self.fail_file:
            raise OSError(5, "Input/output error")
        return self.real_fsync(fd)

    def replace(self, *args, **kwargs):
        if self.fail_replace:
            self.events.append("replace:failed")
            raise OSError(28, "No space left on device")
        self.real_replace(*args, **kwargs)
        self.events.append("replace:completed")

    def write(self, fd, data):
        if self.fail_write:
            raise OSError(28, "No space left on device")
        return self.real_write(fd, data)

    @contextlib.contextmanager
    def active(self):
        with mock.patch.object(os, "open", self.open), mock.patch.object(os, "close", self.close), \
                mock.patch.object(os, "fsync", self.fsync), mock.patch.object(os, "replace", self.replace), \
                mock.patch.object(os, "write", self.write):
            yield self


class Base(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-sharing-durability-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.a, self.b = self.tmp / "campaign-a", self.tmp / "campaign-b"
        for root in (self.a, self.b):
            shutil.copytree(EXAMPLE, root, ignore=shutil.ignore_patterns("public"))
            path = root / "content" / "agencies.json"
            path.write_text(json.dumps(json.loads(path.read_text()), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        self.target = self.a / "content" / "agencies.json"
        self.csv = self.tmp / "sharing.csv"
        self.csv.write_text(FIRST, encoding="utf-8", newline="")
        self.outside = self.tmp / "outside.txt"
        self.outside.write_text("sentinel")

    def probe(self, **kwargs):
        return Probe(self.a / "content", **kwargs)

    def run_import(self, **kwargs):
        arguments = dict(agency_id=AGENCY, csv_path=self.csv, checked_on=CHECKED, source_url=URL, today=TODAY)
        arguments.update(kwargs)
        return import_sharing(self.a, **arguments)

    def run_cli(self, *extra, csv=None, checked_on=CHECKED, url=URL):
        argv = ["campaign_tool", "content", "agency-sharing", "--directory", str(self.a), "--agency", AGENCY,
                "--csv", str(csv or self.csv), "--checked-on", checked_on, "--source-url", url, *extra]
        out, err, code = io.StringIO(), io.StringIO(), 0
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                cli.main()
            except SystemExit as exit_:
                code = exit_.code or 0
        return code, out.getvalue(), err.getvalue()

    def expected_bytes(self, recipients):
        document = json.loads(self.target.read_bytes())
        for agency in document:
            if agency.get("agency_id") == AGENCY:
                agency.setdefault("profile", {})["sharing"] = {"checked_on": CHECKED, "source_url": URL, "recipients": recipients}
        return (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode("utf-8")

    def recipients(self):
        document = json.loads(self.target.read_bytes())
        return [a for a in document if a["agency_id"] == AGENCY][0].get("profile", {}).get("sharing", {}).get("recipients")

    def temps(self):
        return sorted(p.name for p in (self.a / "content").iterdir() if p.name.endswith(".tmp"))

    def seed_existing_list(self):
        self.run_import()
        self.csv.write_text(SECOND, encoding="utf-8", newline="")


FIRST_RECIPIENTS = [{"name": "Cedar Hills PD", "state": "CA", "kind": "local"}, {"name": "Fictional Federal Office", "state": "US", "kind": "federal"},
                    {"name": "Reno Test PD", "state": "NV", "kind": "local"}]
SECOND_RECIPIENTS = [{"name": "Alpha Test PD", "state": "CA", "kind": "state"}, {"name": "Zürich Test PD", "state": "NV", "kind": "local"}]


class FlushAfterReplaceTests(Base):
    def test_initial_and_replace_flush_admitted_content_after_real_replace(self):
        for via_cli in (False, True):
            with self.subTest(via_cli=via_cli, phase="initial"):
                shutil.copy(EXAMPLE / "content" / "agencies.json", self.target)
                self.target.write_text(json.dumps(json.loads(self.target.read_text()), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                self.csv.write_text(FIRST, encoding="utf-8", newline="")
                want = self.expected_bytes(sorted(FIRST_RECIPIENTS, key=lambda r: (r["state"], r["name"])))
                with self.probe().active() as probe:
                    if via_cli:
                        code, out, err = self.run_cli()
                        self.assertEqual((code, err), (0, ""))
                        self.assertTrue(out.endswith("Recorded only. Nothing was fetched or published.\n"))
                        self.assertEqual(json.loads(out.rsplit("Recorded only.", 1)[0]),
                                         {"agency_id": AGENCY, "recipients": 3, "checked_on": CHECKED, "source_url": URL})
                    else:
                        self.assertEqual(self.run_import(), {"agency_id": AGENCY, "recipients": 3, "checked_on": CHECKED, "source_url": URL})
                self.assertEqual(probe.events, ["fsync:file", "replace:completed", "fsync:content-dir"])
                self.assertEqual(len(probe.content_opens), 1)
                self.assertEqual(probe.dir_fsync_fds, probe.content_opens)  # the admitted descriptor, not a reopened directory
                self.assertEqual(self.target.read_bytes(), want)
                self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o600)
                self.assertEqual(probe.opened, set())
            with self.subTest(via_cli=via_cli, phase="replace"):
                self.csv.write_text(SECOND, encoding="utf-8", newline="")
                want = self.expected_bytes(SECOND_RECIPIENTS)
                with self.probe().active() as probe:
                    if via_cli:
                        code, out, err = self.run_cli("--replace")
                        self.assertEqual((code, err), (0, ""))
                    else:
                        self.assertEqual(self.run_import(replace=True)["recipients"], 2)
                self.assertEqual(probe.events, ["fsync:file", "replace:completed", "fsync:content-dir"])
                self.assertEqual(self.target.read_bytes(), want)
                self.assertEqual(self.recipients(), SECOND_RECIPIENTS)
                self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o600)

    def test_directory_only_flush_failure_keeps_complete_new_data(self):
        for phase in ("initial", "replace"):
            for via_cli in (False, True):
                with self.subTest(phase=phase, via_cli=via_cli):
                    shutil.copy(EXAMPLE / "content" / "agencies.json", self.target)
                    self.target.write_text(json.dumps(json.loads(self.target.read_text()), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                    self.csv.write_text(FIRST, encoding="utf-8", newline="")
                    if phase == "replace":
                        self.seed_existing_list()
                    want = self.expected_bytes(SECOND_RECIPIENTS if phase == "replace" else sorted(FIRST_RECIPIENTS, key=lambda r: (r["state"], r["name"])))
                    other = (self.b / "content" / "agencies.json").read_bytes()
                    with self.probe(fail_dir=f"{SECRET} {self.tmp}").active() as probe:
                        if via_cli:
                            code, out, err = self.run_cli(*(["--replace"] if phase == "replace" else []))
                        else:
                            with self.assertRaises(SharingImportError) as caught:
                                self.run_import(replace=phase == "replace")
                            self.assertEqual(str(caught.exception), MESSAGE)
                            self.assertIsNone(caught.exception.__cause__)
                    self.assertEqual(probe.events[-2:], ["replace:completed", "fsync:content-dir"])
                    self.assertEqual(self.target.read_bytes(), want)  # complete new document, no restoration of the old list
                    self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o600)
                    load_agencies(json.loads(self.target.read_text(encoding="utf-8")), "content/agencies.json", TODAY)  # the content reader accepts it
                    self.assertEqual((self.b / "content" / "agencies.json").read_bytes(), other)
                    self.assertEqual(self.outside.read_text(), "sentinel")
                    self.assertEqual(self.temps(), [])
                    self.assertEqual(probe.opened, set())
                    if via_cli:
                        self.assertEqual((code, out), (1, ""))
                        self.assertEqual(err, "Stopped: " + MESSAGE + "\n")

    def test_postcommit_message_does_not_repeat_input_or_os_text(self):
        self.csv.write_text(FIRST.replace("Cedar Hills PD", "Recipient Canary PD"), encoding="utf-8", newline="")
        with self.probe(fail_dir=f"{SECRET} {self.tmp}").active():
            code, out, err = self.run_cli()
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(len(err.strip().splitlines()), 1)
        for leaked in (SECRET, str(self.tmp), "Input/output error", "Errno", URL, "Recipient Canary", "Traceback", "Fix:", "Recorded"):
            self.assertNotIn(leaked, err)
        self.assertNotIn("nothing was written", err)
        self.assertNotIn("nothing was recorded", err)


class PreCommitTests(Base):
    def test_precommit_file_flush_write_replace_and_identity_refusals_keep_original(self):
        os.chmod(self.target, 0o640)
        bystander = self.a / "content" / ".agencies.json.bystander.tmp"
        bystander.write_text("not ours")
        before = (self.target.read_bytes(), self.target.stat().st_ino, stat.S_IMODE(self.target.stat().st_mode), self.target.stat().st_mtime_ns)
        for name in ("fail_write", "fail_file", "fail_replace"):
            with self.subTest(name):
                with self.probe(**{name: True}).active() as probe:
                    with self.assertRaises(OSError):
                        self.run_import()
                self.assertNotIn("fsync:content-dir", probe.events)
                self.assertNotIn("replace:completed", probe.events)
                self.assertEqual((self.target.read_bytes(), self.target.stat().st_ino, stat.S_IMODE(self.target.stat().st_mode),
                                  self.target.stat().st_mtime_ns), before)
                self.assertEqual(self.temps(), [bystander.name])
                self.assertEqual(bystander.read_text(), "not ours")
                self.assertEqual(probe.opened, set())
        with self.subTest("target swapped before install"):
            replacement = b'[{"swapped": true}]\n'

            def swap():
                (self.a / "content" / "swap.tmp").write_bytes(replacement)
                os.replace(self.a / "content" / "swap.tmp", self.target)
            with self.probe().active() as probe, mock.patch("campaign_tool.content_sharing._before_install", swap):
                with self.assertRaises(SharingImportError) as caught:
                    self.run_import()
            self.assertIn("changed while the import was running", str(caught.exception))
            self.assertEqual(self.target.read_bytes(), replacement)
            self.assertNotIn("fsync:content-dir", probe.events)
            self.assertEqual(self.temps(), [bystander.name])
            self.assertEqual(probe.opened, set())

    def test_pre_staging_refusals_and_explicit_replacement_do_not_flush(self):
        self.seed_existing_list()
        before = self.target.read_bytes()
        bad_csv = self.tmp / "bad.csv"
        bad_csv.write_text("name,state,kind\nBad PD,ZZ,local\n", encoding="utf-8")
        for label, kwargs in (("no --replace", {}),
                              ("invalid csv", {"csv_path": bad_csv, "replace": True}),
                              ("invalid date", {"checked_on": "2026-02-30", "replace": True}),
                              ("future date", {"checked_on": "2026-11-02", "replace": True}),
                              ("invalid source", {"source_url": "https://example.com/x", "replace": True}),
                              ("unknown agency", {"agency_id": "no-such-agency", "replace": True})):
            with self.subTest(label):
                with self.probe().active() as probe:
                    with self.assertRaises(ContentError):
                        self.run_import(**kwargs)
                self.assertNotIn("fsync:content-dir", probe.events)
                self.assertNotIn("replace:completed", probe.events)
                self.assertEqual(self.target.read_bytes(), before)
                self.assertEqual(probe.opened, set())

    def test_oversize_encoding_and_decoder_refusals_do_not_flush(self):
        self.target.write_bytes(b"[" * 200000)
        for label, setup in (("oversize", None), ("encoding", lambda: self.target.write_bytes(b"\xff\xfe[]"))):
            with self.subTest(label):
                if setup:
                    setup()
                before = self.target.read_bytes()
                with self.probe().active() as probe:
                    with self.assertRaises(SharingImportError):
                        self.run_import()
                self.assertEqual(probe.events, [])
                self.assertEqual(self.target.read_bytes(), before)
                self.assertEqual(probe.opened, set())


class OrdinaryContractTests(Base):
    def test_ordinary_unicode_bom_and_other_campaign_contracts_stay_unchanged(self):
        other = (self.b / "content" / "agencies.json").read_bytes()
        self.csv.write_text(SECOND, encoding="utf-8", newline="")
        want = self.expected_bytes(SECOND_RECIPIENTS)
        self.assertEqual(self.run_import()["recipients"], 2)
        self.assertEqual(self.target.read_bytes(), want)
        self.assertIn("Zürich Test PD".encode("utf-8"), self.target.read_bytes())
        self.assertTrue(self.target.read_bytes().endswith(b"\n"))
        self.assertEqual(self.temps(), [])
        self.assertEqual((self.b / "content" / "agencies.json").read_bytes(), other)


class DocumentationTests(unittest.TestCase):
    def test_docs_distinguish_before_install_from_installed_unconfirmed(self):
        text = (REPO_ROOT / "docs" / "SITE-CONTENT.md").read_text(encoding="utf-8")
        start = text.index("Record it with `python3 -m campaign_tool content agency-sharing")
        paragraph = " ".join(text[start:text.index("Recipients are stored sorted", start)].split())
        self.assertNotIn("on any error nothing is written", paragraph)
        self.assertIn("before the file is installed", paragraph)
        self.assertIn("previous file is kept", paragraph)
        self.assertIn("flush fails", paragraph)
        self.assertIn("new sharing data was written but crash durability is unconfirmed", paragraph)
        self.assertIn("complete new file stays installed", paragraph)
        self.assertIn("inspect the file before retrying", paragraph)
        self.assertIn("not a promise", paragraph)


if __name__ == "__main__":
    unittest.main()
