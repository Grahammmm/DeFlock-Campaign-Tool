"""RF1005-9: statement add|remove write only into the content/ folder whose roster and store were validated.

Temp copies of the fictional example, synthetic names and .example hosts only; nothing is fetched, built or sent.
The substitution seams are deterministic wrappers around the real installer / real staged-file fsync: the production
readers, validators and filesystem publication are not replaced.
"""
import contextlib
import errno
import io
import json
import os
import shutil
import socket
import stat
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from campaign_tool import cli
from campaign_tool import statements as st

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
TODAY = date(2026, 10, 20)
COUNCIL = "ca-cedar-city-council"
SRC = "https://cedar.example/minutes"
CHANGED = "content/: changed while it was being edited; nothing was written"
MARKER = b"foreign marker"
REAL_INSTALL = st._install
REAL_FSYNC = os.fsync


def person(name):
    return {"name": name, "role": "Council member", "source_url": "https://cedar.example/council",
            "checked_on": "2026-09-01"}


def row(quote="We will review the contract."):
    return {"agency_id": COUNCIL, "official": "Ash Example", "said_on": "2026-10-01", "quote": quote,
            "context": "meeting", "source_url": SRC, "checked_on": "2026-10-10"}


def tree(path):
    """Every entry under path (links not followed): relative name -> (kind, bytes or link target, mode)."""
    found = {}
    for here, folders, files in os.walk(path, followlinks=False):
        for name in folders + files:
            full = os.path.join(here, name)
            info = os.lstat(full)
            key = os.path.relpath(full, path)
            if stat.S_ISLNK(info.st_mode):
                found[key] = ("link", os.readlink(full), None)
            elif stat.S_ISDIR(info.st_mode):
                found[key] = ("dir", None, stat.S_IMODE(info.st_mode))
            else:
                with open(full, "rb") as handle:
                    found[key] = ("file", handle.read(), stat.S_IMODE(info.st_mode))
    return found


class ParentIdentityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-statement-parent-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.root = self.tmp / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.content = self.root / "content"
        self.file = self.content / "statements.json"
        (self.content / "officials.json").write_text(json.dumps({"schema_version": 1, "bodies": [
            {"agency_id": COUNCIL, "officials": [person("Ash Example")]}]}), encoding="utf-8")
        self.swaps = 0

    # --- helpers -------------------------------------------------------------------------------------------------

    def store(self, rows):
        self.file.write_text(json.dumps(rows, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def add(self, quote="Another statement."):
        path = self.tmp / "quote.txt"
        path.write_bytes(quote.encode("utf-8"))
        return st.add_statement(self.root, agency_id=COUNCIL, official="Ash Example", said_on="2026-10-01",
                                quote_file=path, context="meeting", source_url=SRC, checked_on="2026-10-10",
                                today=TODAY)

    def remove(self, index=1):
        return st.remove_statement(self.root, index=index, today=TODAY)

    def substitute(self, kind="real", link_store=False, copy=False):
        """Rename the validated content/ aside and put a different thing at its pathname; -> the detached folder."""
        self.swaps += 1
        detached = self.tmp / f"detached-{self.swaps}"
        self.content.rename(detached)
        if kind == "real":
            if copy:
                shutil.copytree(detached, self.content)
            else:
                self.content.mkdir()
                (self.content / "marker.txt").write_bytes(MARKER)
            if link_store:
                os.link(detached / "statements.json", self.content / "statements.json")
        elif kind == "dangling":
            self.content.symlink_to(self.tmp / "nowhere")
        elif kind == "link":
            self.content.symlink_to(detached)
        elif kind == "file":
            self.content.write_bytes(MARKER)
        return detached

    def at_install(self, **kwargs):
        """Swap content/ at installer entry (roster and store already validated), then run the real installer."""
        def wrapper(*args, **inner):
            self.detached = self.substitute(**kwargs)
            return REAL_INSTALL(*args, **inner)
        return mock.patch.object(st, "_install", wrapper)

    def after_staged_fsync(self, **kwargs):
        """Swap content/ right after the real fsync of the staged file (before the prepublication check)."""
        state = {"done": False}

        def hook(fd):
            REAL_FSYNC(fd)
            if not state["done"]:
                state["done"] = True
                self.detached = self.substitute(**kwargs)
        return mock.patch.object(os, "fsync", side_effect=hook)

    def assert_refused(self, call):
        with self.assertRaises(st.StatementError) as caught:
            call()
        self.assertEqual(str(caught.exception), CHANGED)
        return caught.exception

    def assert_no_stage(self, *folders):
        for folder in folders:
            if Path(folder).is_dir() and not Path(folder).is_symlink():
                self.assertEqual([n for n in os.listdir(folder) if n.startswith(".statements-")], [])

    # --- acceptance: wrong-directory installation is refused -------------------------------------------------------

    def test_first_add_refuses_substituted_real_parent_before_staging(self):
        with self.at_install():
            self.assert_refused(self.add)
        self.assertFalse((self.tmp / "detached-1" / "statements.json").exists())
        self.assertEqual(tree(self.content), {"marker.txt": ("file", MARKER, stat.S_IMODE(os.lstat(
            self.content / "marker.txt").st_mode))})
        self.assert_no_stage(self.content, self.detached)

    def test_existing_add_and_remove_refuse_same_inode_store_in_foreign_parent(self):
        for call in (self.add, self.remove):
            with self.subTest(call=call.__name__):
                self.store([row("First."), row("Second.")] if call == self.remove else [row("First.")])
                with self.at_install(link_store=True):
                    self.assert_refused(call)
                detached = self.detached
                original = (detached / "statements.json").read_bytes()
                # the hard-linked file has the same inode and bytes in both folders: file identity alone cannot tell
                self.assertEqual(os.lstat(detached / "statements.json").st_ino,
                                 os.lstat(self.content / "statements.json").st_ino)
                self.assertEqual((self.content / "statements.json").read_bytes(), original)
                self.assertEqual(json.loads(original), [row("First.")] if call == self.add
                                 else [row("First."), row("Second.")])
                self.assertEqual((self.content / "marker.txt").read_bytes(), MARKER)
                self.assert_no_stage(self.content, detached)
                shutil.rmtree(self.content)
                detached.rename(self.content)

    def test_removing_the_final_row_is_refused_in_a_foreign_parent(self):
        self.store([row("Only.")])
        with self.at_install(link_store=True):
            self.assert_refused(self.remove)
        self.assertEqual(json.loads((self.content / "statements.json").read_bytes()), [row("Only.")])
        self.assertEqual(json.loads((self.detached / "statements.json").read_bytes()), [row("Only.")])

    def test_roster_and_store_directory_identities_must_agree(self):
        # add reads the roster first: a valid replacement folder arrives before the store read
        def swap_before_store(*args, **kwargs):
            self.detached = self.substitute(copy=True)
            return real_store(*args, **kwargs)
        real_store = st._store
        self.store([row("Existing.")])
        with mock.patch.object(st, "_store", swap_before_store):
            self.assert_refused(self.add)
        self.assertEqual(json.loads(self.file.read_bytes()), [row("Existing.")])
        self.assertEqual(json.loads((self.detached / "statements.json").read_bytes()), [row("Existing.")])
        self.assert_no_stage(self.content, self.detached)
        shutil.rmtree(self.content)
        self.detached.rename(self.content)

        # remove reads the store first: the roster arrives from a replacement folder
        def swap_before_roster(*args, **kwargs):
            self.detached = self.substitute(copy=True)
            return real_roster(*args, **kwargs)
        real_roster = st._roster
        with mock.patch.object(st, "_roster", swap_before_roster):
            self.assert_refused(self.remove)
        self.assertEqual(json.loads(self.file.read_bytes()), [row("Existing.")])
        self.assertEqual(json.loads((self.detached / "statements.json").read_bytes()), [row("Existing.")])
        self.assert_no_stage(self.content, self.detached)

    def test_absent_store_still_carries_a_folder_identity_and_defaults_are_unchanged(self):
        self.assertEqual(st._store(self.root), (None, None))
        document, loaded, folder = st._store(self.root, with_folder=True)
        self.assertEqual((document, loaded, folder), (None, None, (os.stat(self.content).st_dev,
                                                                    os.stat(self.content).st_ino)))
        roster = st._roster(self.root, TODAY, fresh=True)
        self.assertIsInstance(roster, dict)
        self.assertEqual(st._roster(self.root, TODAY, fresh=True, with_folder=True), (roster, folder))
        self.assertEqual(st.list_statements(self.root, today=TODAY), [])
        self.store([row()])
        self.assertEqual([r["index"] for r in st.list_statements(self.root, today=TODAY)], [1])

    def test_swap_after_staged_fsync_refuses_before_publication(self):
        cases = {"first": ([], self.add), "append": ([row("First.")], self.add),
                 "remove": ([row("First."), row("Second.")], self.remove)}
        for name, (rows, call) in cases.items():
            with self.subTest(case=name):
                self.swaps = 0
                if self.content.is_symlink() or not self.content.exists():
                    self.fail("content/ was not restored")
                if self.file.exists():
                    self.file.unlink()
                if name != "first":
                    self.store(rows)
                before = tree(self.content)
                with self.after_staged_fsync():
                    self.assert_refused(call)
                self.assertEqual(tree(self.content), {"marker.txt": ("file", MARKER, tree(self.content)[
                    "marker.txt"][2])})
                after = tree(self.detached)
                self.assertEqual({k: v for k, v in after.items() if not k.startswith(".statements-")}, before)
                self.assert_no_stage(self.detached)
                shutil.rmtree(self.content)
                self.detached.rename(self.content)

    def test_missing_symlink_and_non_directory_parent_at_recheck_refuse(self):
        for kind in ("real", "missing", "dangling", "link", "file"):
            for when, hook in (("before-staging", self.at_install), ("prepublication", self.after_staged_fsync)):
                with self.subTest(kind=kind, when=when):
                    self.store([row("First.")])
                    elsewhere = self.tmp / "elsewhere"
                    elsewhere.mkdir(exist_ok=True)
                    with hook(kind=kind):
                        self.assert_refused(self.add)
                    detached = self.detached
                    self.assertEqual(json.loads((detached / "statements.json").read_bytes()), [row("First.")])
                    self.assert_no_stage(detached)
                    self.assertEqual(os.listdir(elsewhere), [])
                    if kind == "file":
                        self.assertEqual(self.content.read_bytes(), MARKER)
                    if self.content.is_symlink() or self.content.is_file():
                        self.content.unlink()
                    elif self.content.exists():
                        shutil.rmtree(self.content)
                    detached.rename(self.content)

    def test_identity_ignores_parent_mtime_and_own_staging_entries(self):
        before_content = os.stat(self.content)

        def sibling(fd):  # an unrelated file arrives in the same folder while the stage is being written
            REAL_FSYNC(fd)
            (self.content / "sibling.txt").write_bytes(b"sibling")
            os.utime(self.content, ns=(before_content.st_atime_ns, before_content.st_mtime_ns - 10 ** 9))
        with mock.patch.object(os, "fsync", side_effect=sibling):
            self.assertEqual(self.add("First."), {"action": "added", "index": 1})
        self.assertEqual((self.content / "sibling.txt").read_bytes(), b"sibling")
        self.assertEqual(self.add("Second."), {"action": "added", "index": 2})
        self.assertEqual(self.remove(1), {"action": "removed", "index": 1})
        self.assertEqual([r["quote"] for r in json.loads(self.file.read_bytes())], ["Second."])
        self.assert_no_stage(self.content)

    # --- descriptors and CLI -------------------------------------------------------------------------------------

    def folder_fds(self):
        opened = []
        real_open = os.open

        def spy(path, flags, *args, **kwargs):
            fd = real_open(path, flags, *args, **kwargs)
            if flags & os.O_DIRECTORY:
                opened.append(fd)
            return fd
        return opened, mock.patch.object(os, "open", spy)

    def assert_closed(self, fds):
        self.assertTrue(fds)
        for fd in fds:
            with self.assertRaises(OSError) as caught:
                os.fstat(fd)
            self.assertEqual(caught.exception.errno, errno.EBADF)

    def test_directory_descriptors_close_and_unrelated_descriptors_survive(self):
        sentinel = os.open(self.tmp, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(lambda: os.path.exists(f"/proc/self/fd/{sentinel}") and os.close(sentinel))
        runs = (("success", None, None), ("entry-mismatch", self.at_install, {}),
                ("prepublication-missing", self.after_staged_fsync, {"kind": "missing"}),
                ("prepublication-real", self.after_staged_fsync, {}))
        for name, hook, kwargs in runs:
            with self.subTest(run=name):
                if self.file.exists():
                    self.file.unlink()
                opened, spy = self.folder_fds()
                with spy:
                    if hook is None:
                        self.add("Success.")
                    else:
                        with hook(**kwargs):
                            self.assert_refused(self.add)
                self.assert_closed(opened)
                os.fstat(sentinel)
                if hook is not None:
                    if self.content.exists() or self.content.is_symlink():
                        shutil.rmtree(self.content)
                    self.detached.rename(self.content)
        with mock.patch.object(st, "_folder_identity", side_effect=OSError(errno.EIO, "synthetic")):
            opened, spy = self.folder_fds()
            with spy:
                self.assert_refused(lambda: REAL_INSTALL(self.root, b"[]\n", None, expected_folder=(0, 0)))
            self.assert_closed(opened)

    def test_cli_refusal_is_controlled_and_private(self):
        recent = (date.today() - timedelta(days=3)).isoformat()
        (self.content / "officials.json").write_text(json.dumps({"schema_version": 1, "bodies": [
            {"agency_id": COUNCIL, "officials": [{**person("Ash Example"), "checked_on": recent}]}]}),
            encoding="utf-8")
        quote = self.tmp / "quote.txt"
        quote.write_bytes(b"Unmistakable Payload.")
        argv = ["campaign-tool", "statement", "add", "--agency-id", COUNCIL, "--official", "Ash Example",
                "--said-on", recent, "--quote-file", str(quote), "--context", "meeting",
                "--source-url", "https://cedar.example/private-path", "--checked-on", recent,
                "--directory", str(self.root)]
        out, err = io.StringIO(), io.StringIO()
        with self.at_install(), mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as caught:
            cli.main()
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(err.getvalue().count("Stopped:"), 1)
        self.assertIn(CHANGED, err.getvalue())
        for canary in ("Traceback", "Unmistakable Payload", "Ash Example", "private-path", str(self.tmp), "Errno"):
            self.assertNotIn(canary, err.getvalue())
        self.assertFalse((self.detached / "statements.json").exists())

    # --- compatibility controls ----------------------------------------------------------------------------------

    def test_legacy_bytes_modes_and_postcommit_durability_survive(self):
        with mock.patch.object(socket, "socket", side_effect=AssertionError("no network")), \
                mock.patch("campaign_tool.site.build_site", side_effect=AssertionError("no build")):
            self.assertEqual(self.add("Caf\u00e9 composed"), {"action": "added", "index": 1})
            self.assertEqual(self.file.read_bytes(),
                             b'[\n  {\n    "agency_id": "ca-cedar-city-council",\n    "official": "Ash Example",\n'
                             b'    "said_on": "2026-10-01",\n    "quote": "Caf\xc3\xa9 composed",\n'
                             b'    "context": "meeting",\n    "source_url": "https://cedar.example/minutes",\n'
                             b'    "checked_on": "2026-10-10"\n  }\n]\n')
            self.assertEqual(stat.S_IMODE(os.lstat(self.file).st_mode), 0o644)
            os.chmod(self.file, 0o640)
            self.assertEqual(self.add("Cafe\u0301 decomposed"), {"action": "added", "index": 2})
            self.assertEqual(stat.S_IMODE(os.lstat(self.file).st_mode), 0o640)
            self.assertEqual([r["quote"] for r in json.loads(self.file.read_text(encoding="utf-8"))],
                             ["Caf\u00e9 composed", "Cafe\u0301 decomposed"])
            self.assertEqual(self.remove(2), {"action": "removed", "index": 2})
            self.assertEqual(self.remove(1), {"action": "removed", "index": 1})
            self.assertEqual(self.file.read_bytes(), b"[]\n")
        self.assert_no_stage(self.content)

    def test_in_folder_hard_link_alias_and_postcommit_flush_error(self):
        self.store([row("First.")])
        os.link(self.file, self.content / "alias.json")  # an in-folder link is not a substitution
        self.assertEqual(self.add("Second."), {"action": "added", "index": 2})
        self.assertEqual(len(json.loads(self.file.read_bytes())), 2)
        self.assertEqual(len(json.loads((self.content / "alias.json").read_bytes())), 1)
        calls = []

        def fail_directory_flush(fd):
            calls.append(fd)
            if len(calls) == 2:  # the folder flush after publication
                raise OSError(errno.EIO, "synthetic")
            REAL_FSYNC(fd)
        with mock.patch.object(os, "fsync", side_effect=fail_directory_flush):
            with self.assertRaises(st.StatementError) as caught:
                self.add("Third.")
        self.assertIn("the new file was written but the folder could not be flushed", str(caught.exception))
        self.assertNotIn("nothing was written", str(caught.exception))
        self.assertEqual([r["quote"] for r in json.loads(self.file.read_bytes())], ["First.", "Second.", "Third."])
        self.assert_no_stage(self.content)


if __name__ == "__main__":
    unittest.main()
