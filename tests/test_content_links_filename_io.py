"""RF0115-3: `content link-check` refuses unsafe or unresolvable filename metadata instead of guessing. Synthetic content only."""
import contextlib
import io
import json
import os
import re
import shutil
import socket
import sys
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool import cli, content, content_links
from tests.test_content_links import FakeOpener, LinkCheckBase, SENTINEL, TODAY, meeting_document

CAP = content.MAX_JSON_BYTES
MEETING_ID = "cedar-council-2026-10"
FINDING_ID = "fnd_0000000000000001"
MEETING_FILE = "2026-10-cedar-council.json"
FINDING_FILE = "cedar-policy-posted.json"
PRIVATE = "filename-metadata-private.json"


def padded(document, size):
    """UTF-8 JSON of ``document`` followed by whitespace so the file is exactly ``size`` bytes."""
    body = json.dumps(document).encode("utf-8")
    assert len(body) <= size
    return body + b" " * (size - len(body))


class FilenameBase(LinkCheckBase):
    def setUp(self):
        super().setUp()
        self.content = self.root / "content"
        self.meetings = self.content / "meetings"
        self.findings = self.content / "findings"
        self.paths = {"meetings": self.meetings / MEETING_FILE, "findings": self.findings / FINDING_FILE}
        self.ids = {"meetings": MEETING_ID, "findings": FINDING_ID}
        (self.root / "kit" / PRIVATE).write_text(json.dumps({"id": "private", "marker": SENTINEL}), encoding="utf-8")
        self.saved = {folder: {p.name: p.read_bytes() for p in (self.content / folder).iterdir()} for folder in self.paths}

    def restorer(self, folder):
        """Return a callable that puts content/<folder> back exactly as setUp left it, whatever a test did to it."""
        def restore():
            directory = self.content / folder
            if directory.is_symlink():
                directory.unlink()
            elif directory.exists():
                shutil.rmtree(directory)
            shutil.rmtree(directory.with_name(folder + "-real"), ignore_errors=True)
            directory.mkdir()
            for name, data in self.saved[folder].items():
                (directory / name).write_bytes(data)
        return restore

    def refusal(self, folder):
        return f"content/{folder}: cannot resolve stored citation locations"

    def run_refused(self, folder, change, *, injected, patches=()):
        """Real load_content, then ``change()``, then run_cli; returns the opener and the recorded os.open paths."""
        real_load = content_links.load_content
        opened = []
        real_open = os.open

        def recording_open(path, *args, **kwargs):
            opened.append(str(path))
            return real_open(path, *args, **kwargs)

        def load_then_change(*args, **kwargs):
            model = real_load(*args, **kwargs)
            change()
            return model

        opener = FakeOpener({})
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(content_links, "load_content", side_effect=load_then_change))
            factory = stack.enter_context(mock.patch.object(content_links, "real_opener"))
            stack.enter_context(mock.patch.object(content_links.os, "open", side_effect=recording_open))
            stack.enter_context(mock.patch.object(socket.socket, "connect", side_effect=AssertionError("socket")))
            stack.enter_context(mock.patch.object(socket, "getaddrinfo", side_effect=AssertionError("dns")))
            for patch in patches:
                stack.enter_context(patch)
            with self.assertRaises(content_links.LinkCheckError) as raised:
                content_links.run_cli(self.root, True, opener=opener if injected else None, today=TODAY)
            factory.assert_not_called()
        self.assertEqual(str(raised.exception), self.refusal(folder))
        self.assertIsNone(raised.exception.__cause__)
        self.assertEqual(opener.requests, [])
        return opened

    def assert_refused(self, folder, change, restore, *, patches=()):
        """Both direct offline and injected-online runs must refuse; ``restore`` undoes ``change`` after each run."""
        for injected in (False, True):
            try:
                opened = self.run_refused(folder, change, injected=injected, patches=patches)
            finally:
                restore()
        return opened


