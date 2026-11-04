"""DF0237-01: source-linked agency actions outside a recorded meeting outcome (``content/agency-actions.json``).

Offline, synthetic agencies and sources only. A reader sees what the agency's own source says, the source link and the
day an organizer checked it; the site never infers an action, credits a change or states a legal conclusion.
"""
import errno
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from campaign_tool import agency_actions, law
from campaign_tool.agency_actions import (ACTION_KINDS, ActionError, action_problems, add_action, parse_index, read_actions,
                                          remove_action, validate_actions)
from campaign_tool.content import ContentError, load_content
from campaign_tool.content_check import content_problems
from campaign_tool.legal_claims import site_notes
from campaign_tool.site import PUBLIC_ACTION_FIELDS, build_site, e

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
SCHEMA = json.loads((REPO_ROOT / "schemas" / "agencies-open-data.schema.json").read_text(encoding="utf-8"))
TODAY = date(2026, 11, 1)
POLICE, SHERIFF, COUNCIL = "ca-cedar-police", "ca-cedar-county-sheriff", "ca-cedar-city-council"
LABEL = "content/agency-actions.json"
SECRET = "ZQ-SECRET-STATEMENT-9f3"


def row(agency=POLICE, on="2026-09-01", kind="policy_adopted", text="Council adopted the policy",
        url="https://example.invalid/cedar/policy", checked="2026-10-01"):
    return {"agency_id": agency, "on": on, "kind": kind, "as_stated": text, "source_url": url, "checked_on": checked}


def document(*rows):
    return {"schema_version": 1, "actions": list(rows)}


def refuse_network(*args, **kwargs):
    raise AssertionError("network access attempted")


class Base(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-actions-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.root = self.tmp / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.path = self.root / "content" / "agency-actions.json"
        self.agencies = [{"agency_id": a} for a in (POLICE, SHERIFF, COUNCIL)]

    def put(self, doc):
        self.path.write_text(json.dumps(doc), encoding="utf-8")

    def add(self, **fields):
        fields = {**dict(on="2026-09-01", kind="policy_adopted", as_stated="Council adopted the policy",
                         source_url="https://example.invalid/cedar/policy", checked_on="2026-10-01"), **fields}
        return add_action(self.root, fields.pop("agency", POLICE), today=TODAY, **fields)

    def stored(self):
        return json.loads(self.path.read_text(encoding="utf-8"))["actions"]

    def refused(self, call, *fragments):
        with self.assertRaises(ActionError) as caught:
            call()
        for fragment in fragments:
            self.assertIn(fragment, str(caught.exception))
        return str(caught.exception)

    def tree(self, base):
        return {p.relative_to(base).as_posix(): p.read_bytes() for p in base.rglob("*") if p.is_file()}

    def build(self, today=TODAY):
        with mock.patch("urllib.request.urlopen", side_effect=refuse_network) as urlopen:
            build_site(self.root, today=today)
        self.assertEqual(urlopen.call_count, 0)
        return self.tree(self.root / "public")

    def cli(self, *args, directory=True):
        extra = ["--directory", str(self.root)] if directory else []
        return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "content", "agency-action", *args, *extra],
                              cwd=REPO_ROOT, capture_output=True, text=True, timeout=120)


ADD_FLAGS = ("--agency", POLICE, "--on", "2026-09-01", "--kind", "policy_adopted", "--as-stated", "Council adopted the policy",
             "--source-url", "https://example.invalid/cedar/policy", "--checked-on", "2026-10-01")


