"""Synthetic-only tests for tools/collection_triage.py (collection-triage-v1). No network, model, mailbox or live path is used."""
import ast
from contextlib import redirect_stdout
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[2]
TOOL = REPO / "tools" / "collection_triage.py"
_spec = importlib.util.spec_from_file_location("collection_triage_under_test", TOOL)
triage = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(triage)

POLICY = ("Records retention policy. Section 1 sets the effective date. All employees shall keep files for the stated term. "
          "Section 2 covers the board of directors.\n")
CONTRACT = ("This agreement is made between the parties. Whereas the vendor supplies the service, hereinafter the vendor "
            "agrees to indemnify the buyer. Governing law is stated below.\n")
MEMO = ("In response to your request, enclosed please find the listed items herewith. As you requested, and pursuant to "
        "the public records request, attached please see the index.\n")
LOW_MEMO = "Pursuant to a schedule, the items are listed. Nothing else is stated here.\n"
LOG = "timestamp\tuser\tquery\thits\n" + "".join(
    "2026-01-%02d\tuser%02d\tterm%02d\t%d\n" % (n, n, n, n) for n in range(1, 13))
NEUTRAL = "Item list for the quarter. Counts are rounded. Totals follow the table of items listed above.\n"
LETTERS = ("Please be advised that this letter is to confirm receipt. Sincerely, the records unit.\n",
           "You are hereby told this letter is to close the file. Kind regards, the records unit.\n",
           "Please be advised the file is closed. Very truly yours, the records unit.\n",
           "Sincerely, the records unit.\n")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def make_collection(root, overrides=None):
    """Twelve synthetic members: policy, contract, log, 4 boilerplate, 2 byte-identical, a memo, no text, unreadable."""
    texts = {"m01-policy": POLICY, "m02-contract": CONTRACT, "m03-log": LOG, "m04-memo": MEMO,
             "m05-letter": LETTERS[0], "m06-letter": LETTERS[1], "m07-letter": LETTERS[2], "m08-letter": LETTERS[3],
             "m09-same": NEUTRAL, "m10-same": NEUTRAL}
    texts.update(overrides or {})
    root.mkdir(mode=0o700)
    members = []
    for member_id, text in texts.items():
        (root / (member_id + ".txt")).write_bytes(text if isinstance(text, bytes) else text.encode("utf-8"))
        members.append({"id": member_id, "text": member_id + ".txt"})
    members += [{"id": "m11-notext", "text": None}, {"id": "m12-missing", "text": "m12-missing.txt"}]
    (root / "collection.json").write_text(json.dumps({"schema": triage.INPUT_SCHEMA, "name": "synthetic", "members": members}),
                                          encoding="utf-8")
    return root


def tree_state(root):
    return {str(p.relative_to(root)): sha(p.read_bytes()) for p in sorted(root.rglob("*")) if p.is_file()}


class Base(unittest.TestCase):
    def setUp(self):
        holder = tempfile.TemporaryDirectory(prefix="triage-")
        self.addCleanup(holder.cleanup)
        self.tmp = Path(holder.name).resolve()
        self.collection = make_collection(self.tmp / "collection")

    def run_tool(self, *argv):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = triage.main(list(argv))
        return code, buffer.getvalue()

    def run_triage(self, name="out", collection=None):
        out = self.tmp / name
        code, text = self.run_tool("run", "--collection", str(collection or self.collection), "--out", str(out))
        return code, text, out

    def rows(self, out):
        document = json.loads((out / "collection-triage-v1.json").read_text(encoding="utf-8"))
        return document, {row["id"]: row for row in document["members"]}