class SymlinkAndFolderTests(FilenameBase):
    def test_metadata_symlink_replacement_refuses_before_payload_or_opener(self):
        for folder in ("meetings", "findings"):
            with self.subTest(folder=folder, kind="leaf"):
                path = self.paths[folder]

                def change():
                    path.unlink()
                    path.symlink_to(self.root / "kit" / PRIVATE)
                opened = self.assert_refused(folder, change, self.restorer(folder))
                self.assertNotIn(str(path), opened)  # the link was refused before it was opened
                self.assertFalse([p for p in opened if PRIVATE in p])

            with self.subTest(folder=folder, kind="linked folder"):
                directory = path.parent
                moved = directory.with_name(folder + "-real")

                def change_folder():
                    directory.rename(moved)
                    directory.symlink_to(moved, target_is_directory=True)
                opened = self.assert_refused(folder, change_folder, self.restorer(folder))
                self.assertFalse([p for p in opened if f"/{folder}/" in p])  # nothing inside the linked folder was opened

    def test_linked_content_directory_is_refused_without_reading(self):
        moved = self.root / "content-real"
        self.content.rename(moved)
        self.content.symlink_to(moved, target_is_directory=True)
        self.addCleanup(lambda: (self.content.unlink(), moved.rename(self.content)))
        with mock.patch.object(content_links.os, "open", side_effect=AssertionError("opened")):
            with self.assertRaises(content_links.LinkCheckError) as raised:
                content_links._file_names(self.content, "meetings", {MEETING_ID}, stem_ids=True)
        self.assertEqual(str(raised.exception), self.refusal("meetings"))

    def test_empty_expected_collection_performs_no_reread(self):
        (self.findings / "broken.json").write_bytes(b"\xff not json")
        with mock.patch.object(content_links.os, "open", side_effect=AssertionError("opened")), \
                mock.patch.object(content_links.os, "listdir", side_effect=AssertionError("listed")):
            self.assertEqual(content_links._file_names(self.content, "findings", set()), {})
            self.assertEqual(content_links._file_names(self.content, "missing-folder", []), {})


