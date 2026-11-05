"""DF1107-03: `finding new --agency ID --search-log SUMMARY.json`. Synthetic campaign, CSV and names only; offline.

Run from the repository root: python3 -B -m unittest tests.test_finding_search_log
"""
import copy
import hashlib
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import finding_search_log, findings, search_log  # noqa: E402
from campaign_tool.review import review_blockers  # noqa: E402

EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign" / "content"
AID, OTHER = "ca-synthetic-police", "ca-other-sheriff"
SENTINELS = ("SENTUSER", "CASE-SENT", "7ABC123", "SENTPURPOSE")
HEADER = "Timestamp,User Name,Case Number,Plate,Search Purpose,Organization,State"


def row(day, purpose, org, state="CA", n=1):
    return f"{day},SENTUSER{n},CASE-SENT{n},7ABC123,{purpose},{org},{state}"


# 12 organizations: Org01 has 5 searches, Org02 and Org03 tie at 3, Org04..Org12 one each (Org11, Org12 fall off the top ten).
ROWS = ([row("2026-01-05", "Robbery SENTPURPOSE1", "Org01", n=i) for i in range(1, 4)]
        + [row("2026-01-20", "investigation", "Org01"), row("2026-02-10", "", "Org01")]
        + [row("2026-02-03", "ICE hold SENTPURPOSE3", "Org03")] * 3
        + [row("2026-02-11", "N/A", "Org02", state="NV")] * 3
        + [row("2026-03-02", "theft", "Org%02d" % k) for k in range(4, 13)]
        + [",,,,,,", "yesterday,SENTUSER9,CASE-SENT9,7ABC123,theft,Org01,CA", "2026-03-01,SENTUSER8"])


def csv_bytes(rows=ROWS, header=HEADER):
    return ("\r\n".join([header, *rows]) + "\r\n").encode("utf-8")


def sha(data):
    return hashlib.sha256(data).hexdigest()


class FindingSearchLogTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-finding-search-log-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(os.path.realpath(temporary.name))
        self.root = self.base / "campaign"
        self.cli("init", "--directory", self.root, "--name", "Synthetic", "--county", "Synthetic County", "--state", "ca")
        self.content, self.private = self.root / "content", self.root / "private"
        (self.content / "findings").mkdir(parents=True, exist_ok=True)
        shutil.copyfile(EXAMPLE / "site.json", self.content / "site.json")
        (self.content / "agencies.json").write_text(json.dumps(
            [{"agency_id": AID, "name": "Synthetic Police"}, {"agency_id": OTHER, "name": "Other Sheriff"}]))
        self.drafts = self.private / "drafts"
        self.data = csv_bytes()
        self.sha = self.ingest(self.data)
        self.register(self.sha)
        self.summary_path = self.make_summary(self.data)

    def cli(self, *arguments, returncode=0):
        result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *map(str, arguments)],
                                cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, returncode, result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        return result

    def ingest(self, data):
        path = self.base / "planted-original-name.csv"
        path.write_bytes(data)
        self.cli("ingest", "--directory", self.root, "--file", path, "--source-id", "synthetic-source")
        return sha(data)

    def register(self, *shas, agency=AID, fragment="synthetic-search-log"):
        entries = [{"sha256": h, "title": "SENTPURPOSE library prose", "agency_id": agency,
                    "received_at": "2026-01-20T16:30:00+00:00", "public_url": None, "fragment": fragment}
                   for h in shas]
        (self.content / "sources.json").write_text(json.dumps(entries))

    def make_summary(self, data, name="summary.json", **kwargs):
        csv_path = self.base / "organizer-log.csv"
        csv_path.write_bytes(data)
        summary = search_log.summarize_csv(csv_path, {"agency_id": AID, "name": "Synthetic Police"}, **kwargs)
        return self.write_summary(summary, name)

    def write_summary(self, summary, name="summary.json"):
        path = self.base / name
        path.write_text(json.dumps(summary, indent=2))
        return path

    def summary(self):
        return json.loads(self.summary_path.read_text())

    def draft(self, path=None, agency=AID, **kwargs):
        return finding_search_log.draft_from_summary(self.root, agency, path or self.summary_path, **kwargs)

    def refused(self, pattern, path=None, agency=AID):
        with self.assertRaisesRegex(ValueError, pattern) as caught:
            self.draft(path, agency)
        for sentinel in SENTINELS + ("Org01", "Org11", "synthetic-source"):
            self.assertNotIn(sentinel, str(caught.exception))
        self.assertFalse(self.drafts.exists(), "a refusal created private/drafts")

    def written(self, result):
        return findings.load_draft(Path(result["written"]))

    # ---- the draft -----------------------------------------------------------------------------------------------

    def test_cli_draft_has_hand_computed_table_markers_and_full_hash_source(self):
        out = self.cli("finding", "new", "--directory", self.root, "--agency", AID, "--search-log",
                       self.summary_path).stdout.splitlines()
        result = json.loads(out[0])
        self.assertEqual(out[1], f"cited: Search log {self.sha[:12]}")
        self.assertIn("Nothing was published.", out[2])
        path = Path(result["written"])
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.drafts.stat().st_mode), 0o700)
        draft = findings.load_draft(path)
        self.assertEqual(draft["sources"], [{"sha256": self.sha, "title": f"Search log {self.sha[:12]}",
                                             "locator": "[EDIT ME: page or section]"}])
        self.assertEqual((draft["state"], draft["published_at"], draft["agency_id"]), ("draft", None, AID))
        self.assertTrue(draft["title"].startswith("[EDIT ME: title]"))
        for field in ("summary", "classification", "confidence"):
            self.assertTrue(draft[field].startswith("[EDIT ME: "), field)
        body = draft["body_md"]
        for line in ("| Period (dates as stated in the log) | 2026-01-05 to 2026-03-02 |",
                     "| Total searches counted | 20 |", "| Rows read | 23 |", "| Rows skipped, not counted | 3 |",
                     "| Purpose blank or N/A | 4 |", "| Purpose mentions immigration terms | 3 |",
                     "| Purpose vague (for example 'investigation') | 1 |", "| Purpose other | 12 |",
                     "| Org01 | CA | 5 |", "| Org02 | NV | 3 |", "| Org03 | CA | 3 |", "| Org10 | CA | 1 |"):
            self.assertIn(line, body)
        self.assertEqual(re.findall(r"^\| (Org\d\d) \|", body, re.M),
                         ["Org01", "Org02", "Org03", "Org04", "Org05", "Org06", "Org07", "Org08", "Org09", "Org10"])
        self.assertIn(search_log.WARNING, body)
        self.assertIn(self.sha[:12], body)
        self.assertIn("[EDIT ME: what these counts do and do not show", body)
        for text in (json.dumps(draft), *out):
            for sentinel in SENTINELS + ("planted-original-name", "organizer-log"):
                self.assertNotIn(sentinel, text)
        self.assertIn("edit_marker:summary", review_blockers(draft, []))
        with self.assertRaises(ValueError):
            findings.publish(self.root, path, "Someone")

    def test_no_org_column_empty_log_and_skipped_only_log(self):
        no_org = b"Timestamp,Search Purpose\r\n2026-01-05,x\r\n2026-01-06,\r\n"
        cases = {"no-org": (no_org, ["| organization column not available | n/a | 2 unattributed |",
                                     "| Total searches counted | 2 |"]),
                 "empty": (csv_bytes([]), ["| Period (dates as stated in the log) | no dated searches |",
                                           "| Total searches counted | 0 |", "| none counted | n/a | 0 |"]),
                 "skipped": (csv_bytes(["yesterday,a,b,c,d,Org01,CA"]), ["no dated searches", "| Rows read | 1 |",
                                                                         "| Rows skipped, not counted | 1 |",
                                                                         "| Total searches counted | 0 |"])}
        for name, (data, lines) in cases.items():
            with self.subTest(name):
                digest = self.ingest(data)
                self.register(digest)
                columns = {"time": "Timestamp", "reason": "Search Purpose"} if name == "no-org" else None
                path = self.make_summary(data, name + ".json", columns=columns)
                body = self.written(self.draft(path))["body_md"]
                for line in lines:
                    self.assertIn(line, body)

    def test_hostile_but_real_organization_label_is_inert(self):
        label = 'A|B <b>x</b> [EDIT ME: y] `c` *d*'
        data = csv_bytes([f'2026-01-05,u,c,p,theft,"{label}",CA'])
        digest = self.ingest(data)
        self.register(digest)
        body = self.written(self.draft(self.make_summary(data, "hostile.json")))["body_md"]
        self.assertNotIn("<", body)
        self.assertNotIn("[EDIT ME: y", body)
        self.assertIn("A\\|B \u2039b\u203ax\u2039/b\u203a (EDIT ME: y) \\`c\\` \\*d\\*", body)

    def test_unsafe_organization_label_refuses_without_echo(self):
        data = csv_bytes(["2026-01-05,u,c,p,theft,leak@example.org,CA"])
        digest = self.ingest(data)
        self.register(digest)
        summary = self.summary()
        summary["input"]["sha256"] = digest
        self.refused("privacy lint: row 1: organization: email", self.write_summary(summary, "unsafe.json"))

    # ---- verification --------------------------------------------------------------------------------------------

    def test_every_count_family_tampered_alone_refuses(self):
        def tamper(function):
            summary = self.summary()
            function(summary)
            return self.write_summary(summary, "tampered.json")
        families = {
            "rows.read": lambda s: s["rows"].__setitem__("read", 30),
            "rows.counted": lambda s: s["rows"].__setitem__("counted", 25),
            "skip reason": lambda s: s["rows"]["skipped"].__setitem__("invalid_time", 0),
            "skip reason moved": lambda s: s["rows"]["skipped"].update(invalid_time=0, column_count=2),
            "period": lambda s: s["period"].__setitem__("last_date", "2026-03-03"),
            "month": lambda s: s["months"]["rows"][0].__setitem__("count", 6),
            "month dropped": lambda s: s["months"]["rows"].pop(),
            "purpose": lambda s: s["purposes"]["counts"].__setitem__("other", 19),
            "top org count": lambda s: s["organizations"]["rows"][0].__setitem__("count", 6),
            "eleventh org": lambda s: s["organizations"]["rows"][-1].__setitem__("count", 2),
            "org dropped": lambda s: s["organizations"]["rows"].pop(),
            "org state": lambda s: s["organizations"]["rows"][0].__setitem__("state", "NV"),
            "unattributed": lambda s: s["organizations"].__setitem__("unattributed", 1),
            "columns": lambda s: s["input"]["columns"].__setitem__("org", None),
            "extra field": lambda s: s.__setitem__("note", "SENTPURPOSE"),
            "extra nested field": lambda s: s["rows"].__setitem__("user", "SENTUSER"),
        }
        for name, function in families.items():
            with self.subTest(name):
                self.refused("no longer match a fresh read", tamper(function))

    def test_summary_structure_agency_and_custom_categories_refuse(self):
        def tamper(function):
            summary = self.summary()
            function(summary)
            return self.write_summary(summary, "tampered.json")
        self.refused("custom categories", tamper(lambda s: s["input"].__setitem__("categories_sha256", "a" * 64)))
        self.refused("version 1", tamper(lambda s: s.__setitem__("schema_version", 2)))
        self.refused("version 1", tamper(lambda s: s["input"].pop("columns")))
        self.refused("version 1", tamper(lambda s: s["input"].__setitem__("sha256", "../../" + "a" * 58)))
        self.refused("version 1", tamper(lambda s: s["input"].__setitem__("sha256", self.sha.upper())))
        self.refused("version 1", tamper(lambda s: s["input"].__setitem__("sha256", self.sha[:12])))
        self.refused("different agency", tamper(lambda s: s.__setitem__("agency_id", OTHER)))
        self.refused("not one of the campaign's agencies", agency="ca-nobody")
        bad = self.base / "bad.json"
        for text in ("[]", "{", '{"a": NaN}', '{"a": 1, "a": 2}', "\xff"):
            bad.write_bytes(text.encode("latin-1"))
            self.refused("not valid UTF-8 JSON|one kit-search-log", bad)

    def test_changed_stored_bytes_with_constant_ledger_hash_refuse(self):
        (self.private / "objects" / self.sha).write_bytes(csv_bytes(ROWS[:-3]))
        self.refused("bytes no longer match its sha256")

    def test_stored_record_never_ingested_registered_or_present_refuses(self):
        self.register()
        self.refused("not registered in content/sources.json")
        self.register(self.sha, agency=OTHER)
        self.refused("registered for a different agency")
        (self.content / "sources.json").write_text(json.dumps([
            {"sha256": self.sha, "title": "t", "agency_id": AID, "received_at": "2026-01-20T16:30:00+00:00",
             "public_url": None, "fragment": fragment} for fragment in ("one-fragment", "two-fragment")]))
        self.refused("several library entries")
        collision = self.sha[:12] + "f" * 52
        (self.content / "sources.json").write_text(json.dumps([
            {"sha256": h, "title": "t", "agency_id": AID, "received_at": "2026-01-20T16:30:00+00:00",
             "public_url": None, "fragment": "frag"} for h in (self.sha, collision)]))
        self.refused("does not identify one library record")
        self.register(self.sha)
        (self.private / "objects" / self.sha).unlink()
        self.refused("no stored object")
        os.symlink(self.base / "nowhere", self.private / "objects" / self.sha)  # dangling symlink in place of the object
        self.refused("no stored object")
        (self.private / "objects" / self.sha).unlink()
        os.mkfifo(self.private / "objects" / self.sha)
        self.refused("no stored object")
        (self.private / "objects" / self.sha).unlink()
        (self.private / "ledger.sqlite").unlink()
        self.refused("not ingested in the ledger")
        (self.content / "sources.json").unlink()
        self.refused("sources|library")

    def test_symlinked_private_roots_and_unreadable_inputs_refuse(self):
        moved = self.base / "moved-private"
        self.private.rename(moved)
        os.symlink(moved, self.private)
        self.refused("must not be symlinks")
        self.private.unlink()
        moved.rename(self.private)
        big, fifo, link = self.base / "big.json", self.base / "fifo.json", self.base / "link.json"
        big.write_bytes(b" " * (finding_search_log.MAX_SUMMARY_BYTES + 1))
        self.refused("larger than 4 MiB", big)
        os.mkfifo(fifo)
        self.refused("must be a regular file", fifo)
        os.symlink(self.summary_path, link)
        self.refused("cannot be opened as a regular file", link)
        self.refused("cannot be opened as a regular file", self.base / "absent.json")

    def test_oversized_stored_csv_refuses(self):
        with mock.patch.object(search_log, "MAX_BYTES", 100):
            self.refused("csv is larger than")

    def test_no_network_and_nothing_else_written(self):
        before = {str(p.relative_to(self.root)): p.read_bytes() for p in sorted(self.root.rglob("*"))
                  if p.is_file() and "ledger.sqlite" not in p.name}
        with mock.patch.object(socket.socket, "connect", side_effect=AssertionError("network used")):
            result = self.draft(author="Ada Lovelace")
        after = {str(p.relative_to(self.root)): p.read_bytes() for p in sorted(self.root.rglob("*"))
                 if p.is_file() and "ledger.sqlite" not in p.name}
        self.assertEqual(set(after) - set(before), {f"private/drafts/{result['id']}.json"})
        self.assertEqual({k: v for k, v in after.items() if k in before}, before)
        self.assertEqual(self.written(result)["author"], "Ada Lovelace")
        second = self.draft()
        self.assertNotEqual(second["id"], result["id"])  # a second run is a new draft, never an overwrite

    # ---- CLI mode separation -------------------------------------------------------------------------------------

    def test_mixed_mode_options_refused_before_reading_anything(self):
        base = ["finding", "new", "--directory", self.root, "--search-log", self.base / "absent.json"]
        for extra, flag in ((["--agency", AID, "--title", "T"], "--title"), (["--agency", AID, "--source", "a" * 12], "--source"),
                            (["--agency", AID, "--request", "r"], "--request"), (["--gap", AID], "--gap")):
            with self.subTest(flag):
                err = self.cli(*base, *extra, returncode=2).stderr
                self.assertIn(f"--search-log cannot be combined with {flag}", err)
        self.assertIn("--search-log requires --agency", self.cli(*base, returncode=2).stderr)
        self.assertFalse(self.drafts.exists())

    def test_cli_refusal_exits_nonzero_without_draft_and_ordinary_new_unchanged(self):
        summary = self.summary()
        summary["rows"]["read"] += 1
        err = self.cli("finding", "new", "--directory", self.root, "--agency", AID, "--search-log",
                       self.write_summary(summary, "t.json"), returncode=2).stderr
        self.assertIn("Stopped: the summary's counts no longer match", err)
        self.assertFalse(self.drafts.exists())
        out = self.cli("finding", "new", "--directory", self.root, "--agency", AID, "--title", "Ordinary title",
                       "--source", self.sha[:12]).stdout
        self.assertEqual(self.written({"written": json.loads(out.splitlines()[0])["written"]})["slug"], "ordinary-title")


if __name__ == "__main__":
    unittest.main()
