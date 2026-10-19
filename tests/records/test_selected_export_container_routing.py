"""Selected synthetic bytes and captured routing; no DB, Docker or native work."""
from contextlib import contextmanager, redirect_stdout, redirect_stderr
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from campaign_tool.records import container_host as host, export_selection, run, unattended
from campaign_tool.records.intake import export_snapshot as snap, folder, mail_delta
from campaign_tool.records.portal.collections import Scope


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


class CapturedSafety:
    """Call-sequencing oracle; deliberately not a persistence implementation."""
    def __init__(self, limit=60):
        self.limit, self.calls = limit, []
        self.state = {"hold": None, "active": None}
        self.phases = {key: {"attempted": 0, "failed": 0, "held": 0, "review_required": 0}
                       for key in ("intake", "advance")}

    @property
    def stopped(self):
        return self.state["hold"] is not None

    def check(self):
        return not self.stopped

    def begin(self, phase, identity):
        assert self.state["active"] is None
        if self.stopped or self.phases[phase]["attempted"] >= self.limit:
            return False
        self.calls.append(("begin", phase, identity))
        self.state["active"] = identity
        self.phases[phase]["attempted"] += 1
        return True

    def finish(self, outcome="success", code=None, originals=()):
        assert self.state["active"] is not None
        self.calls.append(("finish", outcome, code, tuple(originals)))
        self.state["active"] = None
        if outcome != "success":
            self.state["hold"] = code
            self.phases["intake"]["held" if outcome == "held" else "failed"] += 1

    def stop(self, code):
        if not self.stopped:
            self.state["hold"] = code

    def report(self):
        return {"hold": self.state["hold"], "phases": self.phases}


