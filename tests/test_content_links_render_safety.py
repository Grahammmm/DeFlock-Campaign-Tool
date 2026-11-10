"""RF0645-5: `content link-check` escapes stored citation locators at the text-render boundary. Synthetic content only."""
import contextlib
import copy
import io
import json
import os
import re
import socket
import sys
import unittest
from unittest import mock

from campaign_tool import cli, content_links
from tests import test_content_links as _base  # module import: do not re-export its TestCase classes into discovery

HOSTILE = "notice\x1b[2J\n  result: ok (FORGED)\r\t\u202e"
HOSTILE_ESCAPED = "notice\\x1b[2J\\x0a  result: ok (FORGED)\\x0d\\x09\\u202e"
MEETING_FILE = "2026-10-cedar-council.json"
FINDING_FILE = "cedar-policy-posted.json"
AGENDA = "https://council.example.org/agenda"
FINDING_BODY = "See [one](https://finding.example.org/one) and [one again](https://finding.example.org/one)."


def tree_state(root):
    """Entry membership, bytes, modes and mtimes of everything under ``root``."""
    state = {}
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        state[str(path.relative_to(root))] = (path.read_bytes() if path.is_file() else None, info.st_mode, info.st_mtime_ns)
    return state


def expected_escape(text):
    """Independent oracle: C0/DEL/C1/format/separator/surrogate code points as literal \\xNN or \\uNNNN."""
    return "".join(c if c.isprintable() else ("\\x%02x" % ord(c) if ord(c) < 256 else "\\u%04x" % ord(c)) for c in text)


def group(locators, archives=(), url="https://county.example.org/policy.pdf"):
    return {"url": url, "citations": list(locators), "archives": [dict(a) for a in archives]}


def lines_of(text):
    return text.split("\n")


