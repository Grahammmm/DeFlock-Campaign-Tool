"""DF0652-07: ``records holds --routes --mail-history`` names each PRESERVE_PENDING original's clearing route."""
from contextlib import closing, redirect_stderr, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from campaign_tool.records import holds, preserve_routes  # noqa: E402
from campaign_tool.records.ledger.migrations import v001  # noqa: E402

CANARY = "/srv/canary-agency/jane.doe@example.org/secret.pdf"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def run_main(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = holds.main(argv)
    return code, out.getvalue(), err.getvalue()


def tree(root):
    return sorted((str(p.relative_to(root)), p.stat().st_mode, p.read_bytes() if p.is_file() else b"")
                  for p in Path(root).rglob("*") if not p.is_fifo())


class RouteTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(os.path.realpath(self._tmp.name))
        self.project, self.mail = self.tmp / "project", self.tmp / "mail"
        (self.mail / "objects").mkdir(parents=True)
        processing = self.project / "processing-v1"
        processing.mkdir(parents=True)
        self.db = sqlite3.connect(processing / "ledger.sqlite")
        self.db.executescript(v001.SQL)
        self.db.execute("INSERT INTO runs VALUES ('r1','records-runner','2026-11-01T00:00:00Z',NULL,'e',NULL,NULL,"
                        "NULL,'completed','{}')")
        self.n = 0

    def tearDown(self):
        self.db.close()

    def original(self, label, fmt="pdf", *, storage=None, status="pending", reason=None, size=None, mail=None):
        """Insert one unpreserved original; ``mail`` is bytes written to objects/<sha> when given."""
        data = f"{label}-{self.n}".encode()
        self.n += 1
        digest = sha(data)
        self.db.execute("INSERT INTO originals(sha256,bytes,first_seen_at,role,scope,storage_path,preservation_status,"
                        "legacy_format,provenance_json) VALUES (?,?,?,'agency_original','in_scope',?,?,?,?)",
                        (digest, len(data) if size is None else size, "2026-10-01T00:00:00+00:00", storage, status, fmt,
                         json.dumps({"subject": CANARY})))
        self.db.execute("INSERT INTO stage_state VALUES (?,'extract','pending',NULL,'runtime','t','r1',NULL)", (digest,))
        self.db.execute("INSERT INTO stage_state VALUES (?,'preserve','pending',NULL,'runtime','t','r1',?)",
                        (digest, reason))
        if mail is not None:
            (self.mail / "objects" / digest).write_bytes(data if mail is True else mail)
        self.db.commit()
        return digest

    def occurrence(self, digest, root, path, key=None):
        ref = {"scheme": "legacy_filesystem_observation_v1", "legacy_oid": "o", "original_sha256": digest,
               "root": root, "path": path}
        self.db.execute("INSERT INTO occurrences VALUES (?,?,'local',?,NULL,NULL,'captured_legacy_local','{}')",
                        (key or "occ-" + digest[:8] + str(self.n), digest, json.dumps(ref, sort_keys=True)))
        self.n += 1
        self.db.commit()

    def legacy_occurrence(self, digest, root, path, *, payload=None, payload_sha=None):
        body = payload or json.dumps({"oid": f"o{self.n}", "sha": digest, "root": root, "path": path,
                                      "receipt": None}, sort_keys=True)
        self.db.execute("INSERT INTO legacy_rows VALUES ('imp','occurrences',?,?,?,?)",
                        (self.n, json.dumps({"oid": f"o{self.n}", "sha": digest}, sort_keys=True), body,
                         payload_sha or sha(body.encode())))
        self.n += 1
        self.db.commit()

    def listing(self, *extra, mail=None):
        code, out, err = run_main(["--project", str(self.project), "--routes", "--mail-history",
                                   str(self.mail if mail is None else mail), "--json", *extra])
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def routes(self, **kwargs):
        return {i["sha256_prefix"]: i["route"] for i in self.listing(**kwargs)["items"]}

    def all_six(self):
        ids = {"R1": self.original("mail", mail=True), "R3": self.original("wb", "xlsx", storage="retained/x.xlsx",
                                                                           reason="Pending Trusted  Validation"),
               "R4": self.original("meta", "json"), "R5": self.original("none", "json"),
               "R2": self.original("exp"), "R2s": self.original("arch")}
        self.occurrence(ids["R4"], "/exports/received", "a/b.json")
        self.occurrence(ids["R2"], "/data", "received/2026/a.pdf")
        self.legacy_occurrence(ids["R2s"], "/data", "research/x/a.pdf")
        return ids

    def test_all_six_routes_commands_and_totals(self):
        ids = self.all_six()
        doc = self.listing()
        by_prefix = {i["sha256_prefix"]: i for i in doc["items"]}
        for route, digest in ids.items():
            row = by_prefix[digest[:12]]
            self.assertEqual((row["route"], row["clearing_command"]), (route, preserve_routes.COMMANDS[route]))
        self.assertEqual(doc["route_counts"], {"R1": 1, "R2": 1, "R2s": 1, "R3": 1, "R4": 1, "R5": 1, "unavailable": 0})
        self.assertEqual(sum(doc["route_counts"].values()), doc["counts"]["PRESERVE_PENDING"])
        self.assertIn("--write-resolution-policy-proposal", by_prefix[ids["R1"][:12]]["clearing_command"])
        self.assertIn("--report-sources", by_prefix[ids["R2"][:12]]["clearing_command"])
        self.assertEqual(by_prefix[ids["R4"][:12]]["clearing_command"], "G scope decision")
        code, out, _ = run_main(["--project", str(self.project), "--routes", "--mail-history", str(self.mail)])
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertTrue(lines[0].endswith(" route clearing_command"))
        self.assertIn("route_counts R1=1 R2=1 R2s=1 R3=1 R4=1 R5=1 unavailable=0", lines)
        row = next(line for line in lines if line.startswith(ids["R3"][:12]))
        self.assertTrue(row.endswith(" R3 " + preserve_routes.COMMANDS["R3"]))

    def test_missing_mail_history_is_unavailable_never_r5(self):
        self.all_six()
        (self.mail / "objects").rename(self.mail / "moved")
        (self.tmp / "link").symlink_to(self.mail)
        (self.tmp / "file").write_text("x")
        for mail in (self.tmp / "absent", self.mail, self.tmp / "link", self.tmp / "file"):
            before = tree(self.tmp)
            doc = self.listing(mail=mail)
            self.assertEqual({i["route"] for i in doc["items"]}, {"unavailable"}, mail)
            self.assertEqual(doc["route_counts"]["unavailable"], 6)
            self.assertEqual(doc["route_counts"]["R5"], 0)
            self.assertEqual(before, tree(self.tmp))  # nothing created
        (self.mail / "moved").rename(self.mail / "objects")
        (self.mail / "objects").chmod(0)
        try:
            if os.geteuid() != 0:
                self.assertEqual({i["route"] for i in self.listing()["items"]}, {"unavailable"})
        finally:
            (self.mail / "objects").chmod(0o700)

    def test_mail_copy_is_exact_identity_or_unavailable(self):
        local = self.original("local", mail=True)
        self.occurrence(local, "/data", "received/a.pdf")  # mail wins over the local route
        wrong_hash = self.original("wh", mail=b"different bytes!")
        wrong_size = self.original("ws", mail=b"short")
        linked = self.original("ln")
        os.symlink(self.mail / "objects" / local, self.mail / "objects" / linked)
        directory = self.original("dir")
        (self.mail / "objects" / directory).mkdir()
        fifo = self.original("fifo")
        os.mkfifo(self.mail / "objects" / fifo)
        absent = self.original("absent")
        routes = {d[:12]: r for d, r in ((local, "R1"), (wrong_hash, "unavailable"), (wrong_size, "unavailable"),
                                          (linked, "unavailable"), (directory, "unavailable"),
                                          (fifo, "unavailable"), (absent, "R5"))}
        self.assertEqual(self.routes(), routes)

    def test_first_occurrence_stable_not_best_route(self):
        a = self.original("a")
        self.occurrence(a, "/data", "archive/z.pdf")
        self.occurrence(a, "/data", "received/a.pdf", key="zz")  # lexically later; not preferred
        self.occurrence(a, "/data", "received/a.pdf", key="dup")  # duplicate evidence
        impostor = self.original("imp")
        self.occurrence(impostor, "/data", "not-received/received.pdf")
        root_class = self.original("rc")
        self.occurrence(root_class, "/exports/productions", "x/y.pdf")
        bad = {name: self.original(name) for name in ("trav", "ctl", "empty")}
        self.occurrence(bad["trav"], "/data", "received/../x.pdf")
        self.occurrence(bad["ctl"], "/data", "received/a\nb.pdf")
        self.occurrence(bad["empty"], "", "received/a.pdf")
        forged = self.original("forged")
        self.legacy_occurrence(forged, "/data", "received/a.pdf", payload_sha="0" * 64)
        mixed = self.original("mixed", "csv")
        self.legacy_occurrence(mixed, "/data", "received/a.csv")
        self.legacy_occurrence(mixed, "/data", "received/b.csv", payload='{"oid": "x", ')
        got = self.routes()
        self.assertEqual(got[a[:12]], "R2s")
        self.assertEqual(got[impostor[:12]], "unavailable")
        self.assertEqual(got[root_class[:12]], "R2")
        self.assertEqual({got[d[:12]] for d in bad.values()}, {"unavailable"})
        self.assertEqual(got[forged[:12]], "unavailable")
        self.assertEqual(got[mixed[:12]], "unavailable")  # undecodable evidence is never "none"

    def test_retained_workbook_is_not_any_xlsx(self):
        marked = self.original("m", "xlsx", storage="r/m.xlsx", status="pending trusted validation")
        unmarked = self.original("u", "xlsx", storage="r/u.xlsx")
        local = self.original("l", "xlsx")
        self.occurrence(local, "/data", "archive/l.xlsx")
        not_xlsx = self.original("n", "pdf", storage="r/n.pdf", reason="pending trusted validation")
        got = self.routes()
        self.assertEqual((got[marked[:12]], got[unmarked[:12]], got[local[:12]], got[not_xlsx[:12]]),
                         ("R3", "unavailable", "R2s", "unavailable"))

    def test_totals_complete_under_code_filter_and_prefix_collision(self):
        one = self.original("one")
        self.occurrence(one, "/data", "received/a.pdf")
        two = self.original("two")
        for tail, fmt in (("1", "pdf"), ("2", "json")):  # same 12-hex prefix, different full hashes
            fake = "c" * 12 + tail * 52
            self.db.execute("INSERT INTO originals(sha256,bytes,first_seen_at,role,scope,preservation_status,"
                            "legacy_format,provenance_json) VALUES (?,1,'2026-10-01T00:00:00+00:00',"
                            "'agency_original','in_scope','pending',?,'{}')", (fake, fmt))
            self.db.execute("INSERT INTO stage_state VALUES (?,'extract','pending',NULL,'runtime','t','r1',NULL)", (fake,))
            self.db.execute("INSERT INTO stage_state VALUES (?,'preserve','pending',NULL,'runtime','t','r1',NULL)", (fake,))
        self.db.commit()
        doc = self.listing("--code", "EXTRACT_ERROR")
        self.assertEqual(doc["items"], [])
        self.assertEqual((doc["route_counts"]["R2"], doc["route_counts"]["R5"]), (1, 3))
        self.assertEqual(sum(doc["route_counts"].values()), doc["counts"]["PRESERVE_PENDING"])
        self.assertNotEqual(one, two)
        empty = self.tmp / "empty"
        (empty / "processing-v1").mkdir(parents=True)
        with closing(sqlite3.connect(empty / "processing-v1" / "ledger.sqlite")) as db:
            db.executescript(v001.SQL)
        code, out, _ = run_main(["--project", str(empty), "--routes", "--mail-history", str(self.mail), "--json"])
        self.assertEqual((code, set(json.loads(out)["route_counts"].values())), (0, {0}))

    def test_read_only_rerun_and_privacy(self):
        self.all_six()
        before = tree(self.tmp)
        first = run_main(["--project", str(self.project), "--routes", "--mail-history", str(self.mail), "--json"])
        second = run_main(["--project", str(self.project), "--routes", "--mail-history", str(self.mail), "--json"])
        self.assertEqual(first, second)
        self.assertEqual(before, tree(self.tmp))
        text = "".join(first[1:]) + run_main(["--project", str(self.project), "--routes", "--mail-history",
                                              str(self.mail)])[1]
        for canary in (CANARY, "jane.doe", str(self.tmp), "/exports", "received/2026", "b.json"):
            self.assertNotIn(canary, text)
        self.assertFalse(any(len(w) == 64 and set(w) <= set("0123456789abcdef") for w in text.replace('"', " ").split()))

    def test_default_listing_is_unchanged_and_options_go_together(self):
        self.all_six()
        code, out, _ = run_main(["--project", str(self.project), "--json"])
        doc = json.loads(out)
        self.assertEqual(code, 0)
        self.assertNotIn("route_counts", doc)
        self.assertTrue(all(set(i) == {"sha256_prefix", "attempts", "first_run", "last_run", "detail_code", "preserved",
                                       "code", "disposition"} for i in doc["items"]))
        code, text, _ = run_main(["--project", str(self.project)])
        self.assertEqual(text.splitlines()[0],
                         "sha256_prefix code attempts age_days last_run detail_code preserved disposition internal")
        self.assertFalse(any(line.startswith("route_counts") for line in text.splitlines()))
        for argv in (["--routes"], ["--mail-history", str(self.mail)]):
            before = tree(self.tmp)
            code, out, err = run_main(["--project", str(self.project), *argv])
            self.assertEqual((code, out), (2, ""))
            self.assertTrue(err.startswith("ROUTES_OPTIONS"))
            self.assertEqual(before, tree(self.tmp))
        with self.assertRaises(SystemExit):
            run_main(["release", "--project", str(self.project), "--code", "EXTRACT_ERROR", "--routes"])

    def test_catalogue_names_every_route_once(self):
        self.assertEqual(set(preserve_routes.COMMANDS), set(preserve_routes.ROUTES))
        self.assertEqual(preserve_routes.clearing_command("nonsense"), preserve_routes.COMMANDS["unavailable"])
        self.assertEqual(preserve_routes.route_counts(["R1", "bogus"])["unavailable"], 1)


if __name__ == "__main__":
    unittest.main()
