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
        self.assertEqual(result, {"written": f"kit/preservation/{A}--main.md", "agency_id": A, "request": "main",
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
        self.assertEqual(stat.S_IMODE((folder / f"{A}--main.md").stat().st_mode), 0o600)
        self.refuses("no hand-sent request", A, "missing")  # exact identity: no parent or main fallback
        self.send(B, "main", "2026-03-03")
        self.assertIn("2026-03-03", self.letter(self.make(B)))

    def test_conditional_scope_never_expands(self):
        letters = []
        for index, body in enumerate(("Only audit logs, 2020-01-01 to 2026-01-01. plate image 123ABC.\n",
                                      "Only sharing logs.\n", "agreements only\n", "# Hand edited\n\nanything\n")):
            self.send(A, "main", "2026-03-02", "R0123-110125", text=body)
            letters.append(self.letter(self.make()))
            (self.root / "kit" / "preservation" / f"{A}--main.md").unlink()
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
        path = self.root / "kit" / "preservation" / f"{agency}--main.md"
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
                self.assertFalse((self.root / "kit" / "preservation" / f"{A}--main.md").exists())
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
        self.assertEqual(json.loads(out.rsplit("Draft only", 1)[0])["written"], f"kit/preservation/{A}--main.md")
        after = snapshot(self.root)
        for name in ("sent.json", "agencies.json", f"requests/{A}.md"):
            self.assertEqual(after[f"kit/{name}"], before[f"kit/{name}"])  # bytes and mtime unchanged
        self.assertEqual(set(after) - set(before), {f"kit/preservation/{A}--main.md"})
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

    def test_symlinked_campaign_root_refused(self):
        link = self.root.parent / "campaign-link"
        link.symlink_to(self.root, target_is_directory=True)
        before = snapshot(self.root)
        with self.assertRaises(ValueError) as caught:  # callable API
            kit_preserve.preserve_request(link, A, today=TODAY)
        self.assertIn("campaign folder", str(caught.exception))
        code, out, err = run("kit-preserve", "--directory", link, "--agency", A)  # CLI keeps the link visible
        self.assertNotEqual(code, 0)
        self.assertEqual(out, "")
        self.assertIn("campaign folder", err)
        self.assertEqual(snapshot(self.root), before)
        self.assertFalse((self.root / "kit" / "preservation").exists())

    def history(self, steps, agency=A):
        self.sent[agency]["steps"] = steps
        self.save_sent()

    def test_invalid_target_history_refused_before_output(self):
        ack = {"on": "2026-03-10", "kind": "acknowledged"}
        bad = {
            "none step": [None], "string step": ["acknowledged"],
            "unknown kind": [{"on": "2026-03-10", "kind": "mystery"}],
            "missing on": [{"kind": "acknowledged"}],
            "invalid on": [{"on": "2026-13-40", "kind": "acknowledged"}],
            "extra key": [{**ack, "bogus": 1}],
            "illegal partial": [{**ack, "partial": True}],
            "negative fee": [{"on": "2026-03-10", "kind": "fee_estimate", "amount_cents": -5}],
            "bool fee": [{"on": "2026-03-10", "kind": "fee_estimate", "amount_cents": True}],
            "missing fee": [{"on": "2026-03-10", "kind": "fee_estimate"}],
            "before sent": [{"on": "2026-03-01", "kind": "acknowledged"}],
            "decreasing": [{"on": "2026-03-10", "kind": "acknowledged"}, {"on": "2026-03-09", "kind": "extension"}],
            "future step": [{"on": "2026-11-02", "kind": "acknowledged"}],
            "duplicate ack": [ack, {**ack, "on": "2026-03-11"}],
            "clarified without request": [{"on": "2026-03-10", "kind": "clarified"}],
            "valid prefix then bad": [ack, {"on": "2026-03-11", "kind": "mystery"}],
        }
        for name, steps in bad.items():
            self.history(steps)
            before = snapshot(self.root)
            with mock.patch.object(kit_preserve, "_verify_draft") as verify, \
                    mock.patch.object(kit_preserve, "_retention") as retention, \
                    mock.patch.object(kit_preserve, "legal_notes") as notes:
                with self.assertRaises(ValueError, msg=name):
                    self.make()
            for hook in (verify, retention, notes):
                hook.assert_not_called()
            self.assertEqual(snapshot(self.root), before, name)
            self.assertFalse((self.root / "kit" / "preservation").exists(), name)

    def test_sent_date_bounds_include_empty_histories(self):
        for bad in ("2026-11-02", "1999-12-31"):
            self.sent[A]["sent_on"] = bad
            self.save_sent()
            self.refuses("sent date for", A)
        for good in ("2000-01-01", "2026-11-01"):
            self.sent[A]["sent_on"] = good
            self.save_sent()
            self.assertIn(good, self.letter(self.make()))
            self.unlink()
        self.sent[A]["sent_on"] = "2026-11-01"
        self.history([{"on": "2026-11-01", "kind": "acknowledged"}])
        self.assertTrue(self.make()["written"])

    def test_explicit_today_reaches_replay(self):
        self.history([{"on": "2026-11-02", "kind": "acknowledged"}])
        self.refuses("future", A)
        result = kit_preserve.preserve_request(self.root, A, today=date(2026, 11, 2))
        self.assertTrue(result["written"])

    def test_sibling_history_isolation(self):
        self.send(A, "second-ask", "2026-04-01")
        self.sent[kit_track._key(A, "second-ask")]["steps"] = [None]
        self.save_sent()
        before = (self.root / "kit" / "sent.json").read_bytes()
        self.assertTrue(self.make()["written"])  # malformed sibling does not block a valid target
        self.assertEqual((self.root / "kit" / "sent.json").read_bytes(), before)
        self.refuses("malformed steps", A, "second-ask")  # nor does a valid main mask a bad named target
        self.history([None])
        self.send(A, "third", "2026-05-01")
        self.refuses("malformed steps", A)

    def test_invalid_history_cli_and_existing_private_tree(self):
        first = self.make()
        folder = self.root / "kit" / "preservation"
        (folder / "sentinel.txt").write_text("keep", encoding="utf-8")
        mode = stat.S_IMODE(folder.stat().st_mode)
        self.history([{"on": "2026-03-01", "kind": "acknowledged"}])
        before = snapshot(self.root)
        members = sorted(os.listdir(folder))
        code, out, err = run("kit-preserve", "--directory", self.root, "--agency", A, "--request", "main")
        self.assertNotEqual(code, 0)
        self.assertEqual(out, "")
        self.assertIn("before the request was sent", err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(snapshot(self.root), before)
        self.assertEqual(sorted(os.listdir(folder)), members)
        self.assertEqual(stat.S_IMODE(folder.stat().st_mode), mode)
        self.assertTrue((self.root / first["written"]).exists())

    def test_valid_histories_still_draft(self):
        ref = self.letter(self.make())
        self.unlink()
        self.history([{"on": "2026-03-02", "kind": "acknowledged"}, {"on": "2026-03-02", "kind": "extension"}])
        self.assertEqual(self.letter(self.make()), ref)  # valid history leaves the letter bytes unchanged

    def tree(self):
        return {str(p.relative_to(self.root)): (p.lstat().st_mode, p.lstat().st_mtime_ns,
                p.read_bytes() if p.is_file() and not p.is_symlink() else None)
                for p in (self.root, *self.root.rglob("*"))}

    def source(self, data, request="main"):
        path = self.root / "kit" / "requests" / f"{kit_track._stem(A, request)}.md"
        path.write_bytes(data)
        self.sent[kit_track._key(A, request)]["draft_sha256"] = hashlib.sha256(data).hexdigest()
        self.save_sent()
        return path

    @contextlib.contextmanager
    def source_io(self, path, *, before_open=None, after_open=None, after_stat=None,
                  chunk=None, failure=None):
        real_open, real_stat, real_read, real_close = os.open, os.fstat, os.read, os.close
        events = {"fds": [], "reads": [], "closed": []}

        def opening(name, flags, *args, **kwargs):
            if Path(name) != path:
                return real_open(name, flags, *args, **kwargs)
            self.assertEqual(flags & (os.O_NOFOLLOW | os.O_NONBLOCK), os.O_NOFOLLOW | os.O_NONBLOCK)
            if before_open:
                before_open()
            if failure == "open":
                raise OSError("synthetic open failure")
            fd = real_open(name, flags, *args, **kwargs)
            events["fds"].append(fd)
            if after_open:
                after_open()
            return fd

        def inspecting(fd):
            result = real_stat(fd)
            if fd in events["fds"]:
                if failure == "fstat":
                    raise OSError("synthetic fstat failure")
                if after_stat:
                    after_stat()
            return result

        def reading(fd, count):
            if fd not in events["fds"]:
                return real_read(fd, count)
            self.assertGreater(count, 0)
            self.assertLessEqual(count, kit_preserve.MAX_DRAFT + 1 - sum(events["reads"]))
            if failure == "read":
                raise OSError("synthetic read failure")
            data = real_read(fd, min(count, chunk) if chunk else count)
            events["reads"].append(len(data))
            return data

        def closing(fd):
            if fd in events["fds"]:
                events["closed"].append(fd)
            return real_close(fd)

        with mock.patch("os.open", side_effect=opening), mock.patch("os.fstat", side_effect=inspecting), \
                mock.patch("os.read", side_effect=reading), mock.patch("os.close", side_effect=closing):
            yield events
        self.assertEqual(events["closed"], events["fds"])
        for fd in events["fds"]:
            with self.assertRaises(OSError):
                real_stat(fd)

    def test_growth_after_real_fstat_refuses_before_hash_and_output(self):
        for size in (kit_preserve.MAX_DRAFT + 1, kit_preserve.MAX_DRAFT * 3):
            with self.subTest(size=size):
                grown = b"\xff" + b"x" * (size - 1)
                path = self.source(grown)
                path.write_bytes(grown[:5])
                state = []

                def grow():
                    with path.open("ab") as handle:
                        handle.write(grown[5:])
                    state.append(self.tree())

                with self.source_io(path, after_stat=grow) as events, \
                        mock.patch.object(kit_preserve.hashlib, "sha256", side_effect=AssertionError("hash before admission")), \
                        mock.patch.object(kit_preserve, "_retention") as retention, \
                        mock.patch.object(kit_preserve, "legal_notes") as notes:
                    with self.assertRaisesRegex(ValueError, "larger than 1 MiB"):
                        self.make()
                self.assertEqual(sum(events["reads"]), kit_preserve.MAX_DRAFT + 1)
                self.assertEqual(self.tree(), state[0])
                retention.assert_not_called()
                notes.assert_not_called()

    def test_short_reads_exact_cap_and_utf8_byte_accounting(self):
        for size in (0, kit_preserve.MAX_DRAFT - 1, kit_preserve.MAX_DRAFT, kit_preserve.MAX_DRAFT + 1):
            with self.subTest(size=size):
                data = b"x" * (size % 2) + "é".encode() * (size // 2)
                path = self.source(data)
                with self.source_io(path, chunk=7919) as events:
                    if size > kit_preserve.MAX_DRAFT:
                        with self.assertRaisesRegex(ValueError, "larger than 1 MiB"):
                            self.make()
                    else:
                        self.assertEqual(self.make()["written"], f"kit/preservation/{A}--main.md")
                self.assertEqual(sum(events["reads"]), size if size <= kit_preserve.MAX_DRAFT else 0)
                if size <= kit_preserve.MAX_DRAFT:
                    self.assertEqual(events["reads"][-1], 0)
                    self.unlink()

    def test_leaf_replaced_before_open_is_not_followed_or_blocked(self):
        outside = self.root.parent / "outside.md"
        for kind in ("live", "dangling", "directory", "fifo"):
            with self.subTest(kind=kind):
                path = self.source(b"same bytes")
                outside.write_bytes(b"same bytes")
                outside_before = (outside.read_bytes(), outside.stat().st_mtime_ns)
                state = []

                def replace():
                    path.unlink()
                    if kind == "directory":
                        path.mkdir()
                    elif kind == "fifo":
                        os.mkfifo(path)
                    else:
                        path.symlink_to(outside if kind == "live" else self.root.parent / "missing")
                    state.append(self.tree())

                with self.source_io(path, before_open=replace) as events:
                    with self.assertRaises((ValueError, OSError)):
                        self.make()
                self.assertEqual(events["reads"], [])
                self.assertEqual(self.tree(), state[0])
                self.assertEqual((outside.read_bytes(), outside.stat().st_mtime_ns), outside_before)
                if path.is_dir():
                    path.rmdir()
                else:
                    path.unlink()

    def test_open_descriptor_remains_bound_after_pathname_replacement(self):
        for replacement in ("file", "link"):
            for matches in (True, False):
                with self.subTest(replacement=replacement, matches=matches):
                    path = self.source(b"original" if matches else b"replacement")
                    path.write_bytes(b"original")
                    outside = self.root.parent / "replacement.md"
                    outside.write_bytes(b"replacement")
                    moved = self.root.parent / "original.md"

                    def replace():
                        path.rename(moved)
                        if replacement == "link":
                            path.symlink_to(outside)
                        else:
                            path.write_bytes(b"replacement")

                    with self.source_io(path, after_open=replace) as events:
                        if matches:
                            self.assertTrue(self.make()["written"])
                        else:
                            with self.assertRaisesRegex(ValueError, "differs from the draft recorded as sent"):
                                self.make()
                    self.assertEqual(sum(events["reads"]), len(b"original"))
                    self.assertEqual(outside.read_bytes(), b"replacement")
                    path.unlink()
                    self.unlink()

    def test_descriptor_closed_on_failures_and_no_unowned_close(self):
        for failure, data, message in (("open", b"ok", "open failure"),
                                       ("fstat", b"ok", "fstat failure"),
                                       ("read", b"ok", "read failure"),
                                       (None, b"\xff", "not UTF-8"),
                                       (None, b"changed", "differs from the draft")):
            with self.subTest(failure=failure, message=message):
                path = self.source(b"ok")
                path.write_bytes(data)
                before = self.tree()
                with self.source_io(path, failure=failure), \
                        self.assertRaisesRegex((ValueError, OSError), message):
                    self.make()
                self.assertEqual(self.tree(), before)

    def test_valid_letter_bytes_match_prechange_main_and_named(self):
        # Captured from origin/main's verifier using these same synthetic inputs.
        expected = {"main": "2d44822b224ef4df624a8ae6f70904b198304b23f004a7b55dae12b1edc6cf6f",  # pragma: allowlist secret
                    "named": "a720e6cc7c2b6572336787dbd542ad282fd997a52bf7354932eb11b85adae94e"}  # pragma: allowlist secret
        self.send(A, "named", "2026-03-02")
        for request, digest in expected.items():
            result = self.make(A, request)
            self.assertEqual(result, {"written": f"kit/preservation/{A}--{request}.md", "agency_id": A,
                                      "request": request, "note": "Draft only. Nothing was sent."})
            self.assertEqual(hashlib.sha256((self.root / result["written"]).read_bytes()).hexdigest(), digest)

    def test_replaced_named_source_cli_has_normal_error_boundary(self):
        self.send(A, "named", "2026-03-02")
        path = self.root / "kit" / "requests" / f"{A}--named.md"
        outside = self.root.parent / "outside.md"
        outside.write_bytes(path.read_bytes())
        state = []

        def replace():
            path.unlink()
            path.symlink_to(outside)
            state.append(self.tree())

        with self.source_io(path, before_open=replace):
            code, out, err = run("kit-preserve", "--directory", self.root, "--agency", A, "--request", "named")
        self.assertNotEqual(code, 0)
        self.assertEqual(out, "")
        self.assertTrue(err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(self.tree(), state[0])

    def test_source_refusal_api_cli_preserves_private_tree(self):
        for request in ("main", "named"):
            self.send(A, request, "2026-03-02")
            for existing in (False, True):
                folder = self.root / "kit" / "preservation"
                if existing:
                    folder.mkdir(mode=0o700)
                    (folder / "old.md").write_bytes(b"old private letter")
                for use_cli in (False, True):
                    with self.subTest(request=request, existing=existing, cli=use_cli):
                        data = b"x" * (kit_preserve.MAX_DRAFT + 1)
                        path = self.source(data, request)
                        path.write_bytes(b"x")
                        state = []

                        def grow():
                            path.write_bytes(data)
                            state.append(self.tree())

                        with self.source_io(path, after_stat=grow), \
                                mock.patch("socket.socket", side_effect=AssertionError("network used")):
                            if use_cli:
                                code, out, err = run("kit-preserve", "--directory", self.root,
                                                     "--agency", A, "--request", request)
                                self.assertNotEqual(code, 0)
                                self.assertEqual(out, "")
                                self.assertIn("larger than 1 MiB", err)
                                self.assertNotIn("Traceback", err)
                            else:
                                with self.assertRaisesRegex(ValueError, "larger than 1 MiB"):
                                    self.make(A, request)
                        self.assertEqual(self.tree(), state[0])
                if existing:
                    (folder / "old.md").unlink()
                    folder.rmdir()

    @contextlib.contextmanager
    def output_io(self, request="main", *, open_error=None, fdopen_error=None, wrap=None, close_error=None):
        """Forward the real output open/close/fdopen for one leaf; record raw output descriptors only."""
        target = self.root / "kit" / "preservation" / f"{A}--{request}.md"
        real_open, real_close, real_fdopen = os.open, os.close, os.fdopen
        events = {"fds": [], "raw_closed": [], "fdopen": []}

        def opening(name, flags, *args, **kwargs):
            if Path(name) != target:
                return real_open(name, flags, *args, **kwargs)
            if open_error:
                raise open_error
            fd = real_open(name, flags, *args, **kwargs)
            events["fds"].append(fd)
            return fd

        def closing(fd):
            if fd not in events["fds"]:
                return real_close(fd)
            events["raw_closed"].append(fd)
            real_close(fd)
            if close_error:
                raise close_error

        def fdopening(fd, *args, **kwargs):
            events["fdopen"].append(fd)
            if fdopen_error:
                raise fdopen_error  # before any stream exists: nothing owns the descriptor yet
            stream = real_fdopen(fd, *args, **kwargs)
            return wrap(stream) if wrap else stream

        with mock.patch("os.open", side_effect=opening), mock.patch("os.close", side_effect=closing), \
                mock.patch("os.fdopen", side_effect=fdopening), \
                mock.patch("socket.socket", side_effect=AssertionError("network used")):
            yield events

    def assert_closed(self, fd):
        with self.assertRaises(OSError) as caught:
            os.fstat(fd)
        self.assertEqual(caught.exception.errno, 9)

    def sentinel(self):
        fd = os.open(os.devnull, os.O_RDONLY)
        self.addCleanup(lambda: os.path.exists("/proc/self/fd/%d" % fd) and os.close(fd))
        return fd

    def test_output_fdopen_failure_closes_real_descriptor(self):
        sentinel = self.sentinel()
        error = PermissionError(13, "denied")
        with self.output_io(fdopen_error=error) as events:
            with self.assertRaises(PermissionError) as caught:
                self.make()
        self.assertIs(caught.exception, error)
        self.assertEqual(len(events["fds"]), 1)
        self.assertEqual(events["raw_closed"], events["fds"])
        self.assert_closed(events["fds"][0])
        self.assertFalse((self.root / "kit" / "preservation" / f"{A}--main.md").exists())
        os.fstat(sentinel)

    def test_repeated_output_handoff_failures_do_not_accumulate_resources(self):
        self.send(A, "named", "2026-03-02")
        sentinel = self.sentinel()
        folder = self.root / "kit" / "preservation"
        for request in ("main", "named"):
            baseline = self.make(A, request)
            expected = (self.root / baseline["written"]).read_bytes()
            (self.root / baseline["written"]).unlink()
            for existing in (False, True):
                if not existing:
                    folder.rmdir()
                with self.subTest(request=request, existing=existing):
                    before = snapshot(self.root)
                    for _ in range(3):
                        with self.output_io(request, fdopen_error=PermissionError(13, "denied")) as events:
                            with self.assertRaises(PermissionError):
                                self.make(A, request)
                        self.assertEqual(len(events["fds"]), 1)
                        self.assert_closed(events["fds"][0])
                        self.assertFalse((folder / f"{A}--{request}.md").exists())
                        self.assertEqual(snapshot(self.root), before)
                    os.fstat(sentinel)
                    result = self.make(A, request)  # fault cleared: the same letter as before
                    self.assertEqual(result, baseline)
                    path = self.root / result["written"]
                    self.assertEqual(path.read_bytes(), expected)
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                    self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
                    path.unlink()

    def test_output_open_failure_never_closes_unowned_descriptor(self):
        sentinel = self.sentinel()
        before = snapshot(self.root)
        error = OSError(28, "synthetic open failure")
        with self.output_io(open_error=error) as events:
            with self.assertRaises(OSError) as caught:
                self.make()
        self.assertIs(caught.exception, error)
        self.assertEqual((events["fds"], events["fdopen"], events["raw_closed"]), ([], [], []))
        self.assertEqual(snapshot(self.root), before)
        os.fstat(sentinel)
        self.assertEqual(self.make()["written"], f"kit/preservation/{A}--main.md")  # retry works

    def test_output_stream_transfer_closes_once_on_success_and_io_failure(self):
        outer = self

        class Forwarding:
            def __init__(self, real, fail):
                self.real, self.fail = real, fail

            def write(self, text):
                if self.fail == "write":
                    self.real.write(text[:10])
                    raise OSError(5, "synthetic write failure")
                result = self.real.write(text)
                if self.fail == "flush":
                    self.real.flush()
                    raise OSError(5, "synthetic flush failure")
                return result

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.real.close()  # the stream owns and closes the real descriptor
                if self.fail == "close":
                    raise OSError(5, "synthetic close failure")

        sentinel = self.sentinel()
        path = self.root / "kit" / "preservation" / f"{A}--main.md"
        baseline = self.make()
        expected = path.read_bytes()
        path.unlink()
        for fail in (None, "write", "flush", "close"):
            with self.subTest(fail=fail):
                with self.output_io(wrap=lambda real: Forwarding(real, fail)) as events:
                    if fail:
                        with self.assertRaisesRegex(OSError, f"synthetic {fail} failure"):
                            self.make()
                        self.assertFalse(path.exists())
                    else:
                        self.assertEqual(self.make(), baseline)
                        self.assertEqual(path.read_bytes(), expected)
                        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                        path.unlink()
                self.assertEqual(len(events["fds"]), 1)
                self.assertEqual((events["fdopen"], events["raw_closed"]), (events["fds"], []))
                outer.assert_closed(events["fds"][0])
                os.fstat(sentinel)
        self.assertEqual(self.make(), baseline)  # retry after the failures

    def test_output_handoff_cleanup_retains_primary_failure(self):
        path = self.root / "kit" / "preservation" / f"{A}--main.md"
        for error in (OSError(13, "denied"), ValueError("synthetic"), MemoryError(), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):
                with self.output_io(fdopen_error=error) as events:
                    with self.assertRaises(type(error)) as caught:
                        self.make()
                self.assertIs(caught.exception, error)
                self.assertEqual(events["raw_closed"], events["fds"])
                self.assert_closed(events["fds"][0])
                self.assertFalse(path.exists())
        primary = PermissionError(13, "denied")
        with self.output_io(fdopen_error=primary, close_error=OSError(5, "synthetic close failure")) as events:
            with self.assertRaises(PermissionError) as caught:
                self.make()
        self.assertIs(caught.exception, primary)
        self.assertEqual(len(events["raw_closed"]), 1)  # one attempt, never retried
        self.assert_closed(events["fds"][0])
        self.assertFalse(path.exists())

    def test_output_handoff_cli_failure_is_controlled_and_retryable(self):
        self.send(A, "named", "2026-03-02")
        sentinel = self.sentinel()
        for request in ("main", "named"):
            with self.subTest(request=request):
                before = snapshot(self.root)
                with self.output_io(request, fdopen_error=PermissionError(13, "synthetic denied")) as events:
                    code, out, err = run("kit-preserve", "--directory", self.root, "--agency", A,
                                         "--request", request)
                self.assertEqual((code, out), (1, ""))
                self.assertIn("Stopped:", err)
                self.assertIn("Fix:", err)
                self.assertNotIn("Traceback", err)
                self.assertEqual(len(events["fds"]), 1)
                self.assert_closed(events["fds"][0])
                self.assertEqual(snapshot(self.root), before)
                os.fstat(sentinel)
                with mock.patch("socket.socket", side_effect=AssertionError("network used")):
                    code, out, err = run("kit-preserve", "--directory", self.root, "--agency", A,
                                         "--request", request)
                self.assertEqual(code, 0)
                self.assertIn("Draft only. Nothing was sent.", out)
                (self.root / "kit" / "preservation" / f"{A}--{request}.md").unlink()


if __name__ == "__main__":
    unittest.main()