class ValidationTests(Base):
    def test_all_kinds_dates_limits_duplicates(self):
        self.assertEqual(ACTION_KINDS, ("policy_adopted", "policy_amended", "policy_posted", "sharing_started", "sharing_ended",
                                        "retention_changed", "contract_signed", "contract_renewed", "contract_ended",
                                        "contract_paused", "cameras_added", "cameras_removed", "other"))
        every = [row(kind=kind, url=f"https://example.invalid/{kind}") for kind in ACTION_KINDS]
        self.assertEqual(validate_actions(document(*every), self.agencies, today=TODAY), every)
        self.assertEqual(validate_actions(document(), self.agencies, today=TODAY), [])
        self.refused(lambda: validate_actions(document(row(kind="Policy_adopted")), self.agencies, today=TODAY),
                     f"{LABEL}.actions[0].kind: choose one of")
        # 1 / 50 / 51 rows for one agency; the same tuple for another agency is not a duplicate
        many = [row(url=f"https://example.invalid/p/{n}") for n in range(50)]
        self.assertEqual(len(validate_actions(document(*many), self.agencies, today=TODAY)), 50)
        self.assertEqual(len(validate_actions(document(*many[:1]), self.agencies, today=TODAY)), 1)
        self.refused(lambda: validate_actions(document(*many, row(url="https://example.invalid/p/50")), self.agencies, today=TODAY),
                     f"{LABEL}.actions[50].agency_id: more than 50 actions for {POLICE}")
        self.assertEqual(len(validate_actions(document(*many, row(agency=SHERIFF)), self.agencies, today=TODAY)), 51)
        self.assertEqual(len(validate_actions(document(row(), row(agency=COUNCIL)), self.agencies, today=TODAY)), 2)
        # a duplicate agency + on + kind + source_url is refused even with a different statement or checked date
        message = self.refused(lambda: validate_actions(document(row(), row(text="Other words", checked="2026-10-02")),
                                                        self.agencies, today=TODAY),
                               f"{LABEL}.actions[1].agency_id: duplicate", POLICE)
        self.assertNotIn("Other words", message)
        self.assertNotIn("example.invalid", message)
        self.assertEqual(len(validate_actions(document(row(), row(on="2026-09-02"), row(kind="policy_amended"),
                                                       row(url="https://example.invalid/other")), self.agencies, today=TODAY)), 4)
        # unknown and unselected agencies are named by their validated id; a malformed one only by row and field
        self.refused(lambda: validate_actions(document(row(agency="ca-nowhere")), self.agencies, today=TODAY),
                     f"{LABEL}.actions[0].agency_id: unknown agency ca-nowhere")
        message = self.refused(lambda: validate_actions(document(row(agency="Not An ID <b>")), self.agencies, today=TODAY),
                               f"{LABEL}.actions[0].agency_id: invalid agency identifier")
        self.assertNotIn("Not An ID", message)
        for bad in (None, True, 7, ["x"]):
            self.refused(lambda: validate_actions(document(row(agency=bad)), self.agencies, today=TODAY), "invalid agency identifier")

    def test_dates_are_real_ordered_and_never_inferred(self):
        ok = lambda **kw: validate_actions(document(row(**kw)), self.agencies, today=TODAY)
        ok(on="2024-02-29", checked="2024-02-29")  # leap day
        ok(on="2026-11-01", checked="2026-11-01")  # on == checked == today
        ok(on="2026-10-31", checked="2026-11-01")
        for bad in ("2023-02-29", "2026-13-01", "2026-1-5", "2026-01-05T00:00:00", " 2026-01-05", "2026-01-05\n",
                    "２０２６-０１-０５", "", None, True, 20260105, "2026-00-10"):
            self.refused(lambda: ok(on=bad, checked="2026-10-01"), f"{LABEL}.actions[0].on: must be a real YYYY-MM-DD date")
            self.refused(lambda: ok(on="2026-01-05", checked=bad), f"{LABEL}.actions[0].checked_on: must be a real YYYY-MM-DD date")
        self.refused(lambda: ok(on="2026-10-02", checked="2026-10-01"), f"{LABEL}.actions[0].on: must not be after checked_on")
        self.refused(lambda: ok(on="2026-10-01", checked="2026-11-02"), f"{LABEL}.actions[0].checked_on: must not be in the future")
        self.refused(lambda: ok(on="2026-11-02", checked="2026-11-02"), "must not be in the future")

    def test_envelope_rows_and_json_are_strict(self):
        for bad in (None, [], "x", 3, {"actions": []}, {"schema_version": 1}, {"schema_version": 1, "actions": [], "x": 1}):
            self.refused(lambda: validate_actions(bad, self.agencies, today=TODAY), f"{LABEL}: must be an object")
        for version in (True, 2, 0, "1", 1.0, None):
            self.refused(lambda: validate_actions({"schema_version": version, "actions": []}, self.agencies, today=TODAY),
                         f"{LABEL}.schema_version: must be 1")
        for actions in (None, {}, "x", 3):
            self.refused(lambda: validate_actions({"schema_version": 1, "actions": actions}, self.agencies, today=TODAY),
                         f"{LABEL}.actions: must be a list")
        for bad in (None, 3, "x", []):
            self.refused(lambda: validate_actions(document(bad), self.agencies, today=TODAY), f"{LABEL}.actions[0]: must be an object")
        extra = self.refused(lambda: validate_actions(document({**row(), "secret_note": SECRET}), self.agencies, today=TODAY),
                             f"{LABEL}.actions[0]: unknown field not allowed")
        self.assertNotIn("secret_note", extra)
        missing = row()
        del missing["checked_on"]
        self.refused(lambda: validate_actions(document(row(), missing), self.agencies, today=TODAY), f"{LABEL}.actions[1].checked_on: missing")
        for key in ("as_stated", "source_url"):
            for value in (None, True, 5, ["x"]):
                self.refused(lambda: validate_actions(document({**row(), key: value}), self.agencies, today=TODAY), f".{key}:")
        # file reader: duplicate keys, broken, oversize and non-UTF-8 JSON are refused without dumping the file
        for text, fragment in (('{"schema_version": 1, "schema_version": 1, "actions": []}', "repeats a key"),
                               ('{"schema_version": 1, "actions": [', "not valid UTF-8 JSON"),
                               ('{"schema_version": NaN, "actions": []}', "not valid UTF-8 JSON"),
                               ("x" * (agency_actions.MAX_BYTES + 1), "file too large")):
            self.path.write_text(text, encoding="utf-8")
            self.refused(lambda: read_actions(self.root, self.agencies, today=TODAY), f"{LABEL}: ", fragment)
        self.path.write_bytes(b"\xff\xfe{}")
        self.refused(lambda: read_actions(self.root, self.agencies, today=TODAY), "not valid UTF-8 JSON")
        self.put(document(row(), {**row(on="2026-09-02"), "agency_id": POLICE}))
        self.assertEqual(len(read_actions(self.root, self.agencies, today=TODAY)), 2)
        self.path.unlink()
        self.assertIsNone(read_actions(self.root, self.agencies, today=TODAY))
        # every problem of a hand-edited store is listed, one line each, none repeating a value
        self.put(document(row(text=SECRET + " <b>"), row(on="2026-09-02", kind="nope"), row(agency="ca-nowhere", on="2026-09-03")))
        found = action_problems(self.root, self.agencies, today=TODAY)
        self.assertEqual([line.split(": ")[0] for line in found],
                         [f"{LABEL}.actions[0].as_stated", f"{LABEL}.actions[1].kind", f"{LABEL}.actions[2].agency_id"])
        self.assertNotIn(SECRET, "\n".join(found))
        self.assertEqual(action_problems(self.root, None, today=TODAY)[-1].split(": ")[0], f"{LABEL}.actions[1].kind")
        self.assertEqual(len(action_problems(self.root, None, today=TODAY)), 2)  # unknown-id noise skipped without agencies

    def test_text_url_and_guard_are_safe(self):
        ok = lambda **kw: validate_actions(document(row(**kw)), self.agencies, today=TODAY)
        for good in ("Café & O'Brien \"adopted\" a policy: 100% — ñ", "x", "x" * 200, "ASCII 'quote' & ampersand; semi", "日本語のテキスト"):
            self.assertEqual(ok(text=good)[0]["as_stated"], good)  # accepted characters are kept verbatim
        for bad in ("", "x" * 201, " leading", "trailing ", "\ttab", "two\nlines", "carriage\rreturn", "bell\x07", "nul\x00",
                    "<b>bold</b>", "a > b", "bidi \u202e override", "zero\u200bwidth", "join\u200dcr", "line\u2028sep", "para\u2029sep",
                    "lone \ud800 surrogate", "\u00a0nbsp-led", None, True, 5):
            with self.subTest(bad=repr(bad)):
                message = self.refused(lambda: ok(text=bad), f"{LABEL}.actions[0].as_stated: ")
                if isinstance(bad, str) and len(bad) > 3:
                    self.assertNotIn(bad, message)
        for url in ("https://example.invalid/a?x=1&y=2#frag", "https://example.invalid", "https://example.invalid/" + "a" * 470):
            self.assertEqual(ok(url=url)[0]["source_url"], url)
        for url in ("http://example.invalid/a", "https://user:pw@example.invalid/a", "https:///nohost", "https://localhost/a", "ftp://example.invalid",
                    "https://example.invalid/" + "a" * 500, "https://exa mple.invalid/", "//example.invalid/a", "", None, 5,
                    "javascript:alert(1)", "https://example.invalid/<x>"):
            with self.subTest(url=url if not isinstance(url, str) else url[:40]):
                message = self.refused(lambda: ok(url=url), f"{LABEL}.actions[0].source_url: ")
                if isinstance(url, str) and url:
                    self.assertNotIn(url, message)
        # the stored statement is never wrapped or rewritten to get past the legal-claim guard
        accusation = "The agency violated the law by sharing"
        done = self.add(as_stated=accusation, on="2026-09-03", source_url="https://example.invalid/b")
        self.assertEqual(self.stored()[0]["as_stated"], accusation)
        self.assertEqual(done["notes"], [f"review-required: {LABEL}.actions[0].as_stated: legal-claim wording (violation-verb)"])
        self.assertNotIn("violated", "".join(done["notes"]))
        self.assertEqual(self.add(on="2026-09-04", as_stated="Council posted its policy")["notes"], [])
        # a faulting guard is a note, not a new block: the action is still recorded
        with mock.patch("campaign_tool.legal_claims.legal_claims", side_effect=RuntimeError("boom")):
            done = self.add(on="2026-09-05", as_stated="Fault case", source_url="https://example.invalid/c")
        self.assertEqual(done["notes"], ["review-required: legal-claim wording (guard-error)"])
        self.assertEqual(len(self.stored()), 3)
        # hand-edited rows get the same located note from the site scan; content-check stays valid (notes are not errors)
        self.put(document(row(text="Cedar PD is in violation of the statute"), row(on="2026-09-02", text="Fine words")))
        self.assertEqual(site_notes(self.root),
                         [f"review-required: {LABEL}.actions[0].as_stated: legal-claim wording (violation-noun)"])
        self.assertFalse([line for line in content_problems(self.root) if "agency-actions" in line])


