"""FA1310 full-module regressions. All records and policies are synthetic."""
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

# File-launched children need the same portable candidate import path.
REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
from tests.fixtures.fa1310.support import BASE, HOST, URL, Boundary, fresh, modules, safe_path

RUN_ROOT = None
COMPATIBILITY = (
    "tests.test_records_portal.PortalTests.test_inventory_notice_html",
    "tests.test_records_portal.PortalTests.test_notice_entity_decoding_keeps_single_url_generation",
    "tests.records.test_portal_intake.PortalIntakeTests.test_allowlisted_notice_enrolls_link",
)


class Fixture(unittest.TestCase):
    def setUp(self):
        self.root = fresh(RUN_ROOT or tempfile.gettempdir(), "test-")
        self.boundary = Boundary(self.root)
        self.boundary.__enter__()
        self.addCleanup(self.boundary.__exit__, None, None, None)
        self.addCleanup(self.assertEqual, self.boundary.attempts, [])
        (self.Links, self.inventory_notice, self.BodyLinks, self.CollectionInventory,
         self.Scope, self.Queue, self.digest, self.root_lock) = modules()
        self.source = hashlib.sha256(b"FA1310 fictional notice").hexdigest()

    def notice(self, body):
        root = fresh(self.root, "notice-")
        queue = self.Queue(root / "queue")
        self.addCleanup(queue.close)
        with patch.object(queue, "inventory", wraps=queue.inventory) as calls:
            result = self.inventory_notice(queue, body=body, request_id="26-001",
                source_sha256=self.source, known_hosts={HOST})
        return result, [call.args[3] for call in calls.call_args_list], queue

    def discover(self, body):
        root = fresh(self.root, "collection-")
        queue = self.Queue(root / "queue")
        self.addCleanup(queue.close)
        inventory = self.CollectionInventory(queue, self.Scope(frozenset({HOST})))
        preserved = SimpleNamespace(message_id="synthetic-event", receipt_sha256=self.source,
                                    eml_sha256=self.source, attachments=[])
        with self.root_lock(root):
            inventory.register(preserved, {"producer": "synthetic-fa1310", "id": "26-001"})
            inventory.discover(preserved.message_id, [("body-1", body, "text/html")])
            before = list(map(tuple, queue.db.execute("SELECT * FROM export_body_links ORDER BY ordinal")))
            inventory.discover(preserved.message_id, [("body-1", body, "text/html")])
            self.assertEqual(before, list(map(tuple, queue.db.execute("SELECT * FROM export_body_links ORDER BY ordinal"))))
        rows = list(queue.db.execute("SELECT url_private,kind,reason FROM export_body_links ORDER BY ordinal"))
        self.assertEqual(queue.status()["attempts"], 0)
        self.assertEqual(queue.status()["versions"], 0)
        self.assertFalse(inventory.readiness(preserved.message_id)["publication_authorized"])
        return [tuple(row) for row in rows], queue

    def zero(self, body):
        result, calls, queue = self.notice(body)
        self.assertEqual(calls, [], "excluded HTML must not call queue.inventory")
        self.assertEqual(result, {"items": 0})
        self.assertEqual(queue.status()["items"], [])
        rows, queue = self.discover(body)
        self.assertEqual(rows, [])
        self.assertEqual(queue.status()["items"], [])


