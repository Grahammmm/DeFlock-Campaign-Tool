"""RF2235-1: agency-profile mutations flush the admitted content/ directory after replacing agencies.json, and a
post-replacement flush failure is reported as installed-but-unconfirmed. Synthetic agencies in temporary storage; the real
CLI, real fsync and real replace run, with selective failure injection. Nothing is fetched or sent.

Run from the repository root: python3 -B -m unittest tests.test_content_profile_durability -v
"""
import json
import os
import socket
import stat
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import content_profile  # noqa: E402
from campaign_tool.content import load_agencies  # noqa: E402
from tests.test_content_profile_documents import (A, B, CONTRACT_IDS, DEAL_A, DEAL_B, POLICY_IDS, TODAY, URL_A, URL_B,  # noqa: E402
                                                  agencies, ago, run)

REAL_FSYNC, REAL_REPLACE, REAL_OPEN, REAL_CLOSE, REAL_FSTAT = os.fsync, os.replace, os.open, os.close, os.fstat
FIXED = ("content/agencies.json: the new profile data was written, but content/ could not be flushed to disk; "
         "crash durability is unconfirmed. Inspect the file before retrying.")
NO_NETWORK = AssertionError("no network in these tests")
SENTINEL = "/private/sentinel-dir/SECRET-SENTINEL-7731"
FUNDING = {"kind": "grant", "name": "Fictional program", "as_stated_in": "Synthetic award",
           "source_url": "https://example.invalid/award", "checked_on": ago(5)}
REPORT = {"title": "Synthetic report", "kind": "annual_report", "period_start": "2025-01-01",
          "period_end": "2025-12-31", "source_url": "https://example.invalid/report", "checked_on": ago(5)}


def fixture():
    document = agencies()
    document[0]["profile"]["funding"] = [FUNDING]
    document[0]["profile"]["reports"] = [REPORT]
    return document


class Tripwire(Exception):
    pass


