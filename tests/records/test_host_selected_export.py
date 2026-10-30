"""Synthetic host export selection; no Docker daemon or real exports."""
import copy
import io
import json
import os
from types import SimpleNamespace
from unittest.mock import patch
from campaign_tool.records import container_host as host
from tests.records.test_container_host import HostFixture
from tests.records import test_host_offline_inbox as offline
from tests.records.test_host_offline_inbox import OfflineDocker
from tests.records.test_export_bridge import legacy_export


class HostSelectedExportTests(HostFixture):
    worker_namespace = offline.HostOfflineInboxTests.worker_namespace
    entry_command = offline.HostOfflineInboxTests.entry_command

    def selected(self, bind=False):
        self.export = self.root / "exports"
        self.export.mkdir(mode=0o700)
        self.path, self.receipt = legacy_export(self.export)
        from campaign_tool.records.intake.export_snapshot import snapshot
        value = json.loads(self.profile.read_text())
        del value["mail_config"]
        value.update(input_mode="exports", exports=str(self.export) if bind else "/synthetic/exports",
                     export_receipts=[str(self.path.relative_to(self.export))])
        value["export_manifest_sha256"] = snapshot(self.export, tuple(value["export_receipts"]))["sha256"]
        value["mounts"] = [m for m in value["mounts"] if m["target"] != "/synthetic/mail.json"]
        value["mounts"].append(dict(kind="bind" if bind else "volume", source=str(self.export) if bind else "synthetic-exports", target=value["exports"], readonly=True))
        self.write_profile(value)
        return value

    def test_offline_host_and_entry_arguments(self):
        value = self.selected(bind=True)
        fake = OfflineDocker(self.profile)
        self.assertEqual(self.run_host(fake)["status"], "completed")
        for argv, _ in fake.calls:
            if argv[1] == "create" or "probe" in argv:
                self.assertEqual(argv[argv.index("--export-root") + 1], value["exports"])
                self.assertIn("--export-receipt", argv)
                self.assertEqual(argv[argv.index("--export-manifest-sha256") + 1], value["export_manifest_sha256"])
                self.assertNotIn("--mail-config", argv)
                self.assertNotIn("--collection-host", argv)
            if argv[1] == "create":
                self.assertEqual(argv[argv.index("--network") + 1], "none")
        paths = dict(value, python="/synthetic/python", role="worker", job_id="a" * 32)
        _, argv, _ = self.entry_command(paths)
        self.assertEqual(argv[argv.index("--export-root") + 1], value["exports"])
        self.assertNotIn("--collection-host", argv)

    def test_volume_and_profile_refusals(self):
        value = self.selected()
        self.assertEqual(self.run_host(OfflineDocker(self.profile))["status"], "completed")
        candidates = [dict(value, export_receipts=[]), dict(value, export_receipts=[str(i) for i in range(61)]),
                      {k: v for k, v in value.items() if k != "export_manifest_sha256"},
                      dict(value, inbox="/synthetic/inbox"), dict(value, network=dict(mode="container", container_id="a" * 64))]
        for kind in ("rw", "ancestor", "alias"):
            candidate = copy.deepcopy(value)
            if kind == "rw":
                candidate["mounts"][-1]["readonly"] = False
            elif kind == "ancestor":
                candidate["mounts"][-1]["target"] = "/synthetic"
            else:
                candidate["mounts"][2]["source"] = candidate["mounts"][-1]["source"]
            candidates.append(candidate)
        for candidate in candidates:
            self.write_profile(candidate)
            with self.assertRaises(host.Rejected):
                host.Profile(self.profile)

    def test_member_mutation_fences_admission(self):
        self.selected(bind=True)
        fake = OfflineDocker(self.profile)
        member = self.export / self.receipt["attachments"][0]["path"]
        fake.probe_hook = lambda: member.write_bytes(b"changed synthetic")
        self.assertEqual(self.run_host(fake)["status"], "held")
        self.assertFalse(any("admit" in argv for argv, _ in fake.calls))
        self.assertEqual(self.current()["error"], "transport")

    def test_probe_requires_readonly_and_manifest_command_is_redacted(self):
        value = self.selected(bind=True)
        paths = dict(value)
        for key in ("root", "tmp_dir", "worker_state_parent", "board_dir", "release_root", "parser_path"):
            path = self.root / ("probe-" + key)
            path.mkdir(mode=0o700)
            paths[key] = str(path)
        with self.worker_namespace(), patch.object(host.os, "fstatvfs", return_value=SimpleNamespace(f_flag=0)):
            with self.assertRaises(host.Rejected):
                host.probe(paths)
        with patch("sys.stdout", io.StringIO()) as output:
            code = host.main(["export-manifest", "--exports", str(self.export), "--export-receipt", value["export_receipts"][0]])
        result = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(set(result), {"schema", "status", "sha256", "receipts", "members", "bytes"})
        self.assertEqual(result["sha256"], value["export_manifest_sha256"])
        self.assertNotIn(self.path.name, output.getvalue())
