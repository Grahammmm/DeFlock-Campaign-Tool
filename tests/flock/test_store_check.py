"""DF1537-13: tools/flock_store_check.py runs every flock checker over one synthetic store, read-only."""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import tarfile
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
FIX = ROOT / "tests" / "fixtures" / "flock"
SPEC = importlib.util.spec_from_file_location("flock_store_check", ROOT / "tools" / "flock_store_check.py")
check = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check)
AG = "sample-city-police-department"
HEX = "a" * 64
SECRET_VALUE, SECRET_QUOTE, LOCAL = "zqvalue-sentinel", "zq quote sentinel words", "zqlocalpart"
REQ_HEAD = ",".join(check.requests_tool.HEADER)
REQ_ROW = "SAMPLE-1,Sample City Police Department,2026-01-02,agency acknowledgment message,2026-01-05,,,open," + "c" * 64
WATCH_HEAD = ",".join(check.monitor.WATCH_HEADER)
WATCH_ROW = "Sample City Police Department,agenda,https://www.city.example/agendas,,city.example,"


def fact(value=SECRET_VALUE, page="1", quote=SECRET_QUOTE):
    row = {"agency": "Sample City Police Department", "request": "REQ-1", "fact_type": "camera_count", "value": value,
           "source_sha256": HEX, "page": page, "verbatim_quote": quote, "kind": "record", "effective_from": "",
           "effective_to": "", "added_by": "tester", "added_at": "2026-10-30", "status": "unreviewed"}
    row["fact_id"] = check.facts_tool.fact_id(row)
    return ",".join(row[k] for k in check.facts_tool.V2)