class SyntheticCollectionTest(Base):
    def test_done_when_twelve_members(self):
        before = tree_state(self.collection)
        code, text, out = self.run_triage()
        self.assertEqual(code, 1)
        self.assertEqual(text.splitlines()[0], "triage | members=12 responsive=1 policy=1 contract=1 search-log=1 "
                         "boilerplate=4 duplicate=1 unsorted=3 | unreviewed; machine triage, not a review")
        self.assertIn("unreadable: m12-missing", text)
        document, rows = self.rows(out)
        self.assertEqual(len(rows), 12)
        queue = (out / "QUEUE.md").read_text(encoding="utf-8")
        entries = [line for line in queue.splitlines() if re.match(r"\d+\. ", line)]
        self.assertEqual(len(entries), 12)
        for member_id in rows:
            self.assertEqual(sum(1 for line in entries if re.match(r"\d+\. %s: " % re.escape(member_id), line)), 1)
        order = [re.match(r"\d+\. ([^:]+):", line).group(1) for line in entries]
        self.assertEqual(order[0], "m04-memo")
        self.assertEqual(order[1:4], ["m01-policy", "m02-contract", "m03-log"])
        self.assertEqual(order[-1], "m10-same")
        self.assertEqual(order[4:7], ["m09-same", "m11-notext", "m12-missing"])
        self.assertIn("m11-notext: unsorted (no text)", queue)
        self.assertIn("m12-missing: unsorted (unreadable)", queue)
        self.assertEqual(document["unreadable"], ["m12-missing"])
        self.assertEqual(tree_state(self.collection), before)

    def test_each_kind_has_its_own_signal_and_a_reason(self):
        _, _, out = self.run_triage()
        _, rows = self.rows(out)
        expected = {"m04-memo": ("responsive", "responsive-phrases"), "m01-policy": ("policy", "policy-phrases"),
                    "m02-contract": ("contract", "contract-phrases"), "m03-log": ("search-log", "table-log-words"),
                    "m05-letter": ("boilerplate", "boilerplate-phrases"), "m10-same": ("duplicate", "byte-hash"),
                    "m09-same": ("unsorted", "no-rule"), "m11-notext": ("unsorted", "no-text")}
        for member_id, (kind, signal) in expected.items():
            self.assertEqual((rows[member_id]["kind"], rows[member_id]["reason"]["signal"]), (kind, signal), member_id)
        self.assertEqual(rows["m04-memo"]["confidence"], "high")
        for row in rows.values():
            self.assertEqual(set(row["reason"]), {"signal", "locator", "text_sha256"})
            self.assertTrue(row["reason"]["signal"] and row["reason"]["locator"])
            self.assertIn(row["kind"], triage.KINDS)
        self.assertEqual(rows["m01-policy"]["reason"]["text_sha256"], sha(POLICY.encode()))
        self.assertEqual(rows["m10-same"]["reason"]["locator"], "first=m09-same")

    def test_responsive_needs_two_distinct_phrases_not_repeats(self):
        self.assertEqual(triage.classify(MEMO.encode())[:2], ("responsive", "high"))
        self.assertEqual(triage.classify(b"your request your request your request your request\n")[0], "unsorted")

    def test_no_text_is_unsorted_never_boilerplate(self):
        self.assertEqual(triage.classify(None)[:3], ("unsorted", "low", "no-text"))
        _, _, out = self.run_triage(collection=make_collection(self.tmp / "c2", {"m05-letter": b""}))
        _, rows = self.rows(out)
        self.assertEqual((rows["m05-letter"]["kind"], rows["m05-letter"]["reason"]["signal"]), ("unsorted", "no-text"))
        self.assertIsNone(rows["m05-letter"]["reason"]["text_sha256"])

    def test_duplicate_is_by_byte_hash_only(self):
        same_title = {"m09-same": "Quarterly items\n" + NEUTRAL, "m10-same": "Quarterly items\n" + NEUTRAL + " "}
        _, _, out = self.run_triage(collection=make_collection(self.tmp / "c2", same_title))
        document, rows = self.rows(out)
        self.assertEqual(document["counts"]["duplicate"], 0)
        self.assertEqual(rows["m10-same"]["kind"], "unsorted")
        _, _, out = self.run_triage("out2")
        document, rows = self.rows(out)
        self.assertEqual(document["counts"]["duplicate"], 1)
        self.assertEqual(rows["m09-same"]["kind"], "unsorted")

    def test_confidence_is_separate_from_kind(self):
        self.assertEqual(triage.classify(LETTERS[3].encode())[:2], ("boilerplate", "low"))
        self.assertEqual(triage.classify(LETTERS[0].encode())[:2], ("boilerplate", "medium"))
        collection = make_collection(self.tmp / "c2", {"m04-memo": LOW_MEMO, "m08-letter": "Sincerely, the records unit.\n"})
        _, _, out = self.run_triage(collection=collection)
        _, rows = self.rows(out)
        self.assertEqual((rows["m04-memo"]["kind"], rows["m04-memo"]["confidence"]), ("unsorted", "low"))
        collection = make_collection(self.tmp / "c3", {"m04-memo": "Pursuant to your request, the items are listed.\n"})
        _, _, out = self.run_triage("out3", collection=collection)
        _, rows = self.rows(out)
        self.assertEqual((rows["m04-memo"]["kind"], rows["m04-memo"]["confidence"]), ("responsive", "low"))
        order = [re.match(r"\d+\. ([^:]+):", l).group(1) for l in (out / "QUEUE.md").read_text().splitlines() if l[:1].isdigit()]
        self.assertGreater(order.index("m04-memo"), order.index("m12-missing"))
        self.assertLess(order.index("m04-memo"), order.index("m05-letter"))
        self.assertEqual(order[-1], "m10-same")

    def test_review_state_is_always_unreviewed(self):
        _, _, out = self.run_triage()
        for path in out.iterdir():
            text = path.read_text(encoding="utf-8")
            self.assertIsNone(re.search(r"(?<!un)reviewed", text), path.name)
        document, rows = self.rows(out)
        self.assertEqual({row["review_state"] for row in rows.values()}, {"unreviewed"})

    def test_determinism(self):
        _, _, one = self.run_triage("one")
        _, _, two = self.run_triage("two")
        for name in ("QUEUE.md", "triage-status.txt", "triage-notes.json"):
            self.assertEqual((one / name).read_bytes(), (two / name).read_bytes(), name)
        first, second = self.rows(one)[0], self.rows(two)[0]
        first.pop("generated_at"), second.pop("generated_at")
        self.assertEqual(first, second)

    def test_unreadable_members_are_named_and_the_others_still_triaged(self):
        collection = make_collection(self.tmp / "c2", {"m06-letter": b"\xff\xfe not utf-8"})
        (collection / "m07-letter.txt").unlink()
        code, text, out = self.run_triage(collection=collection)
        self.assertEqual(code, 1)
        self.assertIn("unreadable: m06-letter", text)
        self.assertIn("unreadable: m07-letter", text)
        document, rows = self.rows(out)
        self.assertEqual(document["unreadable"], ["m06-letter", "m07-letter", "m12-missing"])
        self.assertEqual(rows["m01-policy"]["kind"], "policy")
        self.assertEqual(sorted(p.name for p in out.iterdir()),
                         ["QUEUE.md", "collection-triage-v1.json", "triage-notes.json", "triage-status.txt"])
        code, text = self.run_tool("report", str(out))
        self.assertEqual(code, 1)
        self.assertIn("unreadable: m06-letter", text)

    def test_a_member_text_link_is_unreadable(self):
        (self.collection / "m06-letter.txt").unlink()
        (self.collection / "m06-letter.txt").symlink_to(self.collection / "m05-letter.txt")
        code, _, out = self.run_triage()
        self.assertEqual(code, 1)
        self.assertEqual(self.rows(out)[0]["unreadable"], ["m06-letter", "m12-missing"])

    def test_output_files_are_private(self):
        _, _, out = self.run_triage()
        self.assertEqual(out.stat().st_mode & 0o777, 0o700)
        for path in out.iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600, path.name)

    def test_report_reads_a_finished_run(self):
        _, _, out = self.run_triage()
        code, text = self.run_tool("report", str(out))
        self.assertEqual(text.splitlines()[0], (out / "triage-status.txt").read_text().strip())
        self.assertEqual(code, 1)
        self.assertEqual(self.run_tool("report", str(self.tmp / "nope"))[0], 2)
        self.assertEqual(self.run_tool("report", "relative")[0], 2)


