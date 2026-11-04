"""kit-verify-agency --mailing-address and kit-copy --for mail: a verified postal address and a printable private
letter plus its plain-text twin. Synthetic fixtures only; no network, no printing, nothing sent."""
import contextlib
import errno
import html
import html.parser
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import cli, content, kit, kit_check, kit_copy  # noqa: E402

TODAY = date(2026, 3, 4)
SOURCE, CHECKED = "https://police.example/records", "2026-01-02"
ADDRESS = "Records Unit|123 Example Street|Exampleville, CA 90000"
LINES = ["Records Unit", "123 Example Street", "Exampleville, CA 90000"]
REQUESTER = ["Pat Example", "1 Test Way", "Exampleville, CA 90001"]
SETTINGS = {"schema_version": 1, "requester": REQUESTER, "fee_cap_usd": 50, "date_from": "2025-01-01"}


def run(*arguments):
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with mock.patch.object(sys, "argv", ["campaign_tool", *map(str, arguments)]), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            cli.main()
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


class Tags(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags, self.attrs, self.blocks, self.inside = [], [], [], False

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.attrs += [(tag, name, value) for name, value in attrs]
        if tag == "p":
            self.inside = True
            self.blocks.append("")

    def handle_endtag(self, tag):
        self.inside = self.inside and tag != "p"

    def handle_data(self, data):
        if self.inside:
            self.blocks[-1] += data


class KitMailTests(unittest.TestCase):
    def setUp(self):
        for target, effect in (("urllib.request.urlopen", AssertionError("network used")),
                               ("socket.socket", AssertionError("socket used")),
                               ("smtplib.SMTP", AssertionError("SMTP used"))):
            patcher = mock.patch(target, side_effect=effect)
            patcher.start()
            self.addCleanup(patcher.stop)
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-kit-mail-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.root = self.tmp / "campaign"
        code, _, err = run("init", "--directory", self.root, "--name", "Synthetic", "--county", "San Luis Obispo",
                           "--state", "CA")
        self.assertEqual(code, 0, err)
        self.kit = self.root / "kit"
        self.kit.mkdir()
        self.settings = self.kit / "request-settings.json"
        self.settings.write_text(json.dumps(SETTINGS), encoding="utf-8")
        code, _, err = run("kit", "--directory", self.root)
        self.assertEqual(code, 0, err)
        self.out = self.kit / "outgoing"
        self.agencies = self.kit / "agencies.json"
        ids = sorted(p.stem for p in (self.kit / "requests").glob("*.md"))
        self.a, self.other = ids[0], ids[1]
        self.draft = self.kit / "requests" / f"{self.a}.md"
        # The To: line of a fresh draft is a placeholder; a hand-filled draft is what a mailed request needs.
        self.draft.write_text(kit_check.PLACEHOLDER.sub("Records Custodian", self.draft.read_text(encoding="utf-8")),
                              encoding="utf-8")

    def verify(self, mailing=ADDRESS, **contact):
        return kit.verify_agency(self.root, self.a, source_url=SOURCE, checked_on=CHECKED, mailing_address=mailing,
                                 today=TODAY, **contact)

    def entry(self, agency=None):
        return next(e for e in json.loads(self.agencies.read_text(encoding="utf-8")) if e["agency_id"] == (agency or self.a))

    def edit_entry(self, **changes):
        agencies = json.loads(self.agencies.read_text(encoding="utf-8"))
        for entry in agencies:
            if entry["agency_id"] == self.a:
                for key, value in changes.items():
                    if value is KeyError:
                        entry.pop(key, None)
                    else:
                        entry[key] = value
        self.agencies.write_text(json.dumps(agencies), encoding="utf-8")

    def mail(self, today=TODAY):
        return kit_copy.copy(self.root, self.a, "mail", today=today)

    def refused(self, reason, today=TODAY):
        """kit-copy --for mail raises ValueError mentioning ``reason`` and leaves kit/outgoing exactly as it was."""
        before = self.listing()
        with self.assertRaises(ValueError) as caught:
            self.mail(today)
        self.assertIn(reason, str(caught.exception))
        self.assertEqual(self.listing(), before, "kit/outgoing changed on a refusal")
        return str(caught.exception)

    def listing(self):
        if not os.path.lexists(self.out) or self.out.is_symlink() or not self.out.is_dir():
            return None
        return {p.name: (p.lstat().st_mode, p.read_bytes() if p.is_file() and not p.is_symlink() else None)
                for p in self.out.iterdir()}

    def private_snapshot(self):
        skip = ("outgoing",)
        return {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob("*")
                if p.is_file() and not any(part in skip for part in p.parts)}

    def ready(self):
        self.verify()

    # --- verification ------------------------------------------------------------------------------------------

    def test_postal_only_and_combined_verification(self):
        self.edit_entry(custom_note="keep me")
        before_other = self.entry(self.other)
        result = self.verify()
        self.assertEqual(result, {"agency_id": self.a, "fields": [], "checked_on": CHECKED, "verified": True,
                                  "mailing_address": "recorded"})
        entry = self.entry()
        self.assertEqual(entry["mailing_address"], LINES)
        self.assertEqual(entry["mailing_verification"], {"source_url": SOURCE, "checked_on": CHECKED})
        self.assertIs(entry["verified"], True)
        self.assertEqual(entry["verification"], {"source_url": SOURCE, "checked_on": CHECKED, "fields": []})
        self.assertEqual(entry["custom_note"], "keep me")
        self.assertEqual(self.entry(self.other), before_other)
        self.assertEqual(stat.S_IMODE(self.agencies.stat().st_mode), 0o600)
        # a later electronic check replaces ordinary verification but keeps the postal proof as recorded
        kit.verify_agency(self.root, self.a, source_url="https://police.example/other", checked_on="2026-02-03",
                          records_email="records@police.example", today=TODAY)
        entry = self.entry()
        self.assertEqual(entry["mailing_address"], LINES)
        self.assertEqual(entry["mailing_verification"], {"source_url": SOURCE, "checked_on": CHECKED})
        self.assertEqual(entry["verification"]["fields"], ["records_email"])
        # the CSV import keeps it too, and a kit rerun keeps the organizer-owned entry
        csv_path = self.tmp / "contacts.csv"
        csv_path.write_text("agency_id,source_url,checked_on,records_email,records_url,portal_vendor,portal_url\n"
                            f"{self.a},https://police.example/third,2026-02-04,second@police.example,,,\n", encoding="utf-8")
        kit.verify_agencies_csv(self.root, csv_path, today=TODAY)
        code, _, err = run("kit", "--directory", self.root)
        self.assertEqual(code, 0, err)
        entry = self.entry()
        self.assertEqual((entry["mailing_address"], entry["mailing_verification"]["checked_on"], entry["records_email"]),
                         (LINES, CHECKED, "second@police.example"))
        # one call may give both; both proofs then carry this invocation's source and date
        combined = kit.verify_agency(self.root, self.a, source_url="https://police.example/both", checked_on="2026-02-05",
                                     records_email="both@police.example", mailing_address="One|Two", today=TODAY)
        self.assertEqual(combined["fields"], ["records_email"])
        entry = self.entry()
        self.assertEqual(entry["mailing_address"], ["One", "Two"])
        self.assertEqual(entry["mailing_verification"], {"source_url": "https://police.example/both",
                                                         "checked_on": "2026-02-05"})
        self.assertEqual(entry["verification"]["source_url"], "https://police.example/both")

    def test_postal_bounds_and_refusals(self):
        for good in ("A|B", "A|B|C|D|E", "x|y", "a" * 80 + "|" + "b" * 80, " padded | lines ", "Straße 5|75001 Paris"):
            self.verify(good)
        self.assertEqual(self.entry()["mailing_address"], ["Straße 5", "75001 Paris"])
        self.verify(" padded | lines ")
        self.assertEqual(self.entry()["mailing_address"], ["padded", "lines"])
        secret = "SECRETLINE"  # pragma: allowlist secret - synthetic postal-address redaction marker
        bad = {"one line": secret, "six lines": "|".join([secret] * 6), "blank middle": f"{secret}||B",
               "blank end": f"{secret}|", "blank only": "", "81 chars": secret + "x" * (81 - len(secret)) + "|B",
               "angle": f"{secret}|<b>", "greater": f"{secret}|a>b", "tab": f"{secret}\t|B", "newline": f"{secret}|B\n",
               "bell": f"{secret}|B\x07", "format": f"{secret}|B\u200b", "bidi": f"{secret}|B\u202e",
               "surrogate": f"{secret}|B\ud800", "line separator": f"{secret}|B\u2028", "paragraph": f"{secret}|B\u2029",
               "c1": f"{secret}|B\x85"}
        self.verify(ADDRESS)
        before = self.agencies.read_bytes()
        for label, value in bad.items():
            with self.subTest(label), self.assertRaises(ValueError) as caught:
                self.verify(value)
            self.assertIn("mailing_address", str(caught.exception))
            self.assertNotIn(secret, str(caught.exception))
            self.assertEqual(self.agencies.read_bytes(), before, label)
        for label, kwargs, word in (("unknown agency", {"agency": "ca-nowhere"}, "exactly once"),
                                    ("bad date", {"checked": "2026-02-30"}, "checked_on"),
                                    ("future date", {"checked": "2026-03-05"}, "after today"),
                                    ("http source", {"source": "http://police.example/r"}, "source_url"),
                                    ("no host", {"source": "https://"}, "source_url")):
            with self.subTest(label), self.assertRaises(ValueError) as caught:
                kit.verify_agency(self.root, kwargs.get("agency", self.a), source_url=kwargs.get("source", SOURCE),
                                  checked_on=kwargs.get("checked", CHECKED), mailing_address=ADDRESS, today=TODAY)
            self.assertIn(word, str(caught.exception))
            self.assertNotIn("Records Unit", str(caught.exception))
            self.assertEqual(self.agencies.read_bytes(), before, label)
        # a duplicate agency id is refused, and no contact at all is still refused
        agencies = json.loads(self.agencies.read_text(encoding="utf-8"))
        agencies.append(next(dict(e) for e in agencies if e["agency_id"] == self.a))
        self.agencies.write_text(json.dumps(agencies), encoding="utf-8")
        duplicated = self.agencies.read_bytes()
        with self.assertRaisesRegex(ValueError, "exactly once"):
            self.verify()
        self.assertEqual(self.agencies.read_bytes(), duplicated)
        agencies.pop()
        self.agencies.write_text(json.dumps(agencies), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "give at least one"):
            kit.verify_agency(self.root, self.a, source_url=SOURCE, checked_on=CHECKED, today=TODAY)

    def test_import_contract_unchanged(self):
        self.verify()
        csv_path = self.tmp / "contacts.csv"
        header = "agency_id,source_url,checked_on,records_email,records_url,portal_vendor,portal_url\n"
        csv_path.write_text(header + f"{self.a},https://police.example/csv,2026-02-06,csv@police.example,,,\n",
                            encoding="utf-8")
        code, out, err = run("kit-verify-agency", "import", "--directory", self.root, "--csv", csv_path)
        self.assertEqual(code, 0, err)
        entry = self.entry()
        self.assertEqual((entry["records_email"], entry["verification"]["fields"], entry["mailing_address"]),
                         ("csv@police.example", ["records_email"], LINES))
        self.assertEqual(entry["mailing_verification"], {"source_url": SOURCE, "checked_on": CHECKED})
        csv_path.write_text(header.strip() + ",mailing_address\n" + f"{self.a},{SOURCE},{CHECKED},x@police.example,,,,{ADDRESS}\n",
                            encoding="utf-8")
        before = self.agencies.read_bytes()
        code, _, err = run("kit-verify-agency", "import", "--directory", self.root, "--csv", csv_path)
        self.assertEqual(code, 2, err)
        self.assertIn("csv header must be exactly", err)
        code, _, err = run("kit-verify-agency", "import", "--directory", self.root, "--csv", csv_path,
                           "--mailing-address", ADDRESS)
        self.assertEqual(code, 2, err)
        self.assertIn("--mailing-address", err)
        self.assertNotIn("Records Unit", err)
        self.assertEqual(self.agencies.read_bytes(), before)
        self.assertEqual(kit.VERIFY_COLUMNS, tuple(header.strip().split(",")))

    # --- the letter --------------------------------------------------------------------------------------------

    def test_letter_blocks_and_escape(self):
        self.verify(records_email="records@police.example")
        hostile = '<script>alert("x")</script> <img src="http://evil.example/p.png" onerror="boom()"> & [x](http://e.example)'
        self.draft.write_text(self.draft.read_text(encoding="utf-8").replace(
            "Rolling production is welcome.", "Rolling production is welcome. " + hostile), encoding="utf-8")
        for target in ("portal", "email"):
            kit_copy.copy(self.root, self.a, target)
        legacy = {p.name: p.read_bytes() for p in self.out.iterdir()}
        self.assertEqual(sorted(legacy), [f"{self.a}.eml", f"{self.a}.txt"])
        result = self.mail()
        part = kit_copy.sendable(self.draft.read_text(encoding="utf-8"))
        blocks = ["\n".join(REQUESTER), "2026-03-04", "\n".join(LINES), "Subject: " + part["subject"], part["body"]]
        plain = "\n\n".join(blocks) + "\n"
        self.assertEqual((self.out / f"{self.a}.letter.txt").read_text(encoding="utf-8"), plain)
        self.assertEqual(result, {"written": [f"kit/outgoing/{self.a}.letter.html", f"kit/outgoing/{self.a}.letter.txt"],
                                  "characters": len(plain), "subject": part["subject"]})
        self.assertTrue(part["body"].endswith("\n".join(REQUESTER)), "the signature stays in the sendable text")
        self.assertIn(hostile, plain)
        self.assertEqual(plain.count("\n".join(REQUESTER)), 2, "return address then signature, both kept")
        page = (self.out / f"{self.a}.letter.html").read_text(encoding="utf-8")
        self.assertTrue(page.startswith("<!DOCTYPE html>\n<html lang=\"en\">"))
        self.assertIn('<meta charset="utf-8">', page)
        self.assertIn("default-src 'none'", page)
        self.assertNotIn("<script", page.lower())
        self.assertNotIn("<img", page.lower())
        self.assertIn("&lt;script&gt;", page)
        self.assertIn("&amp;", page)
        parsed = Tags()
        parsed.feed(page)
        self.assertEqual(set(parsed.tags), {"html", "head", "meta", "title", "style", "body", "p"})
        self.assertEqual(parsed.blocks, blocks, "the HTML blocks hold exactly the plain twin's text, in order")
        for _, name, value in parsed.attrs:
            self.assertFalse(name.startswith("on") or name in ("src", "href", "action", "srcset"), name)
        self.assertNotIn("url(", page)
        self.assertNotIn("@import", page)
        self.assertNotIn("print()", page)
        self.assertEqual({p.name: p.read_bytes() for p in self.out.iterdir() if "letter" not in p.name}, legacy)
        self.assertFalse((self.kit / "sent.json").exists())

    def test_mail_requires_whole_target_check_and_requester(self):
        for p in (self.kit / "requests").glob("*.md"):
            if p != self.draft:
                self.assertTrue(kit_check.draft_problems(p.read_text(encoding="utf-8")), "other drafts stay unfilled")
        self.refused("recipient not verified")  # nothing verified at all: the whole-target check says so
        kit.verify_agency(self.root, self.a, source_url=SOURCE, checked_on=CHECKED, records_email="r@police.example",
                          today=TODAY)
        self.refused("no verified mailing_address")  # electronic proof alone is not a postal address
        self.ready()
        good = self.entry()
        for label, changes, reason in (
                ("unverified", {"verified": False}, "recipient not verified"),
                ("no address", {"mailing_address": KeyError}, "mailing_address"),
                ("no proof", {"mailing_verification": KeyError}, "mailing_address"),
                ("one line", {"mailing_address": ["Only"]}, "not 2-5 plain lines"),
                ("long line", {"mailing_address": ["A", "b" * 81]}, "not 2-5 plain lines"),
                ("markup", {"mailing_address": ["A", "<b>"]}, "not 2-5 plain lines"),
                ("untrimmed", {"mailing_address": ["A", " B"]}, "not 2-5 plain lines"),
                ("not a list", {"mailing_address": "A|B"}, "not 2-5 plain lines"),
                ("bad date", {"mailing_verification": {"source_url": SOURCE, "checked_on": "2026-13-01"}}, "checked_on"),
                ("future", {"mailing_verification": {"source_url": SOURCE, "checked_on": "2026-03-05"}}, "checked_on"),
                ("http", {"mailing_verification": {"source_url": "http://x.example/", "checked_on": CHECKED}}, "https://"),
                ("extra proof key", {"mailing_verification": {"source_url": SOURCE, "checked_on": CHECKED, "x": 1}}, "needs a source_url"),
                ("placeholder address", {"mailing_address": ["[Street]", "Town"]}, "unfilled placeholder")):
            with self.subTest(label):
                self.edit_entry(**changes)
                message = self.refused(reason)
                self.assertNotIn("Records Unit", message)
                self.edit_entry(**{key: good.get(key, KeyError) for key in changes})
        original = self.draft.read_text(encoding="utf-8")
        for label, replace in (("tail placeholder", ("Operator checklist:", "Operator checklist: [todo]")),
                               ("To placeholder", ("To: Records Custodian", "To: [official records custodian]")),
                               ("body placeholder", ("Rolling production", "[cap] Rolling production")),
                               ("subject placeholder", ("Subject: Records", "Subject: [topic] Records")),
                               ("banner placeholder", ("DRAFT ONLY.", "DRAFT [x] ONLY."))):
            with self.subTest(label):
                self.draft.write_text(original.replace(*replace), encoding="utf-8")
                self.refused("kit-check fails")
        self.draft.write_text(original, encoding="utf-8")
        settings_text = self.settings.read_text(encoding="utf-8")
        outside = self.tmp / "settings-elsewhere.json"
        outside.write_text(settings_text, encoding="utf-8")
        for label, write, reason in (
                ("requester placeholder", lambda: self.settings.write_text(json.dumps({**SETTINGS, "requester": ["[Name]"]})), "unfilled placeholder"),
                ("no requester", lambda: self.settings.write_text(json.dumps({"schema_version": 1, "fee_cap_usd": 50})), "needs a requester"),
                ("no settings", lambda: self.settings.unlink(), "needs a requester"),
                ("bad json", lambda: self.settings.write_text("{nope"), "valid JSON"),
                ("unknown key", lambda: self.settings.write_text(json.dumps({**SETTINGS, "extra": 1})), "unknown key"),
                ("symlink", lambda: (self.settings.unlink(), self.settings.symlink_to(outside)), "regular file")):
            with self.subTest(label):
                write()
                self.refused(reason)
                if self.settings.is_symlink() or self.settings.exists():
                    self.settings.unlink()
                self.settings.write_text(settings_text, encoding="utf-8")
        self.assertFalse(self.out.exists())
        self.assertEqual(self.mail()["subject"], kit_copy.sendable(original)["subject"])  # restored: it works

    def test_symlinks_corruption_and_pair_preflight(self):
        self.ready()
        outside = self.tmp / "outside"
        outside.mkdir()
        sentinel = outside / "sentinel.txt"
        sentinel.write_text("untouched", encoding="utf-8")
        names = (f"{self.a}.letter.html", f"{self.a}.letter.txt")

        def assert_outside_untouched():
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "untouched")
            self.assertEqual(sorted(p.name for p in outside.iterdir()), ["sentinel.txt"])

        self.out.symlink_to(outside, target_is_directory=True)
        self.refused("kit/outgoing must be a directory")
        assert_outside_untouched()
        self.out.unlink()
        self.out.write_text("a file, not a directory", encoding="utf-8")
        self.refused("kit/outgoing must be a directory")
        self.out.unlink()
        for index in (0, 1):  # a symlinked or non-regular destination: neither output changes
            self.out.mkdir()
            (self.out / names[1 - index]).write_text("old other twin", encoding="utf-8")
            for make in (lambda path: path.symlink_to(sentinel), lambda path: path.mkdir()):
                make(self.out / names[index])
                self.refused("must be a regular file")
                assert_outside_untouched()
                target = self.out / names[index]
                target.unlink() if target.is_symlink() else target.rmdir()
            (self.out / names[1 - index]).unlink()
            self.out.rmdir()
        for label, hide in (("kit", self.kit), ("requests", self.kit / "requests")):
            with self.subTest(label):
                moved = self.tmp / f"moved-{label}"
                hide.rename(moved)
                hide.symlink_to(moved, target_is_directory=True)
                try:
                    with self.assertRaises(ValueError):
                        self.mail()
                finally:
                    hide.unlink()
                    moved.rename(hide)
        link = self.tmp / "draft-target.md"
        link.write_bytes(self.draft.read_bytes())
        original = self.draft.read_bytes()
        self.draft.unlink()
        self.draft.symlink_to(link)
        self.refused("must be a regular file")
        self.draft.unlink()
        for label, data, reason in (("oversized", b"x" * ((1 << 20) + 1), "larger than 1 MiB"),
                                    ("bad utf-8", original + b"\xff\xfe", "utf-8")):
            with self.subTest(label):
                self.draft.write_bytes(data)
                self.refused(reason)
        self.draft.write_bytes(original)
        good = self.agencies.read_bytes()
        for label, data in (("bad json", b"{nope"), ("bad utf-8", b"[\xff]"), ("not a list", b"{}")):
            with self.subTest(label):
                self.agencies.write_bytes(data)
                with self.assertRaises(ValueError):
                    self.mail()
                self.assertFalse(self.out.exists())
        self.agencies.write_bytes(good)
        self.settings.write_bytes(b"\xff")
        self.refused("must be UTF-8")
        self.settings.write_text(json.dumps(SETTINGS), encoding="utf-8")
        assert_outside_untouched()
        self.assertEqual(self.mail()["written"], [f"kit/outgoing/{n}" for n in names])

    def test_modes_rerun_and_second_write_failure(self):
        self.ready()
        self.out.mkdir(mode=0o755)
        before = self.private_snapshot()
        self.mail(date(2026, 3, 4))
        self.assertEqual(stat.S_IMODE(self.out.stat().st_mode), 0o700)
        names = [f"{self.a}.letter.html", f"{self.a}.letter.txt"]
        for name in names:
            self.assertEqual(stat.S_IMODE((self.out / name).stat().st_mode), 0o600)
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), sorted(names), "temp file left behind")
        self.mail(date(2026, 3, 5))  # rerun replaces both
        for name in names:
            self.assertIn("2026-03-05", (self.out / name).read_text(encoding="utf-8"))
            self.assertNotIn("2026-03-04", (self.out / name).read_text(encoding="utf-8"))
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), sorted(names))
        self.assertEqual(self.private_snapshot(), before, "agencies, drafts, settings, ledger or public tree changed")
        self.assertFalse((self.kit / "sent.json").exists())
        real = kit_copy._write
        for failing in (0, 1):
            calls = []

            def flaky(out, name, data, failing=failing, calls=calls):
                calls.append(name)
                if len(calls) == failing + 1:
                    raise OSError(errno.EIO, "synthetic disk failure")
                return real(out, name, data)

            with self.subTest(failing=failing), mock.patch.object(kit_copy, "_write", flaky):
                with self.assertRaises(ValueError) as caught:
                    self.mail(date(2026, 3, 6))
                message = str(caught.exception)
                self.assertIn("could not be written", message)
                self.assertEqual("already replaced" in message, bool(failing))
                html_page, twin = ((self.out / n).read_text(encoding="utf-8") for n in names)
                self.assertEqual(("2026-03-06" in html_page, "2026-03-06" in twin), (bool(failing), False))
            self.mail(date(2026, 3, 7))  # retry rewrites both
            for name in names:
                self.assertIn("2026-03-07", (self.out / name).read_text(encoding="utf-8"))
            self.assertEqual(sorted(p.name for p in self.out.iterdir()), sorted(names))
        self.assertEqual(self.private_snapshot(), before)

    def test_public_model_strips_postal_and_legacy_copy_unchanged(self):
        self.verify(records_email="records@police.example")
        document = [{k: v for k, v in e.items() if k != "suggested"}
                    for e in json.loads(self.agencies.read_text(encoding="utf-8"))]
        models = content.load_agencies(document, "kit/agencies.json", today=TODAY)
        model = next(m for m in models if m["agency_id"] == self.a)
        self.assertEqual(model["verification"], {"source_url": SOURCE, "checked_on": CHECKED, "fields": ["records_email"]})
        dumped = json.dumps(models)
        for leaked in ("mailing", "Records Unit", "123 Example Street", "Exampleville"):
            self.assertNotIn(leaked, dumped)
        entry = next(e for e in document if e["agency_id"] == self.a)
        for label in ("content/agencies.json", "kit/governing-bodies.json"):
            with self.subTest(label), self.assertRaisesRegex(content.ContentError, "unknown field"):
                content.load_agencies([entry], label)
        for label, changes in (("address alone", {"mailing_verification": KeyError}), ("proof alone", {"mailing_address": KeyError}),
                               ("bad address", {"mailing_address": ["One"]}),
                               ("bad proof", {"mailing_verification": {"source_url": "http://x.example", "checked_on": CHECKED}}),
                               ("future proof", {"mailing_verification": {"source_url": SOURCE, "checked_on": "2026-03-05"}})):
            with self.subTest(label), self.assertRaises(content.ContentError) as caught:
                broken = {k: v for k, v in entry.items() if changes.get(k) is not KeyError}
                broken.update({k: v for k, v in changes.items() if v is not KeyError})
                content.load_agencies([broken], "kit/agencies.json", today=TODAY)
            self.assertNotIn("Records Unit", str(caught.exception))
        code, _, err = run("build", "--directory", self.root)
        self.assertEqual(code, 0, err)
        for path in (self.root / "public").rglob("*"):
            if path.is_file():
                text = path.read_bytes().decode("utf-8", "replace")
                self.assertNotIn("123 Example Street", text, path)
                self.assertNotIn("mailing_", text, path)
        # legacy copy: same bytes and return shapes as before; an unknown target no longer falls through to portal
        self.assertEqual(sorted(kit_copy.copy(self.root, self.a, "portal")), ["characters", "subject", "written"])
        self.assertEqual(kit_copy.copy(self.root, self.a, "portal")["written"], f"kit/outgoing/{self.a}.txt")
        part = kit_copy.sendable(self.draft.read_text(encoding="utf-8"))
        self.assertEqual((self.out / f"{self.a}.txt").read_bytes(), (part["subject"] + "\n\n" + part["body"] + "\n").encode())
        self.assertEqual(kit_copy.copy(self.root, self.a, "email")["written"], f"kit/outgoing/{self.a}.eml")
        with self.assertRaisesRegex(ValueError, "copy target"):
            kit_copy.copy(self.root, self.a, "fax")
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), [f"{self.a}.eml", f"{self.a}.txt"])

    def test_mail_cli_help_privacy_and_offline(self):
        code, out, err = run("kit-copy", "--help")
        self.assertEqual(code, 0, err)
        self.assertIn("portal,email,mail", " ".join(out.split()).replace("{", "").replace("}", "").replace(" ", ""))
        code, out, err = run("kit-verify-agency", "--help")
        self.assertEqual(code, 0, err)
        self.assertIn("--mailing-address", out)
        code, out, err = run("kit-verify-agency", "--directory", self.root, "--agency-id", self.a, "--source-url", SOURCE,
                             "--checked-on", CHECKED, "--mailing-address", ADDRESS)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out.split("\nNothing was sent.")[0])["mailing_address"], "recorded")
        self.assertNotIn("Records Unit", out + err)
        code, out, err = run("kit-verify-agency", "--directory", self.root, "--agency-id", self.a, "--source-url", SOURCE,
                             "--checked-on", CHECKED, "--mailing-address", "Records Unit")
        self.assertEqual(code, 1, err)
        self.assertNotIn("Records Unit", out + err)
        self.assertNotIn("Traceback", err)
        arguments = ("kit-copy", "--directory", self.root, "--agency", self.a, "--for", "mail")
        code, out, err = run(*arguments, "--json")
        self.assertEqual(code, 0, err)
        result = json.loads(out)
        self.assertEqual(result["written"], [f"kit/outgoing/{self.a}.letter.html", f"kit/outgoing/{self.a}.letter.txt"])
        self.assertEqual(err.strip(), "Copy only. Nothing was sent.")
        code, out, err = run(*arguments)
        self.assertEqual(code, 0, err)
        self.assertIn(f"written: kit/outgoing/{self.a}.letter.html, kit/outgoing/{self.a}.letter.txt", out.splitlines())
        self.assertIn(f"characters: {result['characters']}", out.splitlines())
        self.assertEqual(err.strip(), "Copy only. Nothing was sent.")
        for private in ("Records Unit", "123 Example Street", "Pat Example", "1 Test Way", "Please advise"):
            self.assertNotIn(private, out + err)
        self.assertEqual(len(out.splitlines()), 3)
        # a refusal: Stopped line, exit 3, no traceback, no address or requester dump
        (self.out / f"{self.a}.letter.html").unlink()
        (self.out / f"{self.a}.letter.html").mkdir()
        code, out, err = run(*arguments)
        self.assertEqual((code, out), (3, ""))
        self.assertTrue(err.startswith("Stopped: "), err)
        self.assertNotIn("Traceback", err)
        for private in ("Records Unit", "123 Example Street", "Pat Example"):
            self.assertNotIn(private, err)
        (self.out / f"{self.a}.letter.html").rmdir()
        self.settings.unlink()
        code, out, err = run(*arguments)
        self.assertEqual(code, 3, err)
        self.assertIn("requester", err)
        self.settings.write_text(json.dumps(SETTINGS), encoding="utf-8")
        # recording the post by hand is the unchanged kit-sent --channel mail
        code, out, err = run("kit-sent", "--directory", self.root, "--agency", self.a, "--date", "2026-03-04",
                             "--channel", "mail")
        self.assertEqual(code, 0, err)
        record = json.loads((self.kit / "sent.json").read_text(encoding="utf-8"))
        self.assertIn("mail", json.dumps(record))
        code, _, err = run("kit-copy", "--directory", self.root, "--agency", self.a, "--for", "fax")
        self.assertEqual(code, 2, err)


if __name__ == "__main__":
    unittest.main()
