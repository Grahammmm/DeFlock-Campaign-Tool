"""Synthetic host offline ingress; no Docker daemon, records, credentials or model calls."""
import copy
from contextlib import contextmanager
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest import mock

from campaign_tool.records import container_host as host
from campaign_tool.records.extract import ocr
from tests.records import test_container_host as support


class OfflineDocker(support.FakeDocker):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.mount_mutation = None
        self.probe_hook = None
        self.probe_result = None

    def invoke(self, argv, timeout):
        code, raw = super().invoke(argv, timeout)
        if argv[1] == "inspect" and code == 0:
            value = host.decoded(raw)
            if "image" in value:
                actual, configured = [], []
                for mount in self.value["mounts"]:
                    actual.append({"Type": mount["kind"], "Destination": mount["target"],
                                   "RW": not mount["readonly"],
                                   "Source": mount["source"] if mount["kind"] == "bind" else
                                             "/synthetic/volumes/" + mount["source"],
                                   "Name": mount["source"] if mount["kind"] == "volume" else ""})
                    config = {"Type": mount["kind"], "Source": mount["source"],
                              "Target": mount["target"], "ReadOnly": mount["readonly"]}
                    if mount["kind"] == "volume":
                        config["VolumeOptions"] = {"NoCopy": True, "Subpath": mount.get("subpath", "")}
                    configured.append(config)
                value.update(mounts=actual, configured_mounts=configured)
                if self.mount_mutation:
                    self.mount_mutation(value)
                raw = host.encoded(value)
        if "probe" in argv:
            if self.probe_hook:
                self.probe_hook()
            if self.probe_result is not None:
                raw = host.encoded(self.probe_result)
        return code, raw