class PrivacyAndWordingTest(Base):
    def test_planted_personal_data_and_quotes_appear_in_no_output(self):
        planted = ["jdoe@example.org", "(805) 555-0142", "7ABC123", "Jordan Q. Sample",
                   "the quick brown synthetic sentence about nothing"]
        memo = MEMO + " ".join(planted) + "\n"
        collection = make_collection(self.tmp / "c2", {"m04-memo": memo})
        code, _, out = self.run_triage(collection=collection)
        self.assertEqual(code, 1)
        everything = "".join(p.read_text(encoding="utf-8") for p in out.iterdir())
        for value in planted:
            self.assertNotIn(value, everything)
        notes = json.loads((out / "triage-notes.json").read_text())["notes"]
        self.assertIn("review-required: member:m04-memo: email pattern", notes)
        scan = subprocess.run([sys.executable, "-B", str(REPO / "tools" / "flock_privacy_scan.py"), str(out)],
                              capture_output=True, text=True, check=False)
        self.assertEqual(scan.returncode, 0, scan.stdout)
        self.assertIn("findings=0", scan.stdout)

    def test_legal_wording_is_counted_and_never_halts_the_run(self):
        planted = "The agency violated the Public Records Act. The practice is unlawful.\n"
        collection = make_collection(self.tmp / "c2", {"m04-memo": MEMO + planted})
        code, _, out = self.run_triage(collection=collection)
        self.assertEqual(code, 1)
        notes = json.loads((out / "triage-notes.json").read_text())["notes"]
        self.assertEqual(notes, ["review-required: member:m04-memo: legal-claim wording flagged (2 rule)"])
        self.assertEqual(self.rows(out)[1]["m04-memo"]["kind"], "responsive")
        for path in out.iterdir():
            self.assertIsNone(re.search(r"violat|unlawful|illegal|non-?complian|breach", path.read_text(), re.I), path.name)

    def test_clean_run_has_no_notes(self):
        _, _, out = self.run_triage()
        self.assertEqual(json.loads((out / "triage-notes.json").read_text()), {"notes": []})