class EditTests(Base):
    def test_atomic_add_remove_and_refusals(self):
        self.assertFalse(self.path.exists())
        self.assertEqual(self.add(on="2026-09-01"), {"agency_id": POLICE, "index": 1, "notes": []})
        self.assertEqual(oct(self.path.stat().st_mode & 0o777), "0o644")  # a new file is 0644
        self.assertEqual(self.add(agency=COUNCIL, on="2026-08-01", kind="other")["index"], 1)
        self.assertEqual(self.add(on="2026-07-01", kind="sharing_ended")["index"], 2)  # per-agency, stored order
        self.assertEqual(self.add(on="2026-10-01", kind="cameras_added")["index"], 3)
        text = self.path.read_text(encoding="utf-8")
        self.assertTrue(text.endswith("}\n"))
        self.assertEqual(json.loads(text)["schema_version"], 1)
        self.assertEqual([(r["agency_id"], r["kind"]) for r in self.stored()],
                         [(POLICE, "policy_adopted"), (COUNCIL, "other"), (POLICE, "sharing_ended"), (POLICE, "cameras_added")])
        # the removal index counts the agency's rows in STORED order (not the page's newest-first order)
        self.assertEqual(remove_action(self.root, POLICE, 2, today=TODAY), {"agency_id": POLICE, "index": 2})
        self.assertEqual([r["kind"] for r in self.stored()], ["policy_adopted", "other", "cameras_added"])
        for bad in (0, -1, 3, 99, True, False, 1.0, "1", None):
            before = self.path.read_bytes()
            self.refused(lambda: remove_action(self.root, POLICE, bad, today=TODAY), "--index:")
            self.assertEqual(self.path.read_bytes(), before)
        self.refused(lambda: remove_action(self.root, SHERIFF, 1, today=TODAY), f"no actions on record for {SHERIFF}")
        self.refused(lambda: remove_action(self.root, "ca-nowhere", 1, today=TODAY), "--agency: unknown agency")
        self.refused(lambda: remove_action(self.root, 5, 1, today=TODAY), "--agency: unknown agency")
        self.refused(lambda: remove_action(self.root, COUNCIL, 2, today=TODAY), "has 1 action(s)")
        # the index text of the command line: ASCII digits only, no zero, sign, space, decimal or other script
        self.assertEqual(parse_index("12"), 12)
        for bad in ("0", "-1", "+1", "01", "1.0", " 1", "1 ", "", "٣", "1e3", "True", "1_0", "9999999999", None, 3):
            self.refused(lambda: parse_index(bad), "--index:")
        remove_action(self.root, POLICE, 1, today=TODAY)
        remove_action(self.root, POLICE, 1, today=TODAY)
        remove_action(self.root, COUNCIL, 1, today=TODAY)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), {"schema_version": 1, "actions": []})  # valid empty envelope
        self.assertEqual(read_actions(self.root, self.agencies, today=TODAY), [])
        self.path.unlink()
        self.refused(lambda: remove_action(self.root, POLICE, 1, today=TODAY), "does not exist; there is nothing to remove")
        self.assertFalse(self.path.exists())

    def test_refused_add_leaves_the_bytes_and_names_the_field(self):
        self.add()
        before = self.path.read_bytes()
        cases = ((dict(), "duplicate"), (dict(agency="ca-nowhere"), "unknown agency ca-nowhere"),
                 (dict(on="2026-10-02"), ".on: must not be after checked_on"), (dict(checked_on="2026-11-02"), "must not be in the future"),
                 (dict(kind="nope"), ".kind: choose one of"), (dict(as_stated="<b>x</b>"), ".as_stated:"),
                 (dict(source_url="http://example.invalid/x"), ".source_url:"), (dict(on="2026-02-30"), ".on: must be a real"))
        for fields, fragment in cases:
            with self.subTest(fragment=fragment):
                self.refused(lambda: self.add(**fields), f"{LABEL}.actions[1]", fragment)
                self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in self.path.parent.iterdir() if p.name.startswith(".agency-actions")), [])
        # the 51st action of an agency is refused; another agency still has room
        self.put(document(*[row(url=f"https://example.invalid/p/{n}") for n in range(50)]))
        before = self.path.read_bytes()
        self.refused(lambda: self.add(source_url="https://example.invalid/p/50"), "more than 50 actions")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.add(agency=SHERIFF)["index"], 1)
        # an unselected agency is not a known agency, so it is refused by name
        agencies = json.loads((self.root / "content" / "agencies.json").read_text(encoding="utf-8"))
        agencies[0]["selected"] = False
        (self.root / "content" / "agencies.json").write_text(json.dumps(agencies), encoding="utf-8")
        self.refused(lambda: self.add(agency=SHERIFF, on="2026-09-09", source_url="https://example.invalid/z"),
                     f"unknown agency {SHERIFF}")

    def test_invalid_existing_store_refuses_edits_and_keeps_bytes(self):
        for text in ('{"schema_version": 1, "actions": [{"agency_id": "ca-cedar-police"}]}', "not json", "[]",
                     '{"schema_version": 1, "actions": [], "actions": []}'):
            self.path.write_text(text, encoding="utf-8")
            before = self.path.read_bytes()
            self.refused(lambda: self.add(), f"{LABEL}")
            self.refused(lambda: remove_action(self.root, POLICE, 1, today=TODAY), f"{LABEL}")
            self.assertEqual(self.path.read_bytes(), before)
        self.put(document(row(agency="ca-nowhere")))
        before = self.path.read_bytes()
        self.refused(lambda: self.add(agency=COUNCIL), "unknown agency ca-nowhere")
        self.assertEqual(self.path.read_bytes(), before)

    def test_links_special_files_and_missing_folders_are_refused(self):
        real = self.tmp / "real-actions.json"
        real.write_text(json.dumps(document(row())), encoding="utf-8")
        os.symlink(real, self.path)
        self.refused(lambda: self.add(on="2026-09-02"), "symbolic link")
        self.refused(lambda: remove_action(self.root, POLICE, 1, today=TODAY), "symbolic link")
        self.refused(lambda: read_actions(self.root, self.agencies, today=TODAY), "symbolic link")
        self.assertEqual(json.loads(real.read_text(encoding="utf-8")), document(row()))
        self.path.unlink()
        os.symlink(self.tmp / "missing.json", self.path)  # dangling
        self.refused(lambda: self.add(), "symbolic link")
        self.assertFalse((self.tmp / "missing.json").exists())
        self.path.unlink()
        self.put(document(row()))
        os.link(self.path, self.tmp / "second-name.json")  # a multiply-linked target
        self.refused(lambda: self.add(on="2026-09-02"), "regular file with a single link")
        self.assertEqual(json.loads((self.tmp / "second-name.json").read_text(encoding="utf-8")), document(row()))
        os.unlink(self.tmp / "second-name.json")
        self.path.unlink()
        os.mkfifo(self.path)
        self.refused(lambda: self.add(), "regular file with a single link")
        self.refused(lambda: read_actions(self.root, self.agencies, today=TODAY), "regular file with a single link")
        os.unlink(self.path)
        self.path.mkdir()
        self.refused(lambda: self.add(), "regular file with a single link")
        self.path.rmdir()
        # a symlinked content/ folder and a campaign without content/ are refused; nothing is created
        moved = self.tmp / "elsewhere"
        (self.root / "content").rename(moved)
        self.refused(lambda: self.add(), "content/ must be a real folder")
        os.symlink(moved, self.root / "content")
        self.refused(lambda: self.add(), "content/ must be a real folder")
        self.assertFalse((moved / "agency-actions.json").exists())
        (self.root / "content").unlink()
        self.refused(lambda: self.add(), "content/ must be a real folder")
        self.assertFalse((self.root / "content").exists())

    def test_write_failures_leave_the_old_bytes_and_no_temporary_file(self):
        self.add()
        before = self.path.read_bytes()
        os.chmod(self.path, 0o640)
        folder = self.path.parent

        def leftovers():
            return sorted(p.name for p in folder.iterdir() if p.name.startswith(".agency-actions"))

        for target, side_effect in (("campaign_tool.agency_actions.os.replace", OSError(errno.EIO, "replace")),
                                    ("campaign_tool.agency_actions.os.fsync", OSError(errno.ENOSPC, "fsync")),
                                    ("campaign_tool.agency_actions.os.fchmod", PermissionError("chmod")),
                                    ("campaign_tool.agency_actions.tempfile.mkstemp", PermissionError("no temp"))):
            with self.subTest(target=target), mock.patch(target, side_effect=side_effect):
                self.refused(lambda: self.add(on="2026-09-09", source_url="https://example.invalid/w"), f"{LABEL}: write failed")
                self.refused(lambda: remove_action(self.root, POLICE, 1, today=TODAY), f"{LABEL}: write failed")
            self.assertEqual(self.path.read_bytes(), before)
            self.assertEqual(leftovers(), [])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o640)
        self.add(on="2026-09-10", source_url="https://example.invalid/m")
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o640)  # an existing mode is preserved
        self.assertEqual(leftovers(), [])

    def test_target_swapped_before_replacement_is_refused(self):
        self.add()
        other = json.dumps(document(row(text="Someone else wrote this"))).encode()
        real_fsync = os.fsync

        def swap_then_sync(fd):
            replacement = self.path.parent / "swap.tmp"
            replacement.write_bytes(other)
            os.replace(replacement, self.path)  # another writer replaces the file after it was read
            return real_fsync(fd)

        with mock.patch("campaign_tool.agency_actions.os.fsync", side_effect=swap_then_sync):
            self.refused(lambda: self.add(on="2026-09-02"), "changed while it was being edited")
        self.assertEqual(self.path.read_bytes(), other)
        self.assertEqual(sorted(p.name for p in self.path.parent.iterdir() if p.name.startswith(".agency-actions")), [])
        # a file created by someone else while it was still absent is not overwritten either
        self.path.unlink()
        real_fsync = os.fsync

        def create_then_sync(fd):
            self.path.write_bytes(other)
            return real_fsync(fd)

        with mock.patch("campaign_tool.agency_actions.os.fsync", side_effect=create_then_sync):
            self.refused(lambda: self.add(), "changed while it was being edited")
        self.assertEqual(self.path.read_bytes(), other)

    def test_oversize_result_is_refused_before_writing(self):
        self.add()
        before = self.path.read_bytes()
        with mock.patch.object(agency_actions, "MAX_BYTES", len(before) + 10):
            self.refused(lambda: self.add(on="2026-09-02"), "would be larger than")
        self.assertEqual(self.path.read_bytes(), before)


