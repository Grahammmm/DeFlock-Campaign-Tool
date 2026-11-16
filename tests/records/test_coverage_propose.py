"""Synthetic acceptance for `records coverage-propose`; no campaign records, sends or network."""
import contextlib
import csv
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

from campaign_tool.records import approval_queue, cli, coverage_propose as cp, followup_route as fr
from campaign_tool.records.outcomes import received_items, register, response_items, timeline

RUN = "run1"
DAY = "2026-11-06"
ASKED = "1. Policy documents; 2. Audit logs; 3. Contracts"
SHA = "a" * 64


def put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def ask(n, rating, quote=None, item="i1"):
    return {"ask_id": "ask-%d" % n, "text": response_items.split_asked(ASKED)[n - 1]["text"], "rating": rating,
            "flags": [], "evidence": [] if quote is None else [{"item_id": item, "page": 2, "quote": quote}]}


def q(ident, kind="appeal-denial", agency="alpha", request="R-1", status="pending", item="i1", gaps=()):
    evidence = {"findings": [{"rating": "gap", "page": 2, "quote": g} for g in gaps]}
    if item:
        evidence["item_id"] = item
    return {"id": ident, "kind": kind, "status": status, "agency_id": agency, "request_id": request, "evidence": evidence}


# withheld [1], not_addressed [2], produced [3]
MIXED = [ask(1, "withheld", "Q1"), ask(2, "unclear", "Q2"), ask(3, "answered", "Q3")]
# withheld [1, 2], produced [3]
DENIED = [ask(1, "withheld", "Q1"), ask(2, "exemption_cited", "Q2"), ask(3, "answered", "Q3")]


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.project = Path(tmp.name) / "proj"
        self.out = self.project / "run-outputs" / RUN
        self.queue = []
        patcher = mock.patch.object(approval_queue, "load_queue", side_effect=lambda root: list(self.queue))
        patcher.start()
        self.addCleanup(patcher.stop)

    def stage(self, asks_by_agency=None, run_id=RUN):
        asks_by_agency = {"alpha": DENIED} if asks_by_agency is None else asks_by_agency
        rows, items, ratings, tl = [], [], [], []
        for agency, asks in asks_by_agency.items():
            request, ledger = {"alpha": "R-1", "beta": "B-1"}[agency], "L-" + agency
            rows.append({"agency": agency.upper(), "agency_key": agency, "request_id": request,
                         "ledger_request_id": ledger, "asked": ASKED, "issues": []})
            items.append({"item_id": "i1", "original_sha256": SHA, "agency_id": agency, "request_id": ledger})
            ratings.append({"agency_key": agency, "request_id": request, "ledger_request_id": ledger,
                            "flags": [], "responses": ["i1"], "asks": json.loads(json.dumps(asks))})
            tl.append({"agency_key": agency, "request_id": request, "events": [], "clocks": {}, "issues": []})
        # item ids repeat across agencies; the validator needs one identity per id, so disambiguate
        for number, (item, rating) in enumerate(zip(items, ratings)):
            if number:
                tag = "i%d" % (number + 1)
                item["item_id"] = tag
                for a in rating["asks"]:
                    for e in a["evidence"]:
                        e["item_id"] = tag
        put(self.out / "CURRENT-REQUEST-REGISTER.json", {"schema": register.SCHEMA, "as_of": DAY,
                                                         "requests": rows, "request_count": len(rows)})
        put(self.out / "outcomes" / "received-items.json", {"schema": received_items.SCHEMA, "run_id": run_id,
                                                           "items": items})
        put(self.out / "outcomes" / "response-items.json", {"schema": response_items.SCHEMA, "run_id": run_id,
                                                           "requests": ratings})
        put(self.out / "outcomes" / "timeline.json", {"schema": timeline.SCHEMA, "run_id": run_id, "as_of": DAY,
                                                      "requests": tl})

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cp.main(["--project", str(self.project), "--run-id", RUN, *args])
        return code, out.getvalue(), err.getvalue()

    def snapshot(self):
        result = {}
        for path in sorted(self.project.rglob("*")):
            info = path.lstat()
            result[str(path)] = (info.st_mode, info.st_mtime_ns, path.read_bytes() if path.is_file() else None)
        return result

    def rows(self, agency="alpha"):
        text = (self.project / "followup-coverage" / (agency + ".proposed.tsv")).read_text(encoding="utf-8")
        return list(csv.DictReader(io.StringIO(text), delimiter="\t"))

    def by_id(self, agency="alpha"):
        return {r["queue_id"]: r for r in self.rows(agency)}


