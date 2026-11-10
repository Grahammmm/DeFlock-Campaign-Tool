"""Synthetic per-file byte admission and snapshot consistency for kit-check."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool import kit_check
from tests.test_kit_check_suggested import run, snapshot

CAP = 1 << 20
ENTRY = {"agency_id": "ca-a-police", "name": "Synthetic", "kind": "police",
         "selected": True, "verified": True}


class BoundsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="synthetic-kit-bounds-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.primary = self.root / "kit/requests/ca-a-police.md"
        self.suggestion = self.root / "kit/requests.suggested/ca-a-police.md"
        self.primary.parent.mkdir(parents=True)
        self.suggestion.parent.mkdir()
        self.primary.write_bytes(b"clean\n")
        self.set_entries([ENTRY])
        offline = mock.patch("urllib.request.urlopen", side_effect=AssertionError("network used"))
        offline.start()
        self.addCleanup(offline.stop)

    def set_entries(self, entries):
        (self.root / "kit/agencies.json").write_text(json.dumps(entries), encoding="utf-8")

    def row(self, **kwargs):
        return kit_check.agency_report(self.root, ENTRY, **kwargs)

    def error_row(self, error):
        return {"agency_id": ENTRY["agency_id"], "recipient_verified": True,
                "draft": None, "ok": False, "error": error}

    def suggestion_error(self):
        return {"path": "kit/requests.suggested/ca-a-police.md",
                "error": "suggestion exceeds 1 MiB", "compare_command": None}

    def test_primary_and_suggestion_exact_byte_boundaries(self):
        for size in (0, CAP - 1, CAP, CAP + 1):
            for variant in ("primary", "suggestion", "pair"):
                with self.subTest(size=size, variant=variant):
                    self.primary.write_bytes(b"x" * size if variant != "suggestion" else b"clean\n")
                    self.suggestion.unlink(missing_ok=True)
                    if variant != "primary":
                        self.suggestion.write_bytes(b"x" * size)
                    row = self.row(include_suggestion=True)
                    if size > CAP:
                        if variant != "suggestion":
                            expected = self.error_row("draft exceeds 1 MiB")
                            if variant == "pair":
                                expected["suggested_differs"] = self.suggestion_error()
                            self.assertEqual(row, expected)
                        else:
                            self.assertEqual(row["suggested_differs"], self.suggestion_error())
                            self.assertFalse(row["ok"])
                    elif variant != "suggestion":
                        self.assertTrue(row["ok"])
                        self.assertNotIn("suggested_differs", row)
                    else:
                        self.assertNotIn("error", row["suggested_differs"])

    def test_multibyte_draft_limit_is_encoded_bytes(self):
        for path in (self.primary, self.suggestion):
            with self.subTest(path=path.name, directory=path.parent.name):
                path.write_bytes(("é" * (CAP // 2)).encode())
                self.assertNotIn("error", self.row(include_suggestion=True))
                path.write_bytes(("é" * (CAP // 2) + "x").encode())
                row = self.row(include_suggestion=True)
                self.assertFalse(row["ok"])
                if path == self.primary:
                    self.assertEqual(row["error"], "draft exceeds 1 MiB")
                    self.primary.write_bytes(b"clean\n")
                else:
                    self.assertEqual(row["suggested_differs"], self.suggestion_error())

    def test_fstat_oversize_reads_no_body_and_closes_fd(self):
        real_open, real_read, real_close = os.open, os.read, os.close
        for target in (self.primary, self.suggestion):
            target.write_bytes(b"x" * (CAP + 1))
            opened, reads, closed = [], [], []

            def opening(path, flags):
                fd = real_open(path, flags)
                if Path(path) == target:
                    opened.append(fd)
                return fd

            def reading(fd, size):
                if fd in opened:
                    reads.append(size)
                return real_read(fd, size)

            def closing(fd):
                if fd in opened:
                    closed.append(fd)
                return real_close(fd)

            with mock.patch("os.open", side_effect=opening), mock.patch("os.read", side_effect=reading), \
                    mock.patch("os.close", side_effect=closing), \
                    mock.patch.object(kit_check, "draft_problems", wraps=kit_check.draft_problems) as scan, \
                    mock.patch.object(kit_check, "_summary", side_effect=AssertionError("rejected summary")):
                row = self.row(include_suggestion=True)
            self.assertFalse(row["ok"])
            self.assertEqual(reads, [])
            self.assertEqual(len(opened), 1)
            self.assertEqual(closed, opened)
            self.assertEqual(scan.call_count, int(target == self.suggestion))
            target.write_bytes(b"clean\n")

    def test_growth_after_real_fstat_obeys_cap_plus_one(self):
        real_open, real_stat, real_read, real_close = os.open, os.fstat, os.read, os.close
        for target in (self.primary, self.suggestion):
            for short in (False, True):
                with self.subTest(target=target.parent.name, short=short):
                    target.write_bytes(b"small")
                    active, closed, returned, requested = set(), [], [], []

                    def opening(path, flags):
                        fd = real_open(path, flags)
                        if Path(path) == target:
                            active.add(fd)
                        return fd

                    def stating(fd):
                        result = real_stat(fd)
                        if fd in active:
                            target.write_bytes(b"x" * (CAP + 100))
                        return result

                    def reading(fd, size):
                        if fd in active:
                            requested.append(size)
                            data = real_read(fd, min(size, 7919) if short else size)
                            returned.append(len(data))
                            return data
                        return real_read(fd, size)

                    def closing(fd):
                        if fd in active:
                            closed.append(fd)
                            active.remove(fd)
                        return real_close(fd)

                    with mock.patch("os.open", side_effect=opening), mock.patch("os.fstat", side_effect=stating), \
                            mock.patch("os.read", side_effect=reading), mock.patch("os.close", side_effect=closing):
                        report = kit_check.check(self.root)
                    row = report["agencies"][0]
                    self.assertFalse(row["ok"])
                    self.assertEqual(report["problems"], 1)
                    self.assertEqual(sum(returned), CAP + 1)
                    consumed = 0
                    for request, count in zip(requested, returned):
                        self.assertLessEqual(request, CAP + 1 - consumed)
                        consumed += count
                    self.assertEqual(len(closed), 1)
                    self.assertFalse(active)
                    target.write_bytes(b"clean\n")

    def test_primary_bytes_are_read_once_and_reused_for_comparison(self):
        original = b"## Mine\n"
        fresh = b"## Fresh\n"
        self.primary.write_bytes(original)
        self.suggestion.write_bytes(fresh)
        real_open, real_read, real_close = os.open, os.read, os.close
        primary_fds, returned, opens = set(), [], []

        def opening(path, flags):
            fd = real_open(path, flags)
            if Path(path) == self.primary:
                primary_fds.add(fd)
                opens.append(fd)
            return fd

        def reading(fd, size):
            data = real_read(fd, size)
            if fd in primary_fds:
                returned.append(len(data))
            return data

        def closing(fd):
            result = real_close(fd)
            if fd in primary_fds:
                primary_fds.remove(fd)
                self.primary.unlink()
                self.primary.symlink_to(self.suggestion)
            return result

        with mock.patch("os.open", side_effect=opening), mock.patch("os.read", side_effect=reading), \
                mock.patch("os.close", side_effect=closing):
            row = self.row(include_suggestion=True)
        self.assertEqual(len(opens), 1)
        self.assertEqual(sum(returned), len(original))
        self.assertEqual(row["suggested_differs"]["scopes_added"], ["## Fresh"])
        self.assertEqual(row["suggested_differs"]["scopes_removed"], ["## Mine"])

    def test_read_failures_continue_without_private_diagnostics(self):
        later = {**ENTRY, "agency_id": "ca-b-police"}
        self.set_entries([ENTRY, ENTRY, later])
        self.primary.with_name("ca-b-police.md").write_bytes(b"clean\n")
        real_open, real_stat, real_read, real_close = os.open, os.fstat, os.read, os.close
        for seam in ("open", "fstat", "read"):
            active, opened, closed = set(), [], []

            def opening(path, flags):
                if Path(path) == self.primary and seam == "open":
                    raise PermissionError("PRIVATE-MARKER " + str(self.root))
                fd = real_open(path, flags)
                if Path(path) == self.primary:
                    active.add(fd)
                    opened.append(fd)
                return fd

            def stating(fd):
                if fd in active and seam == "fstat":
                    raise OSError("PRIVATE-MARKER")
                return real_stat(fd)

            def reading(fd, size):
                if fd in active and seam == "read":
                    raise OSError("PRIVATE-MARKER")
                return real_read(fd, size)

            def closing(fd):
                if fd in active:
                    closed.append(fd)
                    active.remove(fd)
                return real_close(fd)

            with mock.patch("os.open", side_effect=opening), mock.patch("os.fstat", side_effect=stating), \
                    mock.patch("os.read", side_effect=reading), mock.patch("os.close", side_effect=closing):
                for flags in ((), ("--json",)):
                    code, out, err = run("kit-check", "--directory", self.root, *flags)
                    self.assertEqual(code, 3)
                    if flags:
                        report = json.loads(out)
                        self.assertEqual(report["agencies"][0], self.error_row("draft unreadable"))
                        self.assertTrue(report["agencies"][1]["ok"])
                        self.assertEqual(report["problems"], 1)
                    else:
                        self.assertEqual(out, "ca-a-police: draft unreadable\nca-b-police: OK\n"
                                             "1 of 2 drafts need attention\n")
                    for private in ("PRIVATE-MARKER", str(self.root), "Traceback"):
                        self.assertNotIn(private, out + err)
            self.assertEqual(opened, closed)
            self.assertFalse(active)

    def test_primary_decode_compatibility_and_placeholder_totals(self):
        for newline in (b"\n", b"\r\n", b"\r"):
            data = newline.join([b"\xff [cap]", b"[date range]"] + [b"[gap]"] * 60)
            self.primary.write_bytes(data)
            self.suggestion.write_bytes(data)
            row = self.row(include_suggestion=True)
            self.assertEqual(row["placeholder_count"], 62)
            self.assertEqual(row["placeholders"], [{"line": 1, "placeholder": "[cap]"},
                                                   {"line": 2, "placeholder": "[date range]"}] +
                             [{"line": i, "placeholder": "[gap]"} for i in range(3, 51)])
            self.assertTrue(row["fee_cap_missing"] and row["date_range_missing"])
            self.assertNotIn("suggested_differs", row)
            self.suggestion.write_bytes(data + b"\n")
            self.assertIn("suggested_differs", self.row(include_suggestion=True))

    def test_default_gate_remains_suggestion_blind(self):
        before = self.row()
        self.suggestion.write_bytes(b"x" * (CAP + 1))
        with mock.patch.object(kit_check, "_read_suggestion", side_effect=AssertionError("suggestion opened")):
            self.assertEqual(self.row(), before)
        self.assertEqual(self.row(include_suggestion=True)["suggested_differs"], self.suggestion_error())
        self.primary.write_bytes(b"x" * (CAP + 1))
        self.assertEqual(self.row(), self.error_row("draft exceeds 1 MiB"))
        self.assertFalse(self.row(include_suggestion=True)["ok"])

    def test_oversized_primary_is_refused_by_sent_and_mail_gates(self):
        from tests.test_kit_mail import KitMailTests

        fixture = KitMailTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        fixture.verify()
        fixture.draft.write_bytes(b"Subject: Synthetic\n\n" + b"x" * CAP)
        for command in (("kit-sent", "--date", "2026-03-04", "--channel", "email"),
                        ("kit-copy", "--for", "mail")):
            before = snapshot(fixture.root)
            code, out, err = run(command[0], "--directory", fixture.root, "--agency", fixture.a, *command[1:])
            self.assertEqual(code, 1 if command[0] == "kit-sent" else 3)
            self.assertIn("run kit-check" if command[0] == "kit-sent" else "is larger than 1 MiB", err)
            self.assertNotIn("Traceback", out + err)
            self.assertEqual(snapshot(fixture.root), before)
        # Exercise the mail gate itself; its independent earlier reader also refuses large files.
        from campaign_tool import kit_copy
        from tests.test_kit_mail import TODAY
        with self.assertRaisesRegex(ValueError, "run kit-check"):
            kit_copy._mail(fixture.root, fixture.entry(), {"subject": "Synthetic", "body": "clean"}, TODAY)
        self.assertEqual(snapshot(fixture.root), before)

    def test_size_refusal_preserves_tree_and_legacy_output(self):
        self.primary.write_bytes(b"PRIVATE-MARKER" + b"x" * CAP)
        self.suggestion.write_bytes(self.primary.read_bytes())
        before = snapshot(self.root)
        with mock.patch("subprocess.run", side_effect=AssertionError("external process")), \
                mock.patch("subprocess.Popen", side_effect=AssertionError("external process")):
            for flags in ((), ("--json",)):
                code, out, err = run("kit-check", "--directory", self.root, *flags)
                self.assertEqual(code, 3)
                self.assertNotIn("PRIVATE-MARKER", out + err)
                if flags:
                    expected = self.error_row("draft exceeds 1 MiB")
                    expected["suggested_differs"] = self.suggestion_error()
                    self.assertEqual(json.loads(out), {"agencies": [expected], "problems": 1})
                else:
                    self.assertEqual(out, "ca-a-police: draft exceeds 1 MiB\nca-a-police: "
                                     "kit/requests.suggested/ca-a-police.md not compared: suggestion exceeds 1 MiB\n"
                                     "1 of 1 drafts need attention\n")
        self.assertEqual(snapshot(self.root), before)

    def test_primary_nonregular_files_keep_missing_row(self):
        self.primary.unlink()
        for kind in ("absent", "symlink", "directory", "fifo"):
            with self.subTest(kind=kind):
                if kind == "symlink":
                    self.primary.symlink_to(self.suggestion)
                elif kind == "directory":
                    self.primary.mkdir()
                elif kind == "fifo":
                    os.mkfifo(self.primary)
                self.assertEqual(self.row(), self.error_row(kit_check.MISSING))
                if kind == "directory":
                    self.primary.rmdir()
                else:
                    self.primary.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
