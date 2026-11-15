"""Synthetic-only legacy adoption; read-only mounts are simulated, not bypassed."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from campaign_tool.records import legacy_adoption as adoption
from campaign_tool.records.intake import local_capture as capture
from campaign_tool.records.ledger import store
from campaign_tool.records.ledger.occurrence_projection import project_occurrences
from campaign_tool.records.run import Pipeline
from tests.records import test_ledger_occurrences as fixtures


class LegacyAdoptionTests(unittest.TestCase):
    def setUp(self):
        self.helper = fixtures.OccurrenceProjectionTests()
        self.helper.setUp()
        self.addCleanup(self.helper.doCleanups)
        self.base = self.helper.database.parent
        # Source trees live under the system temp root, not tempfile's TMPDIR: agent
        # runtimes point TMPDIR below a `.hermes` directory, which source_class()
        # refuses by design, so the fixture must not inherit the runner's TMPDIR.
        sources = tempfile.TemporaryDirectory(dir=os.path.realpath("/tmp"))
        self.addCleanup(sources.cleanup)
        self.sources = Path(sources.name)
        self.export = self.sources / "export"
        self.supplement = self.sources / "supplement"
        self.export.mkdir(mode=0o700)
        self.supplement.mkdir(mode=0o700)
        for root in (self.export, self.supplement):
            self.assertNotIn(".hermes", root.parts)
        self.rows = []
        for index, form in enumerate(("txt", "pdf", "png", "json", "csv", "other")):
            data = b"synthetic original" if index == 0 else ("synthetic " + form).encode()
            subject = capture.digest(data)
            if index:
                self.helper.add_doc(data.decode(), form)
                self.helper.add_occurrence(form, subject)
            source = self.export / "received" / (str(index) + ".fictional")
            source.parent.mkdir(mode=0o700, exist_ok=True)
            source.write_bytes(data)
            source.chmod(0o400)
            self.rows.append({"sha256": subject, "size": len(data), "source_path": str(source)})
        project_occurrences(self.helper.database, self.helper.capture())
        self.pipeline = Pipeline(self.base)
        self.manifest = self.base / "manifest.tsv"
        self.policy = self.base / "policy.json"
        self.config = self.base / "config.json"
        self.policy_value = {"schema": "approved-local-capture-policy-v1",
            "inventory_sha256": "a" * 64, "approval_sha256": "b" * 64,
            "originals": {row["sha256"]: {"bytes": row["size"], "role": "original",
                "scope": "in_scope", "evidence_sha256": capture.digest(capture.encoded(row))}
                for row in self.rows[:3]}}
        self.save_inputs()

    def save_inputs(self, *, enabled=True, null_policy=False):
        self.manifest.write_text("sha256\tsize\tsource_path\n" + "".join(
            f'{r["sha256"]}\t{r["size"]}\t{r["source_path"]}\n' for r in self.rows))
        self.policy.write_bytes(capture.encoded(self.policy_value))
        value = {"schema": "legacy-adoption-v1", "manifest_sha256": capture.digest(self.manifest.read_bytes()),
            "policy_sha256": None if null_policy else capture.digest(self.policy.read_bytes()),
            "export_root": str(self.export),
            "supplement_root": str(self.supplement), "adoption": "enabled" if enabled else "disabled"}
        self.config.write_bytes(capture.encoded(value))
        for path in (self.manifest, self.policy, self.config):
            path.chmod(0o600)

    def invoke(self, *extra, policy=True):
        output = io.StringIO()
        policy_args = ["--policy", str(self.policy)] if policy else []
        with contextlib.redirect_stdout(output):
            status = adoption.main(["--root", str(self.base), "--config", str(self.config),
                "--manifest", str(self.manifest), *policy_args, *extra])
        counts = json.loads(output.getvalue())
        self.assertNotIn(str(self.base), output.getvalue())
        self.assertNotIn("fictional", output.getvalue())
        return status, counts

    def snapshot(self):
        return (self.helper.database.read_bytes(), sorted(self.pipeline.root.intake.rglob("*")))

    def ledger_rows(self, sql):
        with store.ledger(self.helper.database, readonly=True) as con:
            return [tuple(row) for row in con.execute(sql)]

    def test_apply_and_replay_share_one_locked_lifecycle(self):
        from types import SimpleNamespace
        with patch.object(capture.os, "fstatvfs", return_value=SimpleNamespace(f_flag=os.ST_RDONLY)):
            status, counts = self.invoke("--apply")
            self.assertEqual(status, 0)
            self.assertEqual(counts["adopted"], 3)
            self.assertEqual(self.ledger_rows("SELECT count(*) FROM original_resolutions"), [(3,)])
            for row in self.rows[:3]:
                with store.ledger(self.helper.database, readonly=True) as con:
                    original = store.effective_original(con, row["sha256"])
                    state = con.execute("SELECT status FROM stage_state WHERE original_sha256=? AND stage='preserve'",
                                        (row["sha256"],)).fetchone()
                self.assertEqual(original["storage_path"], str(self.pipeline.root.intake / "blobs" / row["sha256"]))
                self.assertEqual(state[0], "done")
            untouched = self.ledger_rows("SELECT role,scope,storage_path FROM originals WHERE legacy_format IN ('json','csv','other')")
            self.assertEqual(untouched, [("unresolved", "unresolved", None)] * 3)
            occurrences = self.ledger_rows("SELECT * FROM occurrences ORDER BY id")
            status, counts = self.invoke("--apply")
            self.assertEqual((status, counts["adopted"], counts["already_adopted"]), (0, 0, 3))
            self.assertEqual(self.ledger_rows("SELECT * FROM occurrences ORDER BY id"), occurrences)
            self.assertEqual(self.ledger_rows("SELECT count(*) FROM original_resolutions"), [(3,)])
        receipts = sorted((self.base / "legacy-adoption").glob("*.json"))
        self.assertEqual(len(receipts), 2)
        for path in receipts:
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            receipt = json.loads(path.read_bytes())
            self.assertEqual(receipt["schema"], "legacy-adoption-receipt-v1")
            self.assertNotIn("source_path", path.read_text())
        self.assertEqual(self.ledger_rows("SELECT status FROM runs WHERE run_id LIKE 'legacy-adoption-%'"),
                         [("completed_with_gaps",)] * 2)

    def test_hash_mismatch_counts_and_whole_apply_refusal(self):
        source = Path(self.rows[1]["source_path"])
        source.chmod(0o600)
        source.write_bytes(b"x" * self.rows[1]["size"])
        source.chmod(0o400)
        before = self.snapshot()
        status, counts = self.invoke()
        self.assertEqual((status, counts["hash_mismatch"]), (3, 1))
        from types import SimpleNamespace
        with patch.object(capture.os, "fstatvfs", return_value=SimpleNamespace(f_flag=os.ST_RDONLY)):
            self.assertEqual(self.invoke("--apply")[0], 2)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.ledger_rows("SELECT count(*) FROM runs WHERE run_id LIKE 'legacy-adoption-%'"), [(0,)])

    def test_forbidden_paths_refuse_before_source_open(self):
        for path, code in ((self.export / ".hermes" / "unread.pdf", "legacy_source_path_forbidden"),
                           (self.sources / "outside.pdf", "legacy_source_root_not_admitted"),
                           (self.export / "agent-work" / "flock-mvp" / "unread.pdf", "legacy_source_path_forbidden"),
                           (self.export / "unknown" / "unread.pdf", "legacy_source_class_unknown")):
            with self.subTest(code=code):
                self.rows[0]["source_path"] = str(path)
                self.save_inputs()
                real_open = os.open
                def guarded_open(name, *args, **kwargs):
                    if str(name) in (str(path), path.name):
                        self.fail("forbidden source opened")
                    return real_open(name, *args, **kwargs)
                with patch("os.open", side_effect=guarded_open), patch("builtins.open", side_effect=AssertionError("unexpected open")):
                    status, counts = self.invoke()
                self.assertEqual((status, counts), (2, {code: 1}))

    def test_owner_mode_and_hash_pins_refuse_without_writes(self):
        for target, code in ((self.config, "legacy_input_mode"),
                             (self.manifest, "legacy_manifest_hash_mismatch"),
                             (self.policy, "legacy_policy_hash_mismatch")):
            with self.subTest(target=target.name):
                self.save_inputs()
                if target == self.config:
                    target.chmod(0o644)
                else:
                    target.write_bytes(target.read_bytes() + b" ")
                before = self.snapshot()
                self.assertEqual(self.invoke(), (2, {code: 1}))
                self.assertEqual(self.invoke("--apply"), (2, {code: 1}))
                self.assertEqual(self.snapshot(), before)

    def test_disabled_apply_and_policy_gap_fail_closed(self):
        self.save_inputs(enabled=False)
        self.assertEqual(self.invoke()[0], 0)
        self.assertEqual(self.invoke("--apply"), (2, {"legacy_adoption_disabled": 1}))
        self.policy_value["originals"].pop(self.rows[2]["sha256"])
        self.save_inputs()
        before = self.snapshot()
        from types import SimpleNamespace
        with patch.object(capture.os, "fstatvfs", return_value=SimpleNamespace(f_flag=os.ST_RDONLY)):
            self.assertEqual(self.invoke("--apply"), (2, {"capture_not_in_approved_inventory": 1}))
        self.assertEqual(self.snapshot(), before)

    def test_ranked_source_receipt_and_conflicting_sizes(self):
        lower = dict(self.rows[0], source_path=str(self.supplement / "copy.txt"))
        self.rows.append(lower)
        self.save_inputs()
        from types import SimpleNamespace
        with patch.object(capture.os, "fstatvfs", return_value=SimpleNamespace(f_flag=os.ST_RDONLY)):
            self.assertEqual(self.invoke("--apply")[0], 0)
        receipt = json.loads(next((self.base / "legacy-adoption").glob("*.json")).read_bytes())
        chosen = next(row for row in receipt["rows"] if row["original_sha256"] == lower["sha256"])
        self.assertEqual(chosen["source_class"], "received")
        lower["size"] += 1
        self.save_inputs()
        before = self.snapshot()
        self.assertEqual(self.invoke("--apply"), (2, {"legacy_manifest_conflict": 1}))
        self.assertEqual(self.snapshot(), before)

    def test_policy_proposal_is_private_and_uses_canonical_row_evidence(self):
        proposal = self.base / "proposal.json"
        before = self.snapshot()
        self.assertEqual(self.invoke("--write-policy-proposal", str(proposal))[0], 0)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(proposal.stat().st_mode & 0o777, 0o600)
        value = json.loads(proposal.read_bytes())
        self.assertEqual(value["inventory_sha256"], capture.digest(self.manifest.read_bytes()))
        self.assertEqual(value["approval_sha256"], capture.digest(self.config.read_bytes()))
        self.assertEqual(value["originals"], self.policy_value["originals"])
        with self.assertRaises(FileExistsError):
            # Never truncate another proposal or follow a link.
            adoption.write_proposal(proposal, value)

    def test_replay_policy_gap_is_refused_before_run_writes(self):
        from types import SimpleNamespace
        with patch.object(capture.os, "fstatvfs", return_value=SimpleNamespace(f_flag=os.ST_RDONLY)):
            self.assertEqual(self.invoke("--apply")[0], 0)
            self.policy_value["originals"].pop(self.rows[2]["sha256"])
            self.save_inputs()
            before = self.snapshot()
            self.assertEqual(self.invoke("--apply")[0], 2)
            self.assertEqual(self.snapshot(), before)

    def test_interrupted_enrollment_resumes_pending_preservation(self):
        from types import SimpleNamespace
        backend = adoption.CanonicalOriginalReconciler
        with patch.object(capture.os, "fstatvfs", return_value=SimpleNamespace(f_flag=os.ST_RDONLY)):
            with patch.object(backend, "_accept_locked", side_effect=ValueError("synthetic_failure")):
                self.assertEqual(self.invoke("--apply")[0], 2)
            status, counts = self.invoke("--apply")
            self.assertEqual((status, counts["adopted"]), (0, 3))
        self.assertEqual(self.ledger_rows("SELECT count(*) FROM stage_state WHERE stage='preserve' AND status='done'"), [(3,)])
        self.assertEqual(self.ledger_rows("SELECT count(*) FROM original_resolutions"), [(3,)])

    def test_internal_callbacks_require_the_shared_lock(self):
        backend = adoption.CanonicalOriginalReconciler(self.base / "mail", self.base / "intake",
            self.helper.database, policy=capture.ApprovedCapturePolicy(self.policy.read_bytes(),
                expected_sha256=capture.digest(self.policy.read_bytes())))
        for callback in (lambda: backend._accept_locked(self.rows[0]["sha256"]),
                         lambda: backend._enroll_local_capture_locked(self.rows[0]["source_path"],
                             original_sha256=self.rows[0]["sha256"])):
            with self.assertRaisesRegex(RuntimeError, "context_root_lock_required"):
                callback()

    def test_strict_manifest_and_source_safety(self):
        from types import SimpleNamespace
        for mutate in (lambda: self.rows.append(dict(self.rows[0])),
                       lambda: self.rows[0].update(sha256="F" * 64),
                       lambda: self.rows[0].update(size=-1),
                       lambda: self.rows[0].update(source_path="relative.txt")):
            saved = [dict(row) for row in self.rows]
            mutate()
            self.save_inputs()
            before = self.snapshot()
            with patch.object(adoption, "verify_source", side_effect=AssertionError("source opened")):
                self.assertEqual(self.invoke("--apply")[0], 2)
            self.assertEqual(self.snapshot(), before)
            self.rows = saved
        self.save_inputs()
        source = Path(self.rows[1]["source_path"])
        source.unlink()
        source.symlink_to(self.rows[0]["source_path"])
        with patch.object(capture.os, "fstatvfs", return_value=SimpleNamespace(f_flag=os.ST_RDONLY)):
            before = self.snapshot()
            self.assertEqual(self.invoke("--apply")[0], 2)
            self.assertEqual(self.snapshot(), before)

    def test_stdout_only_fixed_count_keys_and_numbers(self):
        _, counts = self.invoke()
        def assert_counts(value):
            self.assertIsInstance(value, dict)
            for key, count in value.items():
                self.assertRegex(key, r"^[a-z][a-z_-]+$")
                if isinstance(count, dict):
                    assert_counts(count)
                else:
                    self.assertIs(type(count), int)
        assert_counts(counts)
        with patch.object(adoption, "preflight", side_effect=OSError("synthetic private filename and content")):
            status, counts = self.invoke()
        self.assertEqual(status, 2)
        self.assertEqual(counts, {"legacy_adoption_refused": 1})
        assert_counts(counts)

    def test_apply_preflight_checks_later_sources_before_any_capture(self):
        from types import SimpleNamespace
        source = Path(self.rows[2]["source_path"])
        source.chmod(0o644)
        before = self.snapshot()
        with patch.object(capture.os, "fstatvfs", return_value=SimpleNamespace(f_flag=os.ST_RDONLY)):
            self.assertEqual(self.invoke("--apply"), (2, {"legacy_source_unsafe": 1}))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(list((self.pipeline.root.intake / "blobs").iterdir()), [])

    def test_manifest_row_bound_and_bad_header(self):
        config = json.loads(self.config.read_bytes())
        header = b"sha256\tsize\tsource_path\n"
        body = b"".join(f'{index:064x}\t0\t{self.export}/received/{index}\n'.encode()
                        for index in range(10001))
        with self.assertRaisesRegex(ValueError, "legacy_manifest_invalid"):
            adoption.parse_manifest(header + body, config)
        with self.assertRaisesRegex(ValueError, "legacy_manifest_invalid"):
            adoption.parse_manifest(b"sha256 size source_path\n", config)

    def test_dry_run_counts_and_readonly_ledger(self):
        before = self.snapshot()
        status, counts = self.invoke()
        self.assertEqual(status, 0)
        self.assertEqual(counts, {"eligible": 3, "excluded_metadata_class": 2,
            "format_unknown": 1, "already_bound": 0, "already_adopted": 0, "adopted": 0,
            "hash_mismatch": 0, "source_classes": {"received": 6, "received-user-files": 0,
                "portal-intake": 0, "productions": 0, "supplement": 0}})
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.base / "legacy-adoption").exists())

    def test_null_policy_first_proposal_passes_existing_policy_loader(self):
        self.save_inputs(enabled=False, null_policy=True)
        proposal = self.base / "first.json"
        before = self.snapshot()
        self.assertEqual(self.invoke("--write-policy-proposal", str(proposal), policy=False)[0], 0)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.ledger_rows("SELECT count(*) FROM runs WHERE run_id LIKE 'legacy-adoption-%'"), [(0,)])
        self.assertFalse((self.base / "legacy-adoption").exists())
        raw = proposal.read_bytes()
        self.assertEqual(proposal.stat().st_mode & 0o777, 0o600)
        loaded = capture.ApprovedCapturePolicy(raw, expected_sha256=capture.digest(raw))
        self.assertIsNotNone(loaded)
        value = json.loads(raw)
        self.assertEqual(value["originals"], self.policy_value["originals"])
        self.assertEqual(value["approval_sha256"], capture.digest(self.config.read_bytes()))
        self.assertEqual(value["inventory_sha256"], capture.digest(self.manifest.read_bytes()))

    def test_null_policy_refuses_every_other_mode_naming_the_code(self):
        refused = (2, {"legacy_policy_required": 1})
        for enabled in (False, True):
            self.save_inputs(enabled=enabled, null_policy=True)
            proposal = self.base / f"never-{enabled}.json"
            before = self.snapshot()
            with self.subTest(enabled=enabled):
                with patch.object(adoption, "preflight", side_effect=AssertionError("preflight reached")), \
                        patch.object(adoption, "apply_batch", side_effect=AssertionError("apply reached")):
                    self.assertEqual(self.invoke(policy=False), refused)
                    self.assertEqual(self.invoke("--apply", policy=False), refused)
                    self.assertEqual(self.invoke("--apply", "--write-policy-proposal", str(proposal), policy=False), refused)
                    self.assertEqual(self.invoke("--write-policy-proposal", str(proposal)), refused)
                self.assertFalse(proposal.exists())
                self.assertEqual(self.snapshot(), before)

    def test_pinned_config_still_requires_policy_and_matches_proposal_bytes(self):
        self.assertEqual(self.invoke(policy=False), (2, {"legacy_policy_required": 1}))
        proposal = self.base / "pinned.json"
        self.assertEqual(self.invoke("--write-policy-proposal", str(proposal))[0], 0)
        expected = adoption.proposal(json.loads(self.config.read_bytes()),
                                     capture.digest(self.config.read_bytes()),
                                     [(row, "received") for row in self.rows[:3]])
        self.assertEqual(proposal.read_bytes(), capture.encoded(expected) + b"\n")

    def test_null_policy_bootstrap_keeps_preflight_and_pin_refusals(self):
        self.save_inputs(null_policy=True)
        config = json.loads(self.config.read_bytes())
        proposal = self.base / "bad.json"
        for bad in ("", False, {}, "A" * 64, "z" * 64, "a" * 63):
            with self.subTest(pin=bad):
                config["policy_sha256"] = bad
                self.config.write_bytes(capture.encoded(config))
                self.assertEqual(self.invoke("--write-policy-proposal", str(proposal), policy=False)[0], 2)
        del config["policy_sha256"]
        self.config.write_bytes(capture.encoded(config))
        self.assertEqual(self.invoke("--write-policy-proposal", str(proposal), policy=False),
                         (2, {"legacy_config_invalid": 1}))
        self.assertFalse(proposal.exists())
        self.save_inputs(null_policy=True)
        Path(self.rows[1]["source_path"]).chmod(0o600)
        Path(self.rows[1]["source_path"]).write_bytes(b"x" * self.rows[1]["size"])
        Path(self.rows[1]["source_path"]).chmod(0o400)
        self.assertEqual(self.invoke("--write-policy-proposal", str(proposal), policy=False)[0], 3)
        self.assertFalse(proposal.exists())

    def test_guide_bootstrap_contract(self):
        guide = (Path(__file__).resolve().parents[2] / "docs" / "RECORDS-LEGACY-ADOPTION.md").read_text()
        for text in ("legacy_policy_required", "\"policy_sha256\": null", "G approves", "pin", "enable"):
            self.assertIn(text, guide)
        self.assertLess(guide.index("Bootstrap order"), guide.index("Attended T8 order"))




    RO = staticmethod(lambda: patch.object(adoption.os, "fstatvfs", return_value=type("V", (), {"f_flag": os.ST_RDONLY})()))

    def make(self, name, data=b"report data", mode=0o400):
        path = self.export / "received" / name
        path.parent.mkdir(mode=0o700, exist_ok=True)
        path.write_bytes(data)
        path.chmod(mode)
        return {"sha256": capture.digest(data), "size": len(data), "source_path": str(path)}

    def report(self, rows, *extra, policy=False, root=None, null_policy=True):
        self.rows = rows
        self.save_inputs(null_policy=null_policy)
        out, err = io.StringIO(), io.StringIO()
        args = ["--root", str(root or self.base), "--config", str(self.config), "--manifest", str(self.manifest),
                "--report-sources", *(["--policy", str(self.policy)] if policy else []), *extra]
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = adoption.main(args)
        self.assertEqual(err.getvalue(), "")
        for secret in (str(self.sources), "fictional", "report data", "received"):
            self.assertNotIn(secret, out.getvalue())
        lines = [json.loads(line) for line in out.getvalue().splitlines()]
        return status, lines

    def codes(self, rows):
        status, lines = self.report(rows)
        return status, [line["code"] for line in lines[:-1]], lines[-1]

    def tree(self):
        result = {}
        for path in sorted(self.sources.rglob("*")) + sorted(self.base.rglob("*")):
            info = path.lstat()
            result[str(path)] = (info.st_mode, os.readlink(path) if path.is_symlink() else
                                 (path.read_bytes() if path.is_file() else None))
        return result

    def test_report_each_source_rule_first_failure(self):
        good = self.make("ok.bin")
        hard = self.make("multi.bin")
        os.link(hard["source_path"], str(self.export / "received" / "multi-alias"))
        real = self.export / "received" / "realdir"
        real.mkdir(mode=0o700)
        inside = real / "inner.bin"
        inside.write_bytes(b"inner")
        inside.chmod(0o400)
        (self.export / "received" / "linkdir").symlink_to(real)
        (self.export / "received" / "leaflink").symlink_to(inside)
        fifo = self.export / "received" / "pipe"
        os.mkfifo(fifo, 0o600)
        (self.export / "received" / "afile").write_bytes(b"x")
        wrong = self.make("wrong.bin", b"abcdef")
        rows = [
            ("ok", good), ("source_missing", dict(good, source_path=str(self.export / "received" / "gone"))),
            ("source_missing", dict(good, source_path=str(self.export / "received" / "nodir" / "f"))),
            ("source_not_regular", dict(good, source_path=str(real))),
            ("source_not_regular", dict(good, source_path=str(fifo))),
            ("source_not_regular", dict(good, source_path=str(self.export / "received" / "afile" / "f"))),
            ("source_symlink_component", dict(good, source_path=str(self.export / "received" / "linkdir" / "inner.bin"))),
            ("source_symlink_component", dict(good, source_path=str(self.export / "received" / "leaflink"))),
            ("source_multi_link", hard),
            ("source_mode_bits", self.make("m640.bin", mode=0o640)),
            ("source_mode_bits", self.make("m604.bin", b"other", mode=0o604)),
            ("source_size_mismatch", dict(good, size=good["size"] + 1)),
            ("source_size_mismatch", dict(good, size=good["size"] - 1)),
            ("source_sha256_mismatch", dict(wrong, sha256="c" * 64)),
        ]
        with self.RO():
            for code, row in rows:
                self.assertEqual(self.codes([row])[2], {"totals": {code: 1}}, code)
                self.assertEqual(self.codes([row])[1], [code])
            with patch.object(adoption.os, "getuid", return_value=os.getuid() + 1):
                self.assertEqual(adoption.diagnose_source(good), "source_not_owner")
        self.assertEqual(self.codes([good])[1], ["source_not_readonly_fs"])

    def test_report_lists_every_physical_row_and_exact_totals(self):
        a, b = self.make("a.bin", b"aaa"), self.make("b.bin", b"bbb")
        dup = dict(b, source_path=str(self.export / "received" / "b-copy.bin"))
        Path(dup["source_path"]).write_bytes(b"bbb")
        Path(dup["source_path"]).chmod(0o400)
        missing = dict(a, source_path=str(self.export / "received" / "gone"))
        rows = [missing, b, a, dup]
        with self.RO():
            status, lines = self.report(rows)
        self.assertEqual(status, 1)
        self.assertEqual([line["row"] for line in lines[:-1]], [1, 2, 3, 4])
        self.assertEqual([line["code"] for line in lines[:-1]], ["source_missing", "ok", "ok", "ok"])
        for line, row in zip(lines, rows):
            self.assertEqual(set(line), {"row", "sha256_prefix", "code"})
            self.assertEqual(line["sha256_prefix"], row["sha256"][:12])
        self.assertEqual(lines[-1], {"totals": {"ok": 3, "source_missing": 1}})
        config = json.loads(self.config.read_bytes())
        self.assertEqual(len(adoption.parse_manifest(self.manifest.read_bytes(), config)), 2)
        with self.RO():
            self.assertEqual(self.report([b, a])[0], 0)

    def test_report_precedence_and_source_races(self):
        both = self.make("both.bin", mode=0o644)
        os.link(both["source_path"], str(self.export / "received" / "both-alias"))
        rw = dict(self.make("rw.bin"), sha256="d" * 64)
        with self.RO():
            self.assertEqual(self.codes([both])[1], ["source_multi_link"])
        self.assertEqual(self.codes([rw])[1], ["source_not_readonly_fs"])
        good = self.make("race.bin")
        fifo = self.export / "received" / "fifo"
        os.mkfifo(fifo, 0o600)
        opened, closed, reads = [], [], []
        real_open, real_close, real_read = os.open, os.close, os.read
        def tracking_open(*a, **k):
            fd = real_open(*a, **k)
            opened.append((fd, k.get("dir_fd") is not None, a[1] & os.O_NOFOLLOW))
            return fd
        def tracking_close(fd):
            closed.append(fd)
            return real_close(fd)
        def tracking_read(fd, n):
            reads.append(n)
            return real_read(fd, n)
        with self.RO(), patch.object(adoption.os, "open", tracking_open), patch.object(adoption.os, "close", tracking_close), \
                patch.object(adoption.os, "read", tracking_read):
            self.assertEqual([adoption.diagnose_source(row) for row in (dict(good, source_path=str(fifo)), good)],
                             ["source_not_regular", "ok"])
        self.assertEqual(sorted(fd for fd, *_ in opened), sorted(closed))
        self.assertTrue(all(nofollow for *_, nofollow in opened) and reads and max(reads) <= 1024 * 1024)
        self.assertEqual(sum(1 for _fd, relative, _n in opened if not relative), 2)  # only the two anchors
        values = iter(range(100))
        with self.RO(), patch.object(adoption.capture, "fingerprint", side_effect=lambda _v: [next(values)]):
            self.assertEqual(adoption.diagnose_source(good), "source_sha256_mismatch")

    def test_report_malformed_manifest_refuses_before_any_source(self):
        good = self.make("ok.bin")
        line = lambda *v: "\t".join(map(str, v)) + "\n"
        header = "sha256\tsize\tsource_path\n"
        base = [good["sha256"], good["size"], good["source_path"]]
        cases = {
            "header": ("bad\n" + line(*base), "legacy_manifest_invalid"),
            "hash": (header + line("X" * 64, 1, good["source_path"]), "legacy_adoption_refused"),
            "size": (header + line(good["sha256"], "-1", good["source_path"]), "legacy_manifest_invalid"),
            "control": (header + line(good["sha256"], 1, good["source_path"] + "\x01"), "legacy_manifest_invalid"),
            "class": (header + line(good["sha256"], 1, str(self.export / "nope" / "f")), "legacy_source_class_unknown"),
            "root": (header + line(good["sha256"], 1, "/etc/fictional"), "legacy_source_root_not_admitted"),
            "dup": (header + line(*base) * 2, "legacy_manifest_duplicate"),
            "conflict": (header + line(*base) + line(good["sha256"], 99, good["source_path"] + "2"), "legacy_manifest_conflict"),
            "many": (header + "".join(
                line(good["sha256"], good["size"], good["source_path"] + str(i)) for i in range(10001)), "legacy_manifest_invalid"),
        }
        for name, (text, code) in cases.items():
            self.manifest.write_text(text)
            self.manifest.chmod(0o600)
            self.rows = []
            self.save_inputs(null_policy=True)
            self.manifest.write_text(text)
            self.manifest.chmod(0o600)
            value = json.loads(self.config.read_bytes())
            value["manifest_sha256"] = capture.digest(text.encode())
            self.config.chmod(0o600)
            self.config.write_bytes(capture.encoded(value))
            self.config.chmod(0o600)
            out = io.StringIO()
            with patch.object(adoption, "diagnose_source", side_effect=AssertionError), contextlib.redirect_stdout(out):
                status = adoption.main(["--root", str(self.base), "--config", str(self.config),
                                        "--manifest", str(self.manifest), "--report-sources"])
            self.assertEqual((status, json.loads(out.getvalue())), (2, {code: 1}), name)
        self.rows = [good]
        self.save_inputs(null_policy=True)
        self.manifest.write_text(self.manifest.read_text() + "\n")
        self.manifest.chmod(0o600)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status = adoption.main(["--root", str(self.base), "--config", str(self.config),
                                    "--manifest", str(self.manifest), "--report-sources"])
        self.assertEqual((status, json.loads(out.getvalue())), (2, {"legacy_manifest_hash_mismatch": 1}))

    def test_report_null_policy_and_mixed_mode_matrix(self):
        good = self.make("ok.bin")
        with self.RO():
            self.assertEqual(self.report([good])[0], 0)
            self.assertEqual(self.report([good], null_policy=False, policy=True)[0], 0)
        self.rows = [good]
        def run(*extra, policy=False, null_policy=True):
            self.save_inputs(null_policy=null_policy)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                status = adoption.main(["--root", str(self.base), "--config", str(self.config), "--manifest",
                    str(self.manifest), *(["--policy", str(self.policy)] if policy else []), *extra])
            return status, json.loads(out.getvalue().splitlines()[-1])
        self.assertEqual(run(), (2, {"legacy_policy_required": 1}))
        self.assertEqual(run("--apply"), (2, {"legacy_policy_required": 1}))
        self.assertEqual(run("--report-sources", policy=True), (2, {"legacy_policy_required": 1}))
        self.assertEqual(run("--report-sources", "--apply"), (2, {"legacy_config_invalid": 1}))
        proposal = self.base / "proposal.json"
        self.assertEqual(run("--report-sources", "--write-policy-proposal", str(proposal)), (2, {"legacy_config_invalid": 1}))
        self.assertEqual(run("--report-sources", null_policy=False), (2, {"legacy_policy_required": 1}))
        self.assertFalse(proposal.exists())

    def test_report_privacy_no_writes_and_repeatability(self):
        rows = [self.make("a.bin", b"aaa"), dict(self.make("b.bin", b"bbb"), size=1)]
        elsewhere = self.sources / "no-such-root"
        self.rows = rows
        self.save_inputs(null_policy=True)
        before = self.tree()
        ban = patch.multiple(adoption, root_lock=unittest.mock.DEFAULT, preflight=unittest.mock.DEFAULT,
                             apply_batch=unittest.mock.DEFAULT, write_proposal=unittest.mock.DEFAULT)
        with self.RO(), ban as banned, patch.object(store, "ledger", side_effect=AssertionError), \
                patch.object(store, "checked_path", side_effect=AssertionError):
            first = self.report(rows, root=elsewhere)
            second = self.report(rows, root=elsewhere)
        self.assertEqual(first, second)
        self.assertEqual(first[0], 1)
        for mock in banned.values():
            mock.assert_not_called()
        self.assertFalse(elsewhere.exists())
        self.assertEqual(self.tree(), before)

    def test_report_empty_and_unavailable_sources_continue(self):
        with self.RO():
            self.assertEqual(self.report([]), (0, [{"totals": {}}]))
        good = self.make("ok.bin")
        gone = dict(good, source_path=str(self.export / "received" / "gone"))
        with self.RO():
            status, lines = self.report([gone, dict(gone, source_path=gone["source_path"] + "2")])
        self.assertEqual((status, lines[-1]), (1, {"totals": {"source_missing": 2}}))
        for name in ("fstatvfs", "read"):
            with self.RO(), patch.object(adoption.os, name, side_effect=OSError("canary-path")):
                self.assertEqual(adoption.diagnose_source(good), "source_missing", name)
        later = self.make("later.bin", b"later")
        with patch.object(adoption.os, "fstatvfs", side_effect=OSError("canary-path")):
            status, lines = self.report([good, later])
        self.assertEqual((status, lines[-1]), (1, {"totals": {"source_missing": 2}}))

    def test_old_modes_and_parent_proposal_are_unchanged(self):
        self.save_inputs()
        status, counts = self.invoke()
        self.assertEqual((status, counts["eligible"], counts["hash_mismatch"]), (0, 3, 0))
        self.assertEqual(self.invoke("--apply")[1], {"local_capture_requires_readonly_source": 1})
        Path(self.rows[0]["source_path"]).chmod(0o640)
        self.assertEqual(self.invoke(), (2, {"legacy_source_unsafe": 1}))
        Path(self.rows[0]["source_path"]).chmod(0o400)
        proposal = self.base / "p.json"
        self.save_inputs(null_policy=True)
        self.assertEqual(self.invoke("--write-policy-proposal", str(proposal), policy=False)[0], 0)
        self.assertTrue(proposal.exists())

    def test_guide_report_sources_contract(self):
        guide = (Path(__file__).resolve().parents[2] / "docs" / "RECORDS-LEGACY-ADOPTION.md").read_text()
        for code in ("--report-sources", "source_missing", "source_not_regular", "source_symlink_component",
                     "source_not_owner", "source_multi_link", "source_mode_bits", "source_not_readonly_fs",
                     "source_size_mismatch", "source_sha256_mismatch", "sha256_prefix", "current observation"):
            self.assertIn(code, guide)