class MappingTests(Base):
    def test_exact_denial_mapping_equals_router_mapper(self):
        self.stage()
        self.queue = [q("q1")]
        self.assertEqual(self.cli("--apply")[0], 0)
        row = self.by_id()["q1"]
        self.assertEqual((row["ask_numbers"], row["confirmed_by"], row["note"]), ("1,2", "", cp.MAPPED))
        self.assertEqual(fr._mapped(self.queue[0], DENIED, [1, 2]), [1, 2])
        header = (self.project / "followup-coverage" / "alpha.proposed.tsv").read_text().splitlines()[0]
        self.assertEqual(header.split("\t"), ["queue_id", "ask_numbers", "confirmed_by", "note"])

    def test_missing_records_gap_mapping_and_disjoint_pair(self):
        self.stage({"alpha": MIXED})
        self.queue = [q("q1"), q("q2", "followup-missing-records", gaps=["Q2"])]
        self.assertEqual(self.cli("--apply")[0], 0)
        rows = self.by_id()
        self.assertEqual((rows["q1"]["ask_numbers"], rows["q2"]["ask_numbers"]), ("1", "2"))
        self.assertEqual({r["note"] for r in rows.values()}, {cp.MAPPED})
        self.assertEqual(fr._mapped(self.queue[1], MIXED, [2]), [2])

    def test_unmapped_scope_needs_a_person_never_none(self):
        self.stage({"alpha": MIXED})
        self.queue = [q("q1", item=None), q("q2", "followup-missing-records", gaps=["not in any ask"])]
        self.assertEqual(self.cli("--apply")[0], 0)
        for row in self.rows():
            self.assertEqual((row["ask_numbers"], row["confirmed_by"], row["note"]), ("", "", cp.UNMAPPED))

    def test_split_source_ask_is_unmapped(self):
        split = [dict(ask(1, "withheld", "Q1"), evidence=[{"item_id": "i1", "page": 2, "quote": "Q1"},
                                                          {"item_id": "i2", "page": 1, "quote": "Q1"}])] + DENIED[1:]
        self.stage({"alpha": split})
        received = json.loads((self.out / "outcomes" / "received-items.json").read_text())
        received["items"].append({"item_id": "i2", "original_sha256": "b" * 64, "agency_id": "alpha",
                                  "request_id": "L-alpha"})
        put(self.out / "outcomes" / "received-items.json", received)
        self.queue = [q("q1")]
        self.assertEqual(self.cli("--apply")[0], 0)
        self.assertEqual(self.by_id()["q1"]["note"], cp.UNMAPPED)

    def test_overlap_blanks_every_participant_and_keeps_disjoint_rows(self):
        self.stage({"alpha": MIXED})
        self.queue = [q("q1"), q("q2", status="approved"), q("q3", "followup-missing-records", gaps=["Q2"])]
        self.assertEqual(self.cli("--apply")[0], 0)
        rows = self.by_id()
        for ident in ("q1", "q2"):
            self.assertEqual((rows[ident]["ask_numbers"], rows[ident]["note"]), ("", cp.OVERLAP))
        self.assertEqual((rows["q3"]["ask_numbers"], rows["q3"]["note"]), ("2", cp.MAPPED))

    def test_controls_inactive_noncoverer_and_other_requests(self):
        self.stage({"alpha": DENIED, "beta": DENIED})
        self.queue = [q("q1"), q("q2", status="rejected"), q("q3", "reply-extension"),
                      q("q4", agency="beta", request="B-1", item="i2"), q("q5", agency="beta", request="B-1", status="sent", item="i2"),
                      q("q6", agency="gamma", request="G-1")]
        self.assertEqual(self.cli("--apply")[0], 0)
        self.assertEqual(set(self.by_id()), {"q1"})
        self.assertEqual(set(self.by_id("beta")), {"q4"})
        # the same ask numbers in different requests are not an overlap
        self.assertEqual((self.by_id()["q1"]["ask_numbers"], self.by_id("beta")["q4"]["ask_numbers"]), ("1,2", "1,2"))

    def test_no_candidates_is_zero_and_writes_nothing(self):
        self.stage()
        before = self.snapshot()
        code, out, _ = self.cli("--apply")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["rows"], 0)
        self.assertEqual(before, self.snapshot())
        self.assertFalse((self.project / "followup-coverage").exists())


