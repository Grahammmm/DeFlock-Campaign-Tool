"""RF0844-4: finding publish, correct and withdraw never write a file that the bounded finding readers refuse
(MAX_DRAFT_BYTES of UTF-8, indentation and the final newline included). Synthetic campaign only.

Run from the repository root: python3 -B -m unittest tests.test_findings_output_size -v
"""
import json
import os
import stat
import sys
import unittest
from datetime import timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import findings  # noqa: E402
from tests import test_findings_publish as publish_tests  # noqa: E402  (module import: not re-collected here)

NOW, draft = publish_tests.NOW, publish_tests.draft
LATER = NOW + timedelta(days=2)
STAMP, LATER_STAMP = "2026-03-04T05:06:07Z", "2026-03-06T05:06:07Z"
CAP = findings.MAX_DRAFT_BYTES
NOTE, REASON = "Synthetic correction note 7f3.", "Synthetic withdrawal reason 9c1."
EARLIER = [{"date": "2026-03-04", "note": "Synthetic earlier correction."}]
UPDATES = [{"date": "2026-03-04", "note": "Synthetic update recorded."}]
NEW_UPDATE = {"date": "2026-03-05", "note": "Synthetic follow-up recorded."}


def canonical(document, sort_keys=True):
    return (json.dumps(document, sort_keys=sort_keys, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def compact(document):
    return json.dumps(document, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def published(document, **changes):
    """A canonical published finding with correction and update history (both kept by every later writer)."""
    return dict(findings.reviewed_view(document), state="published", published_at=STAMP, updated_at=STAMP,
                corrections=list(EARLIER), updates=list(UPDATES), **changes)


def fill(document, measure, target, char="x"):
    """Spread ``char`` over 60 limitation strings (each under the 20,000-character plain-text limit, the list under its
    100-entry limit), then ASCII for any remainder, until ``measure(document)`` is exactly ``target`` bytes."""
    document["limitations"] = [""] * 60
    width = len(char.encode("utf-8"))
    remaining = target - len(measure(document))
    for index in range(60):
        count = min(19_000, remaining // width)
        document["limitations"][index] = char * count
        remaining -= count * width
    document["limitations"][-1] += "x" * remaining
    assert len(measure(document)) == target, (len(measure(document)), target)
    return document


# The complete file each writer would produce for a draft or published document ``d``.
def publish_output(d):
    return canonical(dict(findings.reviewed_view(d), state="published", published_at=STAMP, corrections=[]))


def correct_output(existing, replacement=None):
    return canonical(dict(findings.reviewed_view(replacement or existing), state="published", published_at=STAMP,
                          updated_at=LATER_STAMP, corrections=EARLIER + [{"date": "2026-03-05", "note": NOTE}],
                          updates=UPDATES))


def update_output(existing):
    return canonical(dict(existing, updates=UPDATES + [NEW_UPDATE], updated_at=LATER_STAMP))


def withdraw_output(existing):
    return canonical(dict(json.loads(canonical(existing)), withdrawn={"at": LATER_STAMP, "reason": REASON}), False)


class FindingOutputSizeTests(unittest.TestCase):
    # Reuse the DF2237-02 synthetic campaign helpers without re-running its tests (no TestCase bound here).
    _p = vars(publish_tests.FindingPublishTests)
    setUp, cli, register, receipt, approve_all, log_lines, public_tree = (
        _p["setUp"], _p["cli"], _p["register"], _p["receipt"], _p["approve_all"], _p["log_lines"], _p["public_tree"])

    @property
    def target(self):
        return self.content / "findings" / "synthetic-title.json"

    @property
    def archive(self):
        return self.root / "private" / "withdrawn" / "synthetic-title.json"

    def snapshot(self):
        """Every file and folder under the synthetic base (campaign, drafts, receipts, log) with its mode and bytes;
        the existing .publish.lock is accounted for separately: acquiring it is allowed."""
        return {str(p.relative_to(self.base)): (stat.S_IMODE(p.lstat().st_mode), p.read_bytes() if p.is_file() else None)
                for p in sorted(self.base.rglob("*")) if p.name != ".publish.lock"}

    def write_draft(self, document, path=None):
        path = path or self.draft
        path.write_bytes(compact(document))
        self.assertLess(path.stat().st_size, CAP)  # the input itself is readable
        self.approve_all(document=document)
        return path

    def seed(self, document, name="synthetic-title.json"):
        path = self.content / "findings" / name
        path.write_bytes(canonical(document))
        self.assertLessEqual(path.stat().st_size, CAP)
        findings.load_draft(path)  # the bounded reader accepts the starting file
        return path

    def refused(self, call, *withheld):
        before = self.snapshot()
        with self.assertRaises(ValueError) as caught:
            call()
        message = str(caught.exception)
        self.assertIn(f"larger than {CAP} bytes", message)
        self.assertLess(len(message), 300)
        for text in withheld:
            self.assertNotIn(text, message)
        self.assertEqual(self.snapshot(), before)  # no finding, archive, folder, mode or journal change
        return message

    def publish(self):
        return findings.publish(self.root, self.draft, "Pat Organizer", now=NOW)

    def correct(self, from_draft=None):
        return findings.correct(self.root, "syn-pub-1", "2026-03-05", NOTE, from_draft=from_draft, now=LATER,
                                approved_by="Pat Organizer")

    def withdraw(self):
        return findings.withdraw(self.root, "syn-pub-1", REASON, now=LATER)

    def readable(self, path):
        rows = findings.status_rows(self.root)
        self.assertNotIn("unreadable", [row["state"] for row in rows], rows)
        if path.parent.name == "withdrawn":
            self.assertEqual(findings.withdrawn_names(self.root), ({"syn-pub-1"}, {"synthetic-title"}))
            self.assertEqual([row["state"] for row in rows], ["withdrawn"])
        else:
            findings.load_draft(path)

    # --- the five lifecycle writers at an exact prospective size -------------------------------------------------
    def prepare(self, operation, size, char="x"):
        """Set up ``operation`` so its complete output is ``size`` bytes: (call, output path, expected bytes)."""
        if operation == "publish":
            self.write_draft(fill(draft(), publish_output, size, char))
            return self.publish, self.target, publish_output(json.loads(self.draft.read_bytes()))
        if operation == "correct":
            existing = fill(published(draft()), correct_output, size, char)
            self.seed(existing)
            return self.correct, self.target, correct_output(existing)
        if operation == "correct --from":
            self.seed(published(draft()))
            replacement = fill(draft(summary="Synthetic replacement summary."),
                               lambda d: correct_output(None, d), size, char)
            path = self.write_draft(replacement, self.base / "replacement.json")
            return lambda: self.correct(path), self.target, correct_output(None, replacement)
        if operation == "update":
            existing = fill(published(draft()), update_output, size, char)
            self.seed(existing)
            return (lambda: findings.update(self.root, "syn-pub-1", NEW_UPDATE["date"], NEW_UPDATE["note"], now=LATER,
                                            approved_by="Pat Organizer"), self.target, update_output(existing))
        existing = fill(published(draft()), withdraw_output, size, char)
        self.seed(existing)
        return self.withdraw, self.archive, withdraw_output(existing)

    def test_exact_serialized_byte_boundary_for_each_lifecycle_writer(self):
        for operation in ("publish", "correct", "correct --from", "update", "withdraw"):
            for size in (CAP - 1, CAP):
                with self.subTest(operation=operation, size=size):
                    self.setUp()
                    if size == CAP:  # one byte over refuses (update keeps its own, unchanged guard text)
                        call, path, _ = self.prepare(operation, CAP + 1)
                        before = self.snapshot()
                        self.assertRaisesRegex(ValueError, f"larger than {CAP} bytes", call)
                        self.assertEqual(self.snapshot(), before)
                        self.setUp()
                    call, path, expected = self.prepare(operation, size)
                    call()
                    self.assertEqual(path.read_bytes(), expected)  # indentation and final newline counted
                    self.assertEqual(path.stat().st_size, size)
                    self.readable(path)

    def test_multibyte_output_uses_utf8_bytes_not_character_count(self):
        for char in ("é", "\U0001d11e"):
            for operation in ("publish", "withdraw"):
                with self.subTest(char=char, operation=operation):
                    self.setUp()
                    call, path, expected = self.prepare(operation, CAP + 1, char)
                    self.assertLess(len(expected.decode("utf-8")), CAP)  # characters fit, bytes do not
                    self.refused(call, REASON)
                    self.setUp()
                    call, path, expected = self.prepare(operation, CAP, char)
                    call()
                    self.assertEqual(path.read_bytes(), expected)
                    self.assertIn(char * 100, json.loads(expected)["limitations"][0])  # never truncated or normalized
                    self.readable(path)

    # --- refusals leave every input and the journal untouched ----------------------------------------------------
    def test_compact_fitting_draft_cannot_publish_unreadable_output(self):
        (self.content / "findings").rmdir()  # publish would create it: a refusal must not
        self.write_draft(fill(draft(), publish_output, CAP + 1))
        self.assertIn("content/findings/synthetic-title.json: publish refused", self.refused(self.publish))
        before = self.snapshot()
        result = self.cli("finding", "publish", self.draft, "--directory", self.root, "--approved-by", "Pat Organizer",
                          returncode=1)
        self.assertIn(f"larger than {CAP} bytes", result.stderr)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.content / "findings").exists())
        self.assertEqual(self.log_lines(), [])

    def test_correction_growth_refuses_without_replacing_or_logging(self):
        for operation in ("correct", "correct --from"):
            with self.subTest(operation=operation):
                self.setUp()
                call, path, expected = self.prepare(operation, CAP + 1)
                self.assertGreater(len(expected), CAP)
                os.chmod(path, 0o640)
                self.log.write_text('{"action": "publish"}\n', encoding="utf-8")
                os.chmod(self.log, 0o600)
                message = self.refused(call, NOTE)
                self.assertIn("content/findings/synthetic-title.json: correct refused", message)
        result = self.cli("finding", "correct", "syn-pub-1", "--directory", self.root, "--date", "2026-03-05",
                          "--note", NOTE, "--from", self.base / "replacement.json", "--approved-by", "Pat Organizer",
                          returncode=1)
        self.assertIn(f"larger than {CAP} bytes", result.stderr)

    def test_withdrawal_metadata_cannot_poison_archive_reservations(self):
        for populated in (False, True):
            with self.subTest(populated=populated):
                self.setUp()
                if populated:  # an earlier, unrelated withdrawal already reserves its names
                    self.seed(published(draft(id="syn-pub-2", slug="other-title")), "other-title.json")
                    findings.withdraw(self.root, "syn-pub-2", "Synthetic earlier reason.", now=LATER)
                    os.chmod(self.archive.parent, 0o750)
                call, path, _ = self.prepare("withdraw", CAP + 1)
                self.assertIn("private/withdrawn/synthetic-title.json: withdraw refused", self.refused(call, REASON))
                before = self.snapshot()
                self.cli("finding", "withdraw", "syn-pub-1", "--directory", self.root, "--reason", REASON, returncode=1)
                self.assertEqual(self.snapshot(), before)
                self.assertTrue(self.target.exists())
                expected = ({"syn-pub-2"}, {"other-title"}) if populated else (set(), set())
                self.assertEqual(findings.withdrawn_names(self.root), expected)
                self.assertEqual(self.archive.parent.exists(), populated)
                if populated:
                    self.assertEqual(stat.S_IMODE(self.archive.parent.stat().st_mode), 0o750)
                self.assertEqual(sorted(row["state"] for row in findings.status_rows(self.root)),
                                 ["corrected"] + ["withdrawn"] * populated)

    def test_refused_operations_preserve_inputs_and_valid_operations_preserve_format(self):
        before = self.public_tree()  # setUp wrote and approved draft() through write_draft above
        self.assertEqual(self.publish(), {"published": "synthetic-title", "path": "content/findings/synthetic-title.json",
                                          "receipt_trust": {"bad_signatures": [], "self_asserted": 3, "signed": 0}})
        document = dict(findings.reviewed_view(draft()), state="published", published_at=STAMP, corrections=[])
        self.assertEqual(self.target.read_bytes(), canonical(document))
        self.assertEqual(self.correct(), {"corrected": "synthetic-title", "corrections": 1})
        document.update(updated_at=LATER_STAMP, corrections=[{"date": "2026-03-05", "note": NOTE}])
        self.assertEqual(self.target.read_bytes(), canonical(document))
        findings.update(self.root, "syn-pub-1", NEW_UPDATE["date"], NEW_UPDATE["note"], now=LATER,
                        approved_by="Pat Organizer")
        document.update(updates=[NEW_UPDATE])
        self.assertEqual(self.target.read_bytes(), canonical(document))
        self.assertEqual(self.withdraw(), {"withdrawn": "synthetic-title", "path": "private/withdrawn/synthetic-title.json"})
        archived = dict(json.loads(canonical(document)), withdrawn={"at": LATER_STAMP, "reason": REASON})
        self.assertEqual(self.archive.read_bytes(), canonical(archived, False))
        self.assertEqual(stat.S_IMODE(self.archive.stat().st_mode), 0o600)
        self.assertEqual([json.loads(line)["action"] for line in self.log_lines()],
                         ["publish", "correct", "update", "withdraw"])
        self.assertEqual(self.public_tree(), before)  # nothing built or deployed


if __name__ == "__main__":
    unittest.main()
