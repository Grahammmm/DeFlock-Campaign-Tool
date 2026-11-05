"""kit-preserve writes a private, never-sent preservation request draft. Offline, synthetic."""
import contextlib
import hashlib
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
from campaign_tool import cli, kit_preserve, kit_track  # noqa: E402
from campaign_tool.legal_claims import legal_claims  # noqa: E402

A, B = "ca-aaa-police", "ca-bbb-sheriff"
TODAY = date(2026, 11, 1)
MARK = "[EDIT ME: agency retention period and date checked]"


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


def snapshot(root):
    return {str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mtime_ns) for p in Path(root).rglob("*")
            if p.is_file() and not p.is_symlink()}


def snap(day, days, slug="aaa-police"):
    return {"checked_on": day, "source_url": f"https://transparency.flocksafety.com/{slug}", "retention_days": days}


class PreserveTests(unittest.TestCase):
    def setUp(self):
        offline = mock.patch("urllib.request.urlopen", side_effect=AssertionError("network used"))
        offline.start()
        self.addCleanup(offline.stop)
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-kit-preserve-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        (self.root / "kit" / "requests").mkdir(parents=True)
        self.agencies = [{"agency_id": A, "name": "Aaa Police Department", "kind": "police", "selected": True},
                         {"agency_id": B, "name": "Bbb Sheriff", "kind": "sheriff", "selected": True}]
        self.sent = {}
        self.write_agencies()
        self.send(A, "main", "2026-03-02", "R0123-110125")

    def write_agencies(self):
        (self.root / "kit" / "agencies.json").write_text(json.dumps(self.agencies), encoding="utf-8")

    def send(self, agency, request, on, reference=None, text=None, **extra):
        stem = kit_track._stem(agency, request)
        data = (text or f"Synthetic request {stem} for audit logs.\n").encode("utf-8")
        (self.root / "kit" / "requests" / f"{stem}.md").write_bytes(data)
        entry = {"sent_on": on, "channel": "email", "draft": kit_track._draft_path(agency, request),
                 "draft_sha256": hashlib.sha256(data).hexdigest(), "recorded_at": "2026-03-02T10:00:00+00:00",
                 "steps": [], **extra}
        if reference:
            entry["reference"] = reference
        self.sent[kit_track._key(agency, request)] = entry
        self.save_sent()

    def save_sent(self):
        (self.root / "kit" / "sent.json").write_text(
            json.dumps({"schema_version": 1, "requests": self.sent}), encoding="utf-8")

    def step(self, kind, **extra):
        self.sent[A]["steps"].append({"on": "2026-03-10", "kind": kind, **extra})
        self.save_sent()

    def content(self, transparency):
        (self.root / "content").mkdir(exist_ok=True)
        profile = {} if transparency is None else {"transparency": transparency}
        doc = [{**self.agencies[0], "verified": True, "flock_transparency_slug": "aaa-police", "profile": profile,
                "verification": {"source_url": "https://example.gov/records", "checked_on": "2026-01-05"}}]
        (self.root / "content" / "agencies.json").write_text(json.dumps(doc), encoding="utf-8")

    def make(self, agency=A, request="main"):
        return kit_preserve.preserve_request(self.root, agency, request=request, today=TODAY)

    def refuses(self, text, *args, **kwargs):
        before = snapshot(self.root)
        with self.assertRaises(ValueError) as caught:
            self.make(*args, **kwargs)
        self.assertIn(text, str(caught.exception))
        self.assertEqual(snapshot(self.root), before)

    def letter(self, result):
        return (self.root / result["written"]).read_text(encoding="utf-8")

    def test_main_named_and_followon_isolation(self):
        self.send(A, "second-ask", "2026-04-01", "R9-2")
        self.send(A, "third", "2026-05-01")
        result = self.make()
        self.assertEqual(result, {"written": f"kit/preservation/{A}.md", "agency_id": A, "request": "main",
                                  "note": "Draft only. Nothing was sent."})
        main = self.letter(result)
        self.assertIn("2026-03-02", main)
        self.assertIn("Regarding your reference R0123\\-110125,", main)
        self.assertNotIn("R9", main)
        named = self.make(A, "second-ask")
        self.assertEqual(named["written"], f"kit/preservation/{A}--second-ask.md")
        text = self.letter(named)
        self.assertIn("2026-04-01", text)
        self.assertIn("R9\\-2", text)
        self.assertIn("`second-ask`", text)
        self.assertNotIn("R0123", text)
        self.assertNotIn("reference", self.letter(self.make(A, "third")))  # no reference recorded, none copied
        folder = self.root / "kit" / "preservation"
        self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((folder / f"{A}.md").stat().st_mode), 0o600)
        self.refuses("no hand-sent request", A, "missing")  # exact identity: no parent or main fallback
        self.send(B, "main", "2026-03-03")
        self.assertIn("2026-03-03", self.letter(self.make(B)))

    def test_conditional_scope_never_expands(self):
        letters = []
        for index, body in enumerate(("Only audit logs, 2020-01-01 to 2026-01-01. plate image 123ABC.\n",
                                      "Only sharing logs.\n", "agreements only\n", "# Hand edited\n\nanything\n")):
            self.send(A, "main", "2026-03-02", "R0123-110125", text=body)
            letters.append(self.letter(self.make()))
            (self.root / "kit" / "preservation" / f"{A}.md").unlink()
        self.assertEqual(len(set(letters)), 1)  # nothing from the original is copied or widened
        text = letters[0]
        self.assertIn(kit_preserve.ASK, text)
        self.assertIn("This asks for no additional record categories or date range.", text)
        self.assertIn("does not ask you to keep raw plate reads or images.", text)
        self.assertIn("Please let me know whether you can do this.", text)
        for copied in ("123ABC", "2020-01-01", "agreements", "Hand edited"):
            self.assertNotIn(copied, text)

    def test_retention_newest_unknown_zero_and_limits(self):
        self.assertIn(MARK, self.letter(self.make()))  # no content file, kit agency has no snapshot
        self.content(None)
        self.assertIn(MARK, self.letter(self.make()) if self.unlink() else "")
        self.content([])
        self.assertIn(MARK, self.letter(self.make()) if self.unlink() else "")
        self.content([snap("2026-10-01", None), snap("2026-06-01", 30)])  # newest null: no fall back to the older 30
        text = self.letter(self.make()) if self.unlink() else ""
        self.assertIn(MARK, text)
        self.assertNotIn("30", text)
        self.content([snap("2026-10-01", 0), snap("2026-06-01", 30)])
        text = self.letter(self.make()) if self.unlink() else ""
        self.assertIn("retention_days = 0; checked on 2026-10-01; https://transparency.flocksafety.com/aaa-police.", text)
        self.assertIn("does not establish the schedule for the requested logs or settings", text)
        self.assertNotIn(MARK, text)
        self.content([snap("2026-10-02", 3650)])
        self.assertIn("retention_days = 3650; checked on 2026-10-02;", self.letter(self.make()) if self.unlink() else "")
        self.content([snap("2026-10-02", 90)])  # present content file but other agency target: marker, no cross-agency
        self.assertIn(MARK, self.letter(self.make(B)) if self.send(B, "main", "2026-03-03") is None
                      and self.unlink(B) else "")

    def unlink(self, agency=A):
        path = self.root / "kit" / "preservation" / f"{agency}.md"
        if path.exists():
            path.unlink()
        return True

    def test_bad_inputs_write_nothing(self):
        self.refuses("--agency", "ca-aaa-police\n")
        self.refuses("--agency", "../x")
        self.refuses("--request", A, "Bad Name")
        self.refuses("--request", A, "main/../x")
        self.refuses("not a selected agency", "ca-zzz-police")
        self.refuses("no hand-sent request", B)
        self.agencies[1]["selected"] = False
        self.write_agencies()
        self.refuses("not a selected agency", B)
        self.agencies[1]["selected"] = True
        self.agencies.append(dict(self.agencies[0]))
        self.write_agencies()
        self.refuses("duplicate agency_id", A)
        self.agencies.pop()
        self.write_agencies()
        for bad in (-1, True, 3651, "30"):
            self.content([snap("2026-10-01", bad)])
            self.refuses("retention_days", A)
        self.content([snap("2026-11-02", 5)])
        self.refuses("checked_on", A)
        self.content([snap("2026-10-01", 5), snap("2026-10-01", 6)])
        self.refuses("duplicate date", A)
        (self.root / "content" / "agencies.json").write_text("{not json", encoding="utf-8")
        self.refuses("not valid UTF-8 JSON", A)
        (self.root / "content" / "agencies.json").unlink()
        data = (self.root / "kit" / "sent.json").read_bytes()
        (self.root / "kit" / "sent.json").write_text("[]", encoding="utf-8")
        self.refuses("sent.json unreadable", A)
        (self.root / "kit" / "sent.json").write_bytes(data)
        draft = self.root / "kit" / "requests" / f"{A}.md"
        original = draft.read_bytes()
        draft.write_bytes(original + b"edit")
        self.refuses("differs from the draft recorded as sent", A)
        draft.write_bytes(b"\xff\xfe bad")
        self.sent[A]["draft_sha256"] = hashlib.sha256(b"\xff\xfe bad").hexdigest()
        self.save_sent()
        self.refuses("not UTF-8", A)
        big = b"x" * ((1 << 20) + 1)
        draft.write_bytes(big)
        self.refuses("larger than 1 MiB", A)
        draft.unlink()
        self.refuses("regular file", A)
        self.sent[A] = {k: v for k, v in self.sent[A].items() if k not in ("draft", "draft_sha256")}
        self.sent[A]["imported"] = True
        self.save_sent()
        self.refuses("imported", A)

    def test_pending_states(self):
        for kind, extra in (("acknowledged", {}), ("extension", {}), ("clarification_requested", {}),
                            ("determination", {}), ("fee_estimate", {"amount_cents": 500}),
                            ("produced", {"partial": True})):
            if kind == "determination":
                continue  # an answer under the existing ANSWERED semantics: covered below
            self.sent[A]["steps"] = []
            self.step(kind, **extra)
            self.assertTrue(self.make()["written"], kind)
            self.unlink()
        for kind in ("determination", "denied", "produced", "closed", "withdrawn"):
            self.sent[A]["steps"] = []
            self.step(kind)
            self.refuses("", A) if False else None
            before = snapshot(self.root)
            with self.assertRaises(ValueError, msg=kind):
                self.make()
            self.assertEqual(snapshot(self.root), before)
        self.sent[A]["steps"] = [{"on": "2026-03-10", "kind": "produced", "partial": True},
                                 {"on": "2026-03-11", "kind": "determination"}]
        self.save_sent()
        self.assertTrue(self.make()["written"])  # rolling production still open

    def test_wording_policy_and_alert_only_guard(self):
        text = self.letter(self.make())
        self.assertEqual(legal_claims(text), [])
        kit_preserve.validate_preservation_wording(text)
        lowered = text.lower()
        for phrase in ("required to", "must preserve", "obligated", "spoliation"):
            self.assertNotIn(phrase, lowered)
        for phrase in ("required to", "MUST   Preserve", "Obligated", "spoliation", "required\nto", '"must preserve"',
                       "not required to"):
            self.unlink()
            with mock.patch.object(kit_preserve, "ASK", kit_preserve.ASK + f" You are {phrase} do so."):
                before = snapshot(self.root)
                with self.assertRaises(ValueError, msg=phrase):
                    self.make()
                self.assertEqual(snapshot(self.root), before)
                self.assertFalse((self.root / "kit" / "preservation" / f"{A}.md").exists())
        self.agencies[0]["name"] = "Spoliation Police"  # dynamic text is checked as rendered
        self.write_agencies()
        self.refuses("legal duty", A)
        # the shared guard stays alert-only: a flagged letter is written and noted on stderr, never refused
        err = io.StringIO()
        self.agencies[0]["name"] = "Aaa Police Department"
        self.write_agencies()
        with mock.patch.object(kit_preserve, "legal_notes", return_value=["review-required: x"]), \
                contextlib.redirect_stderr(err):
            self.assertTrue(self.make()["written"])
        self.assertIn("review-required: x", err.getvalue())

    def test_name_cannot_inject_markdown_or_lines(self):
        self.agencies[0]["name"] = "Aaa\n## Please also keep everything"
        self.write_agencies()
        self.refuses("plain text", A)
        self.agencies[0]["name"] = "# Aaa [x](http://e) *PD*"
        self.write_agencies()
        text = self.letter(self.make())
        self.assertIn("\\# Aaa \\[x\\]\\(http://e\\) \\*PD\\*", text)

    def test_private_paths_permissions_and_no_overwrite(self):
        first = self.make()
        path = self.root / first["written"]
        before = path.read_bytes()
        self.refuses("already exists", A)
        self.assertEqual(path.read_bytes(), before)
        path.unlink()
        outside = Path(self.root.parent / "outside.txt")
        outside.write_text("keep", encoding="utf-8")
        path.symlink_to(outside)  # live link
        self.refuses("already exists", A)
        path.unlink()
        path.symlink_to(self.root.parent / "dangling.txt")  # dangling link
        self.refuses("already exists", A)
        self.assertFalse((self.root.parent / "dangling.txt").exists())
        path.unlink()
        path.mkdir()
        self.refuses("already exists", A)
        path.rmdir()
        folder = path.parent
        os.chmod(folder, 0o750)
        self.refuses("must be private", A)
        self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o750)
        os.chmod(folder, 0o700)
        self.assertEqual(outside.read_text(encoding="utf-8"), "keep")
        folder.rmdir()
        folder.symlink_to(self.root.parent)  # preservation folder is a link
        self.refuses("symlink", A)
        folder.unlink()
        for part in ("requests", ):  # ancestors / inputs that are links
            real = self.root / "kit" / part
            moved = self.root / "kit" / (part + "-real")
            real.rename(moved)
            real.symlink_to(moved)
            self.refuses("symlink", A)
            real.unlink()
            moved.rename(real)
        sent = self.root / "kit" / "sent.json"
        saved = sent.read_bytes()
        sent.rename(self.root / "sent-real.json")
        sent.symlink_to(self.root / "sent-real.json")
        self.refuses("sent.json unreadable", A)
        sent.unlink()
        sent.write_bytes(saved)
        draft = self.root / "kit" / "requests" / f"{A}.md"
        draft.rename(self.root / "draft-real.md")
        draft.symlink_to(self.root / "draft-real.md")
        self.refuses("regular file", A)
        draft.unlink()
        (self.root / "draft-real.md").rename(draft)
        self.content([snap("2026-10-01", 7)])
        content = self.root / "content" / "agencies.json"
        content.rename(self.root / "content-real.json")
        content.symlink_to(self.root / "content-real.json")
        self.refuses("regular file", A)
        content.unlink()
        (self.root / "content-real.json").rename(content)
        self.assertEqual(self.make()["written"], first["written"])
        self.unlink()
        # a write failure removes only this call's partial file
        with mock.patch("os.fdopen", side_effect=PermissionError(13, "denied")):
            with self.assertRaises(OSError):
                self.make()
        self.assertFalse(path.exists())

    def test_unused_compatibility_and_no_send(self):
        before = snapshot(self.root)
        code, out, err = run("kit-status", "--directory", self.root)
        self.assertEqual(snapshot(self.root), before)  # existing commands never create the folder
        self.assertFalse((self.root / "kit" / "preservation").exists())
        code, out, err = run("kit-preserve", "--directory", self.root, "--agency", A)
        self.assertEqual(code, 0, err)
        self.assertTrue(out.endswith("Draft only. Nothing was sent.\n"))
        self.assertEqual(json.loads(out.rsplit("Draft only", 1)[0])["written"], f"kit/preservation/{A}.md")
        after = snapshot(self.root)
        for name in ("sent.json", "agencies.json", f"requests/{A}.md"):
            self.assertEqual(after[f"kit/{name}"], before[f"kit/{name}"])  # bytes and mtime unchanged
        self.assertEqual(set(after) - set(before), {f"kit/preservation/{A}.md"})
        for forbidden in ("repeat-drafts.json", "drafts-written.json"):
            self.assertFalse((self.root / "kit" / forbidden).exists())
        code, _, err = run("kit-preserve", "--directory", self.root, "--agency", A)  # rerun refuses, nothing sent
        self.assertNotEqual(code, 0)
        self.assertIn("already exists", err)
        self.assertEqual(snapshot(self.root), after)
        code, _, err = run("kit-preserve", "--directory", self.root, "--agency", A, "--request", "nope")
        self.assertNotEqual(code, 0)
        with mock.patch("socket.socket", side_effect=AssertionError("socket used")):
            self.unlink()
            self.assertTrue(self.make()["written"])


if __name__ == "__main__":
    unittest.main()