class IoTests(Base):
    def test_dry_run_writes_nothing_and_output_is_counts_only(self):
        self.stage({"alpha": MIXED})
        self.queue = [q("q1"), q("q2"), q("q3", "followup-missing-records", gaps=["Q2"])]
        before = self.snapshot()
        code, out, err = self.cli()
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(before, self.snapshot())
        summary = json.loads(out)
        self.assertEqual(summary, {"schema": cp.SUMMARY_SCHEMA, "run_id": RUN, "dry_run": True, "agencies": 1,
                                   "rows": 3, "mapped": 1, "needs_person": 2, "written": 0, "present": 0})
        for secret in ("q1", "alpha", "R-1", "Q2", "Policy"):
            self.assertNotIn(secret, out)

    def test_apply_is_private_idempotent_and_never_touches_confirmed_table(self):
        self.stage()
        self.queue = [q("q1")]
        folder = self.project / "followup-coverage"
        folder.mkdir()
        folder.chmod(0o755)
        confirmed = folder / "alpha.tsv"
        confirmed.write_bytes(b"queue_id\task_numbers\tconfirmed_by\tnote\nq1\t1\tsomeone\tkept\n")
        os.utime(confirmed, ns=(10**9, 10**9))
        kept = (confirmed.read_bytes(), confirmed.lstat().st_mode, confirmed.lstat().st_mtime_ns)
        self.assertEqual(self.cli("--apply")[0], 0)
        target = folder / "alpha.proposed.tsv"
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o755)  # existing directory mode preserved
        self.assertEqual(kept, (confirmed.read_bytes(), confirmed.lstat().st_mode, confirmed.lstat().st_mtime_ns))
        self.assertEqual(sorted(os.listdir(folder)), ["alpha.proposed.tsv", "alpha.tsv"])
        snapshot = self.snapshot()
        code, out, _ = self.cli("--apply")
        self.assertEqual((code, json.loads(out)["written"], json.loads(out)["present"]), (0, 0, 1))
        self.assertEqual(snapshot, self.snapshot())

    def test_new_directory_is_private(self):
        self.stage()
        self.queue = [q("q1")]
        self.assertEqual(self.cli("--apply")[0], 0)
        self.assertEqual(stat.S_IMODE((self.project / "followup-coverage").stat().st_mode), 0o700)

    def test_differing_proposal_refuses_before_any_write(self):
        self.stage({"alpha": DENIED, "beta": DENIED})
        self.queue = [q("q1"), q("q2", agency="beta", request="B-1")]
        folder = self.project / "followup-coverage"
        folder.mkdir()
        (folder / "beta.proposed.tsv").write_text("different\n")
        before = self.snapshot()
        code, out, err = self.cli("--apply")
        self.assertEqual((code, out, err.strip()), (2, "", "COVERAGE_PROPOSAL_EXISTS"))
        self.assertEqual(before, self.snapshot())
        self.assertEqual(self.cli()[2].strip(), "COVERAGE_PROPOSAL_EXISTS")  # dry run preflights too

    def test_second_agency_write_failure_rolls_back_only_this_call(self):
        self.stage({"alpha": DENIED, "beta": DENIED})
        self.queue = [q("q1"), q("q2", agency="beta", request="B-1")]
        real, calls = os.link, []

        def flaky(src, dst, *a, **k):
            calls.append(dst)
            if len(calls) == 2:
                raise PermissionError("synthetic")
            return real(src, dst, *a, **k)

        with mock.patch.object(os, "link", flaky):
            code, out, err = self.cli("--apply")
        self.assertEqual((code, out, err.strip()), (2, "", "COVERAGE_WRITE_FAILED"))
        self.assertNotIn("synthetic", err)
        self.assertFalse((self.project / "followup-coverage").exists())

    def test_unsafe_targets_and_run_ids(self):
        self.stage()
        self.queue = [q("q1")]
        elsewhere = Path(self.project.parent) / "elsewhere"
        elsewhere.mkdir()
        (self.project / "followup-coverage").symlink_to(elsewhere)
        self.assertEqual(self.cli("--apply")[2].strip(), "COVERAGE_TARGET_UNSAFE")
        self.assertEqual(os.listdir(elsewhere), [])
        (self.project / "followup-coverage").unlink()
        folder = self.project / "followup-coverage"
        folder.mkdir()
        (folder / "alpha.proposed.tsv").symlink_to(elsewhere / "x")
        self.assertEqual(self.cli("--apply")[2].strip(), "COVERAGE_TARGET_UNSAFE")
        for bad in ("../run1", "a/b", ""):
            with self.assertRaises(cp.ProposalError) as ctx:
                cp.propose(self.project, bad)
            self.assertEqual(ctx.exception.code, "COVERAGE_PATH_INVALID")

    def test_invalid_and_unavailable_inputs_refuse_with_fixed_codes_and_no_writes(self):
        self.queue = [q("q1")]
        self.stage()
        (self.out / "outcomes" / "response-items.json").unlink()
        self.assertEqual(self.cli("--apply")[2].strip(), "COVERAGE_INPUT_UNAVAILABLE")
        self.stage(run_id="stale")  # received/response/timeline name another run
        self.assertEqual(self.cli("--apply")[2].strip(), "COVERAGE_INPUT_INVALID")
        self.stage()
        (self.out / "outcomes" / "timeline.json").write_text("{not json")
        self.assertEqual(self.cli("--apply")[2].strip(), "COVERAGE_INPUT_INVALID")
        self.stage()
        reg = json.loads((self.out / "CURRENT-REQUEST-REGISTER.json").read_text())
        reg["as_of"] = "2026-11-05"
        put(self.out / "CURRENT-REQUEST-REGISTER.json", reg)
        self.assertEqual(self.cli("--apply")[2].strip(), "COVERAGE_INPUT_INVALID")
        self.stage()
        with mock.patch.object(approval_queue, "load_queue", side_effect=approval_queue.ApprovalQueueError("x")):
            self.assertEqual(self.cli("--apply")[2].strip(), "COVERAGE_QUEUE_INVALID")
        self.queue = [q("q1"), q("q1")]
        self.assertEqual(self.cli("--apply")[2].strip(), "COVERAGE_QUEUE_INVALID")
        self.assertFalse((self.project / "followup-coverage").exists())

    def test_wrong_original_identity_is_invalid(self):
        self.stage()
        received = json.loads((self.out / "outcomes" / "received-items.json").read_text())
        received["items"][0]["agency_id"] = "other"
        put(self.out / "outcomes" / "received-items.json", received)
        self.queue = [q("q1")]
        self.assertEqual(self.cli("--apply")[2].strip(), "COVERAGE_INPUT_INVALID")

    def test_no_network_send_or_publish(self):
        self.stage()
        self.queue = [q("q1")]
        with mock.patch("socket.socket", side_effect=AssertionError("network")), \
                mock.patch("smtplib.SMTP", side_effect=AssertionError("smtp")):
            self.assertEqual(self.cli("--apply")[0], 0)