class Behavior(Fixture):
    def test_visible_identity_entity_and_replay(self):
        second = BASE + "/documents/456"
        body = '<main><a href="' + URL.replace("&", "&amp;") + '">file</a> ' + second + '</main>'
        result, calls, queue = self.notice(body)
        self.assertEqual(result, {"items": 2})
        self.assertEqual(calls, [URL, second])
        snapshot = list(map(tuple, queue.db.execute("SELECT * FROM portal_items ORDER BY id")))
        self.inventory_notice(queue, body=body, request_id="26-001", source_sha256=self.source, known_hosts={HOST})
        self.assertEqual(snapshot, list(map(tuple, queue.db.execute("SELECT * FROM portal_items ORDER BY id"))))
        self.assertEqual(queue.status()["attempts"], 0)
        self.assertEqual(queue.status()["versions"], 0)
        self.assertEqual([r[0] for r in self.discover(body)[0]], [URL, second])

    def test_script_zero(self):
        self.zero('<script>const documentLink = "' + URL + '";</script>')

    def test_style_zero(self):
        self.zero('<style>.file { background: url("' + URL + '"); }</style>')

    def test_parser_context_matrix(self):
        # HTMLParser treats script/style contents as CDATA, even apparent tags.
        for tag in ("script", "style", "ScRiPt", "StYlE"):
            for content in (URL, '</div>' + URL, '<a href="' + URL + '">file</a>',
                            '<style/>' + URL, '<script>' + URL):
                with self.subTest(tag=tag, content=content):
                    self.zero('<' + tag + '>' + content + '</' + tag + '>')
            with self.subTest(unclosed=tag):
                self.zero('<' + tag + '>' + URL + '<a href="' + URL + '">file</a>')
            with self.subTest(self_closing=tag):
                # Actual startendtag callback opens then closes the exclusion.
                self.assertEqual(self.notice('<' + tag + '/>' + URL + '<p>end</p>')[1], [URL])
                self.assertEqual([r[0] for r in self.discover('<' + tag + '/>' + URL + '<p>end</p>')[0]], [URL])
        self.assertEqual(self.notice('</script>' + URL + '<p>end</p>')[1], [URL])

    def test_incremental_callbacks_and_unrelated_endtag(self):
        for cls in (self.Links, self.BodyLinks):
            body = '<ScRiPt>' + URL + '</div><a href="' + URL + '">x</a></sCrIpT>'
            for width in (1, 2, 7, len(body)):
                parser = cls()
                for start in range(0, len(body), width):
                    parser.feed(body[start:start + width])
                # Visible URL tokenization across feed calls is unchanged; this
                # card tests the exclusion boundary, not a streaming URL buffer.
                parser.feed(URL)
                parser.close()
                self.assertEqual(parser.urls, [URL])
            parser = cls()
            parser.handle_starttag("script", [])
            parser.handle_endtag("div")
            parser.handle_starttag("a", [("href", URL)])
            parser.handle_data(URL)
            self.assertEqual(parser.urls, [])
            parser.handle_endtag("script")
            parser.handle_data(URL)
            self.assertEqual(parser.urls, [URL])

    def test_mixed_order_duplicate_href_and_http_exception(self):
        second = BASE + "/documents/456"
        http = "http://" + HOST + "/documents/789"
        body = ('<script>' + BASE + '/documents/999</script>' + URL.replace('&', '&amp;') +
                '<style>' + BASE + '/documents/888</style><a href="' + second + '">file</a>' +
                '<a href="' + BASE + '/documents/777" href="' + BASE + '/documents/666">ambiguous</a> ' + http)
        self.assertEqual(self.notice(body)[1], [URL, second])
        rows, queue = self.discover(body)
        self.assertEqual(rows, [(URL, "document", None), (second, "document", None),
                               ("sha256:" + self.digest(http.encode()), "blocked", "invalid_url")])
        self.assertEqual(len(queue.status()["items"]), 2)
        for tag in ("script", "style"):
            self.assertEqual(self.discover('<' + tag + '>' + http + '</' + tag + '>')[0], [])
        self.assertEqual(self.boundary.attempts, [])


class ControlCase(Fixture):
    def test_script_zero(self):
        result, calls, queue = self.notice('<script>const href="' + URL + '";</script>')
        print(json.dumps({"control_inventory_calls": calls, "items": result["items"]}))
        self.assertEqual(calls, [], "excluded HTML must not call queue.inventory")
        self.assertEqual(queue.status()["items"], [])

    def test_visible_controls(self):
        for body in (URL + ' ', '<a href="' + URL.replace('&', '&amp;') + '">file</a>'):
            self.assertEqual(self.notice(body)[1], [URL])


def child(case, root, script=None):
    script = script or Path(__file__).resolve()
    argv = [sys.executable, "-B", str(script), case, str(root)]
    process = subprocess.run(argv, cwd=script.parents[2], text=True, capture_output=True)
    root = safe_path(root)
    root.mkdir(parents=True, exist_ok=True)
    (root / "stdout.txt").write_text(process.stdout)
    (root / "stderr.txt").write_text(process.stderr)
    (root / "command.json").write_text(json.dumps({"argv": argv, "cwd": str(script.parents[2]), "exit": process.returncode}, indent=2))
    return process


class GateControl(unittest.TestCase):
    def test_deciding_mutation_and_restoration(self):
        root = fresh(RUN_ROOT or tempfile.gettempdir(), "control-")
        copy = root / "disposable"
        copy.mkdir()
        shutil.copytree(REPO / "campaign_tool", copy / "campaign_tool", ignore=shutil.ignore_patterns("__pycache__"))
        for name in ("tests/__init__.py", "tests/records/__init__.py", "tests/records/test_fa1310.py", "tests/fixtures/fa1310/support.py"):
            target = copy / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(REPO / name, target)
        source = copy / "campaign_tool/records/portal/notice.py"
        original = source.read_text()
        marker = "return self._inert_tag is not None"
        self.assertEqual(original.count(marker), 1)
        source.write_text(original.replace(marker, "return False"))
        broken_root = root / "broken"
        broken = child("control-case", broken_root, copy / "tests/records/test_fa1310.py")
        summary = json.loads((broken_root / "result.json").read_text())
        self.assertEqual(broken.returncode, 1)
        self.assertEqual(summary["tests"], 2)
        self.assertEqual(summary["failures"], ["__main__.ControlCase.test_script_zero"])
        self.assertEqual(summary["errors"], [])
        self.assertEqual(summary["skips"], [])
        self.assertIn("excluded HTML must not call queue.inventory", broken.stderr)
        self.assertIn('"control_inventory_calls": ["' + URL + '"]', broken.stdout)
        self.assertIn('"items": 1', broken.stdout)
        source.write_text(original)
        restored_root = root / "restored"
        restored = child("control-case", restored_root, copy / "tests/records/test_fa1310.py")
        self.assertEqual(restored.returncode, 0, restored.stderr)
        self.assertEqual(json.loads((restored_root / "result.json").read_text())["tests"], 2)


