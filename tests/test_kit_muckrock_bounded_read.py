"""kit-muckrock-import reads at most MAX_BYTES + 1 bytes of the saved JSON, so a file that grows after the size check is
refused before decoding, parsing or kit/sent.json. Offline, synthetic: no socket may be opened."""
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import kit_muckrock, kit_track  # noqa: E402
from tests import test_kit_muckrock as base  # noqa: E402

CAP = kit_muckrock.MAX_BYTES
REAL_OPEN = open


class Growing:
    """Stands in for the reader's `open`: grows the real file to `grow_to` bytes with JSON whitespace (after every
    size check has passed), then hands back a forwarding stream that records every read request and its result."""

    def __init__(self, grow_to=None, read_error=None):
        self.grow_to, self.read_error = grow_to, read_error
        self.requests, self.returned, self.closed, self.opens = [], [], 0, 0

    def __call__(self, file, mode="r", *args, **kwargs):
        self.opens += 1
        if self.grow_to is not None:
            size = Path(file).stat().st_size
            if size < self.grow_to:
                with REAL_OPEN(file, "ab") as extra:
                    extra.write(b" " * (self.grow_to - size))
        outer, real = self, REAL_OPEN(file, mode, *args, **kwargs)

        class Stream:
            def read(self, size=-1):
                outer.requests.append(size)
                if outer.read_error:
                    raise outer.read_error
                data = real.read(size)
                outer.returned.append(len(data))
                return data

            def close(self):
                outer.closed += 1
                real.close()

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.close()

        return Stream()


