"""kit-copy directory durability: the kit/ entry is synced before any replacement, the outgoing directory after each
one, and a failed post-replacement sync is reported as installed-but-unconfirmed. Synthetic fixtures only."""
import contextlib
import errno
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import cli, kit, kit_check, kit_copy  # noqa: E402

TODAY = date(2026, 3, 4)
SETTINGS = {"schema_version": 1, "requester": ["Pat Example", "1 Test Way", "Exampleville, CA 90001"],
            "fee_cap_usd": 50, "date_from": "2025-01-01"}
UNCONFIRMED = ("UNCONFIRMED: kit/outgoing/%s was replaced, but directory sync failed; "
               "rerun the same kit-copy command before using the output")
PARENT = ("kit/outgoing directory entry could not be synced; no copy file was replaced; directory may remain; "
          "rerun kit-copy")
REAL_FSYNC, REAL_REPLACE = os.fsync, os.replace


def run(*arguments):
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with mock.patch.object(sys, "argv", ["campaign_tool", *map(str, arguments)]), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            cli.main()
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


class DurabilityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-kit-durability-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        code, _, err = run("init", "--directory", self.root, "--name", "Synthetic", "--county", "San Luis Obispo",
                           "--state", "CA")
        self.assertEqual(code, 0, err)
        self.kit = self.root / "kit"
        self.kit.mkdir()
        (self.kit / "request-settings.json").write_text(json.dumps(SETTINGS), encoding="utf-8")
        code, _, err = run("kit", "--directory", self.root)
        self.assertEqual(code, 0, err)
        self.out = self.kit / "outgoing"
        self.a = sorted(p.stem for p in (self.kit / "requests").glob("*.md"))[0]
        draft = self.kit / "requests" / f"{self.a}.md"
        draft.write_text(kit_check.PLACEHOLDER.sub("Records Custodian", draft.read_text(encoding="utf-8")),
                         encoding="utf-8")
        kit.verify_agency(self.root, self.a, source_url="https://police.example/records", checked_on="2026-01-02",
                          mailing_address="Records Unit|123 Example Street|Exampleville, CA 90000", today=TODAY,
                          records_email="records@example.org")
        self.events = []

    def names(self, target):
        return {"portal": [f"{self.a}.txt"], "email": [f"{self.a}.eml"],
                "mail": [f"{self.a}.letter.html", f"{self.a}.letter.txt"]}[target]

    def copy(self, target):
        return kit_copy.copy(self.root, self.a, target, today=TODAY)

    @contextlib.contextmanager
    def hooks(self, fail_parent=None, fail_outgoing_on=None):
        """Record real fsync/replace order; fail kit/-directory fsync or the Nth outgoing-directory fsync."""
        kit_id = (self.kit.stat().st_dev, self.kit.stat().st_ino)
        seen = {"out": 0}

        def fsync(fd):
            info = os.fstat(fd)
            if stat.S_ISDIR(info.st_mode):
                if (info.st_dev, info.st_ino) == kit_id:
                    self.events.append("fsync:parent")
                    if fail_parent:
                        raise OSError(errno.EIO, "canary-strerror")
                else:
                    seen["out"] += 1
                    self.events.append("fsync:outgoing")
                    if seen["out"] == fail_outgoing_on:
                        raise OSError(errno.EIO, "canary-strerror")
                return REAL_FSYNC(fd)
            self.events.append("fsync:file")
            return REAL_FSYNC(fd)

        def replace(src, dst, **kwargs):
            self.events.append(f"replace:{dst}")
            return REAL_REPLACE(src, dst, **kwargs)

        with mock.patch("os.fsync", fsync), mock.patch("os.replace", replace):
            yield

    def leftovers(self):
        return [p.name for p in self.out.iterdir() if p.name.startswith(".")] if self.out.is_dir() else []

    def test_barrier_order_for_all_targets_and_directory_states(self):
        for target in ("portal", "email", "mail"):
            for existing in (False, True):
                with self.subTest(target=target, existing=existing):
                    if existing:
                        self.out.mkdir(exist_ok=True)
                    elif self.out.exists():
                        for p in self.out.iterdir():
                            p.unlink()
                        self.out.rmdir()
                    self.events.clear()
                    with self.hooks():
                        result = self.copy(target)
                    expected = ["fsync:parent"]
                    for name in self.names(target):
                        expected += ["fsync:file", f"replace:{name}", "fsync:outgoing"]
                    self.assertEqual(self.events, expected)
                    self.assertEqual(result["written"],
                                     [f"kit/outgoing/{n}" for n in self.names(target)] if target == "mail"
                                     else f"kit/outgoing/{self.names(target)[0]}")
                    self.assertEqual(self.leftovers(), [])

    def test_parent_barrier_failure_never_stages_or_replaces(self):
        for target in ("portal", "email", "mail"):
            for existing in (False, True):
                with self.subTest(target=target, existing=existing):
                    if existing:
                        self.out.mkdir(exist_ok=True)
                    self.events.clear()
                    with self.hooks(fail_parent=True), self.assertRaises(ValueError) as caught:
                        self.copy(target)
                    self.assertEqual(str(caught.exception), PARENT)
                    self.assertNotIn("canary", str(caught.exception))
                    self.assertFalse(any(e.startswith("replace") for e in self.events))
                    self.assertEqual(list(self.out.iterdir()) if self.out.exists() else [], [])
        with mock.patch("os.open", side_effect=PermissionError(errno.EACCES, "canary")) as opener, \
                self.assertRaises(ValueError) as caught:
            kit_copy._sync_parent(self.out)
        self.assertEqual(str(caught.exception), PARENT)
        self.assertEqual(opener.call_count, 1)

    def test_parent_failure_cli_is_exit_3_without_success(self):
        with self.hooks(fail_parent=True):
            code, out, err = run("kit-copy", "--directory", self.root, "--agency", self.a, "--for", "portal")
        self.assertEqual((code, out), (3, ""))
        self.assertEqual(err, f"Stopped: {PARENT}\n")

    def test_retry_after_uncertain_directory_creation_resyncs_parent(self):
        with self.hooks(fail_parent=True), self.assertRaises(ValueError):
            self.copy("portal")
        self.assertTrue(self.out.is_dir())  # created by the failed attempt, left in place
        self.events.clear()
        with self.hooks(fail_parent=True), self.assertRaises(ValueError) as caught:
            self.copy("portal")
        self.assertEqual(str(caught.exception), PARENT)
        self.assertIn("fsync:parent", self.events)
        self.events.clear()
        with self.hooks():
            self.copy("portal")
        self.assertEqual(self.events[0], "fsync:parent")
        self.assertTrue((self.out / self.names("portal")[0]).is_file())

    def test_postreplace_sync_failure_retains_installed_copy(self):
        self.out.mkdir()
        name = self.names("portal")[0]
        (self.out / name).write_bytes(b"old")
        (self.out / ".foreign.tmp").write_bytes(b"foreign")
        with self.hooks(fail_outgoing_on=1), mock.patch.object(kit_copy, "_discard") as discard, \
                self.assertRaises(kit_copy.CopyDurabilityError) as caught:
            self.copy("portal")
        self.assertEqual(str(caught.exception), UNCONFIRMED % name)
        self.assertNotIn("canary", str(caught.exception))
        discard.assert_not_called()
        self.assertNotEqual((self.out / name).read_bytes(), b"old")
        self.assertEqual((self.out / ".foreign.tmp").read_bytes(), b"foreign")
        self.assertEqual(self.leftovers(), [".foreign.tmp"])
        with self.hooks(fail_outgoing_on=1):
            code, out, err = run("kit-copy", "--directory", self.root, "--agency", self.a, "--for", "email")
        self.assertEqual((code, out), (3, ""))
        self.assertEqual(err, f"Stopped: {UNCONFIRMED % self.names('email')[0]}\n")
        self.assertNotIn("Nothing was sent", err)

    def test_outgoing_sync_uses_the_open_descriptor(self):
        opened = []
        real_open = os.open

        def tracking_open(path, flags, *args, **kwargs):
            fd = real_open(path, flags, *args, **kwargs)
            if flags & os.O_DIRECTORY:
                opened.append(fd)
            return fd

        synced = []

        def fsync(fd):
            synced.append(fd)
            return REAL_FSYNC(fd)

        with mock.patch("os.open", tracking_open), mock.patch("os.fsync", fsync):
            self.copy("portal")
        # parent open, then the outgoing descriptor opened by _write is synced after its file descriptor
        self.assertEqual(len(opened), 2)
        self.assertEqual(synced[0], opened[0])
        self.assertEqual(synced[-1], opened[1])

    def test_mail_uncertainty_names_current_and_previous_twin_truthfully(self):
        html_name, txt_name = self.names("mail")
        with self.hooks(fail_outgoing_on=1), self.assertRaises(kit_copy.CopyDurabilityError) as caught:
            self.copy("mail")
        self.assertEqual(str(caught.exception), UNCONFIRMED % html_name)
        self.assertTrue((self.out / html_name).is_file())
        self.assertFalse((self.out / txt_name).exists())
        self.assertNotIn("replace:" + txt_name, self.events)
        self.events.clear()
        (self.out / html_name).unlink()
        with self.hooks(fail_outgoing_on=2), self.assertRaises(kit_copy.CopyDurabilityError) as caught:
            self.copy("mail")
        self.assertEqual(str(caught.exception),
                         UNCONFIRMED % txt_name + f"; kit/outgoing/{html_name} was already replaced and synced")
        self.assertNotIn("could not be written", str(caught.exception))
        self.assertTrue((self.out / html_name).is_file() and (self.out / txt_name).is_file())

    def test_ordinary_precommit_failures_keep_their_old_messages(self):
        html_name, txt_name = self.names("mail")
        calls = []

        def second_file_fsync_fails(fd):
            if not stat.S_ISDIR(os.fstat(fd).st_mode):
                calls.append(fd)
                if len(calls) == 2:
                    raise OSError(errno.EIO, "disk")
            return REAL_FSYNC(fd)

        with mock.patch("os.fsync", second_file_fsync_fails), self.assertRaises(ValueError) as caught:
            self.copy("mail")
        self.assertNotIsInstance(caught.exception, kit_copy.CopyDurabilityError)
        self.assertEqual(str(caught.exception), f"kit/outgoing/{txt_name} could not be written (disk); "
                                                f"{html_name} was already replaced, run the same command again")
        self.assertEqual(self.leftovers(), [])

    def test_unrelated_errors_inside_write_are_not_relabelled(self):
        self.out.mkdir()
        with mock.patch("os.replace", side_effect=OSError(errno.EIO, "disk")), self.assertRaises(OSError) as caught:
            kit_copy._write(self.out, "x.txt", b"data")
        self.assertNotIsInstance(caught.exception, ValueError)
        self.assertEqual(self.leftovers(), [])

    def test_healthy_retry_after_failure_matches_a_clean_run(self):
        with self.hooks(fail_outgoing_on=1), self.assertRaises(kit_copy.CopyDurabilityError):
            self.copy("email")
        result = self.copy("email")
        installed = (self.out / self.names("email")[0]).read_bytes()
        self.assertEqual(result["written"], f"kit/outgoing/{self.names('email')[0]}")
        self.assertIn(b"X-Unsent: 1", installed)
        self.assertEqual(stat.S_IMODE((self.out / self.names("email")[0]).stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.out.stat().st_mode), 0o700)
        code, out, err = run("kit-copy", "--directory", self.root, "--agency", self.a, "--for", "email")
        self.assertEqual(code, 0, err)
        self.assertEqual(err, "Copy only. Nothing was sent.\n")
        self.assertEqual((self.out / self.names("email")[0]).read_bytes(), installed)


if __name__ == "__main__":
    unittest.main()