class Isolation(unittest.TestCase):
    def test_fresh_child_paths_and_guards(self):
        root = fresh(RUN_ROOT or tempfile.gettempdir(), "isolation-")
        result = child("probe", root / "child")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads((root / "child/probe.json").read_text())
        self.assertTrue(report["before_import"])
        self.assertEqual(report["unexpected_attempts"], [])
        self.assertEqual(report["probe_refusals"], ["live_file", "network", "model"])
        for name, path in report["module_paths"].items():
            self.assertEqual(Path(path), REPO / (name.replace(".", "/") + ".py"))

    def test_linked_output_refused_before_writes(self):
        root = fresh(RUN_ROOT or tempfile.gettempdir(), "links-")
        target = root / "target"
        target.mkdir()
        alias = root / "alias"
        alias.symlink_to(target, target_is_directory=True)
        for index, path in enumerate((alias, alias / "absent" / "child")):
            with self.assertRaisesRegex(ValueError, "unsafe_output_ancestry"):
                fresh(path)
            argv = [sys.executable, "-B", str(REPO / "tools/accept_fa1310.py"),
                    "--case", "behavior", "--output-root", str(path)]
            result = subprocess.run(argv,
                                    cwd=REPO, capture_output=True, text=True)
            (root / ("refused-" + str(index) + ".json")).write_text(json.dumps({
                "argv": argv, "cwd": str(REPO), "exit": result.returncode,
                "stdout": result.stdout, "stderr": result.stderr}, indent=2))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("unsafe_output_ancestry", result.stderr)
        self.assertEqual(list(target.iterdir()), [])

    def test_shared_process_boundary_is_reversible(self):
        root = fresh(RUN_ROOT or tempfile.gettempdir(), "reversible-")
        before = (dict(os.environ), sys.getprofile(), sys.dont_write_bytecode, tempfile.tempdir, open, subprocess.Popen)
        with Boundary(root):
            modules()
        after = (dict(os.environ), sys.getprofile(), sys.dont_write_bytecode, tempfile.tempdir, open, subprocess.Popen)
        self.assertEqual(before, after)


def run(case, root):
    global RUN_ROOT
    sys.dont_write_bytecode = True
    RUN_ROOT = safe_path(root)
    RUN_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    if case == "probe":
        before = not any(name.startswith("campaign_tool") for name in sys.modules)
        with Boundary(RUN_ROOT) as boundary:
            modules()
            paths = {name: sys.modules[name].__file__ for name in (
                "campaign_tool.records.portal.notice", "campaign_tool.records.portal.collections")}
            unexpected = list(boundary.attempts)
            from campaign_tool.digest.model import chat_json
            for action in (lambda: open(RUN_ROOT.parent / "synthetic-forbidden-file"),
                           lambda: __import__("socket").create_connection(("synthetic.invalid", 1)),
                           lambda: chat_json(None, [])):
                try:
                    action()
                except RuntimeError:
                    pass
                else:
                    raise RuntimeError("sentinel_did_not_refuse")
            report = {"before_import": before, "module_paths": paths, "unexpected_attempts": unexpected,
                      "probe_refusals": boundary.attempts}
            (RUN_ROOT / "probe.json").write_text(json.dumps(report, indent=2))
        return 0
    selections = {"behavior": ["__main__.Behavior"], "negative-control": ["__main__.GateControl"],
                  "isolation": ["__main__.Isolation"], "control-case": ["__main__.ControlCase"],
                  "compatibility": list(COMPATIBILITY)}
    if case not in selections:
        raise ValueError("unknown_case")
    stream = io.StringIO()
    def execute():
        suite = unittest.defaultTestLoader.loadTestsFromNames(selections[case])
        return unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    if case == "compatibility":
        with Boundary(RUN_ROOT) as boundary:
            result = execute()
            if boundary.attempts:
                raise RuntimeError("unexpected_boundary_attempt")
    else:
        result = execute()
    sys.stderr.write(stream.getvalue())
    summary = {"tests": result.testsRun, "failures": [test.id() for test, _ in result.failures],
               "errors": [test.id() for test, _ in result.errors], "skips": [test.id() for test, _ in result.skipped]}
    (RUN_ROOT / "result.json").write_text(json.dumps(summary, indent=2))
    return int(not result.testsRun or not result.wasSuccessful() or bool(result.skipped))


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1], Path(sys.argv[2])))
