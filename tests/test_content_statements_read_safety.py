"""RF0755-3: statement bytes are admitted only from a verified regular descriptor (synthetic, offline).

Real scratch files and FIFOs; the open/fstat/fdopen seams are driven through a proxy that replaces ``os`` inside
``campaign_tool.content_statements`` only. Nothing is fetched, sent or published.
"""
import contextlib
import io
import json
import os
import socket
import stat
import unittest
from collections.abc import Callable
from unittest import mock

from campaign_tool import content_statements as reader
from campaign_tool import officials_cmd, recent
from campaign_tool.content import ContentError
from campaign_tool.content_check import content_problems, run_cli
from tests import test_meeting_pack_statements as pack_fixture
from tests import test_officials_commands as officials_fixture
from tests import test_site_statements as site_fixture

DAY = site_fixture.BUILD_DAY
NOT_REGULAR = "statements.json: must be a regular file"
TOO_LARGE = "statements.json: file too large"
UNREADABLE = "statements.json: could not be read"
CANARY = "canary-os-text-9f3a"


class Handle:
    """A real file object with injectable read/exit failures."""

    def __init__(self, handle, read_error=None, exit_error=None):
        self.handle, self.read_error, self.exit_error = handle, read_error, exit_error

    def __enter__(self):
        return self

    def read(self, size=-1):
        if self.read_error is not None:
            raise self.read_error
        return self.handle.read(size)

    def __exit__(self, *exc):
        self.handle.close()
        if self.exit_error is not None:
            raise self.exit_error
        return False


class Probe:
    """Stands in for ``os`` inside content_statements: delegates everything, observes the statements.json descriptor."""

    def __init__(self, path):
        self.path, self.payload = str(path), None
        self.fds, self.fdopens, self.keepers, self.aside = [], [], [], None
        self.before_open: Callable | None = None
        self.after_fstat: Callable | None = None
        self.fstat_error: BaseException | None = None
        self.fdopen_error: BaseException | None = None
        self.handle_kwargs: dict | None = None

    def __getattr__(self, name):
        return getattr(os, name)

    def swap_to_fifo(self, payload: bytes | None = b"[]"):
        """Arm a real FIFO at the path whenever it is opened (rearmed for every read); payload None: no writer."""
        self.payload = payload
        self.before_open = self._install

    def _install(self):
        if not stat.S_ISFIFO(os.lstat(self.path).st_mode):
            self.aside = self.path + ".aside"
            os.rename(self.path, self.aside)
            os.mkfifo(self.path)

    def open(self, path, flags, *args, **kwargs):
        mine = str(path) == self.path
        if mine and self.before_open:
            self.before_open()
        fd = os.open(path, flags, *args, **kwargs)
        if mine:
            self.fds.append(fd)
            if self.payload is not None and stat.S_ISFIFO(os.fstat(fd).st_mode):  # a writer after the reader exists; data waits in the pipe
                keeper = os.open(self.path, os.O_WRONLY | os.O_NONBLOCK)
                os.write(keeper, self.payload)
                self.keepers.append(keeper)
        return fd

    def fstat(self, fd):
        if fd in self.fds and self.fstat_error is not None:
            raise self.fstat_error
        result = os.fstat(fd)
        if fd in self.fds and self.after_fstat:
            self.after_fstat()
        return result

    def fdopen(self, fd, *args, **kwargs):
        self.fdopens.append(fd)
        if self.fdopen_error is not None:
            raise self.fdopen_error
        handle = os.fdopen(fd, *args, **kwargs)
        return Handle(handle, **self.handle_kwargs) if self.handle_kwargs is not None else handle

    def restore(self):
        for keeper in self.keepers:
            with contextlib.suppress(OSError):
                os.close(keeper)
        self.keepers = []
        if self.aside and os.path.lexists(self.aside):
            os.unlink(self.path)
            os.rename(self.aside, self.path)
            self.aside = None
        for fd in self.fds:  # a deliberately leaked descriptor must not outlive the test
            with contextlib.suppress(OSError):
                os.close(fd)

    def is_open(self, fd):
        try:
            os.fstat(fd)
        except OSError:
            return False
        return True


def one_row():
    return json.dumps([site_fixture.statement()]).encode()