class CommandTests(Base):
    def test_add_and_remove_commands(self):
        added = self.cli("add", *ADD_FLAGS)
        self.assertEqual((added.returncode, added.stdout, added.stderr),
                         (0, f"Agency action added: {POLICE}, index 1.\nContent only; nothing was built, sent or deployed.\n", ""))
        again = self.cli("add", *ADD_FLAGS)
        self.assertEqual(again.returncode, 1)
        self.assertIn(f"Stopped: {LABEL}.actions[1].agency_id: duplicate", again.stderr)
        self.assertEqual(again.stdout, "")
        for flag in ("--agency", "--on", "--kind", "--as-stated", "--source-url", "--checked-on"):
            index = ADD_FLAGS.index(flag)
            partial = ADD_FLAGS[:index] + ADD_FLAGS[index + 2:]
            self.assertEqual(self.cli("add", *partial).returncode, 2, flag)  # argparse: every flag is required
        self.assertEqual(self.cli("add", *ADD_FLAGS, directory=False).returncode, 2)  # explicit directory required
        self.assertEqual(self.cli("add", *ADD_FLAGS[:5], "nope", *ADD_FLAGS[6:]).returncode, 2)
        self.assertEqual(len(self.stored()), 1)
        removed = self.cli("remove", "--agency", POLICE, "--index", "1")
        self.assertEqual((removed.returncode, removed.stdout, removed.stderr),
                         (0, f"Agency action removed: {POLICE}, index 1.\nContent only; nothing was built, sent or deployed.\n", ""))
        self.assertEqual(self.stored(), [])
        for index in ("1", "0", "-1", "1.5", "٣", "x"):
            result = self.cli("remove", "--agency", POLICE, "--index", index)
            self.assertEqual(result.returncode, 1, index)
            self.assertEqual(result.stdout, "")
        self.assertEqual(self.cli("remove", "--agency", POLICE, directory=False).returncode, 2)
        self.assertEqual(self.cli("remove", "--agency", POLICE, "--index", "1", directory=False).returncode, 2)
        # content init stays refused on an existing content/ and creates nothing here
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), {"schema_version": 1, "actions": []})

    def test_legal_note_goes_to_stderr_without_the_text_and_add_succeeds(self):
        flags = list(ADD_FLAGS)
        flags[flags.index("Council adopted the policy")] = "Cedar PD violated the statute"
        result = self.cli("add", *flags)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, f"review-required: {LABEL}.actions[0].as_stated: legal-claim wording (violation-verb)\n")
        self.assertNotIn("violated", result.stdout + result.stderr)
        self.assertEqual(self.stored()[0]["as_stated"], "Cedar PD violated the statute")

    def test_refused_add_prints_no_supplied_text(self):
        flags = list(ADD_FLAGS)
        flags[flags.index("Council adopted the policy")] = SECRET + " <i>"
        result = self.cli("add", *flags)
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"{LABEL}.actions[0].as_stated", result.stderr)
        self.assertNotIn(SECRET, result.stdout + result.stderr)
        self.assertFalse(self.path.exists())
        flags = list(ADD_FLAGS)
        flags[flags.index("https://example.invalid/cedar/policy")] = "http://example.invalid/" + SECRET
        result = self.cli("add", *flags)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn(SECRET, result.stdout + result.stderr)


