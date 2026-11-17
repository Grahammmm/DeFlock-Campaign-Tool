"""RF1830-1: a JSON integer the decoder refuses (conversion guard) becomes a named ContentError. Synthetic data only.

Real guarded decoding runs in child interpreters started with ``-X int_max_str_digits=640``; the parent setting is untouched."""
import json
import shutil
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool import content_events
from campaign_tool.content import ContentError
from campaign_tool.content_events import read_events
from campaign_tool.site import build_site

from tests.test_site_events import TODAY, EventsCase, event

REPO_ROOT = Path(__file__).resolve().parents[1]
GUARD = "640"
FIXED = "events.json: not valid UTF-8 JSON (value could not be decoded)"
REAL_OPEN = open


def digits(count):
    return "9" * count


def tree_snapshot(root):
    """Relative path -> (bytes, mode, mtime_ns) for every file, so later runs can prove nothing was touched."""
    return {p.relative_to(root).as_posix(): (p.read_bytes(), p.stat().st_mode, p.stat().st_mtime_ns)
            for p in sorted(root.rglob("*")) if p.is_file()}


class GuardedChild:
    """Mixin: run python / the CLI in a child whose integer conversion guard is 640 digits."""

    def child(self, *args, stdin=None):
        return subprocess.run([sys.executable, "-B", "-X", f"int_max_str_digits={GUARD}", *args], cwd=REPO_ROOT, input=stdin,
                              capture_output=True, text=True, encoding="utf-8", timeout=120)

    def cli(self, *args):
        return self.child("-m", "campaign_tool", *args, "--directory", str(self.root))

    def collect(self):
        """read_events(root, [\"earlier\"]) in a guarded child: (return value, problems) as JSON."""
        script = textwrap.dedent(f"""
            import json, sys
            from campaign_tool.content_events import read_events
            problems = ["earlier"]
            print(json.dumps([read_events({str(self.root)!r}, problems), problems]))
            """)
        result = self.child("-", stdin=script)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)


class GuardedDecoderTests(GuardedChild, EventsCase):
    def test_real_guarded_integer_decoder_refuses_top_nested_and_known_fields(self):
        big, small = digits(int(GUARD) + 1), digits(int(GUARD))
        for name, text in (("top", f"[{big}]"), ("unknown", '[{"surprise":%s}]' % big),
                           ("nested", '[{"surprise":[[%s]]}]' % big), ("negative", f"[-{big}]"),
                           ("title", '[{"title":%s}]' % big)):
            with self.subTest(name=name):
                self.put(text.encode("utf-8"))
                self.assertEqual(self.collect(), [None, ["earlier", FIXED]])
        for name, text, message in (("top", f"[{small}]", "events.json[0]: expected an object"),):
            with self.subTest(name="at-limit-" + name):
                self.put(text.encode("utf-8"))
                self.assertEqual(self.collect(), [None, ["earlier", message]])
        self.put(('[{"title":"%s"}]' % big).encode("utf-8"))  # quoted long text is a string: old rules, no coercion
        got = self.collect()
        self.assertIsNone(got[0])
        self.assertNotIn(FIXED, got[1])

    def test_collector_preserves_prefix_and_raises_without_a_list(self):
        self.put(b"[1]")
        with mock.patch.object(content_events.json, "loads", side_effect=ValueError("canary")), \
                mock.patch.object(content_events, "load_events", side_effect=AssertionError("loaded")):
            problems = ["earlier"]
            self.assertIsNone(read_events(self.root, problems))
            self.assertEqual(problems, ["earlier", FIXED])
            with self.assertRaises(ContentError) as caught:
                read_events(self.root)
            self.assertEqual(str(caught.exception), FIXED)
            self.assertIsNone(caught.exception.__cause__)
            self.assertTrue(caught.exception.__suppress_context__)

    def test_checker_keeps_independent_findings(self):
        self.put(f"[{digits(int(GUARD) + 1)}]".encode("utf-8"))
        self.put({"status": "postponed"}, name="meetings/d.json")
        script = textwrap.dedent(f"""
            import json
            from campaign_tool.content_check import content_problems
            print(json.dumps(content_problems({str(self.root)!r})))
            """)
        result = self.child("-", stdin=script)
        self.assertEqual(result.returncode, 0, result.stderr)
        problems = json.loads(result.stdout)
        self.assertEqual(problems.count(FIXED), 1)
        self.assertGreater(len(problems), 1)
        self.assertTrue(any("d.json" in p for p in problems), problems)

    def test_plain_and_json_cli_restore_checker_problem_contract(self):
        self.put(f"[{digits(int(GUARD) + 1)}]".encode("utf-8"))
        text = self.cli("content-check")
        self.assertEqual((text.returncode, text.stderr), (3, ""))
        self.assertIn(FIXED, text.stdout.splitlines())
        self.assertNotIn("Traceback", text.stdout + text.stderr)
        self.assertNotIn("set_int_max_str_digits", text.stdout + text.stderr)
        result = self.cli("content-check", "--json")
        self.assertEqual((result.returncode, result.stderr), (3, ""))
        doc = json.loads(result.stdout)
        self.assertEqual(doc["schema"], "content-check-v1")
        self.assertIn({"file": "events.json", "message": FIXED[len("events.json: "):]}, doc["problems"])
        for command, code in ((("build",), 1), (("event", "list"), 2)):  # each command's existing ContentError exit code
            with self.subTest(command=command):
                refused = self.cli(*command)
                shown = refused.stdout + refused.stderr
                self.assertEqual(refused.returncode, code)
                self.assertIn(FIXED, shown)
                self.assertNotIn("set_int_max_str_digits", shown)
                self.assertNotIn("Traceback", shown)

    def test_bad_input_install_preserves_campaign_tree(self):
        for prebuilt in (True, False):
            with self.subTest(prebuilt=prebuilt):
                if prebuilt:
                    self.build()
                self.put(f"[{digits(int(GUARD) + 1)}]".encode("utf-8"))
                before = tree_snapshot(self.root)
                self.assertEqual(self.cli("build").returncode, 1)
                self.assertEqual(self.cli("event", "list").returncode, 2)
                self.assertEqual(tree_snapshot(self.root), before)  # bytes, modes, mtimes and membership of files
                self.assertEqual(self.public.exists(), prebuilt)
                if prebuilt:
                    shutil.rmtree(self.public)


