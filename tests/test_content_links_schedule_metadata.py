"""RF2210-2: `content link-check` refuses unsafe or inconsistent body-schedule citation metadata. Synthetic content only."""
import contextlib
import io
import json
import os
import socket
import sys
import unittest
from unittest import mock

from campaign_tool import cli, content, content_links
from tests import test_content_links as base

CAP = content.MAX_JSON_BYTES
REFUSAL = "content/body-schedules.json: cannot resolve stored citation locations"
ZED, ALPHA = "https://sched-zed.example.org/z", "https://sched-alpha.example.org/a"


def schedule(body, url, **extra):
    row = {"body": body, "weekday": "tuesday", "ordinals": [1], "local_start_time": "18:00",
           "time_zone": "America/Los_Angeles", "source_url": url, "checked_on": "2026-10-01"}
    row.update(extra)
    return row


def document(*rows):
    return {"schema_version": 1, "schedules": list(rows)}


def padded(doc, size):
    body = json.dumps(doc).encode("utf-8")
    return body + b" " * (size - len(body))


class ScheduleMetadataTests(base.LinkCheckBase):
    def setUp(self):
        super().setUp()
        self.path = self.root / "content" / "body-schedules.json"
        self.initial = document(schedule("Zed Board", ZED), schedule("Alpha Board", ALPHA))
        self.path.write_text(json.dumps(self.initial), encoding="utf-8")

    def collect_after(self, replace):
        """Run the real loader, then ``replace()`` the file before collection continues."""
        real_load = content_links.load_content

        def load_then_replace(*args, **kwargs):
            model = real_load(*args, **kwargs)
            replace()
            return model
        try:
            with mock.patch.object(content_links, "load_content", side_effect=load_then_replace):
                return content_links.collect_links(self.root, today=base.TODAY)
        finally:
            self.restore()

    def restore(self):
        if self.path.is_dir() and not self.path.is_symlink():
            self.path.rmdir()
        elif self.path.is_symlink() or self.path.exists():
            self.path.unlink()
        self.write(self.initial)

    def locators(self, groups):
        return {g["url"]: g["citations"] for g in groups}

    def assertRefused(self, replace):
        with self.assertRaises(content_links.LinkCheckError) as raised:
            self.collect_after(replace)
        self.assertEqual(str(raised.exception), REFUSAL)
        self.assertIsNone(raised.exception.__cause__)

    def write(self, data):
        self.path.write_bytes(data if isinstance(data, bytes) else json.dumps(data).encode("utf-8"))

    def test_healthy_stored_order_and_reordered_rows(self):
        groups, _ = self.collect_after(lambda: None)
        found = self.locators(groups)
        self.assertEqual(found[ZED], ["content/body-schedules.json:schedules[0].source_url"])
        self.assertEqual(found[ALPHA], ["content/body-schedules.json:schedules[1].source_url"])
        # a harmless reorder after admission is accepted and the locators follow the file now on disk
        groups, _ = self.collect_after(lambda: self.write(document(*reversed(self.initial["schedules"]))))
        found = self.locators(groups)
        self.assertEqual(found[ZED], ["content/body-schedules.json:schedules[1].source_url"])
        self.assertEqual(found[ALPHA], ["content/body-schedules.json:schedules[0].source_url"])

    def test_symlink_special_file_and_linked_directory_refuse_without_reading_payload(self):
        payload = self.root / "kit" / "payload.json"
        payload.write_text(json.dumps(self.initial), encoding="utf-8")

        def link():
            self.path.unlink()
            os.symlink(payload, self.path)

        def dangling():
            self.path.unlink()
            os.symlink(self.root / "kit" / "absent.json", self.path)

        def directory():
            self.path.unlink()
            self.path.mkdir()

        def fifo():
            self.path.unlink()
            os.mkfifo(self.path)
        replacements = [link, dangling, directory] + ([fifo] if hasattr(os, "mkfifo") else [])
        for replace in replacements:
            with self.subTest(replace.__name__):
                self.assertRefused(replace)
        linked = self.root / "linked-content"
        os.symlink(self.root / "content", linked)
        from campaign_tool import body_schedules
        rows = body_schedules.load_body_schedules(self.initial, today=base.TODAY)
        with self.assertRaises(content_links.LinkCheckError) as raised:
            content_links._stored_schedule_positions(linked, rows, base.TODAY)
        self.assertEqual(str(raised.exception), REFUSAL)

    def test_leaf_replaced_just_before_open_is_refused(self):
        payload = self.root / "kit" / "payload.json"
        payload.write_text(json.dumps(self.initial), encoding="utf-8")
        real_open = os.open
        opens = []

        def swap_then_open(path, *args, **kwargs):
            if os.fspath(path).endswith("body-schedules.json"):
                opens.append(path)
                if len(opens) == 2:  # the secondary read, after the loader's own open
                    os.unlink(path)
                    os.symlink(payload, path)
            return real_open(path, *args, **kwargs)
        try:
            with mock.patch.object(os, "open", side_effect=swap_then_open):
                with self.assertRaises(content_links.LinkCheckError) as raised:
                    content_links.collect_links(self.root, today=base.TODAY)
            self.assertEqual(str(raised.exception), REFUSAL)
        finally:
            self.restore()
        self.assertEqual(len(opens), 2)

    def test_actual_byte_budget(self):
        for size in (CAP - 1, CAP):
            groups, _ = self.collect_after(lambda size=size: self.write(padded(self.initial, size)))
            self.assertEqual(len(self.locators(groups)[ZED]), 1)
        for size in (CAP + 1, CAP * 3):
            self.assertRefused(lambda size=size: self.write(padded(self.initial, size)))
        # the bound is in bytes, not characters
        multibyte = document(schedule("Zed Board", ZED), schedule("Alpha Board", ALPHA))
        raw = json.dumps(multibyte, ensure_ascii=False).encode("utf-8") + "é".encode("utf-8") * (CAP // 2)
        self.assertRefused(lambda: self.write(raw))

    def test_read_is_bounded(self):
        reads = []
        real_fdopen = os.fdopen

        class Spy:
            def __init__(self, handle):
                self.handle = handle

            def __getattr__(self, name):
                return getattr(self.handle, name)

            def __enter__(self):
                self.handle.__enter__()
                return self

            def __exit__(self, *exc):
                return self.handle.__exit__(*exc)

            def read(self, size=-1):
                reads.append(size)
                return self.handle.read(size)

        def spy_fdopen(descriptor, mode="r", *args, **kwargs):
            handle = real_fdopen(descriptor, mode, *args, **kwargs)
            return Spy(handle) if "b" in mode else handle
        with mock.patch.object(os, "fdopen", side_effect=spy_fdopen):
            self.assertRefused(lambda: self.write(padded(self.initial, CAP * 3)))
        self.assertIn(CAP + 1, reads)
        self.assertNotIn(-1, reads)

    def test_changed_duplicate_and_invalid_metadata_refuse(self):
        rows = self.initial["schedules"]
        cases = {
            "changed url": document(schedule("Zed Board", "https://replaced.example.org/x"), rows[1]),
            "changed time": document(schedule("Zed Board", ZED, local_start_time="19:00"), rows[1]),
            "changed weekday": document(schedule("Zed Board", ZED, weekday="monday"), rows[1]),
            "changed ordinals": document(schedule("Zed Board", ZED, ordinals=[2]), rows[1]),
            "changed checked_on": document(schedule("Zed Board", ZED, checked_on="2026-10-02"), rows[1]),
            "removed row": document(rows[0]),
            "added row": document(*rows, schedule("Extra Board", "https://extra.example.org/e")),
            "duplicate body": document(rows[0], rows[1], schedule("Zed Board", ZED)),
            "unknown key": document(schedule("Zed Board", ZED, extra=1), rows[1]),
            "wrong version": {"schema_version": 2, "schedules": rows},
            "no schedules key": {"schema_version": 1},
            "schedules not list": {"schema_version": 1, "schedules": "x"},
        }
        for label, doc in cases.items():
            with self.subTest(label):
                self.assertRefused(lambda doc=doc: self.write(doc))
                self.write(self.initial)

    def test_invalid_decode_and_parser_failures_are_content_free(self):
        deep = ("[" * 100000 + "]" * 100000).encode()
        for label, raw in {"bad utf8": b"\xff\xfe" + SENTINEL_BYTES, "malformed": b'{"schedules": ' + SENTINEL_BYTES,
                           "null": b"null", "list": b"[]", "string": b'"x"', "deep": deep,
                           "int digits": b'{"schema_version": 1, "schedules": ' + b"9" * 5000 + b"}"}.items():
            with self.subTest(label):
                with self.assertRaises(content_links.LinkCheckError) as raised:
                    self.collect_after(lambda raw=raw: self.write(raw))
                self.assertEqual(str(raised.exception), REFUSAL)
                self.assertNotIn(base.SENTINEL, repr(raised.exception.args))
                self.write(self.initial)

    def test_empty_or_absent_schedules_perform_no_secondary_read(self):
        real_open = os.open
        opens = []

        def counting_open(path, *args, **kwargs):
            if os.fspath(path).endswith("body-schedules.json"):
                opens.append(path)
            return real_open(path, *args, **kwargs)

        def schedule_citations(groups):
            return [c for g in groups for c in g["citations"] if "body-schedules" in c]
        self.path.unlink()
        with mock.patch.object(os, "open", side_effect=counting_open):
            groups, _ = content_links.collect_links(self.root, today=base.TODAY)
            self.assertEqual((opens, schedule_citations(groups)), ([], []))
            self.write(document())
            groups, _ = content_links.collect_links(self.root, today=base.TODAY)
            self.assertEqual((len(opens), schedule_citations(groups)), (1, []))  # the loader's own read only
            self.path.unlink()
            del opens[:]
            groups, _ = self.collect_after(lambda: self.write(self.initial))  # arrives after an empty model
            self.assertEqual(schedule_citations(groups), [])
            self.assertEqual(len(opens), 0)

    def test_refusal_cli_and_fake_online_make_no_request_and_change_nothing(self):
        real_load = content_links.load_content

        def load_then_change(*args, **kwargs):
            model = real_load(*args, **kwargs)
            self.write(document(schedule("Zed Board", "https://replaced.example.org/x"), self.initial["schedules"][1]))
            return model
        opener = base.FakeOpener({})
        with mock.patch.object(content_links, "load_content", side_effect=load_then_change), \
                mock.patch.object(content_links, "real_opener", side_effect=AssertionError("opener built")), \
                self.assertRaises(content_links.LinkCheckError):
            content_links.run_cli(self.root, True, opener=opener, today=base.TODAY)
        self.assertEqual(opener.requests, [])
        self.restore()
        before = base.snapshot(self.root)
        modes = {p: os.lstat(p).st_mode for p in self.root.rglob("*")}
        out, err = io.StringIO(), io.StringIO()
        argv = ["campaign", "content", "link-check", "--online", "--directory", str(self.root)]
        with mock.patch.object(content_links, "load_content", side_effect=load_then_change), \
                mock.patch.object(content_links, "real_opener", side_effect=AssertionError("opener built")), \
                mock.patch.object(socket.socket, "connect", side_effect=AssertionError("socket")), \
                mock.patch.object(socket, "getaddrinfo", side_effect=AssertionError("dns")), \
                mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                self.assertRaises(SystemExit) as raised:
            cli.main()
        self.assertEqual(raised.exception.code, 1)
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(err.getvalue().strip(), "Stopped: " + REFUSAL)
        self.assertNotIn("Traceback", err.getvalue())
        after = base.snapshot(self.root)
        self.assertEqual(sorted(before), sorted(after))
        self.assertEqual({k: v for k, v in after.items() if "body-schedules" not in k},
                         {k: v for k, v in before.items() if "body-schedules" not in k})
        self.assertEqual(modes, {p: os.lstat(p).st_mode for p in self.root.rglob("*")})

    def test_healthy_fake_online_controls_keep_exit_codes(self):
        self.write(self.initial)
        opener = base.FakeOpener({})
        content_links.run_cli(self.root, True, opener=opener, today=base.TODAY)
        fetched = [url for _, url, _, _ in opener.requests]
        self.assertEqual(fetched.count(ZED), 1)
        self.assertEqual(fetched.count(ALPHA), 1)


SENTINEL_BYTES = base.SENTINEL.encode()

if __name__ == "__main__":
    unittest.main()
