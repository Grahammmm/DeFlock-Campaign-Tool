"""Deployed-format (legacy coordinator) portal allowlist: read-only mapping.

T11j: the root-owned allowlist deployed for the existing NextRequest intake
uses purpose ``flock-nextrequest-portal-intake`` with no ``version`` or
``path_prefixes``; the engine refused it as ``approval_invalid`` on real data.
These tests pin the read-only mapping with synthetic hosts only: no sockets, no
live URLs, no host policy file reads.
"""
import json
import os
import stat
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from campaign_tool.records.portal import Queue
from campaign_tool.records.portal.intake import intake_notices
from campaign_tool.records.portal.policy import PortalError, load_approval, parse_approval
from tests.test_records_portal import NOW, SOURCE

PORTAL_A = "cityone.records.example.test"
PORTAL_B = "citytwo.records.example.test"
OBJECTS = "assets.objects.example.test"
MODE = stat.S_IFREG | 0o644


def legacy_data():
    expiry = (NOW + timedelta(days=30)).isoformat().replace("+00:00", "Z")
    return {"enabled": True, "purpose": "flock-nextrequest-portal-intake",
            "expires_utc": expiry, "expires_at": expiry,
            "portal_hosts": [PORTAL_A, PORTAL_B], "approved_portal_hosts": [PORTAL_B, PORTAL_A],
            "redirect_hosts": [OBJECTS], "approved_redirect_hosts": [OBJECTS]}


def parse(data):
    return parse_approval(json.dumps(data), uid=0, mode=MODE, now=NOW)


def notice(url):
    return {"body": f'<a href="{url}">file</a>', "request_id": "26-001", "source_sha256": SOURCE}


class LegacyApprovalTests(unittest.TestCase):
    def test_deployed_format_loads_with_explicit_scopes(self):
        approval = parse(legacy_data())
        self.assertEqual(approval.portals, frozenset({PORTAL_A, PORTAL_B}))
        self.assertEqual(approval.redirects, frozenset({OBJECTS}))
        self.assertEqual(approval.scopes[PORTAL_A], ("/documents/",))
        self.assertEqual(approval.scopes[OBJECTS], ("/cityone/", "/citytwo/"))
        self.assertEqual(set(approval.scopes), {PORTAL_A, PORTAL_B, OBJECTS})

    def test_authorizes_only_what_the_file_names(self):
        approval = parse(legacy_data())
        doc = f"https://{PORTAL_A}/documents/42/download?token=synthetic-only"
        self.assertEqual(approval.authorize(doc, PORTAL_A, NOW), PORTAL_A)
        self.assertEqual(approval.authorize(f"https://{OBJECTS}/cityone/26-001/a.pdf", PORTAL_A, NOW), OBJECTS)
        for url, origin, code in (
                (f"https://{PORTAL_A}/admin/42", PORTAL_A, "path_not_approved"),
                (f"https://{OBJECTS}/other/26-001/a.pdf", PORTAL_A, "path_not_approved"),
                (f"https://other.example.test/documents/1", PORTAL_A, "redirect_not_approved"),
                (doc, "other.example.test", "portal_not_approved"),
                (f"https://{OBJECTS}/documents/42", OBJECTS, "portal_not_approved")):
            with self.subTest(url=url, origin=origin), self.assertRaisesRegex(PortalError, code):
                approval.authorize(url, origin, NOW)
        with self.assertRaisesRegex(PortalError, "approval_expired"):
            approval.authorize(doc, PORTAL_A, NOW + timedelta(days=31))

    def test_malformed_deployed_format_refused(self):
        cases = {
            "disabled": {"enabled": False},
            "unknown_key": {"path_prefixes": {PORTAL_A: ["/"]}},
            "version_key": {"version": 1},
            "mirror_differs": {"approved_portal_hosts": [PORTAL_A]},
            "redirect_mirror_differs": {"approved_redirect_hosts": []},
            "no_portals": {"portal_hosts": [], "approved_portal_hosts": []},
            "duplicate_host": {"portal_hosts": [PORTAL_A, PORTAL_A], "approved_portal_hosts": [PORTAL_A, PORTAL_A]},
            "host_in_both": {"redirect_hosts": [PORTAL_A], "approved_redirect_hosts": [PORTAL_A]},
            "bad_host": {"portal_hosts": ["Bad Host"], "approved_portal_hosts": ["Bad Host"]},
            "expires_disagree": {"expires_at": (NOW + timedelta(days=99)).isoformat()},
            "naive_expiry": {"expires_utc": "2031-01-01T00:00:00", "expires_at": "2031-01-01T00:00:00"},
            "hosts_not_list": {"portal_hosts": PORTAL_A, "approved_portal_hosts": PORTAL_A},
        }
        for name, change in cases.items():
            data = {**legacy_data(), **change}
            with self.subTest(name), self.assertRaises(PortalError):
                parse(data)
        for missing in ("expires_utc", "portal_hosts", "redirect_hosts"):
            data = legacy_data()
            del data[missing]
            with self.subTest(missing=missing), self.assertRaisesRegex(PortalError, "approval_invalid"):
                parse(data)

    def test_expired_deployed_format_refused(self):
        data = legacy_data()
        data["expires_utc"] = data["expires_at"] = NOW.isoformat()
        with self.assertRaisesRegex(PortalError, "approval_expired"):
            parse(data)

    def test_ownership_still_enforced(self):
        for uid, mode in ((1000, MODE), (0, stat.S_IFREG | 0o666)):
            with self.subTest(uid=uid, mode=mode), self.assertRaisesRegex(PortalError, "approval_not_admin_owned"):
                parse_approval(json.dumps(legacy_data()), uid=uid, mode=mode, now=NOW)

    def test_v1_format_unchanged(self):
        from tests.test_records_portal import approval
        self.assertEqual(approval().scopes, {"portal.example.test": ("/documents/",),
                                             "objects.example.test": ("/production/",)})

    def test_load_approval_reads_deployed_file_without_changing_it(self):
        # The full loader on a real file; ownership is faked to root because tests
        # never run as root. The file bytes and mtime are unchanged afterwards.
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "approval.json"
            path.write_text(json.dumps(legacy_data()), encoding="utf-8")
            path.chmod(0o644)
            before = (path.read_bytes(), path.stat().st_mtime_ns)
            real_fstat, real_stat = os.fstat, Path.stat

            def as_root(result):
                values = list(result)
                values[stat.ST_UID] = 0
                return os.stat_result(values)

            with patch("campaign_tool.records.portal.policy.os.fstat", lambda fd: as_root(real_fstat(fd))), \
                    patch.object(Path, "stat", lambda self, **kw: as_root(real_stat(self, **kw))):
                approval = load_approval(path, NOW)
            self.assertEqual(approval.portals, frozenset({PORTAL_A, PORTAL_B}))
            self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)

    def test_intake_enrolls_with_deployed_format(self):
        with tempfile.TemporaryDirectory() as temp:
            queue = Queue(Path(temp) / "queue")
            try:
                approval = parse(legacy_data())
                result = intake_notices(queue, notices=[
                    notice(f"https://{PORTAL_A}/documents/42/download?token=synthetic-only"),
                    notice(f"https://{PORTAL_B}/documents/7"),
                    notice(f"https://unlisted.example.test/documents/9/download?token=synthetic-only")],
                    approval_loader=lambda: approval, now=NOW)
                self.assertEqual(result, {"enrolled": 2, "refused": 1, "skipped": 0})
                self.assertEqual(queue.status()["refusals"][0]["reason"], "portal_not_approved")
            finally:
                queue.close()


if __name__ == "__main__":
    unittest.main()