class BoundedReadTests(base.KitMuckrockTests):
    # Re-running the inherited tests here is wasteful; keep only the helpers (setUp, saved, filed, imp, steps).
    for _name in [n for n in dir(base.KitMuckrockTests) if n.startswith("test_")]:
        locals()[_name] = None
    del _name

    def padded(self, size, **fields):  # the saved fixture as exactly `size` bytes of JSON plus trailing whitespace
        path = self.saved(**fields)
        raw = path.read_bytes()
        self.assertLessEqual(len(raw), size)
        path.write_bytes(raw + b" " * (size - len(raw)))
        return path

    def tree(self):
        return {str(p.relative_to(self.root)): (p.stat().st_mode, p.stat().st_mtime_ns,
                                                 p.read_bytes() if p.is_file() else None)
                for p in sorted(self.root.rglob("*"))}

    def grow(self, size=None, **kwargs):
        return mock.patch.object(kit_muckrock, "open", Growing(size, **kwargs), create=True)

    def test_growth_after_the_size_check_refuses_before_decode_json_or_history(self):
        path = self.saved(status="processed")
        self.filed()
        with self.grow(CAP + 1), mock.patch.object(kit_muckrock.json, "loads", side_effect=AssertionError("parsed")), \
                mock.patch.object(kit_track, "read_sent", side_effect=AssertionError("history read")), \
                mock.patch.object(kit_track, "record_step", side_effect=AssertionError("recorded")):
            with self.assertRaises(ValueError) as caught:
                kit_muckrock._load(path)
            self.assertEqual(str(caught.exception), f"{path}: larger than 1 MiB")
            with self.assertRaises(ValueError) as caught:
                kit_muckrock.propose(self.root, self.agency, path)
            self.assertEqual(str(caught.exception), f"{path}: larger than 1 MiB")
            with self.assertRaises(ValueError):
                kit_muckrock.run(self.root, self.agency, path, apply=True)

    def test_binary_read_asks_for_at_most_cap_plus_one_and_always_closes(self):
        path = self.saved()
        stream = Growing(CAP + 5 * (1 << 20))  # far larger than the cap: an unlimited read would return it all
        with mock.patch.object(kit_muckrock, "open", stream, create=True):
            with self.assertRaises(ValueError):
                kit_muckrock._load(path)
        self.assertEqual((stream.opens, stream.requests, stream.returned, stream.closed),
                         (1, [CAP + 1], [CAP + 1], 1))
        small = Growing()  # normal return
        with mock.patch.object(kit_muckrock, "open", small, create=True):
            self.assertEqual(kit_muckrock._load(self.saved())["id"], 90001)
        self.assertEqual((small.opens, small.requests, small.closed), (1, [CAP + 1], 1))
        for label, body in (("not utf-8", b"\xff\xfe"), ("broken json", b"{")):
            with self.subTest(label):
                bad = self.tmp / "bad.json"
                bad.write_bytes(body)
                stream = Growing()
                with mock.patch.object(kit_muckrock, "open", stream, create=True):
                    with self.assertRaisesRegex(ValueError, "not UTF-8 JSON"):
                        kit_muckrock._load(bad)
                self.assertEqual((stream.opens, stream.closed), (1, 1))
        stream = Growing(read_error=OSError("synthetic read failure"))
        with mock.patch.object(kit_muckrock, "open", stream, create=True):
            with self.assertRaisesRegex(OSError, "synthetic read failure"):
                kit_muckrock._load(self.saved())
        self.assertEqual((stream.opens, stream.requests, stream.closed), (1, [CAP + 1], 1))

    def test_exact_utf8_byte_boundaries(self):
        for size in (CAP - 1, CAP):
            with self.subTest(size=size):
                self.assertEqual(kit_muckrock._load(self.padded(size))["id"], 90001)
        multibyte = self.tmp / "multibyte.json"  # few characters, over the cap in encoded bytes
        text = json.dumps({"id": 90001, "status": "processed", "pad": "é" * (CAP // 2)}, ensure_ascii=False)
        self.assertLess(len(text), CAP)
        multibyte.write_bytes(text.encode("utf-8"))
        self.assertGreater(multibyte.stat().st_size, CAP)
        with self.assertRaisesRegex(ValueError, "larger than 1 MiB"):
            kit_muckrock._load(multibyte)
        edge = self.tmp / "edge.json"  # exactly the cap in bytes with multibyte content is admitted
        body = json.dumps({"id": 90001, "status": "processed", "pad": ""}, ensure_ascii=False).encode("utf-8")
        pad = CAP - len(body)
        edge.write_bytes(json.dumps({"id": 90001, "status": "processed", "pad": "é" * (pad // 2)},
                                    ensure_ascii=False).encode("utf-8") + b" " * (pad % 2))
        self.assertEqual(edge.stat().st_size, CAP)
        self.assertEqual(kit_muckrock._load(edge)["status"], "processed")
        with self.assertRaisesRegex(ValueError, "larger than 1 MiB"):  # statically over the cap
            kit_muckrock._load(self.padded_over())
        with self.grow(CAP + 1):  # exactly the cap at the stat, one byte more at the read
            with self.assertRaisesRegex(ValueError, "larger than 1 MiB"):
                kit_muckrock._load(self.padded(CAP))
        for label, body in (("empty", b""), ("not utf-8", b"\xff"), ("broken", b"{"), ("array", b"[1]")):
            with self.subTest(control=label):
                path = self.tmp / "control.json"
                path.write_bytes(body)
                with self.assertRaises(ValueError) as caught:
                    kit_muckrock._load(path)
                self.assertNotIn("1 MiB", str(caught.exception))

    def padded_over(self):
        path = self.padded(CAP)
        with REAL_OPEN(path, "ab") as extra:
            extra.write(b" ")
        return path

    def test_all_cli_modes_refuse_grown_json_without_partial_output_or_write(self):
        for apply in (False, True):
            for as_json in (False, True):
                for stored in (True, False):
                    with self.subTest(apply=apply, json=as_json, stored=stored):
                        if stored:
                            self.filed()
                        elif self.sent.exists():
                            self.sent.unlink()
                        path = self.saved(status="processed")
                        before = self.tree()
                        with self.grow(CAP + 1), \
                                mock.patch.object(kit_track, "read_sent", side_effect=AssertionError("history read")), \
                                mock.patch.object(kit_track, "record_step", side_effect=AssertionError("recorded")):
                            code, out, err = self.imp(path, *(["--apply"] * apply), *(["--json"] * as_json))
                        self.assertEqual((code, out), (1, ""))
                        self.assertIn(f"Stopped: {path}: larger than 1 MiB", err)
                        self.assertNotIn("Traceback", err)
                        self.assertEqual(self.tree(), before)
                        self.assertEqual(self.sent.exists(), stored)

    def test_no_step_and_duplicate_paths_do_not_bypass_byte_admission(self):
        self.filed()
        waiting = self.saved(status="started")
        self.assertIsNone(kit_muckrock.propose(self.root, self.agency, waiting)["step"])  # small: ordinary no-op
        self.assertEqual(self.imp(self.saved(status="processed"), "--apply")[0], 0)
        repeat = self.saved(status="processed")
        self.assertEqual(kit_muckrock.propose(self.root, self.agency, repeat)["reason"], "already recorded")
        for label, path in (("waiting", waiting), ("duplicate", repeat)):
            with self.subTest(label), self.grow(CAP + 1), \
                    mock.patch.object(kit_track, "read_sent", side_effect=AssertionError("history read")):
                with self.assertRaisesRegex(ValueError, "larger than 1 MiB"):
                    kit_muckrock.propose(self.root, self.agency, path)
                code, out, err = self.imp(path, "--apply")
                self.assertEqual((code, out), (1, ""))
                self.assertIn("larger than 1 MiB", err)

    def test_normal_apply_appends_once_and_replay_is_a_noop(self):
        self.filed()
        path = self.saved(status="processed")
        before = self.sent.read_bytes()
        code, out, _ = self.imp(path)
        self.assertEqual(code, 0)
        self.assertIn("Dry run: nothing written", out)
        self.assertEqual(self.sent.read_bytes(), before)
        self.assertEqual(self.imp(path, "--apply")[0], 0)
        self.assertEqual([s["kind"] for s in self.steps()], ["acknowledged"])
        code, out, _ = self.imp(path, "--apply")
        self.assertEqual(code, 0)
        self.assertIn("already recorded", out)
        self.assertEqual(len(self.steps()), 1)
        link = self.tmp / "link.json"
        link.symlink_to(path)
        for label, target in (("symlink", link), ("dangling", self.tmp / "gone-link.json"), ("directory", self.tmp)):
            if label == "dangling":
                target.symlink_to(self.tmp / "nowhere.json")
            with self.subTest(label), self.assertRaisesRegex(ValueError, "not a regular file"):
                kit_muckrock._load(target)


if __name__ == "__main__":
    unittest.main()
