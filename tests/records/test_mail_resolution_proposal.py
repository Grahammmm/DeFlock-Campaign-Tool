"""DF0652-01 synthetic tests: policy proposal writer. No live data."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from campaign_tool.records import mail_adoption as adoption
from campaign_tool.records import mail_resolution_config as config
from campaign_tool.records import mail_resolution_proposal as proposal
from campaign_tool.records.ledger import store
from campaign_tool.records.ledger.mail_resolution_policy import ApprovedMailResolutionPolicy
from tests.records import test_mail_resolution_census as base
from tests.records.test_mail_adoption import write_private


def h(n):
    return hashlib.sha256(str(n).encode()).hexdigest()


def entry(state, n, receipts=2):
    ids = [h(("r", n, i)) for i in range(receipts)]
    members = [{"receipt_sha256": r, "mime_precheck": None, "occurrence_ids": [h(("o", r))]}
               for r in sorted(ids)]
    if state == "held":
        prop = {"state": "held", "code": "resolution_no_eligible_binding_receipt"}
    else:
        binding = members[0]
        prop = {"state": state, "verification_receipt_sha256": binding["receipt_sha256"],
                "binding_occurrence_id": binding["occurrence_ids"][0],
                "dependent_receipts": [m["receipt_sha256"] for m in members[1:]]}
    return {"bytes": 10 + n, "legacy_format": "pdf", "kinds": ["attachment"],
            "legacy_binding_sha256": h(("l", n)), "state": "resolved" if state == "fixed" else "unresolved",
            "eligibility_code": None, "receipts": members, "proposal": prop, "warnings": []}


def census(entries):
    return {"schema": "legacy-mail-resolution-inventory-v1", "counts": {"originals": len(entries)},
            "originals": entries}


class Workdir(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.root, self.mail = self.base / "records", self.base / "mail"
        self.root.mkdir(mode=0o700)
        self.mail.mkdir(mode=0o700)
        (self.root / "ledger.sqlite").write_bytes(b"ledger")
        (self.mail / "r.json").write_bytes(b"mail")
        self.out = self.base / "out"
        self.out.mkdir(mode=0o700)
        self.approval = self.out / "approval.txt"
        write_private(self.approval, b"opaque approval\n")

    def inv(self, value, name="inv.json"):
        path = self.out / name
        write_private(path, store.canonical(value).encode())
        return path

    def run_main(self, *args):
        out, err = io.StringIO(), io.StringIO()
        argv = ["adopt-mail", "--root", str(self.root), "--mail-history", str(self.mail), *map(str, args)]
        with patch.object(sys, "argv", argv), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = adoption.main()
            except SystemExit as exit_:
                code = exit_.code
        return code, out.getvalue(), err.getvalue()

    def snap(self):
        result = {}
        for top in (self.root, self.mail):
            for dirpath, dirs, files in os.walk(top):
                for name in [".", *dirs, *files]:
                    p = Path(dirpath) / name
                    st = p.lstat()
                    result[str(p)] = (st.st_ino, st.st_mode, st.st_size, st.st_mtime_ns,
                                      p.read_bytes() if p.is_file() else None)
        return result

    def write(self, inv, name="proposal.json"):
        return proposal.write_proposal(self.root, self.mail, self.out / name, inventory_path=inv,
                                       approval_record_path=self.approval)


class ProposalTests(Workdir):
    def test_mixed_census_projects_proposed_only(self):
        entries = {h("p1"): entry("proposed", 1, 3), h("p2"): entry("proposed", 2, 1),
                   h("f1"): entry("fixed", 3), h("h1"): entry("held", 4), h("h0"): entry("held", 5)}
        inv = self.inv(census(entries))
        before = self.snap()
        summary = self.write(inv)
        self.assertEqual(self.snap(), before)
        raw = (self.out / "proposal.json").read_bytes()
        self.assertEqual(summary, {"counts": {"originals": 2, "held": 2, "fixed_omitted": 1},
                                   "proposal_sha256": hashlib.sha256(raw).hexdigest()})
        self.assertEqual(stat.S_IMODE((self.out / "proposal.json").stat().st_mode), 0o600)
        value = json.loads(raw)
        self.assertEqual(set(value), {"schema", "inventory_sha256", "approval_sha256", "originals", "held"})
        self.assertEqual(value["inventory_sha256"], hashlib.sha256(inv.read_bytes()).hexdigest())
        self.assertEqual(value["approval_sha256"], hashlib.sha256(self.approval.read_bytes()).hexdigest())
        self.assertEqual(sorted(value["originals"]), sorted([h("p1"), h("p2")]))
        self.assertEqual(value["held"], sorted([h("h1"), h("h0")]))
        self.assertNotIn(h("f1"), raw.decode())
        for held in value["held"]:
            self.assertNotIn(held, value["originals"])
        p1 = entries[h("p1")]
        self.assertEqual(value["originals"][h("p1")], {
            "bytes": p1["bytes"], "role": "original", "scope": "in_scope",
            "legacy_binding_sha256": p1["legacy_binding_sha256"],
            "verification_receipt_sha256": p1["proposal"]["verification_receipt_sha256"],
            "binding_occurrence_id": p1["proposal"]["binding_occurrence_id"],
            "dependent_receipts": p1["proposal"]["dependent_receipts"]})
        ApprovedMailResolutionPolicy(raw, expected_sha256=summary["proposal_sha256"])
        # Held receipt digests never appear anywhere in originals.
        for r in entries[h("h1")]["receipts"]:
            self.assertNotIn(r["receipt_sha256"], json.dumps(value["originals"]))

    def test_hashes_exact_and_deterministic(self):
        inv = self.inv(census({h("p"): entry("proposed", 1)}))
        a = self.write(inv, "a.json")
        b = self.write(inv, "b.json")
        self.assertEqual(a, b)
        write_private(self.approval, b"opaque approval \n")
        c = self.write(inv, "c.json")
        self.assertNotEqual(a["proposal_sha256"], c["proposal_sha256"])
        self.assertEqual(json.loads((self.out / "a.json").read_bytes())["originals"],
                         json.loads((self.out / "c.json").read_bytes())["originals"])

    def test_existing_output_is_refused_untouched(self):
        inv = self.inv(census({h("p"): entry("proposed", 1)}))
        existing = self.out / "proposal.json"
        write_private(existing, b"keep")
        ino = existing.stat().st_ino
        with self.assertRaisesRegex(adoption.AdoptionError, "resolution_proposal_output_exists"):
            self.write(inv)
        self.assertEqual((existing.read_bytes(), existing.stat().st_ino), (b"keep", ino))
        (self.out / "dangling.json").symlink_to(self.out / "nowhere")
        with self.assertRaises(adoption.AdoptionError):
            self.write(inv, "dangling.json")
        self.assertFalse((self.out / "nowhere").exists())

    def test_invalid_inputs_fail_closed_without_output(self):
        good = census({h("p"): entry("proposed", 1)})
        bad_state = census({h("p"): dict(entry("proposed", 1), proposal={"state": "weird"})})
        bad_binding = census({h("p"): entry("proposed", 1)})
        bad_binding["originals"][h("p")]["proposal"]["binding_occurrence_id"] = h("unlisted")
        bad_dependents = census({h("p"): entry("proposed", 1)})
        bad_dependents["originals"][h("p")]["proposal"]["dependent_receipts"] = []
        bad_bytes = census({h("p"): entry("proposed", 1)})
        bad_bytes["originals"][h("p")]["bytes"] = True
        wrong_schema = dict(good, schema="other")
        cases = [self.inv(v, f"c{i}.json") for i, v in enumerate(
            (bad_state, bad_binding, bad_dependents, bad_bytes, wrong_schema))]
        for name, raw in (("noncanon.json", json.dumps(good, indent=1).encode()),
                          ("dup.json", b'{"schema":"x","schema":"y"}'), ("nan.json", b'{"a":NaN}')):
            write_private(self.out / name, raw)
            cases.append(self.out / name)
        cases += [self.out / "missing.json", self.out]
        before = sorted(os.listdir(self.out))
        for path in cases:
            with self.assertRaisesRegex(adoption.AdoptionError, "resolution_proposal_input_invalid"):
                self.write(path)
        big = self.out / "big.json"
        write_private(big, b"x" * 100)
        with patch.object(proposal, "MAX_INVENTORY_BYTES", 10):
            with self.assertRaises(adoption.AdoptionError):
                self.write(big)
        self.assertEqual(sorted(os.listdir(self.out)), sorted(before + ["big.json"]))

    def test_inputs_require_owner_only_permissions(self):
        inv = self.inv(census({h("p"): entry("proposed", 1)}))
        for path in (inv, self.approval):
            for mode in (0o640, 0o604):
                with self.subTest(input=path.name, mode=oct(mode)):
                    path.chmod(mode)
                    before = self.snap()
                    inputs = {p.name: (p.read_bytes(), p.stat()) for p in self.out.iterdir()}
                    try:
                        with self.assertRaisesRegex(adoption.AdoptionError,
                                                    "resolution_proposal_input_invalid"):
                            self.write(inv, f"refused-{path.stem}-{mode:o}.json")
                        self.assertEqual(self.snap(), before)
                        self.assertEqual({p.name: (p.read_bytes(), p.stat())
                                          for p in self.out.iterdir()}, inputs)
                    finally:
                        path.chmod(0o600)

    def test_inputs_require_current_owner(self):
        inv = self.inv(census({h("p"): entry("proposed", 1)}))
        real_fstat = os.fstat
        for path in (inv, self.approval):
            identity = (path.stat().st_dev, path.stat().st_ino)

            def foreign_owner(fd):
                info = real_fstat(fd)
                if (info.st_dev, info.st_ino) == identity:
                    info = SimpleNamespace(**{name: getattr(info, name) for name in dir(info)
                                              if name.startswith("st_")})
                    info.st_uid = os.getuid() + 1
                return info

            with self.subTest(input=path.name):
                before = self.snap()
                inputs = {p.name: (p.read_bytes(), p.stat()) for p in self.out.iterdir()}
                with patch.object(os, "fstat", side_effect=foreign_owner):
                    with self.assertRaisesRegex(adoption.AdoptionError,
                                                "resolution_proposal_input_invalid"):
                        self.write(inv, f"refused-{path.stem}.json")
                self.assertEqual(self.snap(), before)
                self.assertEqual({p.name: (p.read_bytes(), p.stat())
                                  for p in self.out.iterdir()}, inputs)

    def test_output_confinement(self):
        inv = self.inv(census({}))
        for target in (self.root / "p.json", self.mail / "p.json", inv, self.approval,
                       self.base / "missing-dir" / "p.json"):
            with self.assertRaisesRegex(adoption.AdoptionError, "resolution_proposal_output_unsafe"):
                proposal.write_proposal(self.root, self.mail, target, inventory_path=inv,
                                        approval_record_path=self.approval)
        loose = self.base / "loose"
        loose.mkdir(mode=0o755)
        os.chmod(loose, 0o755)
        with self.assertRaisesRegex(adoption.AdoptionError, "resolution_proposal_output_unsafe"):
            proposal.write_proposal(self.root, self.mail, loose / "p.json", inventory_path=inv,
                                    approval_record_path=self.approval)
        (self.base / "link").symlink_to(self.out)
        with self.assertRaises(adoption.AdoptionError):
            proposal.write_proposal(self.root, self.mail, self.base / "link" / "p.json",
                                    inventory_path=inv, approval_record_path=self.approval)
        self.assertEqual(sorted(os.listdir(self.out)), ["approval.txt", "inv.json"])

    def test_write_failure_removes_only_own_file(self):
        inv = self.inv(census({h("p"): entry("proposed", 1)}))
        with patch.object(os, "fsync", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                self.write(inv)
        self.assertFalse((self.out / "proposal.json").exists())


class CliTests(Workdir):
    def test_mode_matrix_and_stdout(self):
        inv = self.inv(census({h("p"): entry("proposed", 1), h("h"): entry("held", 2)}))
        flags = ["--from-inventory", inv, "--approval-record", self.approval]
        for args in (["--write-resolution-policy-proposal", self.out / "x.json"],
                     ["--write-resolution-policy-proposal", self.out / "x.json", "--from-inventory", inv],
                     flags, ["--from-inventory", inv],
                     ["--write-resolution-policy-proposal", self.out / "x.json", *flags, "--apply"],
                     ["--write-resolution-policy-proposal", self.out / "x.json", *flags, "--config", self.approval],
                     ["--write-resolution-policy-proposal", self.out / "x.json", *flags,
                      "--legacy-resolution-inventory", self.out / "y.json"]):
            before = self.snap()
            code, out, _ = self.run_main(*args)
            self.assertEqual((code, out), (2, ""))
            self.assertEqual(self.snap(), before)
            self.assertEqual(sorted(os.listdir(self.out)), ["approval.txt", "inv.json"])
        before = self.snap()
        with patch.object(adoption, "adopt", side_effect=AssertionError), \
                patch("campaign_tool.records.run.root_lock", side_effect=AssertionError):
            code, out, err = self.run_main("--write-resolution-policy-proposal", self.out / "x.json", *flags)
        self.assertEqual((code, err), (0, ""))
        value = json.loads(out)
        self.assertEqual(set(value), {"counts", "proposal_sha256"})
        self.assertEqual(value["counts"], {"originals": 1, "held": 1, "fixed_omitted": 0})
        self.assertNotIn(h("p"), out)
        self.assertNotIn(str(self.out), out)
        self.assertEqual(self.snap(), before)
        code, out, _ = self.run_main("--write-resolution-policy-proposal", self.out / "x.json", *flags)
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out)["code"], "resolution_proposal_output_exists")

    def test_input_refusal_is_coded_without_paths(self):
        code, out, err = self.run_main("--write-resolution-policy-proposal", self.out / "x.json",
                                       "--from-inventory", self.out / "nope.json",
                                       "--approval-record", self.approval)
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out)["code"], "resolution_proposal_input_invalid")
        self.assertNotIn("nope", out + err)


class RoundTripTests(unittest.TestCase):
    setUp = base.CensusTests.setUp
    declare = base.CensusTests.declare
    cfg = base.CensusTests.cfg
    tree = base.CensusTests.tree
    save_policy = base.CensusTests.save_policy

    def test_real_census_pinned_dry_run_loads_without_mutation(self):
        out = self.base / "out"
        out.mkdir(mode=0o700)
        approval = out / "approval.txt"
        write_private(approval, b"synthetic approval record")
        inv, prop = out / "inv.json", out / "proposal.json"
        argv = ["adopt-mail", "--root", str(self.root), "--mail-history", str(self.mail)]
        before = self.tree()
        with patch.object(sys, "argv", argv + ["--legacy-resolution-inventory", str(inv)]), \
                contextlib.redirect_stdout(io.StringIO()):
            adoption.main()
        census_after = self.tree()
        self.assertEqual(census_after, before)
        stdout = io.StringIO()
        with patch.object(sys, "argv", argv + ["--write-resolution-policy-proposal", str(prop),
                          "--from-inventory", str(inv), "--approval-record", str(approval)]), \
                contextlib.redirect_stdout(stdout):
            self.assertEqual(adoption.main(), 0)
        self.assertEqual(self.tree(), before)
        summary = json.loads(stdout.getvalue())
        self.assertEqual(summary["counts"], {"originals": 1, "held": 0, "fixed_omitted": 0})
        raw = prop.read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), summary["proposal_sha256"])
        self.assertEqual(json.loads(raw)["inventory_sha256"], hashlib.sha256(inv.read_bytes()).hexdigest())
        # Separate synthetic install/pin step, not done by the proposal mode.
        directory = self.root / "resolution-policies"
        directory.mkdir(mode=0o700)
        write_private(directory / summary["proposal_sha256"], raw)
        path = self.cfg(**{config.CURRENT: summary["proposal_sha256"]})
        pinned = self.tree()
        report = io.StringIO()
        with patch.object(sys, "argv", argv + ["--config", str(path)]), contextlib.redirect_stdout(report):
            adoption.main()
        self.assertEqual(json.loads(report.getvalue())["resolution_policy"]["state"], "loaded")
        self.assertEqual(self.tree(), pinned)


if __name__ == "__main__":
    unittest.main()