class PublishTests(Base):
    def seed(self):
        self.add(on="2026-09-01", kind="policy_adopted", as_stated="Council adopted the policy & set 'retention'",
                 source_url="https://example.invalid/cedar/policy?a=1&b=2")
        self.add(on="2026-09-20", kind="sharing_ended", as_stated="Stopped sharing with outside agencies",
                 source_url="https://example.invalid/cedar/sharing", checked_on="2026-10-02")
        self.add(on="2026-09-20", kind="contract_paused", as_stated="Contract paused", source_url="https://example.invalid/cedar/pause")
        self.add(on="2026-09-20", kind="contract_paused", as_stated="Same day, other source", source_url="https://example.invalid/cedar/pause2")
        self.add(agency=SHERIFF, on="2026-08-15", kind="retention_changed", as_stated="Retention is now 30 days",
                 source_url="https://example.invalid/sheriff/retention", checked_on="2026-09-01")

    def line(self, action_on, kind, text, url, checked):
        return (f'<li><time datetime="{action_on}">{action_on}</time>: {kind}: {e(text)} (<a href="{e(url)}" '
                f'rel="noopener noreferrer">source</a>, checked <time datetime="{checked}">{checked}</time>)</li>')

    def test_sections_timeline_export_agree(self):
        baseline = self.build()
        self.assertNotIn("agencies/cedar-county-sheriff.html", baseline)
        self.seed()
        files = self.build()
        page = files["agencies/cedar.html"].decode("utf-8")
        start = page.index("<h3>Changes on record</h3>")
        section = page[start:page.index("</ul>", start)]
        self.assertEqual(page.count("Changes on record"), 1)  # rendered once
        lines = ["<li>" + part for part in section.split("<li>")[1:]]
        self.assertEqual(lines, [  # newest first; the 2026-09-20 ties keep their stored order
            self.line("2026-09-20", "Sharing ended", "Stopped sharing with outside agencies",
                      "https://example.invalid/cedar/sharing", "2026-10-02"),
            self.line("2026-09-20", "Contract paused", "Contract paused", "https://example.invalid/cedar/pause", "2026-10-01"),
            self.line("2026-09-20", "Contract paused", "Same day, other source", "https://example.invalid/cedar/pause2", "2026-10-01"),
            self.line("2026-09-01", "Policy adopted", "Council adopted the policy & set 'retention'",
                      "https://example.invalid/cedar/policy?a=1&b=2", "2026-10-01"),
        ])
        self.assertLess(page.index("Changes on record"), page.index("Published findings"))
        # the detail page only: never on the agency cards page
        self.assertNotIn("Changes on record", files["agencies.html"].decode("utf-8"))
        # an action-only agency now has a linked page and an export row
        self.assertIn("agencies/cedar-county-sheriff.html", files)
        sheriff = files["agencies/cedar-county-sheriff.html"].decode("utf-8")
        self.assertIn("Retention is now 30 days", sheriff)
        self.assertIn('href="/agencies/cedar-county-sheriff.html"', files["agencies.html"].decode("utf-8"))
        # timeline.html: kind agency_action, text AGENCY: KIND: T, the agency page and the official source
        model = load_content(self.root, today=TODAY)
        from campaign_tool.timeline import KINDS, LEAD, timeline_events
        self.assertEqual(KINDS[-1], "agency_action")
        events = [ev for ev in timeline_events(model, TODAY, {"ca-cedar-police": "/agencies/cedar.html"}) if ev["kind"] == "agency_action"]
        self.assertEqual([(ev["date"], ev["text"]) for ev in events], [
            ("2026-09-20", "Cedar Police Department (fictional): Contract paused: Contract paused"),
            ("2026-09-20", "Cedar Police Department (fictional): Contract paused: Same day, other source"),
            ("2026-09-20", "Cedar Police Department (fictional): Sharing ended: Stopped sharing with outside agencies"),
            ("2026-09-01", "Cedar Police Department (fictional): Policy adopted: Council adopted the policy & set 'retention'"),
            ("2026-08-15", "Cedar County Sheriff's Office (fictional): Retention changed: Retention is now 30 days"),
        ])
        self.assertEqual({ev["href"] for ev in events if "Police" in ev["text"]}, {"/agencies/cedar.html"})
        self.assertEqual(events[-1]["source_url"], "https://example.invalid/sheriff/retention")
        timeline = files["timeline.html"].decode("utf-8")
        self.assertIn(LEAD + " Agency actions are transcribed from their cited official sources.</p>", timeline)
        self.assertEqual(timeline.count('rel="noopener noreferrer">official record</a>'),
                         baseline["timeline.html"].decode().count("official record") + 5)
        self.assertIn(">Cedar Police Department (fictional): Policy adopted: Council adopted the policy &amp; set &#x27;retention&#x27;</a>", timeline)
        # data/agencies.json carries them newest first, the page's order, with exactly the five fields
        exported = {a["agency_id"]: a for a in json.loads(files["data/agencies.json"])["agencies"]}
        police = exported[POLICE]["actions"]
        self.assertEqual([(a["on"], a["kind"], a["source_url"]) for a in police],
                         [("2026-09-20", "sharing_ended", "https://example.invalid/cedar/sharing"),
                          ("2026-09-20", "contract_paused", "https://example.invalid/cedar/pause"),
                          ("2026-09-20", "contract_paused", "https://example.invalid/cedar/pause2"),
                          ("2026-09-01", "policy_adopted", "https://example.invalid/cedar/policy?a=1&b=2")])
        self.assertEqual(set(police[0]), {"on", "kind", "as_stated", "source_url", "checked_on"})
        self.assertEqual(police[3]["as_stated"], "Council adopted the policy & set 'retention'")
        self.assertEqual(exported[SHERIFF]["actions"][0]["checked_on"], "2026-09-01")
        self.assertNotIn(COUNCIL, exported)
        # the other kinds (meetings and their decisions, findings, requests) are exactly what they were without actions
        for agency in model["agencies"]:
            agency.pop("actions", None)
        others = [ev for ev in timeline_events(load_content(self.root, today=TODAY), TODAY) if ev["kind"] != "agency_action"]
        self.assertTrue(others == timeline_events(model, TODAY))
        # deterministic: a second build is byte-identical
        self.assertTrue(self.build() == files, "second build differs")
        # no wording that credits the campaign or anyone else with a change
        words = section.lower()
        for phrase in ("campaign", "thanks to", "because of", "as a result", "victory", "won", "pressure", "credit", "our work",
                       "forced", "caused", "led to", "organiz"):
            self.assertNotIn(phrase, words, phrase)

    def test_export_schema_and_no_private_leaks(self):
        self.seed()
        agencies = json.loads((self.root / "content" / "agencies.json").read_text(encoding="utf-8"))
        agencies[1]["profile"]["contract"] = {"vendor": "Synthetic", "start": "2024-01-01", "end": "2027-01-01", "renewal_note": SECRET,
                                              "source_url": "https://example.invalid/c", "checked_on": "2026-09-01"}
        agencies[1]["profile"]["funding"] = [{"kind": "grant", "name": "Synthetic grant", "as_stated_in": "Award letter",
                                              "source_url": "https://example.invalid/f", "checked_on": "2026-09-01"}]
        (self.root / "content" / "agencies.json").write_text(json.dumps(agencies), encoding="utf-8")
        files = self.build()
        document = json.loads(files["data/agencies.json"])
        law.validate(document, SCHEMA)
        police = next(a for a in document["agencies"] if a["agency_id"] == POLICE)
        self.assertEqual(police["funding"][0]["name"], "Synthetic grant")  # funding and contract are unchanged
        self.assertEqual(police["contract"]["vendor"], "Synthetic")
        self.assertNotIn("agency_id", police["actions"][0])  # the parent supplies it
        for name, data in files.items():  # the page shows the contract note itself; the open data never carries it
            if name.startswith("data/"):
                self.assertNotIn(SECRET.encode(), data, name)
        # the schema refuses extra keys, bad kinds, empty and over-long lists and non-https sources
        good = police["actions"][0]
        for broken in ({**good, "agency_id": POLICE}, {**good, "kind": "won"}, {**good, "source_url": "http://x.invalid/"},
                       {**good, "as_stated": ""}, {**good, "on": "2026-9-1"}):
            doc = json.loads(json.dumps(document))
            next(a for a in doc["agencies"] if a["agency_id"] == POLICE)["actions"] = [broken]
            with self.assertRaises(Exception):
                law.validate(doc, SCHEMA)
        doc = json.loads(json.dumps(document))
        next(a for a in doc["agencies"] if a["agency_id"] == POLICE)["actions"] = []
        with self.assertRaises(ValueError):
            law.validate(doc, SCHEMA)
        # law.validate does not read maxLength or maxItems, so the declared limits are pinned directly
        defs = SCHEMA["$defs"]
        self.assertEqual((defs["actions"]["minItems"], defs["actions"]["maxItems"]), (1, 50))
        self.assertEqual((defs["action"]["properties"]["as_stated"]["minLength"], defs["action"]["properties"]["as_stated"]["maxLength"]),
                         (1, 200))
        self.assertEqual(defs["action"]["properties"]["source_url"]["maxLength"], 500)
        self.assertEqual(defs["action"]["properties"]["kind"]["enum"], list(ACTION_KINDS))
        self.assertEqual(defs["action"]["required"], list(PUBLIC_ACTION_FIELDS))
        self.assertIs(defs["action"]["additionalProperties"], False)
        # the CSV header is pinned: actions live in the JSON only, so the spreadsheet copy never changes with the data
        self.assertEqual(files["data/agencies.csv"].decode().splitlines()[0],
                         "agency_id,alpr_policy,asked,camera_count,contract,funding,kind,name,transparency,url")

    def test_absent_empty_and_remove_restore_baseline(self):
        baseline = self.build()
        self.assertFalse(any(b"Changes on record" in data or b"agency_action" in data for data in baseline.values()))
        self.assertNotIn(b'"actions"', baseline["data/agencies.json"])
        self.put(document())  # an empty store is the same as no file
        self.assertEqual(self.build(), baseline)
        self.assertIsNone(load_content(self.root, today=TODAY)["agencies"][0].get("actions"))
        self.seed()
        with_actions = self.build()
        self.assertNotEqual(with_actions, baseline)
        self.assertTrue(set(with_actions) - set(baseline) == {"agencies/cedar-county-sheriff.html"})
        for index in (1, 1, 1, 1):
            remove_action(self.root, POLICE, index, today=TODAY)
        remove_action(self.root, SHERIFF, 1, today=TODAY)
        self.assertEqual(self.build(), baseline)  # last removal and a rebuild: the action-only page is gone again
        self.assertEqual(self.tree(self.root / "public"), baseline)

    def test_invalid_actions_stop_build_and_check_with_safe_paths_and_keep_public(self):
        baseline = self.build()
        inputs = self.tree(self.root / "content")
        for doc, fragment in ((document(row(text=SECRET + "<b>")), f"{LABEL}.actions[0].as_stated"),
                              (document(row(agency="ca-nowhere")), f"{LABEL}.actions[0].agency_id: unknown agency ca-nowhere"),
                              (document(row(), row(text="again")), f"{LABEL}.actions[1].agency_id: duplicate"),
                              (document(row(on="2026-12-01", checked="2026-12-02")), f"{LABEL}.actions[0].checked_on: must not be in the future"),
                              ({"schema_version": 2, "actions": []}, f"{LABEL}.schema_version: must be 1")):
            with self.subTest(fragment=fragment):
                self.put(doc)
                before_inputs = self.tree(self.root / "content")
                with self.assertRaises(ContentError) as caught:
                    self.build()
                self.assertIn(fragment, str(caught.exception))
                self.assertNotIn(SECRET, str(caught.exception))
                self.assertEqual(self.tree(self.root / "public"), baseline)  # pre-existing public/ untouched
                self.assertEqual(self.tree(self.root / "content"), before_inputs)
                problems = content_problems(self.root)
                self.assertTrue(any(line.startswith(fragment) for line in problems), problems)
                self.assertNotIn(SECRET, "\n".join(problems))
        self.path.unlink()
        self.assertEqual(self.tree(self.root / "content"), inputs)
        # the command line: build exits 1 and content-check exits 3, both naming the file, row and field only
        self.put(document(row(agency="ca-nowhere", text=SECRET)))
        built = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "build", "--directory", str(self.root)],
                               cwd=REPO_ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual(built.returncode, 1)
        self.assertIn(f"{LABEL}.actions[0].agency_id: unknown agency ca-nowhere", built.stderr)
        checked = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "content-check", "--directory", str(self.root)],
                                 cwd=REPO_ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual(checked.returncode, 3)
        self.assertIn(f"{LABEL}.actions[0].agency_id: unknown agency ca-nowhere", checked.stdout)
        self.assertNotIn(SECRET, built.stdout + built.stderr + checked.stdout + checked.stderr)

    def test_agency_loading_failure_adds_no_unknown_id_noise(self):
        self.put(document(row()))
        (self.root / "content" / "agencies.json").write_text("{not json", encoding="utf-8")
        lines = content_problems(self.root)
        self.assertTrue(any("agencies.json" in line for line in lines))
        self.assertFalse([line for line in lines if "unknown agency" in line])


if __name__ == "__main__":
    unittest.main()
