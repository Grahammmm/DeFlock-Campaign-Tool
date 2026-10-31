import contextlib, importlib.util, io, os, tempfile, unittest
from pathlib import Path
from unittest import mock

from tests.flock import test_report as base

SPEC = importlib.util.spec_from_file_location("_report_io_tool", base.TOOL)
TOOL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(TOOL)
REAL_FDOPEN, REAL_REPLACE = os.fdopen, os.replace
PARTIAL = "io: partial write (REPORT-DRAFT.md replaced, SOURCES.json not); rerun build"
FINALS = ("REPORT-DRAFT.md", "SOURCES.json")
FOREIGN = (".X.tmp", ".REPORT-DRAFT.md.tmp", ".SOURCES.json.notours.tmp")  # never ours: must survive every failure
OLD = {"REPORT-DRAFT.md": b"old report\n", "SOURCES.json": b"[]\n"}


class Boom:  # an fdopen handle whose write raises
    def __init__(self, handle, exc):
        self.handle, self.exc = handle, exc
    def __enter__(self):
        return self
    def __exit__(self, *exc):
        self.handle.close()
    def write(self, text):
        raise self.exc


def failing(real, nth, exc, wrap=None):  # real call except the nth one
    calls = []
    def fake(*args, **kwargs):
        calls.append(args)
        if len(calls) != nth:
            return real(*args, **kwargs)
        if wrap:
            return wrap(real(*args, **kwargs), exc)
        raise exc
    return fake


class FlockReportIoTest(unittest.TestCase):
    stage = base.FlockReportTest.stage

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp, self.n = Path(tmp.name), 0

    def run_build(self, config, patch=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), (patch or contextlib.nullcontext()):
            code = TOOL.build(str(config), None)
        return code, out.getvalue()

    def tmps(self, report):
        return sorted(f.name for f in report.iterdir() if f.name.endswith(".tmp"))

    def finals(self, report):
        return {name: (report / name).read_bytes() for name in FINALS if (report / name).exists()}

    def staged(self, old=False):
        config, p = self.stage()
        report = p["CO"] / "report"
        report.mkdir()
        (report / FOREIGN[0]).write_text("foreign")
        (report / FOREIGN[1]).symlink_to(p["CO"] / "FINDINGS.csv")
        (report / FOREIGN[2]).write_text("stale")
        for name, data in OLD.items() if old else ():
            (report / name).write_bytes(data)
        return config, report

    def test_happy_path_leaves_no_temp(self):
        config, p = self.stage()
        self.assertEqual(self.run_build(config), (0, base.HAPPY))
        report = p["CO"] / "report"
        self.assertEqual(sorted(self.finals(report)), sorted(FINALS))
        self.assertEqual(self.tmps(report), [])

    def test_write_failures(self):
        for nth in (1, 2):
            for old in (False, True):
                with self.subTest(write=nth, old=old):
                    config, report = self.staged(old)
                    patch = mock.patch.object(TOOL.os, "fdopen", failing(REAL_FDOPEN, nth, OSError("disk full"), Boom))
                    code, out = self.run_build(config, patch)
                    self.assertEqual(code, 1)
                    self.assertEqual(out.splitlines()[0], "io: OSError")
                    self.assertNotIn(PARTIAL, out)
                    self.assertEqual(self.tmps(report), sorted(FOREIGN))
                    self.assertEqual(self.finals(report), OLD if old else {})

    def test_first_rename_fails(self):
        config, report = self.staged(old=True)
        code, out = self.run_build(config, mock.patch.object(TOOL.os, "replace", failing(REAL_REPLACE, 1, OSError("busy"))))
        self.assertEqual((code, out.splitlines()[0]), (1, "io: OSError"))
        self.assertNotIn(PARTIAL, out)
        self.assertEqual(self.tmps(report), sorted(FOREIGN))
        self.assertEqual(self.finals(report), OLD)

    def test_second_rename_fails_names_partial_write(self):
        config, report = self.staged(old=True)
        code, out = self.run_build(config, mock.patch.object(TOOL.os, "replace", failing(REAL_REPLACE, 2, OSError("busy"))))
        self.assertEqual((code, out), (1, PARTIAL + "\n" + base.HAPPY.replace("errors=0", "errors=1")))
        self.assertEqual(self.tmps(report), sorted(FOREIGN))
        after = self.finals(report)
        self.assertNotEqual(after["REPORT-DRAFT.md"], OLD["REPORT-DRAFT.md"])
        self.assertEqual(after["SOURCES.json"], OLD["SOURCES.json"])
        self.assertEqual(self.run_build(config), (0, base.HAPPY))
        good = self.finals(report)
        self.assertEqual(good["REPORT-DRAFT.md"], after["REPORT-DRAFT.md"])
        self.assertNotEqual(good["SOURCES.json"], OLD["SOURCES.json"])
        self.assertEqual(self.run_build(config), (0, base.HAPPY))
        self.assertEqual(self.finals(report), good)  # the rerun pair is stable, i.e. consistent
        self.assertEqual(self.tmps(report), sorted(FOREIGN))

    def test_keyboard_interrupt_is_reraised(self):
        cases = {"first write": ("fdopen", REAL_FDOPEN, 1, Boom), "second write": ("fdopen", REAL_FDOPEN, 2, Boom),
                 "second rename": ("replace", REAL_REPLACE, 2, None)}
        for label, (name, real, nth, wrap) in cases.items():
            with self.subTest(label):
                config, report = self.staged(old=True)
                patch = mock.patch.object(TOOL.os, name, failing(real, nth, KeyboardInterrupt(), wrap))
                with self.assertRaises(KeyboardInterrupt):
                    self.run_build(config, patch)
                self.assertEqual(self.tmps(report), sorted(FOREIGN))
                self.assertEqual(self.finals(report)["SOURCES.json"], OLD["SOURCES.json"])