class SafetyBase(_base.LinkCheckBase):
    def setUp(self):
        super().setUp()
        self.content = self.root / "content"
        for patcher in (mock.patch.object(socket.socket, "connect", side_effect=AssertionError("socket")),
                        mock.patch.object(socket, "getaddrinfo", side_effect=AssertionError("dns"))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def rename(self, folder, old, new):
        (self.content / folder / old).rename(self.content / folder / new)

    def set_finding_body(self):
        path = self.content / "findings" / FINDING_FILE
        document = json.loads(path.read_text(encoding="utf-8"))
        document["body_md"] = FINDING_BODY
        path.write_text(json.dumps(document), encoding="utf-8")

    def hostile_tree(self, meeting=HOSTILE + ".json", finding=""):
        self.rename("meetings", MEETING_FILE, meeting)
        if finding:
            self.set_finding_body()
            self.rename("findings", FINDING_FILE, finding)
        return meeting, finding

    def assert_clean(self, text):
        self.assertFalse(re.search(r"[\x00-\x09\x0b-\x1f\x7f-\x9f\u2028\u2029\u202a-\u202e\u2066-\u2069]", text), "raw control survived")

    def run_text(self, online=False, urls=None):
        opener = _base.FakeOpener(urls or {})
        return content_links.run_cli(self.root, online, opener=opener if online else None, today=_base.TODAY), opener


class RealFilenameTests(SafetyBase):
    def test_real_meeting_filename_controls_are_escaped_offline_and_online(self):
        name, _ = self.hostile_tree()
        before = tree_state(self.root)
        groups = self.groups()
        raw = [c for g in groups for c in g["citations"] if name in c]
        self.assertEqual(len(raw), 6)  # exact coordinates keep the real filename
        self.assertIn(f"content/meetings/{name}:agenda_url", raw)
        for online in (False, True):
            (text, code), opener = self.run_text(online)
            self.assertEqual(code, 0)
            self.assert_clean(text.replace("\n", ""))
            self.assertIn(f"  cited by content/meetings/{HOSTILE_ESCAPED}.json:agenda_url\n", text)
            self.assertEqual(text.count(f"{HOSTILE_ESCAPED}.json:"), 6)
            self.assertNotIn("\x1b", text)
            self.assertNotIn("\r", text)
            self.assertNotIn("\t", text)
            self.assertNotIn("\u202e", text)
            self.assertNotIn("\n  result: ok (FORGED)", text)
            if online:
                self.assertEqual(text.count("\n  result: "), len(groups))
                self.assertEqual(len(opener.requests), len(groups))
            else:
                self.assertEqual(len(opener.requests), 0)
            self.assertEqual(tree_state(self.root), before)

    def test_real_published_finding_filename_controls_are_escaped(self):
        _, finding = self.hostile_tree(meeting=MEETING_FILE, finding="policy\x1b]0;pwn\x07\n  cited by forged.json")
        groups = self.by_url()
        one = groups["https://finding.example.org/one"]
        self.assertEqual(one["citations"], [f"content/findings/{finding}:body_md[anchor 1]",
                                            f"content/findings/{finding}:body_md[anchor 2]"])
        self.assertTrue((self.content / "findings" / finding).is_file())
        escaped = "policy\\x1b]0;pwn\\x07\\x0a  cited by forged.json"
        (text, code), _ = self.run_text(False)
        self.assertEqual(code, 0)
        self.assertEqual(text.count("https://finding.example.org/one\n"), 1)
        self.assertIn(f"  cited by content/findings/{escaped}:body_md[anchor 1]\n"
                      f"  cited by content/findings/{escaped}:body_md[anchor 2]\n", text)
        self.assertNotIn("\n  cited by forged.json", text)
        self.assertNotIn("\x1b", text)
        self.assertNotIn("\x07", text)

    def test_raw_locator_round_trip_and_transport_identity_are_preserved(self):
        plain_requests = sorted(url for _, url, _, _ in self.run_text(True)[1].requests)
        plain_groups = self.groups()
        name, _ = self.hostile_tree()
        groups = self.groups()
        self.assertEqual([g["url"] for g in groups], [g["url"] for g in plain_groups])
        resolved = 0
        for g in groups:
            for locator in g["citations"]:
                if name in locator:
                    path = self.root / locator.rsplit(":", 1)[0]
                    self.assertEqual(os.listdir(path.parent).count(name), 1)
                    document = json.loads(path.read_text(encoding="utf-8"))
                    self.assertEqual(document["id"], "cedar-council-2026-10")
                    resolved += 1
        self.assertEqual(resolved, 6)
        (_, _), opener = self.run_text(True)
        self.assertEqual(sorted(url for _, url, _, _ in opener.requests), plain_requests)
        self.assertEqual(len(opener.requests), len({url for _, url, _, _ in opener.requests}))


class DirectRenderTests(unittest.TestCase):
    def render(self, groups, results=None):
        return content_links.render(groups, "note", results)

    def test_archive_locator_uses_the_same_safe_uncapped_boundary(self):
        archive = {"locator": f"content/agencies.json:{HOSTILE}.url", "archived_url": _base.WB, "archived_on": "2026-01-01"}
        groups = [group([archive["locator"]], [archive])]
        before = copy.deepcopy(groups)
        text = self.render(groups)
        cited = f"  cited by content/agencies.json:{HOSTILE_ESCAPED}.url\n"
        recorded = (f"  archive recorded at content/agencies.json:{HOSTILE_ESCAPED}.url: {_base.WB} (archived_on 2026-01-01)\n")
        self.assertIn(cited + recorded, text)
        self.assertEqual(len(lines_of(text)), len(lines_of(self.render([group(["content/a.json:x"], [{**archive, "locator": "content/a.json:x"}])]))))
        self.assertEqual(groups, before)
        self.assertEqual(text, self.render(groups))

    def test_locator_control_matrix_is_single_line_and_printable(self):
        codes = list(range(0, 32)) + [0x7F] + list(range(0x80, 0xA0)) + [0x2028, 0x2029, 0x200E, 0x200F, 0x202A, 0x202B, 0x202C,
                                                                    0x202D, 0x202E, 0x2066, 0x2067, 0x2068, 0x2069, 0xFEFF, 0xD800, 0xDFFF]
        for code in codes:
            locator = f"content/meetings/a{chr(code)}b.json:agenda_url"
            archive = {"locator": locator, "archived_url": _base.WB, "archived_on": "2026-01-01"}
            text = self.render([group([locator], [archive])])
            want = "\\x%02x" % code if code < 256 else "\\u%04x" % code
            self.assertIn(f"  cited by content/meetings/a{want}b.json:agenda_url\n", text, hex(code))
            self.assertIn(f"  archive recorded at content/meetings/a{want}b.json:agenda_url: {_base.WB}", text, hex(code))
            self.assertEqual(len(lines_of(text)), len(lines_of(self.render([group(["content/a.json:x"], [{**archive, "locator": "content/a.json:x"}])]))), hex(code))
            if code != 10:
                self.assertNotIn(chr(code), text.replace("\n", ""), hex(code))
        payloads = ["\x1b]0;title\x07", "\x1b[H\x1b[2J", "\x08\x08\x08", "\x1b[2K\r"]
        for payload in payloads:
            text = self.render([group([f"content/x{payload}.json:f"])])
            self.assertIn("  cited by content/x" + expected_escape(payload) + ".json:f\n", text)
            self.assertNotIn("\x1b", text)
            self.assertNotIn("\x07", text)
            self.assertNotIn("\x08", text)

    def test_long_escaped_locators_are_complete_not_300_char_truncated(self):
        long_name = "\x07" * 80
        locator = f"content/meetings/{long_name}.json:agenda_url"
        expected = "content/meetings/" + "\\x07" * 80 + ".json:agenda_url"
        self.assertGreater(len(expected), 300)
        raw_long = "content/" + "a" * 400 + "\x1b[0m" + "b" * 400 + ".json:body_md[anchor 12]"
        archive = {"locator": raw_long, "archived_url": _base.WB, "archived_on": "2026-01-01"}
        text = self.render([group([locator, raw_long, raw_long], [archive])])
        self.assertIn(f"  cited by {expected}\n", text)
        self.assertEqual(text.count("  cited by " + expected_escape(raw_long) + "\n"), 2)
        self.assertIn(f"  archive recorded at {expected_escape(raw_long)}: {_base.WB} (archived_on 2026-01-01)\n", text)
        self.assertNotIn("...", text)
        # existing redirect/detail display stays bounded
        self.assertEqual(content_links._safe("x" * 400), "x" * 300 + "...")
        self.assertEqual(content_links._safe("\x1b" * 100), "\\x1b" * 75 + "...")
        self.assertEqual(content_links._safe("\x1b" * 100, 10), "\\x1b\\x1b\\x" + "...")
        self.assertEqual(content_links._safe("x" * 400, None), "x" * 400)
        self.assertEqual(content_links._safe("\x1b" * 100, limit=None), "\\x1b" * 100)
        results = [{"status": "ok", "chain": ["https://a.example.org/" + "r" * 400], "detail": "d" * 400}]
        text = self.render([group(["content/a.json:x"])], results)
        self.assertIn("  redirect -> https://a.example.org/" + "r" * 278 + "...\n", text)
        self.assertIn("  result: ok (" + "d" * 300 + "...)\n", text)

    def test_printable_unicode_and_ordinary_output_are_byte_identical(self):
        locators = ["content/meetings/caf\u00e9 \u4f1a\u8b70 \U0001f600 'q' [b] a\\b.json:outcome.source_url",
                    "content/meetings/x.json:comment_kit_md[anchor 2]", "content/sources.json[0].public_url"]
        archive = {"locator": locators[0], "archived_url": _base.WB, "archived_on": "2026-01-01"}
        groups = [group(locators, [archive]), group(["law/local-rules.json:rules[3].source_url"], url="https://b.example.org/")]
        before = copy.deepcopy(groups)
        text = self.render(groups)
        self.assertEqual(text, "\n".join([
            content_links.OFFLINE_LINE, "note", "2 external https URL(s) cited.", "",
            "https://county.example.org/policy.pdf",
            "  cited by " + locators[0], "  cited by " + locators[1], "  cited by " + locators[2],
            f"  archive recorded at {locators[0]}: {_base.WB} (archived_on 2026-01-01)", "",
            "https://b.example.org/", "  cited by law/local-rules.json:rules[3].source_url", "", content_links.DISCLAIMER]) + "\n")
        self.assertEqual(text, self.render(groups))
        self.assertEqual(groups, before)


class IntegratedOrdinaryOutputTests(SafetyBase):
    def legacy(self, groups, results=None):
        """Independent copy of the pre-change layout for printable data."""
        lines = [content_links.OFFLINE_LINE if results is None else "Online check: each URL requested once, GET, first 64 KiB, "
                 "no cookies, 15 s timeout, at most 5 https redirects.", self.note, f"{len(groups)} external https URL(s) cited."]
        for number, g in enumerate(groups):
            lines += ["", g["url"]] + [f"  cited by {c}" for c in g["citations"]]
            lines += [f"  archive recorded at {a['locator']}: {a['archived_url']} (archived_on {a['archived_on']})" for a in g["archives"]]
            if results is not None:
                lines.append(f"  result: {results[number]['status']} ({results[number]['detail']})")
        if results is not None:
            counts = {}
            for r in results:
                counts[r["status"]] = counts.get(r["status"], 0) + 1
            lines += ["", "Summary: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))]
        return "\n".join(lines + ["", content_links.DISCLAIMER]) + "\n"

    def test_ordinary_whole_report_bytes_are_unchanged_offline_and_online(self):
        self.set_finding_body()
        groups, self.note = content_links.collect_links(self.root, today=_base.TODAY)
        self.assertEqual(content_links.run_cli(self.root, today=_base.TODAY)[0], self.legacy(groups))
        text, code = content_links.run_cli(self.root, True, opener=_base.FakeOpener({}), today=_base.TODAY)
        self.assertEqual(code, 0)
        results = [{"status": "ok", "detail": "HTTP 200"}] * len(groups)
        self.assertEqual(text, self.legacy(groups, results))


class CliTests(SafetyBase):
    def invoke(self, online, opener=None):
        argv = ["campaign", "content", "link-check", "--directory", str(self.root)] + (["--online"] if online else [])
        out, err = io.StringIO(), io.StringIO()
        factory = mock.patch.object(content_links, "real_opener", side_effect=AssertionError("default opener") if opener is None else None,
                                    return_value=opener)
        code = 0
        with mock.patch.object(sys, "argv", argv), factory as patched, contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                cli.main()
            except SystemExit as exit_:
                code = exit_.code or 0
        return out.getvalue(), err.getvalue(), code, patched

    def test_cli_prints_safe_coordinates_without_changing_exits_or_files(self):
        self.hostile_tree()
        before = tree_state(self.root)
        out, err, code, patched = self.invoke(False)
        self.assertEqual((code, err), (0, ""))
        patched.assert_not_called()
        self.assertIn(f"  cited by content/meetings/{HOSTILE_ESCAPED}.json:agenda_url\n", out)
        self.assertNotIn("\x1b", out)
        self.assertNotIn("\n  result: ok (FORGED)", out)
        self.assertEqual(tree_state(self.root), before)

        opener = _base.FakeOpener({})
        out, err, code, _ = self.invoke(True, opener)
        self.assertEqual((code, err), (0, ""))
        total = len(self.groups())
        self.assertEqual(len(opener.requests), total)
        self.assertEqual(out.count("\n  result: "), total)
        self.assertIn(f"Summary: ok {total}\n", out)
        self.assertNotIn("\x1b", out)

        opener = _base.FakeOpener({AGENDA: {"status": 404}})
        out, err, code, _ = self.invoke(True, opener)
        self.assertEqual((code, err), (3, ""))
        self.assertIn(f"Summary: gone 1, ok {total - 1}\n", out)
        self.assertIn(f"{AGENDA}\n  cited by content/meetings/{HOSTILE_ESCAPED}.json:agenda_url\n  result: gone (HTTP 404)\n", out)
        self.assertNotIn("\x1b", out)
        self.assertNotIn("\n  result: ok (FORGED)", out)
        self.assertEqual(tree_state(self.root), before)


if __name__ == "__main__":
    unittest.main()
