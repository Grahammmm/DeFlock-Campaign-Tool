"""RF1155-4: decisions-elsewhere raw descriptors close when stream construction fails. Offline, synthetic only."""
import errno
import json
import os
import shutil
import tempfile
import unittest
import unittest.mock
from datetime import date
from pathlib import Path

from campaign_tool import elsewhere_cmd
from campaign_tool.elsewhere_cmd import ElsewhereError, add_entry, list_entries, remove_entry

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
TODAY = date(2026, 11, 1)
CANARY = "synthetic-canary"


class Boom(BaseException):
    pass


def row(n):
    return {"place": f"Springfield {n}", "state": "OR", "body": "Springfield City Council", "decided_on": "2026-03-10",
            "kind": "paused", "as_stated": f"The council voted to pause contract {n}.",
            "source_url": f"https://records.example.test/minutes/{n}", "checked_on": "2026-09-15"}


def add_kwargs(n):
    return {**row(n), "today": TODAY}


class FdOwnership(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-elsewhere-fd-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.path = self.root / "content" / "elsewhere.json"
        self.path.unlink(missing_ok=True)

    def put(self, rows, mode=0o600):
        self.path.write_text(json.dumps(rows), encoding="utf-8")
        os.chmod(self.path, mode)

    def snapshot(self):
        return {str(p.relative_to(self.root)): (p.read_bytes(), p.stat().st_mode, p.stat().st_mtime_ns)
                for p in sorted(self.root.rglob("*")) if p.is_file()}

    def inject(self, mode, exc_factory, after=0):
        """Patch os.fdopen: record every fd handed to it for ``mode`` and raise before returning a stream."""
        real, seen = os.fdopen, []

        def fake(fd, m="r", *args, **kwargs):
            if m == mode:
                seen.append(fd)
                if len(seen) > after:
                    raise exc_factory()
            return real(fd, m, *args, **kwargs)

        patcher = unittest.mock.patch.object(elsewhere_cmd.os, "fdopen", fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return seen

    def assert_closed(self, fds):
        self.assertTrue(fds)
        for fd in fds:
            with self.assertRaises(OSError) as ctx:
                os.fstat(fd)
            self.assertEqual(ctx.exception.errno, errno.EBADF)

    def oserror(self):
        return OSError(errno.EMFILE, CANARY)

    def test_store_read_failure_closes_fd_for_every_verb(self):
        self.put([row(1)])
        before = self.snapshot()
        seen = self.inject("rb", self.oserror)
        for call in (lambda: list_entries(self.root, today=TODAY), lambda: add_entry(self.root, **add_kwargs(2)),
                     lambda: remove_entry(self.root, 1, today=TODAY)):
            for _ in range(3):
                with self.assertRaises(ElsewhereError) as ctx:
                    call()
                self.assertIn("cannot be read", str(ctx.exception))
                self.assertNotIn(CANARY, str(ctx.exception))
        self.assertEqual(len(seen), 9)
        self.assert_closed(seen)
        self.assertEqual(self.snapshot(), before)

    def test_staged_citation_read_failure_closes_leaf_fd(self):
        self.put([row(1)])
        meetings = self.root / "content" / "meetings"
        meetings.mkdir(exist_ok=True)
        (meetings / "2026-10-synthetic.json").write_text("{}", encoding="utf-8")
        before = self.snapshot()
        seen = self.inject("rb", self.oserror, after=1)  # the store read succeeds, the staged meeting read fails
        with self.assertRaises(ElsewhereError):
            remove_entry(self.root, 1, today=TODAY)
        self.assertEqual(len(seen), 2)
        self.assert_closed(seen)
        self.assertEqual(self.snapshot(), before)

    def test_staging_write_failure_closes_unlinked_inode(self):
        for existing in (False, True):
            for verb in ("add", "remove"):
                if verb == "remove" and not existing:
                    continue
                self.path.unlink(missing_ok=True)
                if existing:
                    self.put([row(1), row(2)])
                before = self.snapshot()
                seen = self.inject("wb", self.oserror)
                for _ in range(3):
                    with self.assertRaises(ElsewhereError) as ctx:
                        add_entry(self.root, **add_kwargs(3)) if verb == "add" else remove_entry(self.root, 2, today=TODAY)
                    self.assertIn("write failed; the previous file was kept", str(ctx.exception))
                    self.assertNotIn(CANARY, str(ctx.exception))
                self.assert_closed(seen)
                self.assertEqual(len(seen), 3)
                self.assertEqual(self.snapshot(), before)
                self.assertEqual([p.name for p in (self.root / "content").iterdir() if p.name.endswith(".tmp")], [])
                unittest.mock.patch.stopall()

    def test_unexpected_errors_propagate_after_cleanup(self):
        self.put([row(1), row(2)])
        before = self.snapshot()
        for mode, call in (("rb", lambda: list_entries(self.root, today=TODAY)),
                           ("wb", lambda: add_entry(self.root, **add_kwargs(3)))):
            for exc in (RuntimeError, Boom):
                seen = self.inject(mode, exc)
                with self.assertRaises(exc):
                    call()
                self.assert_closed(seen)
                unittest.mock.patch.stopall()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual([p.name for p in (self.root / "content").iterdir() if p.name.endswith(".tmp")], [])

    def test_successful_lifecycle_preserves_bytes_and_modes(self):
        opened = []
        real_open = os.open

        def tracking(*args, **kwargs):
            fd = real_open(*args, **kwargs)
            opened.append(fd)
            return fd

        unittest.mock.patch.object(elsewhere_cmd.os, "open", tracking).start()
        self.addCleanup(unittest.mock.patch.stopall)
        self.assertIsNone(list_entries(self.root, today=TODAY))
        self.assertEqual(add_entry(self.root, **add_kwargs(1))["index"], 1)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o644)
        os.chmod(self.path, 0o600)
        self.assertEqual(add_entry(self.root, **add_kwargs(2))["index"], 2)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        expected = (json.dumps([row(1), row(2)], indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        self.assertEqual(self.path.read_bytes(), expected)
        self.assertEqual(remove_entry(self.root, 2, today=TODAY)["last"], False)
        self.assertEqual(remove_entry(self.root, 1, today=TODAY)["last"], True)
        self.assertFalse(self.path.exists())
        self.assert_closed(opened)


if __name__ == "__main__":
    unittest.main()