class SelectedExportContainerTests(unittest.TestCase):
    def setUp(self):
        parent = os.environ.get("TMPDIR", str(Path.home()))
        if parent in ("/tmp", "/var/tmp", "/private/tmp"):
            parent = str(Path.home())
        temporary = tempfile.TemporaryDirectory(prefix="selected-export-", dir=parent)
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.exports, self.canonical = self.base / "exports", self.base / "canonical"
        for path in (self.exports, self.exports / "receipts", self.exports / "objects", self.canonical,
                     self.canonical / "intake", self.canonical / "runs"):
            path.mkdir(mode=0o700)
        self.scope = Scope(frozenset({"archive.example.invalid"}), frozenset({"social.example.invalid"}))
        self.eml = b"From: fictional@example.invalid\nSubject: synthetic\n\nNo private record.\n"
        self.blob = b"opaque synthetic attachment bytes"
        self.write(self.exports / "objects/message", self.eml)
        self.write(self.exports / "objects/shared", self.blob)
        self.receipts = []
        for index in range(2):
            name = "receipts/" + str(index) + ".json"
            value = {"complete": True, "receipt_id": "f" * 64, "account_id": "synthetic-account",
                     "folder": "Synthetic", "uidvalidity": 123456, "uid": 7001 + index,
                     "bytes": len(self.eml),
                     "original_eml": {"path": "objects/message", "bytes": len(self.eml), "sha256": sha(self.eml)},
                     "attachments": [{"part": part, "path": "objects/shared", "bytes": len(self.blob),
                                      "sha256": sha(self.blob)} for part in (2, 4)]}
            self.write(self.exports / name, host.encoded(value))
            self.receipts.append(name)
        self.ro = self.enterContext(patch.object(snap.os, "fstatvfs", return_value=SimpleNamespace(f_flag=os.ST_RDONLY)))
        self.enterContext(patch("campaign_tool.records.intake.imap_intake.load_config",
                                side_effect=AssertionError("credential access forbidden")))

    def write(self, path, raw):
        path.write_bytes(raw)
        path.chmod(0o600)

    def change_receipt(self, change, index=0):
        path = self.exports / self.receipts[index]
        value = json.loads(path.read_bytes())
        change(value)
        self.write(path, host.encoded(value))

    def snapshot(self, **options):
        values = dict(receipts=self.receipts, scope=self.scope, logical_root=self.exports,
                      output=self.canonical / "intake")
        values.update(options)
        return snap.snapshot(self.exports, **values)

    def paths(self, **changes):
        values = dict(input_mode="exports", root=str(self.canonical), export_root=str(self.exports),
                      export_receipts=list(self.receipts), export_manifest_sha256="a" * 64,
                      collection_hosts=list(self.scope.hosts), collection_excluded_hosts=list(self.scope.excluded_hosts))
        values.update(changes)
        return values

    def selected(self):
        return export_selection.ExportSelection(self.exports, tuple(self.receipts), self.scope, self.canonical)

    def pipeline(self, limit=60):
        p = unattended.UnattendedPipeline.__new__(unattended.UnattendedPipeline)
        p.root = SimpleNamespace(path=self.canonical, intake=self.canonical / "intake",
                                 ledger=self.canonical / "ledger.sqlite", sub=lambda name: self.canonical / name)
        p.run_id, p.config_sha256 = "synthetic-run", "c" * 64
        p.policy, p.clock = unattended.RunSafetyPolicy(), None
        p.model = p.challenge = None
        p.review_retirement = {"state": "complete"}
        p._start_review_retirement = Mock()
        p._arm_review_retirement = Mock()
        p._open_stage_run = Mock()
        p._identity = Mock(return_value={"runtime": {}})
        p._query = Mock(return_value=[])
        p._unsupported = Mock(return_value=())
        p.advance_all = Mock(return_value=[])
        self.safety = p.safety = CapturedSafety(limit)
        self.queue = SimpleNamespace(root=self.canonical / "portal", close=Mock())
        self.inventory = SimpleNamespace(queue=self.queue, scope=self.scope, readiness=Mock(return_value={
            "event": "synthetic-event", "retrieval_complete": True, "gaps": [],
            "digestion_complete": False, "publication_authorized": False}))
        self.backend = Mock()
        self.enterContext(patch.object(unattended, "CanonicalMailBackend", return_value=self.backend))
        self.enroll = self.enterContext(patch("campaign_tool.records.export_bridge.enroll_export",
            return_value=SimpleNamespace(message_id="synthetic-event", eml_sha256=sha(self.eml), documents=[sha(self.blob)])))
        self.queue_factory = self.enterContext(patch.object(export_selection, "Queue", return_value=self.queue))
        self.inventory_factory = self.enterContext(patch.object(export_selection, "CollectionInventory", return_value=self.inventory))
        self.enterContext(patch.object(unattended, "RunSafety", return_value=self.safety))
        self.enterContext(patch.object(run.stages, "counts", return_value={"stages": {}, "originals": 0,
                                                                           "candidate_seven_stage_complete": False}))
        self.enterContext(patch.object(unattended, "_private_bytes"))
        @contextmanager
        def ledger(*args, **kwargs):
            yield Mock()
        self.enterContext(patch.object(unattended.store, "ledger", side_effect=ledger))
        return p

    def run_pipeline(self, p):
        return p.run(exports=self.selected(), export_manifest_sha256=self.snapshot()["sha256"])

    def profile(self, change=lambda value: None):
        value = self.paths()
        value.update(schema=1, name_prefix="synthetic", image_id="sha256:" + "a" * 64,
            release_commit="b" * 40, package_version="0.0.synthetic", dependency_lock_sha256="c" * 64,
            docker_path=str(self.base / "fake-docker"), python="/synthetic/python", release_root="/synthetic/release",
            parser_path="/synthetic/parser", tmp_dir="/synthetic/tmp", worker_state_parent="/synthetic/lifecycle",
            board_dir="/synthetic/board", host_receipts=str(self.base / "host"), wall_seconds=300,
            owner_launch_approved=True, seccomp_path=str(self.base / "seccomp.json"), seccomp_sha256="d" * 64,
            memory_bytes=2 * 1024**3, pids_limit=64, cpus=1, network={"mode": "none", "container_id": None})
        keys = ("release_root", "parser_path", "tmp_dir", "worker_state_parent", "board_dir", "root", "export_root")
        value["mounts"] = [{"kind": "volume", "source": "synthetic-" + key, "target": value[key],
                            "readonly": key in {"release_root", "parser_path", "export_root"}} for key in keys]
        change(value)
        path = self.base / "profile.json"
        self.write(path, host.encoded(value))
        return path

    def test_secure_fixture_paths_and_selected_only_reads(self):
        # First selected gate case exercises actual no-link opens, not a path stub.
        with folder.secure_open(self.exports / "objects/message") as stream:
            self.assertEqual(stream.read(), self.eml)
        self.write(self.exports / "unselected", b"not selected")
        with patch.object(snap.os, "listdir", side_effect=AssertionError("no input scan")):
            result = self.snapshot()
        self.assertEqual(result["messages"], 2)
        self.assertNotIn("unselected", [item["path"] for item in result["files"]])

    def test_incomplete_exclusive_and_count_selections_reject_early(self):
        for changes in ({"export_receipts": []}, {"export_receipts": [str(i) for i in range(61)]},
                        {"export_receipts": [self.receipts[0], str(self.exports / self.receipts[0])]},
                        {"mail_config": "/synthetic/config"}):
            with self.subTest(changes=changes), self.assertRaises((host.Rejected, ValueError)):
                host.input_selection(self.paths(**changes))
        for count in (1, 60):
            values = self.paths(export_receipts=["receipts/" + str(i) for i in range(count)])
            self.assertEqual(len(host.export_parameters(values)["receipts"]), count)

    def test_invalid_manifest_or_scope_rejects_before_runtime(self):
        runtime = self.enterContext(patch("campaign_tool.records.runtime.require"))
        constructor = self.enterContext(patch.object(unattended, "UnattendedPipeline"))
        for extra in ([], ["--export-manifest-sha256", "bad"],
                      ["--export-manifest-sha256", "a" * 64, "--collection-host", "https://invalid.example.invalid"],
                      ["--export-manifest-sha256", "a" * 64, "--mail-config", "/synthetic/config"]):
            with self.subTest(extra=extra), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                unattended.main(["--root", str(self.base / "absent"), "--export-root", str(self.exports),
                                 "--export-receipt", self.receipts[0], *extra])
        runtime.assert_not_called()
        constructor.assert_not_called()
        self.assertFalse((self.base / "absent").exists())
        runtime.return_value = True
        constructor.return_value.run.return_value = {"status": "completed_with_gaps", "exit_code": 3}
        with patch.dict(os.environ, {}, clear=True), redirect_stdout(io.StringIO()):
            code = unattended.main(["--root", str(self.canonical), "--export-root", str(self.exports),
                "--export-receipt", self.receipts[0], "--export-manifest-sha256", "a" * 64])
        self.assertEqual(code, 3)
        selected = constructor.return_value.run.call_args.kwargs["exports"]
        self.assertIs(type(selected), export_selection.ExportSelection)
        self.assertEqual(selected.receipts, (self.exports / self.receipts[0],))
        self.assertEqual(selected.scope.hosts, frozenset())

    def test_existing_mailbox_inbox_and_documents_routing_unchanged(self):
        self.assertEqual(host.input_arguments({"mail_config": "/synthetic/mail"}), ["--mail-config", "/synthetic/mail"])
        for mode, key, flag in (("inbox", "inbox", "inbox_manifest_sha256"),
                                ("documents", "documents", "document_manifest_sha256")):
            values = {"input_mode": mode, key: "/synthetic/input", flag: "a" * 64}
            self.assertEqual(host.input_arguments(values), ["--input-mode", mode, "--" + key,
                "/synthetic/input", "--" + flag.replace("_", "-"), "a" * 64])

    def test_snapshot_binds_receipt_bytes_and_original_identity(self):
        result = self.snapshot()
        first = result["receipts"][0]
        self.assertEqual(first["identity"], ["synthetic-account", "Synthetic", "123456", "7001"])
        self.assertEqual(first["sha256"], sha((self.exports / self.receipts[0]).read_bytes()))
        self.assertNotEqual(first["sha256"], "f" * 64)
        self.change_receipt(lambda value: value.update(receipt_id="e" * 64))
        with self.assertRaises(host.Rejected):
            self.snapshot(expected=result["sha256"])

    def test_snapshot_covers_all_parts_and_shared_object_once(self):
        result = self.snapshot()
        self.assertEqual([[p["part"] for p in entry["parts"]] for entry in result["receipts"]], [["0", "2", "4"]] * 2)
        self.assertEqual(len(result["files"]), 4)
        self.assertEqual(result["bytes"], len(self.eml) + len(self.blob) + sum(
            (self.exports / name).stat().st_size for name in self.receipts))

    def test_conflicting_source_declarations_and_aliases_reject(self):
        self.change_receipt(lambda value: value["attachments"][1].update(sha256="d" * 64))
        with self.assertRaises(host.Rejected):
            self.snapshot()
        with self.assertRaises(host.Rejected):
            self.snapshot(receipts=[self.receipts[0], "receipts/./0.json"])

    def test_escape_symlink_and_output_sources_reject(self):
        for path in ("../outside", str(self.base / "outside")):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.snapshot(receipts=[path])
        alias = self.exports / "alias"
        alias.symlink_to(self.exports / "objects")
        self.change_receipt(lambda value: value["original_eml"].update(path="alias/message"))
        with self.assertRaises((OSError, ValueError, host.Rejected)):
            self.snapshot()
        with self.assertRaises(ValueError):
            snap.selected_paths(self.base, [str(self.canonical / "intake/file")], self.canonical / "intake")

    def test_snapshot_owner_mode_hardlink_and_readonly_checks(self):
        source = self.exports / "objects/shared"
        source.chmod(0o644)
        with self.assertRaises(host.Rejected):
            self.snapshot()
        source.chmod(0o600)
        link = self.exports / "linked"
        os.link(source, link)
        with self.assertRaises(host.Rejected):
            self.snapshot()
        link.unlink()
        real_fstat = os.fstat
        inode = source.stat().st_ino
        def wrong_owner(fd):
            info = real_fstat(fd)
            if info.st_ino == inode:
                return SimpleNamespace(**{name: (os.getuid() + 1 if name == "st_uid" else getattr(info, name))
                    for name in ("st_uid", "st_mode", "st_nlink")})
            return info
        with patch.object(snap.os, "fstat", side_effect=wrong_owner), self.assertRaises(host.Rejected):
            self.snapshot()
        self.ro.return_value = SimpleNamespace(f_flag=0)
        with self.assertRaises(host.Rejected):
            self.snapshot(readonly=True)

    def test_blob_size_hash_and_missing_part_reject(self):
        source = self.exports / "objects/shared"
        for raw in (self.blob[:-1], b"x" * len(self.blob)):
            self.write(source, raw)
            with self.assertRaises(host.Rejected):
                self.snapshot()
        source.unlink()
        with self.assertRaises(FileNotFoundError):
            self.snapshot()

    def test_selected_input_total_boundary_includes_receipts(self):
        total = self.snapshot()["bytes"]
        with patch.object(snap, "MAX_TOTAL", total):
            self.assertEqual(self.snapshot()["bytes"], total)
        with patch.object(snap, "MAX_TOTAL", total - 1), self.assertRaises(host.Rejected):
            self.snapshot()
        with patch.object(mail_delta, "MAX_ATTACHMENT", len(self.blob) - 1), self.assertRaises(ValueError):
            self.snapshot()

    def test_snapshot_detects_midread_and_prior_fingerprint_changes(self):
        before = self.snapshot()
        source = self.exports / "objects/shared"
        info = source.stat()
        os.utime(source, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000))
        with self.assertRaises(host.Rejected):
            self.snapshot(prior=before)
        original = mail_delta.load_receipt
        def changed(path):
            result = original(path)
            info = path.stat()
            os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000))
            return result
        with patch.object(mail_delta, "load_receipt", side_effect=changed), self.assertRaises(host.Rejected):
            self.snapshot()

    def test_manifest_changes_with_selection_or_inventory_scope(self):
        before = self.snapshot()["sha256"]
        self.write(self.exports / "unselected", b"ignored")
        self.assertEqual(self.snapshot()["sha256"], before)
        self.assertNotEqual(self.snapshot(receipts=self.receipts[:1])["sha256"], before)
        self.assertNotEqual(self.snapshot(scope=Scope(frozenset()))["sha256"], before)

    def test_host_bind_translation_preserves_logical_paths(self):
        logical = Path("/synthetic/container/export")
        self.change_receipt(lambda value: value["original_eml"].update(path=str(logical / "objects/message")))
        self.change_receipt(lambda value: value["original_eml"].update(path=str(logical / "objects/message")), 1)
        first = self.snapshot(logical_root=logical, receipts=[str(logical / name) for name in self.receipts])
        mirror = self.base / "mirror"
        shutil.copytree(self.exports, mirror)
        second = snap.snapshot(mirror, logical_root=logical, receipts=self.receipts,
                               scope=self.scope, output=self.canonical / "intake")
        self.assertEqual(first["sha256"], second["sha256"])

    def test_export_profile_requires_network_none_and_exact_ro_mount(self):
        with host.Profile(self.profile()) as profile:
            self.assertEqual(profile.value["network"], {"mode": "none", "container_id": None})
        changes = [lambda p: p.update(network={"mode": "container", "container_id": "e" * 64}),
                   lambda p: p["mounts"][-1].update(readonly=False),
                   lambda p: p.update(unknown=True),
                   lambda p: p["mounts"].append(dict(p["mounts"][-1], target=str(self.exports / "objects"))),
                   lambda p: p["mounts"][0].update(source=p["mounts"][-1]["source"])]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(host.Rejected):
                with host.Profile(self.profile(change)):
                    pass

    def test_mapped_owner_requires_worker_verification_without_relaxation(self):
        values = self.paths()
        values["mounts"] = [{"kind": "bind", "source": "/synthetic/host/export",
                             "target": str(self.exports), "readonly": True}]
        docker = host.Docker(SimpleNamespace(value=values))
        with patch.object(host, "bind_input_anchor", return_value=(os.getuid() + 1, 2, 3)), \
                patch.object(host, "offline_snapshot") as check:
            docker.check_input()
        check.assert_not_called()
        paths = dict(values, tmp_dir="/synthetic/tmp", worker_state_parent="/synthetic/state",
                     board_dir="/synthetic/board", release_root="/synthetic/release", parser_path="/synthetic/parser")
        with patch.object(host.os, "getuid", return_value=1000), patch.object(host.os, "geteuid", return_value=1000), \
                patch.object(host, "PrivateDir") as directory, patch.object(host, "offline_snapshot", side_effect=host.Rejected):
            directory.return_value.__enter__.return_value = Mock()
            with self.assertRaises(host.Rejected):
                host.probe(paths)

    def entry(self, snapshot_error=None):
        paths = self.paths(role="worker", job_id="a" * 32, wall_seconds=300,
                           worker_state_parent="/synthetic/state", python="/synthetic/python", board_dir="/synthetic/board")
        gate = Mock()
        gate.read.return_value = (host.encoded({"schema": 1, "job_id": paths["job_id"], "role": "worker"}), None)
        directory = self.enterContext(patch.object(host, "PrivateDir"))
        directory.return_value.__enter__.return_value.child.return_value.__enter__.return_value = gate
        self.enterContext(patch.object(host.os, "getuid", return_value=1000))
        self.enterContext(patch.object(host.os, "geteuid", return_value=1000))
        check = self.enterContext(patch.object(host, "offline_snapshot", side_effect=snapshot_error))
        execute = self.enterContext(patch.object(host.os, "execve", side_effect=RuntimeError("captured exec")))
        return paths, check, execute

    def test_probe_and_entry_forward_exact_selected_export_arguments(self):
        paths, check, execute = self.entry()
        with self.assertRaisesRegex(RuntimeError, "captured exec"):
            host.entry(paths)
        check.assert_called_once_with("exports", str(self.exports), expected="a" * 64, readonly=True, selection=paths)
        argv = execute.call_args.args[1]
        self.assertIn("--unattended", argv)
        self.assertIn(host.WORKER_MODULE, argv)
        self.assertNotIn("--mail-config", argv)
        self.assertNotIn("--input-mode", argv)
        for receipt in self.receipts:
            self.assertIn(receipt, argv)
        self.assertEqual(argv[argv.index("--export-manifest-sha256") + 1], "a" * 64)
        self.assertIn("social.example.invalid", argv)

    def test_entry_snapshot_refusal_prevents_exec(self):
        paths, check, execute = self.entry(host.Rejected)
        with self.assertRaises(host.Rejected):
            host.entry(paths)
        execute.assert_not_called()

    def test_root_lock_and_inventory_close_on_success(self):
        p = self.pipeline()
        def queue_factory(root):
            run.require_root_lock(self.canonical)
            self.assertEqual(root, self.canonical / "portal")
            return self.queue
        def inventory_factory(queue, scope):
            run.require_root_lock(self.canonical)
            self.assertIs(queue, self.queue)
            self.assertEqual(scope, self.scope)
            return self.inventory
        self.queue_factory.side_effect = queue_factory
        self.inventory_factory.side_effect = inventory_factory
        self.queue.close.side_effect = lambda: run.require_root_lock(self.canonical)
        def enrolled(*args, **kwargs):
            run.require_root_lock(self.canonical)
            self.assertIs(kwargs["backend"], self.backend)
            return SimpleNamespace(message_id="synthetic-event", eml_sha256=sha(self.eml), documents=[])
        self.enroll.side_effect = enrolled
        self.assertEqual(self.run_pipeline(p)["exit_code"], 0)
        self.queue.close.assert_called_once()
        with self.assertRaises(run.RunError):
            run.require_root_lock(self.canonical)

    def test_inventory_construction_and_core_failure_cleanup(self):
        p = self.pipeline()
        self.inventory_factory.side_effect = RuntimeError("synthetic inventory refusal")
        self.assertEqual(self.run_pipeline(p)["exit_code"], 2)
        self.queue.close.assert_called_once()
        self.backend.start_run.assert_not_called()

    def test_one_backend_run_for_multiple_receipts_and_replay(self):
        p = self.pipeline()
        p._query.side_effect = lambda sql, *args: [{"id": "seen"}] if sql.startswith("SELECT id FROM mail_messages") else []
        report = self.run_pipeline(p)
        self.backend.start_run.assert_called_once()
        self.backend.finish_run.assert_called_once()
        self.assertEqual(len(report["intake"]["replayed"]), 2)
        self.assertEqual([call[0] for call in self.safety.calls], ["begin", "finish", "begin", "finish"])
        self.assertEqual([args.args[0] for args in self.enroll.call_args_list], [self.exports / name for name in self.receipts])
        queries = [c.args[1] for c in p._query.call_args_list if len(c.args) > 1]
        self.assertEqual(queries, [("synthetic-account", "Synthetic", 123456, 7001),
                                   ("synthetic-account", "Synthetic", 123456, 7002)])

    def test_budget_stop_defers_exact_remaining_receipts(self):
        for limit in (0, 1):
            with self.subTest(limit=limit):
                p = self.pipeline(limit)
                result = self.run_pipeline(p)
                self.assertEqual(result["intake"]["deferred"], 2 - limit)
                self.assertEqual(self.enroll.call_count, limit)
                self.assertEqual(result["exit_code"], 3)
                self.backend.finish_run.assert_called_once()

    def test_enrollment_fault_uses_existing_fixed_code_and_finalizes_once(self):
        p = self.pipeline()
        self.enroll.side_effect = mail_delta.Rejected("source_path_escape")
        result = self.run_pipeline(p)
        self.assertEqual(result["exit_code"], 2)
        self.assertEqual(result["intake"]["failures"][0]["code"], "source_path_escape")
        self.assertEqual(result["intake"]["deferred"], 1)
        self.backend.finish_run.assert_called_once()
        self.queue.close.assert_called_once()
        p = self.pipeline()
        original = snap.verify
        calls = []
        def changed(path, snapshot, **options):
            calls.append(1)
            # run_pipeline and ingest each construct one snapshot first.
            if len(calls) == 4:
                raise host.Rejected()
            return original(path, snapshot, **options)
        with patch.object(snap, "verify", side_effect=changed):
            result = self.run_pipeline(p)
        self.assertEqual(result["exit_code"], 2)
        self.assertEqual(result["intake"]["failures"][0]["code"], "retention_integrity_failed")
        self.assertEqual(self.enroll.call_count, 1)

    def test_mutation_with_enrollment_exception_explicitly_stops(self):
        # Ordinary faults can remain below RunSafety's rate threshold. This
        # capture deliberately permits them; only explicit stop blocks advance.
        for mutate in (False, True):
            for fault_position in (0, 1):
                with self.subTest(mutate=mutate, fault_position=fault_position):
                    p = self.pipeline()
                    safety = self.safety
                    def finish(outcome="success", code=None, originals=()):
                        self.assertIsNotNone(safety.state["active"])
                        safety.calls.append(("finish", outcome, code, tuple(originals)))
                        safety.state["active"] = None
                        if outcome == "fault":
                            safety.phases["intake"]["failed"] += 1
                        elif outcome == "held":
                            safety.state["hold"] = code
                    safety.finish = finish
                    safety.stop = Mock(wraps=safety.stop)
                    calls = []
                    def enrollment(*args, **kwargs):
                        position = len(calls)
                        calls.append(args[0])
                        if position == fault_position:
                            if mutate:
                                source = self.exports / "objects/shared"
                                info = source.stat()
                                os.utime(source, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000))
                            raise mail_delta.Rejected("source_path_escape")
                        return SimpleNamespace(message_id="synthetic-event", eml_sha256=sha(self.eml), documents=[])
                    self.enroll.side_effect = enrollment
                    result = self.run_pipeline(p)
                    failure = result["intake"]["failures"][0]
                    self.backend.finish_run.assert_called_once()
                    self.queue.close.assert_called_once()
                    if mutate:
                        self.assertEqual(failure["code"], "retention_integrity_failed")
                        safety.stop.assert_called_once_with("retention_integrity_failed")
                        self.assertEqual(result["exit_code"], 2)
                        self.assertEqual(self.enroll.call_count, fault_position + 1)
                        self.assertEqual(result["intake"]["deferred"], 1 - fault_position)
                        p.advance_all.assert_not_called()
                    else:
                        self.assertEqual(failure["code"], "source_path_escape")
                        safety.stop.assert_not_called()
                        self.assertEqual(result["exit_code"], 3)
                        self.assertEqual(self.enroll.call_count, 2)
                        p.advance_all.assert_called_once()

    def test_interrupt_keeps_active_attempt_and_closes_resources(self):
        p = self.pipeline()
        self.enroll.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.run_pipeline(p)
        self.assertIsNotNone(self.safety.state["active"])
        self.assertEqual(self.safety.state["hold"], "interrupted_attempt")
        self.assertEqual(self.backend.finish_run.call_args.args[1], "failed")
        self.backend.finish_run.assert_called_once()
        self.queue.close.assert_called_once()

    def test_pending_unknown_failed_and_deferred_are_explicit_gaps(self):
        p = self.pipeline()
        self.inventory.readiness.return_value = {"retrieval_complete": False,
            "gaps": ["collection_membership_unknown_or_exception"], "digestion_complete": False,
            "publication_authorized": False}
        result = self.run_pipeline(p)
        self.assertEqual((result["status"], result["exit_code"]), ("completed_with_gaps", 3))
        self.assertEqual(result["intake"]["events"][0], self.inventory.readiness.return_value)
        self.assertEqual(self.backend.finish_run.call_args.args[1], "completed_with_gaps")

    def test_explicit_scope_exclusion_preserves_existing_readiness(self):
        p = self.pipeline()
        result = self.run_pipeline(p)
        self.assertEqual(result["exit_code"], 0)
        self.assertFalse(result["substantive_complete"])
        for event in result["intake"]["events"]:
            self.assertTrue(event["retrieval_complete"])
            self.assertFalse(event["digestion_complete"])
            self.assertFalse(event["publication_authorized"])

    def test_existing_stop_model_and_review_holds_remain_authoritative(self):
        p = self.pipeline()
        self.safety.stop("decoder_needed")
        result = self.run_pipeline(p)
        self.assertEqual(result["exit_code"], 2)
        self.enroll.assert_not_called()
        p.advance_all.assert_not_called()
        self.assertEqual(self.safety.state["hold"], "decoder_needed")
        p = self.pipeline()
        p.model = object()
        self.assertEqual(self.run_pipeline(p)["exit_code"], 2)
        self.assertEqual(self.safety.state["hold"], "model_configuration_forbidden")
        self.enroll.assert_not_called()
        p = self.pipeline()
        p.review_retirement = {"state": "pending"}
        self.assertEqual(self.run_pipeline(p)["exit_code"], 3)

    def test_backend_finalize_failure_is_failed_and_not_retried(self):
        p = self.pipeline()
        self.backend.finish_run.side_effect = ValueError("synthetic finalize refusal")
        result = self.run_pipeline(p)
        self.assertEqual(result["exit_code"], 2)
        self.backend.finish_run.assert_called_once()
        self.queue.close.assert_called_once()
        self.assertEqual(self.safety.state["hold"], "unexpected_run_fault")


if __name__ == "__main__":
    unittest.main()