class HostOfflineInboxTests(support.HostFixture):
    @contextmanager
    def worker_namespace(self):
        # Project only this host owner's UID, not mode, links or inode identity.
        owner, real_fstat = os.getuid(), os.fstat
        def projected(fd):
            value = real_fstat(fd)
            fields = {name: getattr(value, name) for name in dir(value)
                      if name.startswith("st_")}
            if value.st_uid == owner:
                fields["st_uid"] = 1000
            return SimpleNamespace(**fields)
        with mock.patch.object(host.os, "getuid", return_value=1000), \
             mock.patch.object(host.os, "geteuid", return_value=1000), \
             mock.patch.object(host.os, "fstat", side_effect=projected):
            yield

    def offline(self, *, bind=False):
        self.inbox = self.root / "inbox"
        self.inbox.mkdir(mode=0o700)
        self.eml = self.inbox / "fixture.eml"
        self.eml.write_bytes(
            b"From: records@example.invalid\r\nTo: owner@example.invalid\r\n"
            b"Message-ID: <offline-fixture@example.invalid>\r\n"
            b"Subject: Synthetic records\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
            b"Synthetic ALPR policy. No records are sent. --mail-config is data, not an argument.\r\n")
        self.eml.chmod(0o600)
        value = json.loads(self.profile.read_text())
        del value["mail_config"]
        value["input_mode"] = "inbox"
        value["inbox"] = str(self.inbox) if bind else "/synthetic/inbox"
        value["inbox_manifest_sha256"] = host.inbox_snapshot(self.inbox)["sha256"]
        value["mounts"] = [m for m in value["mounts"] if m["target"] != "/synthetic/mail.json"]
        value["mounts"].append({"kind": "bind" if bind else "volume",
                                "source": str(self.inbox) if bind else "synthetic-inbox",
                                "target": value["inbox"], "readonly": True})
        self.write_profile(value)
        return value

    def entry_command(self, paths):
        identity = {"schema": 1, "job_id": paths["job_id"], "role": "worker"}
        gate, parent = mock.MagicMock(), mock.MagicMock()
        gate.__enter__.return_value = gate
        gate.read.return_value = (host.encoded(identity), None)
        parent.__enter__.return_value = parent
        parent.child.return_value = gate
        original = host.PrivateDir
        def directory(path, **kwargs):
            return parent if path == paths["worker_state_parent"] else original(path, **kwargs)
        with self.worker_namespace(), \
             mock.patch.object(host, "PrivateDir", side_effect=directory), \
             mock.patch.object(host.os, "fstatvfs", return_value=SimpleNamespace(f_flag=os.ST_RDONLY)), \
             mock.patch.object(host.os, "execve") as execute:
            with self.assertRaises(host.Rejected):
                host.entry(paths)
        return execute.call_args.args

    def test_exact_offline_create_probe_and_receipt_contract_without_secret_mount(self):
        value = self.offline()
        fake = OfflineDocker(self.profile)
        provider_key = "_".join(("OPENAI", "API", "KEY"))
        with mock.patch.dict(os.environ, {"MODEL_BASE_URL": "https://provider.example.invalid",
                                        provider_key: "fixture"}):
            self.assertEqual(self.run_host(fake)["status"], "completed")
        receipt = self.current()
        self.assertEqual(receipt["profile_sha256"], host.digest(self.profile.read_bytes()))
        self.assertEqual(receipt["worker_status"], "completed")
        self.assertEqual(receipt["ack"], "natural")
        for argv, _ in fake.calls:
            if argv[1] == "create" or "probe" in argv:
                self.assertNotIn("--mail-config", argv)
                self.assertEqual(argv[argv.index("--inbox") + 1], value["inbox"])
                self.assertEqual(argv[argv.index("--inbox-manifest-sha256") + 1],
                                 value["inbox_manifest_sha256"])
                self.assertNotIn(provider_key + "=fixture", argv)
            if argv[1] == "create":
                self.assertEqual(argv[argv.index("--network") + 1], "none")
                mounts = [argv[i + 1] for i, item in enumerate(argv) if item == "--mount"]
                self.assertTrue(any("dst=" + value["inbox"] + ",readonly" in m for m in mounts))
                self.assertFalse(any("mail.json" in m for m in mounts))
                self.assertIn("MODEL_BASE_URL=", argv)
                self.assertIn("CHALLENGE_MODEL_BASE_URL=", argv)
        self.assertTrue(receipt["worker_removed"] and receipt["reporter_removed"])

    def test_real_accepted_cli_intakes_fixture_into_empty_root(self):
        value = self.offline(bind=True)
        records = self.root / "fresh-records"
        records.mkdir(mode=0o700)
        self.assertEqual(list(records.iterdir()), [])
        paths = dict(value, root=str(records), python=sys.executable,
                     role="worker", job_id=support.sha("offline-real-entry")[:32])
        project = str(Path(__file__).resolve().parents[2])
        environment_profile = SimpleNamespace(value=dict(
            value, release_root=project, parser_path=os.environ.get("PYTHONPATH") or project))
        env = dict(item.split("=", 1) for item in host.Docker(environment_profile).environment()[2:])
        with mock.patch.dict(os.environ, env, clear=True):
            _, argv, _ = self.entry_command(paths)
            child = argv[argv.index("--") + 1:]
            self.assertEqual(child[:5], [sys.executable, "-B", "-m", "campaign_tool.records", "run"])
            self.assertEqual(child[child.index("--inbox") + 1], str(self.inbox))
            self.assertNotIn("--mail-config", child)
            from campaign_tool.records import run, unattended
            output = io.StringIO()
            with mock.patch.object(unattended.manual, "local_ocr_tools", return_value=(
                     {**{name: {"available": True, "path": "/synthetic/bin/" + name,
                                "version": name + " 0.0-test"} for name in ocr.OCR_TOOLS},
                      "pypdf": {"available": True, "version": "0.0-test"}},
                     support.sha("synthetic-ocr-discovery"))), \
                 mock.patch.object(ocr, "extract_image_only_pages",
                                   side_effect=AssertionError("text_fixture_must_not_run_ocr")), \
                 mock.patch("socket.create_connection", side_effect=AssertionError("offline_network")), \
                 mock.patch("socket.socket", side_effect=AssertionError("offline_network")), \
                 mock.patch("sys.stdout", output):
                code = run.main(child[5:])
        report = json.loads(output.getvalue())
        self.assertIsInstance(code, int)
        self.assertEqual(report["intake"]["messages"], 1)
        self.assertEqual(report["intake"]["failures"], [])
        self.assertGreaterEqual(report["originals"], 1)
        self.assertFalse(report["substantive_complete"])
        self.assertEqual(report["substantive_review_status"], "queued")

    def test_legacy_and_explicit_mailbox_profiles_are_unchanged(self):
        old = json.loads(self.profile.read_text())
        with host.Profile(self.profile) as profile:
            self.assertEqual(host.input_arguments(profile.value), ["--mail-config", old["mail_config"]])
        old["input_mode"] = "mailbox"
        self.write_profile(old)
        with host.Profile(self.profile) as profile:
            self.assertEqual(host.input_arguments(profile.value), ["--mail-config", old["mail_config"]])
        self.assertEqual(self.run_host(self.fake())["status"], "completed")

    def test_dual_neither_mode_and_arbitrary_arguments_fail_closed(self):
        value = self.offline()
        cases = [
            dict(value, mail_config="/synthetic/forbidden.json"),
            {k: v for k, v in value.items() if k != "inbox"},
            {k: v for k, v in value.items() if k != "inbox_manifest_sha256"},
            dict(value, input_mode="mailbox"),
            dict(value, input_mode="callback"),
            dict(value, worker_args=["--mail-config", "/synthetic/forbidden.json"]),
            dict(value, inbox_manifest_sha256=True),
            dict(value, inbox="/synthetic/../escape"),
            dict(value, network={"mode": "container", "container_id": support.sha("network")}),
        ]
        for candidate in cases:
            with self.subTest(keys=sorted(candidate)):
                self.write_profile(candidate)
                with self.assertRaises(host.Rejected):
                    host.Profile(self.profile)

    def test_exact_mount_deepest_readonly_and_no_competing_source_aliases(self):
        value = self.offline()
        selected = value["mounts"][-1]
        value["inbox"] = selected["target"] = "/synthetic/records/inbox"
        self.write_profile(value)
        self.assertEqual(self.run_host(OfflineDocker(self.profile))["status"], "completed")
        original = copy.deepcopy(value)
        for change in ("rw", "ancestor-only", "alias", "descendant"):
            value = copy.deepcopy(original)
            if change == "rw":
                value["mounts"][-1]["readonly"] = False
            elif change == "ancestor-only":
                value["mounts"].pop()
                value["mounts"][2]["readonly"] = True
            elif change == "alias":
                value["mounts"][2]["source"] = value["mounts"][-1]["source"]
            else:
                value["mounts"].append({"kind": "volume", "source": "synthetic-overlay",
                                       "target": value["inbox"] + "/fixture.eml", "readonly": False})
            self.write_profile(value)
            with self.subTest(change=change), self.assertRaises(host.Rejected):
                host.Profile(self.profile)

    def test_runtime_rw_or_missing_mount_blocks_admission(self):
        value = self.offline()
        fake = OfflineDocker(self.profile)
        def changed(runtime):
            for mount in runtime["mounts"]:
                if mount["Destination"] == value["inbox"]:
                    mount["RW"] = True
        fake.mount_mutation = changed
        self.assertEqual(self.run_host(fake)["status"], "held")
        self.assertFalse(any("admit" in argv for argv, _ in fake.calls))
        self.assertTrue(self.current()["hold"])

    def test_runtime_volume_subpath_must_match_profile(self):
        value = self.offline()
        value["mounts"][-1]["subpath"] = "synthetic/eml"
        self.write_profile(value)
        fake = OfflineDocker(self.profile)
        def changed(runtime):
            for mount in runtime["configured_mounts"]:
                if mount["Target"] == value["inbox"]:
                    mount["VolumeOptions"]["Subpath"] = "wrong"
        fake.mount_mutation = changed
        self.assertEqual(self.run_host(fake)["status"], "held")
        self.assertFalse(any("admit" in argv for argv, _ in fake.calls))

    def test_bind_manifest_and_metadata_mutation_fences_admission(self):
        self.offline(bind=True)
        fake = OfflineDocker(self.profile)
        fake.probe_hook = lambda: self.eml.write_bytes(b"changed synthetic bytes")
        self.assertEqual(self.run_host(fake)["status"], "held")
        self.assertFalse(any("admit" in argv for argv, _ in fake.calls))
        self.assertEqual(self.current()["error"], "transport")

    def test_missing_unsafe_symlink_and_changed_input_snapshots_fail(self):
        self.offline(bind=True)
        with self.assertRaises(OSError):
            host.inbox_snapshot(self.root / "absent")
        initial = host.inbox_snapshot(self.inbox)
        self.eml.write_bytes(b"changed synthetic")
        with self.assertRaises(host.Rejected):
            host.inbox_snapshot(self.inbox, expected=initial["sha256"])
        with self.assertRaises(host.Rejected):
            host.inbox_snapshot(self.inbox, prior=initial)
        self.eml.unlink()
        self.eml.symlink_to(self.profile)
        with self.assertRaises(OSError):
            host.inbox_snapshot(self.inbox)
        self.eml.unlink()
        self.eml.write_bytes(b"synthetic")
        self.eml.chmod(0o644)
        with self.assertRaises(host.Rejected):
            host.inbox_snapshot(self.inbox)

    def test_snapshot_identity_rejects_equal_bytes_replacement_and_hardlinks(self):
        self.offline(bind=True)
        initial = host.inbox_snapshot(self.inbox)
        raw = self.eml.read_bytes()
        self.eml.rename(self.root / "previous.eml")
        self.eml.write_bytes(raw)
        self.eml.chmod(0o600)
        self.assertEqual(host.inbox_snapshot(self.inbox)["sha256"], initial["sha256"])
        with self.assertRaises(host.Rejected):
            host.inbox_snapshot(self.inbox, prior=initial)
        os.link(self.eml, self.root / "alias.eml")
        with self.assertRaises(host.Rejected):
            host.inbox_snapshot(self.inbox)

    def test_profile_change_cannot_adopt_existing_job_identity(self):
        value = self.offline()
        fake = OfflineDocker(self.profile, launch=host.TransportError())
        self.assertEqual(self.run_host(fake)["status"], "held")
        prior = self.current()
        value["inbox_manifest_sha256"] = support.sha("different-input")
        self.write_profile(value)
        with self.assertRaises(host.Rejected):
            self.stop_host(fake)
        self.assertEqual(self.current()["profile_sha256"], prior["profile_sha256"])
        self.assertTrue(self.current()["hold"])

    def test_probe_readonly_requires_actual_flags_without_mailbox_config(self):
        self.offline(bind=True)
        paths = {"input_mode": "inbox", "inbox": str(self.inbox),
                 "inbox_manifest_sha256": host.inbox_snapshot(self.inbox)["sha256"]}
        for key in ("root", "tmp_dir", "worker_state_parent", "board_dir", "release_root", "parser_path"):
            path = self.root / ("probe-" + key)
            path.mkdir(mode=0o700)
            paths[key] = str(path)
        with self.worker_namespace():
            with mock.patch.object(host.os, "fstatvfs", return_value=SimpleNamespace(f_flag=0)):
                with self.assertRaises(host.Rejected):
                    host.probe(paths)
            with mock.patch.object(host.os, "fstatvfs", return_value=SimpleNamespace(f_flag=os.ST_RDONLY)):
                self.assertEqual(host.probe(paths), {"schema": 1, "status": "runtime_ready", "uid": 1000})

    def test_non_worker_host_uid_projects_without_changing_inode_metadata(self):
        owner, real_fstat = os.getuid(), os.fstat
        def host_namespace(fd):
            value = real_fstat(fd)
            fields = {name: getattr(value, name) for name in dir(value)
                      if name.startswith("st_")}
            if value.st_uid == owner:
                fields["st_uid"] = 2001
            return SimpleNamespace(**fields)
        with mock.patch.object(host.os, "getuid", return_value=2001), \
             mock.patch.object(host.os, "geteuid", return_value=2001), \
             mock.patch.object(host.os, "fstat", side_effect=host_namespace):
            self.test_probe_readonly_requires_actual_flags_without_mailbox_config()

    def test_worker_namespace_does_not_exempt_foreign_file_owner(self):
        self.offline(bind=True)
        inode, real_fstat = self.eml.stat().st_ino, os.fstat
        foreign_uid = max(self.eml.stat().st_uid, os.getuid(), 1000) + 1
        self.assertNotIn(foreign_uid, (self.eml.stat().st_uid, 1000))
        def foreign_file(fd):
            value = real_fstat(fd)
            fields = {name: getattr(value, name) for name in dir(value)
                      if name.startswith("st_")}
            if value.st_ino == inode:
                fields["st_uid"] = foreign_uid
            return SimpleNamespace(**fields)
        with mock.patch.object(host.os, "fstat", side_effect=foreign_file), \
             self.worker_namespace(), \
             mock.patch.object(host.os, "fstatvfs", return_value=SimpleNamespace(f_flag=os.ST_RDONLY)), \
             self.assertRaises(host.Rejected):
            host.inbox_snapshot(self.inbox, readonly=True)

    def test_manifest_command_is_sanitized_and_bounded(self):
        self.offline(bind=True)
        output = io.StringIO()
        with mock.patch("sys.stdout", output):
            code = host.main(["inbox-manifest", "--inbox", str(self.inbox)])
        value = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(set(value), {"schema", "status", "sha256", "messages", "bytes"})
        self.assertEqual(value["messages"], 1)
        self.assertNotIn("fixture.eml", output.getvalue())
        extra = self.inbox / "not-eml.txt"
        extra.write_bytes(b"synthetic")
        extra.chmod(0o600)
        with self.assertRaises(host.Rejected):
            host.inbox_snapshot(self.inbox)

    def test_missing_ocr_discovery_still_refuses_before_genuine_intake(self):
        self.offline(bind=True)
        from campaign_tool.records import run, unattended
        output = io.StringIO()
        with mock.patch.dict(os.environ, {"MODEL_BASE_URL": "", "CHALLENGE_MODEL_BASE_URL": ""}, clear=True), \
             mock.patch.object(unattended.manual, "local_ocr_tools", return_value=(None, None)), \
             mock.patch("socket.create_connection", side_effect=AssertionError("offline_network")), \
             mock.patch("sys.stdout", output):
            code = run.main(["--root", str(self.root / "missing-ocr-records"),
                             "--inbox", str(self.inbox), "--unattended", "--ocr", "--json"])
        report = json.loads(output.getvalue())
        self.assertEqual(code, 2)
        self.assertIsNone(report["intake"])
        self.assertEqual(report["originals"], 0)
        self.assertEqual(report["safety"]["stop_code"], "ocr_runtime_unavailable")

    def test_shared_volume_disjoint_subpaths_and_realized_source_binding(self):
        value = self.offline()
        for index, mount in enumerate(value["mounts"]):
            mount.update(source="synthetic-shared-home", subpath="case/path-" + str(index))
        self.write_profile(value)
        self.assertEqual(self.run_host(OfflineDocker(self.profile))["status"], "completed")
        for change in ("same", "ancestor", "whole-volume"):
            candidate = copy.deepcopy(value)
            selected = candidate["mounts"][-1]["subpath"]
            candidate["mounts"][2]["subpath"] = {
                "same": selected, "ancestor": "case", "whole-volume": None}[change]
            if change == "whole-volume":
                del candidate["mounts"][2]["subpath"]
            self.write_profile(candidate)
            with self.subTest(change=change), self.assertRaises(host.Rejected):
                host.Profile(self.profile)

    def test_runtime_shared_volume_alias_and_unbound_source_are_rejected(self):
        value = self.offline()
        for index, mount in enumerate(value["mounts"]):
            mount.update(source="synthetic-shared-home", subpath="case/path-" + str(index))
        self.write_profile(value)
        with host.Profile(self.profile) as profile:
            fake = OfflineDocker(self.profile)
            docker = host.Docker(profile, invoke=fake.invoke, containment=fake)
            journal = host.Journal(profile)
            self.addCleanup(journal.close)
            job_id = journal.allocate()
            journal.change(job_id, phase="preflight")
            cid = docker.create(job_id, "worker")
            runtime = docker.inspect(cid, job_id, "worker")
            for change in ("subpath", "source", "cross-kind"):
                bad = copy.deepcopy(runtime)
                if change == "subpath":
                    bad["configured_mounts"][2]["VolumeOptions"]["Subpath"] = value["mounts"][-1]["subpath"]
                elif change == "source":
                    bad["mounts"][2]["Source"] = "/synthetic/unbound"
                else:
                    bad["mounts"][2].update(Type="bind", Source=bad["mounts"][-1]["Source"])
                with self.subTest(change=change), self.assertRaises(host.Rejected):
                    host.check_inbox_mounts(profile.value, bad)

    def test_mapped_bind_owner_is_metadata_anchored_not_permission_relaxed(self):
        self.offline(bind=True)
        original = host.trusted_path
        def mapped(path, **kwargs):
            result = original(path, **kwargs)
            if path == str(self.inbox) and kwargs.get("metadata_only"):
                attrs = {key: getattr(result, key) for key in
                         ("st_uid", "st_gid", "st_mode", "st_dev", "st_ino",
                          "st_size", "st_mtime_ns", "st_ctime_ns")}
                attrs["st_uid"] = 100999
                return SimpleNamespace(**attrs)
            return result
        with mock.patch.object(host, "trusted_path", side_effect=mapped), \
             mock.patch.object(host, "inbox_snapshot", side_effect=AssertionError("host_cannot_read_mapped_owner")):
            self.assertEqual(self.run_host(OfflineDocker(self.profile))["status"], "completed")
        self.assertEqual(self.inbox.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.eml.stat().st_mode & 0o777, 0o600)

    def test_mapped_bind_still_requires_exact_worker_uid_readiness(self):
        self.offline(bind=True)
        original = host.bind_input_anchor
        def mapped(path):
            anchor = original(path)
            return (100999, *anchor[1:])
        fake = OfflineDocker(self.profile)
        fake.probe_result = {"schema": 1, "status": "runtime_ready", "uid": 0}
        with mock.patch.object(host, "bind_input_anchor", side_effect=mapped):
            self.assertEqual(self.run_host(fake)["status"], "held")
        self.assertFalse(any("admit" in argv for argv, _ in fake.calls))

    def test_bind_anchor_replacement_and_raw_pdf_inbox_fail_closed(self):
        self.offline(bind=True)
        with host.Profile(self.profile) as profile:
            docker = host.Docker(profile)
            docker.check_input()
            previous = self.root / "old-input"
            self.inbox.rename(previous)
            self.inbox.mkdir(mode=0o700)
            with self.assertRaises(host.Rejected):
                docker.check_input()
        raw = self.inbox / "standalone.pdf"
        raw.write_bytes(b"synthetic raw PDF is not an EML message")
        raw.chmod(0o600)
        with self.assertRaises(host.Rejected):
            host.inbox_snapshot(self.inbox)


if __name__ == "__main__":
    unittest.main()