class ConsumerBoundaryTests(Base):
    def route(self):
        return fr.route_run(self.project, RUN)[0]

    def test_router_ignores_proposal_until_a_person_confirms_and_renames(self):
        self.stage({"alpha": MIXED})
        self.queue = [q("q1"), q("q2", "followup-missing-records", gaps=["unmapped quote"])]
        self.assertEqual(self.route()["reason"], "coverage_unreconciled")
        self.assertEqual(self.cli("--apply")[0], 0)
        folder = self.project / "followup-coverage"
        self.assertEqual(self.route()["reason"], "coverage_unreconciled")  # proposal alone changes nothing
        self.assertEqual(self.by_id()["q1"]["ask_numbers"], "1")
        # naive rename keeps blank confirmed_by: refused by the real consumer
        (folder / "alpha.proposed.tsv").rename(folder / "alpha.tsv")
        self.assertEqual(self.route()["reason"], "coverage_table_invalid")
        # an explicit human edit settles it, including a choice for the blank row
        (folder / "alpha.tsv").write_text(
            "queue_id\task_numbers\tconfirmed_by\tnote\nq1\t1\treviewer\tchecked\nq2\t2\treviewer\tchosen\n")
        routed = self.route()
        self.assertEqual(routed["handled_by"], "received_items")
        self.assertEqual({(c["queue_id"], c["basis"]) for c in routed["covered"]},
                         {("q1", "operator_table"), ("q2", "operator_table")})

    def test_proposal_for_exact_rows_equals_route_covered(self):
        self.stage({"alpha": MIXED})
        self.queue = [q("q1"), q("q2", "followup-missing-records", gaps=["Q2"])]
        before = self.route()
        self.assertEqual({(c["queue_id"], tuple(c["items"])) for c in before["covered"]}, {("q1", (1,)), ("q2", (2,))})
        self.assertEqual(self.cli("--apply")[0], 0)
        rows = self.by_id()
        self.assertEqual({(i, tuple(int(n) for n in r["ask_numbers"].split(","))) for i, r in rows.items()},
                         {(c["queue_id"], tuple(c["items"])) for c in before["covered"]})


class CliTests(Base):
    def test_top_level_dispatch_and_refusal(self):
        self.stage()
        self.queue = [q("q1")]
        self.assertEqual(cli.COMMANDS["coverage-propose"], ("coverage_propose", "main"))
        out = io.StringIO()
        with mock.patch("sys.argv", ["records", "coverage-propose", "--project", str(self.project), "--run-id", RUN]), \
                contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(), 0)
        self.assertTrue(json.loads(out.getvalue())["dry_run"])
        self.assertFalse((self.project / "followup-coverage").exists())
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            cp.main(["--project", str(self.project), "--run-id", RUN, "--confirm"])


if __name__ == "__main__":
    unittest.main()