class DurabilityTests(unittest.TestCase):
    def setUp(self):
        for patch in (mock.patch.object(socket, "socket", side_effect=NO_NETWORK),
                      mock.patch.object(socket, "create_connection", side_effect=NO_NETWORK),
                      mock.patch.object(urllib.request, "urlopen", side_effect=NO_NETWORK)):
            patch.start()
            self.addCleanup(patch.stop)
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-profile-durability-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "campaign"
        (self.root / "content").mkdir(parents=True)
        self.target = self.root / "content" / "agencies.json"
        self.target.write_text(json.dumps(fixture(), ensure_ascii=False) + "\n", encoding="utf-8")
        (self.base / "sentinel.txt").write_text("outside", encoding="utf-8")
        self.patches = []
        self.addCleanup(self.reset)
        info = os.stat(self.root / "content")
        self.content_id = (info.st_dev, info.st_ino)

    def tree(self):
        """Name -> (bytes, mode) of every file under the campaign and of the outside sentinel."""
        files = {p.relative_to(self.base).as_posix(): (p.read_bytes(), stat.S_IMODE(p.stat().st_mode))
                 for p in self.base.rglob("*") if p.is_file()}
        return files

    def commands(self):
        """The nine mutating CLI invocations; each changes the stored file."""
        root = self.root
        base = ("content", "agency-profile")
        today = TODAY.isoformat()
        elements = lambda ids: [f"--element={key}=addressed" for key in ids]  # noqa: E731
        entry = lambda data, skip=(): [x for k, v in data.items() if k not in skip  # noqa: E731
                                       for x in ("--" + k.replace("_", "-"), v)]
        return {
            "recheck": (*base, "recheck", "--directory", root, "--agency", A, "--field", "policy_check",
                        "--checked-on", ago(10)),
            "policy": (*base, "policy", "--directory", root, "--agency", A, "--url", URL_B, "--source-url",
                       "https://example.invalid/alpha", "--checked-on", today, "--replace", "--drop-check"),
            "contract": (*base, "contract", "--directory", root, "--agency", A, "--vendor", "Synthetic vendor",
                         "--start", "2026-01-01", "--end", "2028-12-31", "--source-url", DEAL_B, "--checked-on", today,
                         "--replace", "--drop-check"),
            "policy-check": (*base, "policy-check", "--directory", root, "--agency", A, "--checked-on", ago(10),
                             "--replace", *elements(POLICY_IDS)),
            "contract-check": (*base, "contract-check", "--directory", root, "--agency", A, "--checked-on", ago(10),
                               "--replace", *elements(CONTRACT_IDS)),
            "funding-add": (*base, "funding", "add", "--directory", root, "--agency", A,
                            *entry(FUNDING | {"source_url": "https://example.invalid/award-2"})),
            "funding-remove": (*base, "funding", "remove", "--directory", root, "--agency", A, "--index", "1"),
            "report-add": (*base, "report", "add", "--directory", root, "--agency", A,
                           *entry(REPORT | {"source_url": "https://example.invalid/report-2"})),
            "report-remove": (*base, "report", "remove", "--directory", root, "--agency", A, "--index", "1"),
        }

    def instrument(self, fail_dir=None, fail_file=None, fail_replace=None):
        """Patch real fsync/replace/open/close with recorders; returns the event list.

        Each event is a tuple whose first item is the kind. ``fail_*`` is an exception raised in place of the real call
        (after the real replace for a directory failure, so the new file really is installed)."""
        events = []

        def fsync(fd):
            info = REAL_FSTAT(fd)
            directory = stat.S_ISDIR(info.st_mode)
            events.append(("fsync:dir" if directory else "fsync:file", (info.st_dev, info.st_ino),
                           sys.stdout.getvalue() == ""))
            if directory and fail_dir is not None:
                raise fail_dir
            if not directory and fail_file is not None:
                raise fail_file
            return REAL_FSYNC(fd)

        def replace(*args, **kwargs):
            if fail_replace is not None:
                events.append(("replace:failed",))
                raise fail_replace
            REAL_REPLACE(*args, **kwargs)
            events.append(("replace:completed",))

        def opener(*args, **kwargs):
            fd = REAL_OPEN(*args, **kwargs)
            events.append(("open", fd, str(args[0])))
            return fd

        def closer(fd):
            events.append(("close", fd))
            return REAL_CLOSE(fd)

        # The opener delegates dir_fd unchanged. Preserve that capability if a lazy CLI import
        # probes os.open while instrumentation is active (for example kit_assign via today).
        if REAL_OPEN in os.supports_dir_fd:
            patch = mock.patch.object(os, "supports_dir_fd", os.supports_dir_fd | {opener})
            patch.start()
            self.patches.append(patch)
        for name, wrapper in (("fsync", fsync), ("replace", replace), ("open", opener), ("close", closer)):
            patch = mock.patch.object(os, name, wrapper)
            patch.start()
            self.patches.append(patch)
        return events

    def reset(self):
        """A pristine campaign for the next subtest; stops the recorders of the previous one."""
        while self.patches:
            self.patches.pop().stop()
        self.target.write_text(json.dumps(fixture(), ensure_ascii=False) + "\n", encoding="utf-8")
        os.chmod(self.target, 0o644)
        for extra in (self.root / "content").iterdir():
            if extra != self.target:
                extra.unlink()

    def run_with(self, name, events_kwargs=None):
        events = self.instrument(**(events_kwargs or {}))
        return run(*self.commands()[name]), events

    def assert_new_file_valid(self):
        document = json.loads(self.target.read_text(encoding="utf-8"))
        load_agencies(document, "content/agencies.json", TODAY)
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o600)
        self.assertEqual(sorted(p.name for p in (self.root / "content").iterdir()), ["agencies.json"])
        return document

    def kinds(self, events):
        return [e[0] for e in events if e[0] in ("fsync:file", "fsync:dir", "replace:completed", "replace:failed")]

    def test_changed_commands_flush_admitted_directory_after_replace_before_success(self):
        for name in self.commands():
            with self.subTest(command=name):
                self.reset()
                before = self.target.read_bytes()
                (code, out, err), events = self.run_with(name)
                self.assertEqual((code, err), (0, ""), (out, err))
                self.assertNotEqual(self.target.read_bytes(), before)
                self.assertEqual(self.kinds(events), ["fsync:file", "replace:completed", "fsync:dir"])
                directory = [e for e in events if e[0] == "fsync:dir"][0]
                self.assertEqual(directory[1], self.content_id)  # the admitted content/ descriptor, not another
                self.assertTrue(directory[2], "directory flush ran after output had already been emitted")
                self.assertTrue(out.strip(), "success text expected")
                self.assertNotIn("could not be flushed", out)
                self.assert_new_file_valid()

    def test_directory_flush_failure_is_postcommit_not_rollback(self):
        for name in self.commands():
            with self.subTest(command=name):
                self.reset()
                before = self.tree()
                (code, out, err), events = self.run_with(name, {"fail_dir": OSError(5, f"EIO {SENTINEL}")})
                self.assertEqual(code, 1)
                self.assertEqual(out, "")
                self.assertIn(FIXED, err)
                self.assertEqual(self.kinds(events), ["fsync:file", "replace:completed", "fsync:dir"])
                after = self.tree()
                self.assertNotEqual(after["campaign/content/agencies.json"], before["campaign/content/agencies.json"])
                self.assertEqual(after["sentinel.txt"], before["sentinel.txt"])  # outside sentinel untouched
                document = self.assert_new_file_valid()
                profile = document[0]["profile"]
                if name == "policy":  # the deliberate reading removal stays installed
                    self.assertEqual(profile["alpr_policy"]["url"], URL_B)
                    self.assertNotIn("policy_check", profile)
                if name == "contract":
                    self.assertEqual(profile["contract"]["source_url"], DEAL_B)
                    self.assertNotIn("contract_check", profile)
                if name == "funding-remove":
                    self.assertNotIn("funding", profile)
                if name == "report-remove":
                    self.assertNotIn("reports", profile)
                if name == "funding-add":
                    self.assertEqual(len(profile["funding"]), 2)
                self.assertEqual(document[1], agencies()[1])  # an unrelated agency is unchanged

    def test_precommit_file_flush_and_replace_failures_preserve_original(self):
        cases = (("file fsync", {"fail_file": OSError(5, "injected file fsync")}, ["fsync:file"]),
                 ("replace", {"fail_replace": OSError(5, "injected replace")}, ["fsync:file", "replace:failed"]))
        for name in self.commands():
            for label, kwargs, expected in cases:
                with self.subTest(command=name, failure=label):
                    self.reset()
                    before = self.tree()
                    (code, out, err), events = self.run_with(name, kwargs)
                    self.assertNotEqual(code, 0)
                    self.assertEqual(out, "")
                    self.assertNotIn("was written, but", err)
                    self.assertNotIn("durability is unconfirmed", err)
                    self.assertEqual(self.kinds(events), expected)  # no directory flush after the failed phase
                    self.assertEqual(self.tree(), before)  # bytes and modes; no temporary file left

    def test_same_day_recheck_and_list_do_not_flush_or_replace(self):
        stored = ago(30)  # policy_check.checked_on in the fixture
        before = self.tree()
        events = self.instrument(fail_dir=Tripwire("no directory flush"), fail_file=Tripwire("no file flush"),
                                 fail_replace=Tripwire("no replace"))
        base = ("content", "agency-profile")
        variants = ((*base, "recheck", "--directory", self.root, "--agency", A, "--field", "policy_check",
                     "--checked-on", stored),
                    (*base, "list", "--directory", self.root), (*base, "list", "--directory", self.root, "--json"),
                    (*base, "list", "--directory", self.root, "--agency", A))
        for arguments in variants:
            with self.subTest(arguments=arguments[2]):
                code, out, err = run(*arguments)
                self.assertEqual((code, err), (0, ""))
                self.assertTrue(out.strip())
        self.assertEqual(self.kinds(events), [])
        self.assertEqual(self.tree(), before)

    def test_postcommit_diagnostic_withholds_os_and_input_text(self):
        arguments = list(self.commands()["policy"])
        arguments[arguments.index(URL_B)] = f"https://example.invalid/sentinel-url-{SENTINEL[-4:]}.pdf"
        events = self.instrument(fail_dir=OSError(5, f"EIO {SENTINEL}", SENTINEL))
        code, out, err = run(*arguments)
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn(FIXED, err)
        self.assertEqual(self.kinds(events)[-1], "fsync:dir")
        for text in (SENTINEL, "SECRET-SENTINEL", "sentinel-url", str(self.base), "Traceback", "OSError", "EIO"):
            self.assertNotIn(text, err)
        for text in ("nothing was written", "Recorded", "Rechecked", "Dropped", "Removed"):
            self.assertNotIn(text, err)
        # the same fixed diagnostic, untouched, for the entry commands that append "nothing was written" to refusals
        self.reset()
        events = self.instrument(fail_dir=OSError(5, SENTINEL))
        code, out, err = run(*self.commands()["funding-add"])
        self.assertEqual((code, out), (1, ""))
        self.assertIn(FIXED, err)
        self.assertNotIn("nothing was written", err)

    def test_directory_descriptor_is_not_reopened_and_all_owned_fds_close(self):
        scenarios = (("success", {}), ("precommit", {"fail_file": OSError(5, "injected")}),
                     ("postcommit", {"fail_dir": OSError(5, "injected")}))
        for name in ("recheck", "funding-add"):
            for label, kwargs in scenarios:
                with self.subTest(command=name, scenario=label):
                    self.reset()
                    (code, out, err), events = self.run_with(name, kwargs)
                    opened = {e[1] for e in events if e[0] == "open"}
                    closed = {e[1] for e in events if e[0] == "close"}
                    self.assertEqual(opened - closed, set())  # every descriptor opened by the operation is closed
                    names = [e[0] for e in events]
                    if "replace:completed" in names:
                        after = names.index("replace:completed")
                        self.assertNotIn("open", names[after:])  # the flush uses the already-admitted descriptor
                    if label == "success":
                        self.assertEqual(code, 0)
                    else:
                        self.assertEqual(code, 1)
                        self.assertEqual(out, "")


if __name__ == "__main__":
    unittest.main()