class DecoderPrecedenceTests(EventsCase):
    def test_specific_decoder_errors_keep_legacy_text(self):
        self.put(b"\xff\xfe[]")
        with self.assertRaisesRegex(ContentError, r"^events\.json: not valid UTF-8 JSON \(.*utf-8"):
            read_events(self.root)
        self.put(b"{not json")
        with self.assertRaises(ContentError) as caught:
            read_events(self.root)
        self.assertRegex(str(caught.exception), r"^events\.json: not valid UTF-8 JSON \(Expecting property name")
        self.assertNotEqual(str(caught.exception), FIXED)
        self.put(b"[" * 100000)
        with self.assertRaisesRegex(ContentError, "nested too deeply|not valid"):
            read_events(self.root)

    def test_canary_valueerror_is_value_free(self):
        self.put(b"[]")
        with mock.patch.object(content_events.json, "loads", side_effect=ValueError("SECRET-CANARY /private/path")):
            with self.assertRaises(ContentError) as caught:
                read_events(self.root)
        self.assertEqual(str(caught.exception), FIXED)
        self.assertNotIn("CANARY", str(caught.exception))

    def test_validator_and_fatal_errors_are_not_relabeled(self):
        self.put(b"[]")
        with mock.patch.object(content_events, "load_events", side_effect=ValueError("validator")):
            with self.assertRaisesRegex(ValueError, "^validator$"):
                read_events(self.root)
        with mock.patch.object(content_events.json, "loads", side_effect=MemoryError()):
            with self.assertRaises(MemoryError):
                read_events(self.root)

    def test_size_wins_before_decoding(self):
        self.put(b" " * (content_events.MAX_JSON_BYTES + 1))
        with mock.patch.object(content_events.json, "loads", side_effect=ValueError("decoded")):
            with self.assertRaisesRegex(ContentError, r"^events\.json: file too large$"):
                read_events(self.root)

    def test_stream_closes_on_repeated_refusal(self):
        closed = []

        class Spy:
            def __init__(self, stream):
                self.stream = stream

            def read(self, *args):
                return self.stream.read(*args)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.stream.close()
                closed.append(1)
                return False
        self.put(b"[1]")
        with mock.patch.object(content_events, "open", lambda p, mode="r": Spy(REAL_OPEN(p, mode)), create=True), \
                mock.patch.object(content_events.json, "loads", side_effect=ValueError("x")):
            for _ in range(3):
                self.assertIsNone(read_events(self.root, []))
        self.assertEqual(len(closed), 3)

    def test_healthy_missing_and_empty_events_unchanged(self):
        before_limit = sys.get_int_max_str_digits()
        self.put([event(1, title="Café ñ 事件")])
        events = read_events(self.root)
        self.assertEqual([e["title"] for e in events], ["Café ñ 事件"])
        first = self.build()
        self.assertEqual(self.build(), first)
        self.put(b"[]")
        self.assertEqual(read_events(self.root), [])
        self.path.unlink()
        self.assertIsNone(read_events(self.root))
        self.assertEqual(sys.get_int_max_str_digits(), before_limit)
        build_site(self.root, today=TODAY)


if __name__ == "__main__":
    unittest.main()
