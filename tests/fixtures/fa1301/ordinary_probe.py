"""FA1301 ordinary-loading peer probe (synthetic fixture support, file-launched).

Acts as an unrelated peer unittest process: fixes an ordinary umask, imports
tests.records.test_fa1301 with the plain unittest loader (load_tests protocol),
runs whatever ordinary loading yields, then records ambient state before the
import, after it, after loading and after running, plus the mode of a later
unrelated peer file. It installs no hook or sentinel of its own; the Isolation
test that launches it judges the recorded result.
Usage: python3 -B ordinary_probe.py --result PATH
"""
import io
import json
import os
from pathlib import Path
import socket
import sqlite3
import sys
import tempfile
import unittest

REPO = Path(os.path.abspath(__file__)).parents[3]
APP_PREFIX = "campaign_tool"


def ambient_state():
    current = os.umask(0o022)
    os.umask(current)
    return {
        "umask": current, "dont_write_bytecode": sys.dont_write_bytecode, "tempdir": tempfile.gettempdir(),
        "environ": dict(os.environ), "path": list(sys.path), "cwd": os.getcwd(),
        "app_modules": sorted(name for name in sys.modules
                              if name == APP_PREFIX or name.startswith(APP_PREFIX + ".")),
        "create_connection": id(socket.create_connection), "getaddrinfo": id(socket.getaddrinfo),
        "socket_connect": id(socket.socket.connect), "sqlite_connect": id(sqlite3.connect),
    }


def test_ids(item):
    if isinstance(item, unittest.TestSuite):
        out = []
        for child in item:
            out.extend(test_ids(child))
        return out
    return [item.id()]


def main(argv):
    if len(argv) != 2 or argv[0] != "--result":
        print("usage: ordinary_probe.py --result PATH", file=sys.stderr)
        return 2
    result_path = Path(argv[1])
    os.umask(0o022)
    sys.path.insert(0, str(REPO))
    import importlib

    before = ambient_state()
    module = importlib.import_module("tests.records.test_fa1301")
    after_import = ambient_state()
    suite = unittest.TestLoader().loadTestsFromModule(module)
    after_load = ambient_state()
    loaded_ids = test_ids(suite)  # before running: TestSuite releases tests as they finish
    stream = io.StringIO()
    outcome = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    after_run = ambient_state()
    peer = Path(after_run["tempdir"]) / "fa1301-unrelated-peer.txt"
    peer.write_text("unrelated peer\n")
    report = os.environ.get("FA1301_ORDINARY_PROBE_REPORT", "")
    children = json.loads(Path(report).read_text()) if report and os.path.isfile(report) else {}
    data = {
        "test_module": os.path.abspath(str(module.__file__)), "before": before, "after_import": after_import,
        "after_load": after_load, "after_run": after_run, "ids": loaded_ids, "run": outcome.testsRun,
        "failures": [[case.id(), text] for case, text in outcome.failures],
        "errors": [[case.id(), text] for case, text in outcome.errors],
        "skipped": [[case.id(), text] for case, text in outcome.skipped],
        "peer_mode": peer.stat().st_mode & 0o777, "children": children, "output": stream.getvalue(),
    }
    result_path.write_text(json.dumps(data, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