def write(path, *lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def snapshot(root):
    return sorted((p.relative_to(root).as_posix(), p.is_dir() or p.read_bytes(), os.lstat(p).st_mtime_ns)
                  for p in Path(root).rglob("*"))


class StoreCheckTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.store, self.bdir = self.tmp / "store", self.tmp / "backups"
        self.bdir.mkdir()
        agency = self.store / "agencies" / AG
        write(agency / "FACTS.csv", ",".join(check.facts_tool.V2), fact())
        shutil.copy(FIX / "policy-sample.csv", agency / "POLICY.csv")
        write(agency / "REQUESTS.csv", REQ_HEAD, REQ_ROW)
        write(agency / "CONTACT.csv", "agency,role,email,source_url,source_sha256,checked_at,added_by",
              f"Sample City Police Department,records,{LOCAL}@city.example,https://www.city.example/records,{HEX},2026-10-30,t")
        shutil.copy(FIX / "law-sample.csv", self.store / "LAW.csv")
        write(self.store / "regions" / "central" / "ROSTER.csv", ",".join(check.region.ROSTER),
              f"Sample City Police Department,{AG},yes,city police,sample-city")
        write(self.store / "regions" / "central" / "WATCHLIST.csv", WATCH_HEAD, WATCH_ROW)

    def run_check(self, *extra, store=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = check.main([str(store or self.store), *map(str, extra)])
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def section(self, out, name):
        return [ln.split(" | ", 1)[1] for ln in out.splitlines() if ln.startswith(name + " | ")]

    def backup(self, name="store.tar"):
        check.backup.create(self.store, self.bdir / name)
        with tarfile.open(self.bdir / name) as tar:
            made = json.loads(tar.extractfile(tar.next()).read())["created_at"]
        return datetime.fromisoformat(made).date()

    def test_clean_store_every_section_zero(self):
        code, out, err = self.run_check()
        self.assertEqual((code, err), (0, ""), out)
        for name in check.SECTIONS:
            self.assertEqual(self.section(out, name)[-1], "errors=0", (name, out))
        self.assertEqual(self.section(out, "backup"), ["not checked", "errors=0"])
        self.assertIn("publish | runs=0 ", out)
        self.assertEqual(out.splitlines()[-1],
                         "store check | sections=8 errors=0 notes=0 | machine validity only; review-required")
        self.assertEqual([ln.split(" | ")[0] for ln in out.splitlines()[:-1]],
                         sorted([ln.split(" | ")[0] for ln in out.splitlines()[:-1]], key=check.SECTIONS.index))

    def test_bad_fact_and_ragged_request_named_others_unaffected(self):
        agency = self.store / "agencies" / AG
        write(agency / "FACTS.csv", ",".join(check.facts_tool.V2), fact(), fact(value="other", page="0"))
        write(agency / "REQUESTS.csv", REQ_HEAD, REQ_ROW, "SAMPLE-2,Sample City Police Department,2026-01-02")
        code, out, _ = self.run_check()
        self.assertEqual(code, 1)
        self.assertIn("facts | file 1: row 3: page must be an integer >= 1", out)
        self.assertIn(f"requests | {AG}: row 3: columns expected 9, found 3", out)
        self.assertEqual((self.section(out, "facts")[-1], self.section(out, "requests")[-1]), ("errors=1", "errors=1"))
        for name in ("law-policy", "contacts", "watchlists", "publish", "slugs", "backup"):
            self.assertEqual(self.section(out, name)[-1], "errors=0", name)
        self.assertIn("store check | sections=8 errors=2 notes=0", out)

    def test_orphan_slugs_errors_and_unrostered_folder_note(self):
        write(self.store / "regions" / "central" / "ROSTER.csv", ",".join(check.region.ROSTER),
              f"Sample City Police Department,{AG},yes,city police,", "Ghost Town PD,ghost-town,yes,x,")
        write(self.store / "regions" / "central" / "WATCHLIST.csv", WATCH_HEAD, WATCH_ROW,
              "Nowhere Sheriff,agenda,https://www.nowhere.example/a,,nowhere.example,",
              "Nowhere Sheriff,news,https://www.nowhere.example/n,,nowhere.example,")
        (self.store / "agencies" / "extra-pd").mkdir()
        code, out, _ = self.run_check()
        self.assertEqual(code, 1)
        self.assertEqual(self.section(out, "slugs"), ["central: ROSTER slug ghost-town has no agency folder",
                                                      "central: WATCHLIST agency nowhere-sheriff has no agency folder",
                                                      "note: agency folder extra-pd is in no ROSTER", "errors=2"])
        self.assertIn("notes=1", out.splitlines()[-1])
        (self.store / "regions" / "central" / "ROSTER.csv").unlink()  # no ROSTER at all: the note is not printed
        self.assertNotIn("note:", self.run_check()[1])

    def test_linked_agency_and_region_folders_named_not_followed(self):
        outside = self.tmp / "outside"
        write(outside / "ROSTER.csv", ",".join(check.region.ROSTER), "Linked,linked-ghost,yes,x,")
        (self.store / "agencies" / "linked-pd").symlink_to(outside, target_is_directory=True)
        (self.store / "regions" / "linked-region").symlink_to(outside, target_is_directory=True)
        code, out, _ = self.run_check()
        self.assertEqual(code, 1)
        self.assertIn("slugs | agencies/linked-pd is a link; not followed", out)
        self.assertIn("slugs | regions/linked-region is a link; not followed", out)
        self.assertNotIn("linked-ghost", out)
        self.assertNotIn("contacts | linked-pd", out)

    def test_raising_checker_reported_by_class_and_run_continues(self):
        with mock.patch.object(check.law_policy, "load_all", side_effect=OSError("/secret/path")):
            code, out, _ = self.run_check()
        self.assertEqual(code, 1)
        self.assertEqual(self.section(out, "law-policy"), ["failed: OSError", "errors=1"])
        self.assertNotIn("/secret/path", out)
        for name in ("requests", "contacts", "watchlists", "publish", "slugs", "backup"):
            self.assertEqual(self.section(out, name)[-1], "errors=0", name)
        self.assertIn("errors=1 notes=0", out.splitlines()[-1])

    def test_invalid_watchlist_rows_counted(self):
        write(self.store / "regions" / "central" / "WATCHLIST.csv", WATCH_HEAD, WATCH_ROW,
              "Sample City Police Department,agenda,http://www.city.example/x,,city.example,")
        code, out, _ = self.run_check()
        self.assertEqual(code, 1)
        self.assertIn("watchlists | central: row 3: url must be empty for social, else https://", out)
        (self.store / "regions" / "central" / "WATCHLIST.csv").unlink()
        self.assertEqual(self.section(self.run_check()[1], "watchlists"), ["none", "errors=0"])

    def test_error_lines_capped_at_twenty(self):
        bad = [fact(value=f"v{i}", page="0") for i in range(25)]
        write(self.store / "agencies" / AG / "FACTS.csv", ",".join(check.facts_tool.V2), *bad)
        code, out, _ = self.run_check()
        lines = self.section(out, "facts")
        self.assertEqual((code, len(lines), lines[-2:]), (1, 22, ["… and 5 more", "errors=25"]))

    def test_backup_age_ok_old_none_link_and_foreign_kind(self):
        made = self.backup()
        today = (made + timedelta(days=3)).isoformat()
        code, out, _ = self.run_check("--backups", self.bdir, "--max-backup-age-days", 7, "--today", today)
        self.assertEqual(code, 0, out)
        self.assertIn("backup | newest is 3 days old (max 7) ok; hashes not re-verified (flock_backup.py verify)", out)
        code, out, _ = self.run_check("--backups", self.bdir, "--max-backup-age-days", 2, "--today", today)
        self.assertEqual((code, self.section(out, "backup")[-1]), (1, "errors=1"))
        self.assertIn("backup | newest is 3 days old (max 2); hashes not re-verified", out)
        code, out, _ = self.run_check("--backups", self.bdir, "--max-backup-age-days", 7,
                                      "--today", (made - timedelta(days=1)).isoformat())
        self.assertIn("backup | newest is dated after --today", out)  # a clock or manifest out of order fails
        foreign = json.dumps({"kind": "other", "created_at": f"{today}T00:00:00+00:00"}).encode()
        with tarfile.open(self.bdir / "other.tar", "w") as tar:
            info = tarfile.TarInfo("manifest.json")
            info.size = len(foreign)
            tar.addfile(info, io.BytesIO(foreign))
        (self.bdir / "junk.tar").write_bytes(b"not a tar")
        code, out, _ = self.run_check("--backups", self.bdir, "--max-backup-age-days", 2, "--today", today)
        self.assertIn("backup | skipped_links=0 ignored=2", out)
        self.assertIn("newest is 3 days old (max 2)", out)  # the newer foreign-kind tar is not the newest backup
        linked = self.tmp / "linked"
        linked.mkdir()
        (linked / "store.tar").symlink_to(self.bdir / "store.tar")
        code, out, _ = self.run_check("--backups", linked, "--max-backup-age-days", 7, "--today", today)
        self.assertEqual(self.section(out, "backup"), ["none found", "skipped_links=1 ignored=0", "errors=1"])
        empty = self.tmp / "empty"
        empty.mkdir()
        code, out, _ = self.run_check("--backups", empty, "--max-backup-age-days", 7)
        self.assertEqual((code, self.section(out, "backup")[0]), (1, "none found"))

    def test_usage_and_store_in_repo_refused(self):
        for args in (["--backups", self.bdir], ["--max-backup-age-days", 7], ["--backups", self.bdir, "--max-backup-age-days", 0],
                     ["--backups", self.bdir, "--max-backup-age-days", 366], ["--today", "31/10/2026"]):
            self.assertEqual(self.run_check(*args)[0], 2, args)
        code, out, err = self.run_check(store=FIX)
        self.assertEqual((code, out, err.strip()), (2, "", "store check: STORE is inside the repository"))
        self.assertEqual(self.run_check(store=self.tmp / "missing")[0], 2)

    def test_read_only_tree_unchanged(self):
        self.backup()
        before = (snapshot(self.store), snapshot(self.bdir))
        for extra in ([], ["--backups", self.bdir, "--max-backup-age-days", 30]):
            self.run_check(*extra)
        self.assertEqual((snapshot(self.store), snapshot(self.bdir)), before)
        self.assertFalse(os.path.lexists(self.store / ".monitor.lock"))

    def test_output_never_shows_values_quotes_or_local_part(self):
        agency = self.store / "agencies" / AG
        write(agency / "FACTS.csv", ",".join(check.facts_tool.V2), fact(), fact(page="0"))
        write(agency / "CONTACT.csv", "agency,role,email,source_url,source_sha256,checked_at,added_by",
              f"Sample City Police Department,press,{LOCAL}@city.example,https://www.city.example/r,{HEX},2026-10-30,t")
        code, out, err = self.run_check()
        self.assertEqual(code, 1)
        self.assertIn(f"contacts | {AG}: row 2: role is invalid", out)
        for sentinel in (SECRET_VALUE, SECRET_QUOTE, LOCAL):
            self.assertNotIn(sentinel, out + err)


if __name__ == "__main__":
    unittest.main()