class ByteBudgetTests(FilenameBase):
    def recording_fdopen(self, log, fail_read=False):
        real = os.fdopen

        class Handle:
            def __init__(self, inner):
                self.inner, self.closed = inner, False

            def read(self, size=-1):
                if fail_read:
                    raise OSError("synthetic read failure")
                data = self.inner.read(size)
                log.append((size, len(data)))
                return data

            def close(self):
                self.closed = True
                self.inner.close()

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.close()

        handles = []

        def wrapper(descriptor, mode="r", *args, **kwargs):
            handle = Handle(real(descriptor, mode, *args, **kwargs))
            handles.append(handle)
            return handle
        wrapper.handles = handles
        return wrapper

    def test_metadata_read_budget_and_actual_utf8_boundary(self):
        for folder in ("meetings", "findings"):
            path = self.paths[folder]
            original = path.read_bytes()
            document = {"id": self.ids[folder], "note": "é" * 8}  # multibyte text
            for size, ok in ((CAP - 1, True), (CAP, True), (CAP + 1, False), (CAP * 3, False)):
                with self.subTest(folder=folder, size=size):
                    log = []
                    wrapper = self.recording_fdopen(log)
                    path.write_bytes(padded(document, size))
                    try:
                        with mock.patch.object(content_links.os, "fdopen", side_effect=wrapper):
                            if ok:
                                names = content_links._file_names(self.content, folder, {self.ids[folder]}, stem_ids=True)
                                self.assertEqual(names, {self.ids[folder]: path.name})
                            else:
                                with self.assertRaises(content_links.LinkCheckError):
                                    content_links._file_names(self.content, folder, {self.ids[folder]}, stem_ids=True)
                    finally:
                        path.write_bytes(original)
                    self.assertEqual(log, [(CAP + 1, min(size, CAP + 1))])  # bounded request, actual length checked
                    self.assertTrue(all(handle.closed for handle in wrapper.handles))

    def test_multibyte_characters_count_as_bytes_not_characters(self):
        path = self.paths["findings"]
        original = path.read_bytes()
        text = "é" * ((CAP - 80) // 2)  # fewer than CAP characters but more than CAP bytes once padded below
        body = json.dumps({"id": FINDING_ID, "note": text}, ensure_ascii=False).encode("utf-8")
        body += b" " * (CAP + 1 - len(body))
        self.assertEqual(len(body), CAP + 1)
        self.assertLess(len(body.decode("utf-8")), CAP)
        path.write_bytes(body)
        self.addCleanup(path.write_bytes, original)
        with self.assertRaises(content_links.LinkCheckError):
            content_links._file_names(self.content, "findings", {FINDING_ID})

    def test_handles_close_on_decode_parse_and_read_failures(self):
        path = self.paths["findings"]
        original = path.read_bytes()
        self.addCleanup(path.write_bytes, original)
        for label, payload, fail in (("decode", b"\xff\xfe", False), ("parse", b'{"id": ', False),
                                     ("read", original, True)):
            with self.subTest(label=label):
                path.write_bytes(payload)
                wrapper = self.recording_fdopen([], fail_read=fail)
                with mock.patch.object(content_links.os, "fdopen", side_effect=wrapper):
                    with self.assertRaises(content_links.LinkCheckError):
                        content_links._file_names(self.content, "findings", {FINDING_ID})
                self.assertEqual(len(wrapper.handles), 1)
                self.assertTrue(wrapper.handles[0].closed)

    def test_oversize_replacement_refuses_via_command(self):
        for folder in ("meetings", "findings"):
            path = self.paths[folder]

            def change():
                path.write_bytes(padded({"id": self.ids[folder]}, CAP + 1))
            self.assert_refused(folder, change, self.restorer(folder))


class UnresolvableTests(FilenameBase):
    def test_metadata_missing_malformed_or_nonregular_never_invents_filename(self):
        changes = {
            "missing": lambda p: p.unlink(),
            "bad-utf8": lambda p: p.write_bytes(b'{"id": "\xff"}'),
            "malformed": lambda p: p.write_text('{"id": ' + SENTINEL, encoding="utf-8"),
            "deep": lambda p: p.write_bytes(b'{"id": "x", "a": ' + b"[" * 200000 + b"]" * 200000 + b"}"),
            "null": lambda p: p.write_text("null", encoding="utf-8"),
            "list": lambda p: p.write_text("[]", encoding="utf-8"),
            "string": lambda p: p.write_text('"text"', encoding="utf-8"),
            "directory": lambda p: (p.unlink(), p.mkdir()),
            "dangling": lambda p: (p.unlink(), p.symlink_to(self.root / "kit" / "no-such-file.json")),
            "fifo": lambda p: (p.unlink(), os.mkfifo(p)),
        }
        if os.geteuid() != 0:
            changes["unreadable"] = lambda p: p.chmod(0)
        for folder in ("meetings", "findings"):
            path = self.paths[folder]
            for name, apply in changes.items():
                with self.subTest(folder=folder, change=name):
                    self.assert_refused(folder, lambda: apply(path), self.restorer(folder))

    def test_metadata_identity_mapping_is_complete_and_unique(self):
        for folder in ("meetings", "findings"):
            path = self.paths[folder]
            directory = path.parent
            original = path.read_bytes()
            document = json.loads(original)
            extra = directory / "zz-extra.json"
            cases = {
                "changed": lambda: path.write_text(json.dumps({**document, "id": "someone-else"}), encoding="utf-8"),
                "missing": lambda: path.write_text(json.dumps({k: v for k, v in document.items() if k != "id"}),
                                                   encoding="utf-8") if folder == "findings" else None,
                "number": lambda: path.write_text(json.dumps({**document, "id": 5}), encoding="utf-8"),
                "null": lambda: path.write_text(json.dumps({**document, "id": None}), encoding="utf-8"),
                "empty": lambda: path.write_text(json.dumps({**document, "id": ""}), encoding="utf-8"),
                "duplicate": lambda: extra.write_bytes(original),
                "additional": lambda: extra.write_text(json.dumps({**document, "id": "extra-id"}), encoding="utf-8"),
            }
            for name, apply in cases.items():
                if folder == "meetings" and name == "missing":
                    continue  # a meeting without an id legitimately uses its filename stem (tested below)
                with self.subTest(folder=folder, change=name):
                    self.assert_refused(folder, apply, self.restorer(folder))

    def test_meeting_without_id_uses_its_filename_stem_but_null_id_does_not(self):
        self.assertEqual(content_links._file_names(self.content, "meetings", {MEETING_ID}, stem_ids=True),
                         {MEETING_ID: MEETING_FILE})
        document = meeting_document()
        del document["id"]
        (self.meetings / MEETING_FILE).unlink()
        (self.meetings / "stem-named-meeting.json").write_text(json.dumps(document), encoding="utf-8")
        text, code = content_links.run_cli(self.root, today=TODAY)
        self.assertEqual(code, 0)
        self.assertIn("cited by content/meetings/stem-named-meeting.json:agenda_url", text)
        for explicit_null in ("null", '""'):
            (self.meetings / "stem-named-meeting.json").write_text(
                json.dumps(document).replace('"body"', '"id": %s, "body"' % explicit_null, 1), encoding="utf-8")
            with self.assertRaises(content_links.LinkCheckError):
                content_links._file_names(self.content, "meetings", {"stem-named-meeting"}, stem_ids=True)

    def test_findings_require_their_own_id(self):
        document = json.loads(self.paths["findings"].read_text(encoding="utf-8"))
        del document["id"]
        self.paths["findings"].write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaises(content_links.LinkCheckError):
            content_links._file_names(self.content, "findings", {"cedar-policy-posted"})


class NormalOutputTests(FilenameBase):
    def add_documents(self):
        """Second/third meetings and a second finding with filenames unrelated to (and sorted against) their ids."""
        later = meeting_document()
        later.update({"id": "alpha-hearing", "starts_at": "2026-11-18T18:00:00-08:00", "agenda_url": "https://council.example.org/agenda-two",
                      "comment_kit_md": "Read [the packet](https://council.example.org/packet#p2) and [again](https://council.example.org/packet#p2)."})
        del later["outcome"], later["participation"]
        (self.meetings / "zz-first-file.json").write_text(json.dumps(later), encoding="utf-8")
        stemmed = dict(later)
        del stemmed["id"]
        stemmed.update({"starts_at": "2026-12-02T18:00:00-08:00", "agenda_url": "https://council.example.org/agenda-three"})
        (self.meetings / "a-stem-id.json").write_text(json.dumps(stemmed), encoding="utf-8")
        finding = json.loads(self.paths["findings"].read_text(encoding="utf-8"))
        finding.update({"id": "fnd_0000000000000000", "slug": "second-finding", "published_at": "2026-02-02T18:00:00+00:00",
                        "body_md": "See [one](https://finding.example.org/one) and [one again](https://finding.example.org/one)."})
        (self.findings / "000-renamed.json").write_text(json.dumps(finding), encoding="utf-8")

    def test_both_collection_locators_round_trip_and_normal_output_is_unchanged(self):
        self.add_documents()
        text, code = content_links.run_cli(self.root, today=TODAY)
        self.assertEqual(code, 0)
        self.assertEqual(text, content_links.run_cli(self.root, today=TODAY)[0])
        groups = self.groups()
        seen = set()
        for group in groups:
            for locator in group["citations"]:
                found = re.fullmatch(r"content/(meetings|findings)/([^:]+\.json):(\w+)(?:\[anchor (\d+)\])?", locator)
                if not found:
                    continue
                folder, name, field, anchor = found.groups()
                seen.add((folder, name))
                value = json.loads((self.content / folder / name).read_text(encoding="utf-8"))[field]
                if anchor is None:
                    self.assertEqual(value, group["url"])
                else:
                    self.assertIn(group["url"].split("#")[0], value)
        self.assertEqual(seen, {("meetings", MEETING_FILE), ("meetings", "zz-first-file.json"), ("meetings", "a-stem-id.json"),
                                ("findings", "000-renamed.json")})
        by_url = {g["url"]: g for g in groups}
        self.assertEqual(by_url["https://finding.example.org/one"]["citations"],
                         ["content/findings/000-renamed.json:body_md[anchor 1]", "content/findings/000-renamed.json:body_md[anchor 2]"])
        self.assertEqual(by_url["https://council.example.org/packet#p2"]["citations"],
                         [f"content/meetings/{MEETING_FILE}:comment_kit_md[anchor 1]",
                          "content/meetings/a-stem-id.json:comment_kit_md[anchor 1]",
                          "content/meetings/a-stem-id.json:comment_kit_md[anchor 2]",
                          "content/meetings/zz-first-file.json:comment_kit_md[anchor 1]",
                          "content/meetings/zz-first-file.json:comment_kit_md[anchor 2]"])
        self.assertEqual(by_url["https://council.example.org/agenda-three"]["citations"],
                         ["content/meetings/a-stem-id.json:agenda_url"])
        self.assertEqual(by_url["https://council.example.org/agenda-two"]["citations"],
                         ["content/meetings/zz-first-file.json:agenda_url"])
        self.assertEqual(by_url["https://council.example.org/agenda"]["citations"], [f"content/meetings/{MEETING_FILE}:agenda_url"])


class RefusalCommandTests(FilenameBase):
    def state(self):
        out = {}
        for path in sorted(self.root.rglob("*")):
            info = path.lstat()
            out[str(path.relative_to(self.root))] = (info.st_mode, info.st_mtime_ns,
                                                     path.read_bytes() if path.is_file() and not path.is_symlink() else None)
        return out

    def test_metadata_refusal_cli_and_injected_online_are_read_only(self):
        path = self.paths["meetings"]
        original = path.read_bytes()
        real_load = content_links.load_content

        def load_then_corrupt(*args, **kwargs):
            model = real_load(*args, **kwargs)
            path.write_text('{"id": ' + SENTINEL, encoding="utf-8")
            return model
        os.utime(path, ns=(1_000_000_000, 1_000_000_000))
        out, err = io.StringIO(), io.StringIO()
        argv = ["campaign", "content", "link-check", "--online", "--directory", str(self.root)]
        with mock.patch.object(content_links, "load_content", side_effect=load_then_corrupt), \
                mock.patch.object(content_links, "real_opener", side_effect=AssertionError("opener built")), \
                mock.patch.object(socket.socket, "connect", side_effect=AssertionError("socket")), \
                mock.patch.object(socket, "getaddrinfo", side_effect=AssertionError("dns")), \
                mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                self.assertRaises(SystemExit) as raised:
            cli.main()
        self.assertEqual(raised.exception.code, 1)  # a refusal, not an availability result (exit 3)
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(err.getvalue().strip(), "Stopped: " + self.refusal("meetings"))
        self.assertNotIn("Traceback", err.getvalue())
        self.assertNotIn(SENTINEL, err.getvalue())
        # production never changed the corrupted bytes, the sentinel kit file or any other file
        after_refusal = self.state()
        self.assertEqual(path.read_text(encoding="utf-8"), '{"id": ' + SENTINEL)
        path.write_bytes(original)
        os.utime(path, ns=(1_000_000_000, 1_000_000_000))
        before_normal = self.state()
        self.assertEqual({k: v for k, v in after_refusal.items() if not k.endswith(MEETING_FILE)},
                         {k: v for k, v in before_normal.items() if not k.endswith(MEETING_FILE)})
        opener = FakeOpener({"https://council.example.org/agenda": {"status": 404}})
        text, code = content_links.run_cli(self.root, True, opener=opener, today=TODAY)
        self.assertEqual(code, 3)  # normal injected-online behaviour is preserved
        self.assertEqual(len(opener.requests), len({g["url"].split("#")[0] for g in self.groups()}))
        self.assertIn(f"cited by content/meetings/{MEETING_FILE}:agenda_url", text)
        self.assertEqual(self.state(), before_normal)


if __name__ == "__main__":
    unittest.main()