class RefusalTest(Base):
    def refused(self, *argv):
        code, text = self.run_tool(*argv)
        self.assertEqual(code, 2, text)
        self.assertEqual(len(text.strip().splitlines()), 1)
        return json.loads(text)["error"]

    def test_output_path_refusals_create_nothing(self):
        before = tree_state(self.collection)
        collection = str(self.collection)
        existing = self.tmp / "exists"
        existing.mkdir()
        link_parent = self.tmp / "link"
        link_parent.symlink_to(existing)
        cases = ((self.tmp / "exists", "output_dir_exists"), (self.collection / "inside", "output_dir_overlaps_collection"),
                 (self.tmp, "output_dir_exists"), (REPO / "triage-out-never-created", "output_dir_inside_repository"),
                 (link_parent / "out", "output_parent_unavailable"), (self.tmp / "missing" / "out", "output_parent_unavailable"))
        for out, error in cases:
            self.assertEqual(self.refused("run", "--collection", collection, "--out", str(out)), error, out)
            self.assertFalse(out.exists() and out != existing and out != self.tmp)
        self.assertEqual(self.refused("run", "--collection", collection, "--out", "relative-out"), "absolute_paths_required")
        self.assertEqual(self.refused("run", "--collection", collection, "--out", str(self.tmp / "a" / ".." / "b")),
                         "normalized_paths_required")
        (self.tmp / "empty").mkdir()
        self.assertEqual(self.refused("run", "--collection", str(self.tmp / "empty"), "--out", str(self.tmp / "o")),
                         "manifest_missing")
        self.assertEqual(os.listdir(existing), [])
        self.assertEqual(tree_state(self.collection), before)

    def test_usage_errors_exit_two(self):
        self.assertEqual(self.refused("run"), "usage_invalid")
        self.assertEqual(self.refused("run", "--collection", str(self.collection)), "usage_invalid")
        self.assertEqual(self.refused("frobnicate", "x"), "usage_invalid")
        self.assertEqual(self.refused(), "usage_invalid")

    def test_bad_manifests_are_refused_and_nothing_is_written(self):
        bad = {"duplicate id": '{"schema": "%s", "name": "n", "members": [{"id": "a", "text": null}, {"id": "a", "text": null}]}',
               "extra key": '{"schema": "%s", "name": "n", "members": [], "x": 1}',
               "path escape": '{"schema": "%s", "name": "n", "members": [{"id": "a", "text": "../x"}]}',
               "absolute path": '{"schema": "%s", "name": "n", "members": [{"id": "a", "text": "/etc/x"}]}',
               "duplicate key": '{"schema": "%s", "schema": "%s", "name": "n", "members": [{"id": "a", "text": null}]}',
               "nonfinite": '{"schema": "%s", "name": NaN, "members": [{"id": "a", "text": null}]}'}
        for label, body in bad.items():
            folder = self.tmp / ("bad-" + label.replace(" ", "-"))
            folder.mkdir()
            (folder / "collection.json").write_text(body.replace("%s", triage.INPUT_SCHEMA), encoding="utf-8")
            out = self.tmp / ("out-" + folder.name)
            self.assertEqual(self.run_tool("run", "--collection", str(folder), "--out", str(out))[0], 2, label)
            self.assertFalse(out.exists(), label)

    def test_help_exits_zero(self):
        self.assertEqual(self.run_tool("--help")[0], 0)


class StaticTest(unittest.TestCase):
    def test_no_network_subprocess_model_or_delete(self):
        tree = ast.parse(TOOL.read_text(encoding="utf-8"))
        banned = {"socket", "ssl", "http", "urllib", "requests", "ftplib", "smtplib", "imaplib", "subprocess", "shutil",
                  "openai", "anthropic", "ollama"}
        imported = {alias.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for alias in n.names}
        imported |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        self.assertEqual(imported & banned, set())
        calls = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        self.assertEqual(calls & {"remove", "unlink", "rmdir", "rmtree", "removedirs", "system", "popen"}, set())

    def test_schema_matches_the_tool(self):
        schema = json.loads((REPO / "schemas" / "collection-triage-v1.json").read_text(encoding="utf-8"))
        row = schema["properties"]["members"]["items"]["properties"]
        self.assertEqual(tuple(row["kind"]["enum"]), triage.KINDS)
        self.assertEqual(tuple(schema["properties"]["counts"]["required"]), triage.KINDS)
        self.assertEqual(row["review_state"], {"const": "unreviewed"})


if __name__ == "__main__":
    unittest.main()