class ReadSafetyTests(unittest.TestCase):
    def setUp(self):
        self.fixture = site_fixture.StatementsTests("test_unreadable_file_kinds_refused")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root, self.content, self.public = self.fixture.root, self.fixture.content, self.fixture.public
        self.file = self.content / "statements.json"
        self.active = []
        self.addCleanup(self.undo, 0)
        self.start(mock.patch.object(socket.socket, "connect", side_effect=AssertionError("network used")))

    def start(self, patcher):
        patcher.start()
        self.active.append(patcher)

    def undo(self, keep=1):
        """Stop the probe/parse patches, keeping the network guard (the first); several may be stacked."""
        while len(self.active) > keep:
            self.active.pop().stop()

    def probe(self):
        probe = Probe(self.file)
        self.addCleanup(probe.restore)
        self.start(mock.patch.object(reader, "os", probe))
        return probe

    def no_parse(self):
        for name in ("_parse", "check_statements"):
            self.start(mock.patch.object(reader, name, side_effect=AssertionError(f"{name} reached")))

    def refused(self, message, roster=None):
        with self.assertRaises(ContentError) as caught:
            reader.read_statements(self.root, roster, DAY)
        self.assertEqual(str(caught.exception), message)
        self.assertIsNone(caught.exception.__cause__)

    def tree(self):
        """Mode and bytes of every file except the test-owned input swapped in and out (directory mtimes move with it)."""
        return {p.relative_to(self.root).as_posix(): (p.lstat().st_mode, p.read_bytes())
                for p in self.root.rglob("*") if p.is_file() and p != self.file}

    # --- descriptor type -------------------------------------------------------------------------------------
    def test_fifo_substituted_after_lstat_is_refused_before_read(self):
        for payload in (b"[]", one_row()):
            self.fixture.statements(site_fixture.statement())
            probe = self.probe()
            probe.swap_to_fifo(payload)
            self.no_parse()
            self.refused(NOT_REGULAR)
            self.assertEqual(len(probe.fds), 1)
            self.assertEqual(probe.fdopens, [])  # no file object, so no payload read
            self.assertFalse(probe.is_open(probe.fds[0]))
            probe.restore()
            self.undo()
        # no writer at all: still an immediate refusal, never a wait
        self.fixture.statements(site_fixture.statement())
        probe = self.probe()
        probe.swap_to_fifo(None)
        self.no_parse()
        self.refused(NOT_REGULAR)
        self.assertFalse(probe.is_open(probe.fds[0]))

    def test_static_kinds_keep_their_diagnostics(self):
        self.file.mkdir()
        self.refused(NOT_REGULAR)
        self.file.rmdir()
        outside = self.root / "outside.json"
        outside.write_text("[]", encoding="utf-8")
        self.file.symlink_to(outside)
        self.refused("statements.json: symlinks are not read")
        self.file.unlink()
        os.mkfifo(self.file)
        self.refused(NOT_REGULAR)
        self.file.unlink()
        self.assertIsNone(reader.read_statements(self.root, None, DAY))

    # --- descriptor size and growth --------------------------------------------------------------------------
    def test_descriptor_size_preflight_and_post_fstat_growth(self):
        self.fixture.statements(site_fixture.statement())
        probe = self.probe()
        probe.before_open = lambda: os.truncate(self.file, reader.MAX_BYTES + 1)  # enlarged after lstat, before open
        self.no_parse()
        self.refused(TOO_LARGE)
        self.assertEqual(probe.fdopens, [])
        self.assertFalse(probe.is_open(probe.fds[0]))
        self.undo()
        # grown after a successful fstat: the actual bytes read still refuse, before parsing
        os.truncate(self.file, reader.MAX_BYTES)
        probe = self.probe()
        probe.after_fstat = lambda: os.truncate(self.file, reader.MAX_BYTES + 1)
        self.no_parse()
        self.refused(TOO_LARGE)
        self.assertEqual(len(probe.fdopens), 1)
        self.assertFalse(probe.is_open(probe.fds[0]))

    def test_exact_cap_is_admitted_by_bytes_not_characters(self):
        row = site_fixture.statement(quote="Caf\u00e9 \u4e2d\u6587 quote.")
        body = json.dumps([row], ensure_ascii=False).encode("utf-8")
        self.assertGreater(len(body), len(body.decode("utf-8")))
        self.file.write_bytes(body + b" " * (reader.MAX_BYTES - len(body)))
        self.assertEqual(reader.read_statements(self.root, None, DAY), [row])
        self.file.write_bytes(body + b" " * (reader.MAX_BYTES + 1 - len(body)))
        self.refused(TOO_LARGE)

    # --- raw descriptor ownership ----------------------------------------------------------------------------
    def test_fstat_and_fdopen_failures_close_raw_fd_once(self):
        self.fixture.statements(site_fixture.statement())
        for stage in ("fstat", "fdopen"):
            with self.subTest(stage=stage):
                probe = self.probe()
                setattr(probe, f"{stage}_error", OSError(5, CANARY))
                self.no_parse()
                self.refused(UNREADABLE)
                self.assertEqual(len(probe.fds), 1)
                self.assertFalse(probe.is_open(probe.fds[0]), "raw descriptor leaked")
                probe.restore()
                self.undo()

    def test_process_control_errors_propagate_and_still_close_the_fd(self):
        self.fixture.statements(site_fixture.statement())
        for error in (KeyboardInterrupt(), RuntimeError("programming error")):
            for stage in ("fstat", "fdopen"):
                with self.subTest(error=type(error).__name__, stage=stage):
                    probe = self.probe()
                    setattr(probe, f"{stage}_error", error)
                    with self.assertRaises(type(error)):
                        reader.read_statements(self.root, None, DAY)
                    self.assertFalse(probe.is_open(probe.fds[0]))
                    probe.restore()
                    self.undo()

    def test_owned_file_object_read_and_close_failures(self):
        self.fixture.statements(site_fixture.statement())
        for kwargs in ({"read_error": OSError(5, CANARY)}, {"exit_error": OSError(5, CANARY)}):
            with self.subTest(**{k: "OSError" for k in kwargs}):
                probe = self.probe()
                probe.handle_kwargs = kwargs
                self.no_parse()
                self.refused(UNREADABLE)
                self.assertFalse(probe.is_open(probe.fds[0]))
                probe.restore()
                self.undo()
        probe = self.probe()
        probe.handle_kwargs = {"read_error": RuntimeError("programming error")}
        with self.assertRaises(RuntimeError):
            reader.read_statements(self.root, None, DAY)
        self.assertFalse(probe.is_open(probe.fds[0]))

    # --- raising and accumulator modes -----------------------------------------------------------------------
    def test_reader_raise_and_accumulator_modes_preserve_input(self):
        self.fixture.statements(site_fixture.statement())
        cases = {
            "fifo": (NOT_REGULAR, lambda p: p.swap_to_fifo()),
            "fstat": (UNREADABLE, lambda p: setattr(p, "fstat_error", OSError(5, CANARY))),
            "size": (TOO_LARGE, lambda p: setattr(p, "before_open", lambda: os.truncate(self.file, reader.MAX_BYTES + 1))),
        }
        for name, (message, arm) in cases.items():
            with self.subTest(case=name):
                self.fixture.statements(site_fixture.statement())
                probe = self.probe()
                arm(probe)
                before = self.tree()
                self.refused(message)
                problems = ["earlier problem"]
                self.assertIsNone(reader.read_statements(self.root, None, DAY, problems))
                self.assertEqual(problems, ["earlier problem", message])
                self.assertNotIn(CANARY, repr(problems))
                probe.restore()
                self.assertEqual(self.tree(), before)
                self.undo()
        # absent and valid empty stay distinct from a refusal
        self.file.unlink()
        problems = []
        self.assertIsNone(reader.read_statements(self.root, None, DAY, problems))
        self.assertEqual(problems, [])
        self.file.write_text("[]", encoding="utf-8")
        self.assertEqual(reader.read_statements(self.root, None, DAY, problems), [])
        self.assertEqual(problems, [])

    # --- real consumers --------------------------------------------------------------------------------------
    def test_build_and_checker_refuse_fifo_without_public_changes(self):
        fixture = self.fixture
        for good in (False, True):
            fixture.statements(site_fixture.statement())
            if good:
                fixture.build()
            before = (fixture.snapshot() if self.public.exists() else None,
                      sorted(p.name for p in self.public.rglob("*")) if self.public.exists() else None)
            probe = self.probe()
            probe.swap_to_fifo()
            with self.assertRaises(ContentError) as caught:
                fixture.build()
            self.assertEqual(str(caught.exception), NOT_REGULAR)
            after = (fixture.snapshot() if self.public.exists() else None,
                     sorted(p.name for p in self.public.rglob("*")) if self.public.exists() else None)
            self.assertEqual(after, before)
            self.assertTrue(any(line.startswith("statements.json") and NOT_REGULAR in line
                                for line in content_problems(self.root, limit=None, today=DAY)))
            out = io.StringIO()
            with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as code:
                run_cli(self.root, as_json=True)
            self.assertEqual(code.exception.code, 3)
            record = json.loads(out.getvalue())
            self.assertGreaterEqual(record["count"], 1)
            self.assertIn("statements.json", out.getvalue())
            probe.restore()
            self.undo()

    def test_pack_references_and_recent_retain_fail_closed_semantics(self):
        pack = pack_fixture.StatementPackTests("test_two_newest_per_member_in_roster_order_with_csv_rows")
        pack.setUp()
        self.addCleanup(pack.doCleanups)
        pack.put([pack_fixture.statement("Ann Alpha", "2026-03-01", "quote one")])
        good = pack.read_pack()
        probe = Probe(pack.content / "statements.json")
        probe.swap_to_fifo(json.dumps([pack_fixture.statement("Ann Alpha", "2026-03-01", "quote one")]).encode())
        self.addCleanup(probe.restore)
        with mock.patch.object(reader, "os", probe):
            with self.assertRaises(ContentError) as caught:
                pack.pack(replace=True)
        self.assertEqual(str(caught.exception), NOT_REGULAR)
        probe.restore()
        self.assertEqual({n: (pack.folder / n).read_bytes() for n in good}, good)

        official = officials_fixture.OfficialsCommandTests("test_remove_refuses_votes_and_statements_with_same_body_identity")
        official.setUp()
        self.addCleanup(official.doCleanups)
        official.add("Ash Example")
        official.add("Birch Example")
        statements = official.content / "statements.json"
        statements.write_bytes(one_row())
        roster = official.file.read_bytes()
        probe = Probe(statements)
        probe.swap_to_fifo(one_row())
        self.addCleanup(probe.restore)
        with mock.patch.object(reader, "os", probe):
            with self.assertRaises(officials_cmd.OfficialsError) as caught:
                officials_cmd.remove_official(official.root, agency_id=officials_fixture.COUNCIL, name="Ash Example",
                                              today=officials_fixture.TODAY)
        self.assertIn(NOT_REGULAR, str(caught.exception))
        probe.restore()
        self.assertEqual(official.file.read_bytes(), roster)

        class Sink:
            unreadable_calls = []

            def unreadable(self, group, label):
                self.unreadable_calls.append((group, label))

            def add(self, *args):
                raise AssertionError("a refused file must supply no event")

        probe = Probe(self.file)
        probe.swap_to_fifo(one_row())
        self.addCleanup(probe.restore)
        self.fixture.statements(site_fixture.statement())
        sink = Sink()
        with mock.patch.object(reader, "os", probe):
            recent._statements(self.root, sink, DAY, {})
        self.assertEqual(sink.unreadable_calls, [(recent.OUTREACH, "content/statements.json")])

    # --- compatibility ---------------------------------------------------------------------------------------
    def test_regular_success_and_legacy_refusals_remain_compatible(self):
        self.assertIsNone(reader.read_statements(self.root, None, DAY))  # absent
        self.file.write_text("[]", encoding="utf-8")
        self.assertEqual(reader.read_statements(self.root, None, DAY), [])
        rows = [site_fixture.statement(quote="  Spaced \u00e9\u4e2d  quote  "), site_fixture.statement(),
                site_fixture.statement(said_on="2026-09-11"), site_fixture.statement(said_on="2026-09-10")]
        self.fixture.statements(*rows)
        self.assertEqual(reader.read_statements(self.root, None, DAY), rows)
        self.assertEqual([r["said_on"] for r in reader.newest_first(rows)],
                         ["2026-09-11", "2026-09-10", "2026-09-10", "2026-09-10"])
        for raw, message in ((b"\xff\xfe", "statements.json: not valid UTF-8 JSON"),
                             (b'[{"a":1,"a":2}]', "statements.json: a JSON object repeats a key"),
                             (b"[{", "statements.json: not valid UTF-8 JSON")):
            self.file.write_bytes(raw)
            self.refused(message)
        self.fixture.statements(site_fixture.statement(quote="Write me at pat@example.org please"))
        with self.assertRaises(ContentError) as caught:
            reader.read_statements(self.root, None, DAY)
        self.assertNotIn("pat@example.org", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
