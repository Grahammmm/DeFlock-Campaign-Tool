"""RF0925-2: profile-local decoder refusals, using synthetic external campaigns."""
import errno
import json
import os
import socket
import subprocess
import sys
import tempfile
import traceback
import unittest
import urllib.request
from contextlib import ExitStack
from datetime import date
from pathlib import Path
from unittest import mock

from campaign_tool import content_profile as cp
from campaign_tool.content import ContentError, MAX_JSON_BYTES
from tests import test_content_profile_checks as checks
from tests import test_content_profile_entries as entries
from tests import test_content_profile_transparency as transparency

DAY = "2026-01-02"
TODAY = date(2026, 11, 10)
READ_ERROR = "content/agencies.json: JSON nesting is too deep"
WRITE_ERROR = READ_ERROR + "; nothing was written"
CANARY = "SYNTHETIC-PRIVATE-DECODER-CANARY"


def deep_value(objects=False):
    # Raw repetition, not recursive serialization or a changed recursion limit.
    left, right = (b'{"unknown":', b'}') if objects else (b'[', b']')
    return left * 20000 + json.dumps(CANARY).encode() + right * 20000


class ProfileDepthTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-profile-depth-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "campaign"
        (self.root / "content").mkdir(parents=True)
        self.target = self.root / "content/agencies.json"
        for module, name in ((socket, "socket"), (socket, "create_connection"), (urllib.request, "urlopen")):
            patch = mock.patch.object(module, name, side_effect=AssertionError("network forbidden"))
            patch.start()
            self.addCleanup(patch.stop)
        self.target.write_bytes(deep_value())

    def snapshot(self):
        info = self.target.stat()
        return (self.target.read_bytes(), info.st_ino, info.st_mode, info.st_mtime_ns,
                sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob("*")))

    def command(self, *args, subprocess_cli=False):
        arguments = ["content", "agency-profile", *map(str, args), "--directory", str(self.root)]
        if subprocess_cli:
            env = dict(os.environ, DEFLOCK_CAMPAIGN_REGISTER=str(self.base / "registry.json"))
            result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *arguments],
                                    cwd=Path(__file__).resolve().parents[1], env=env,
                                    capture_output=True, text=True, timeout=30)
            return result.returncode, result.stdout, result.stderr
        return checks.run(*arguments)

    def assert_depth(self, call, message):
        with self.assertRaises(cp.ContentProfileError) as caught:
            call()
        self.assertEqual(str(caught.exception), message)
        self.assertTrue(caught.exception.__suppress_context__)
        shown = "".join(traceback.format_exception(caught.exception))
        self.assertNotIn(CANARY, shown)
        self.assertNotIn("RecursionError", shown)

    def mutations(self):
        common = ["--agency", checks.A, "--checked-on", DAY]
        yield ("recheck", lambda: cp.recheck_profile(self.root, checks.A, "alpr_policy", DAY, today=TODAY),
               ["recheck", *common, "--field", "alpr_policy"])
        yield ("policy", lambda: cp.write_policy(self.root, checks.A, url=checks.URL_A,
                                                 source_url=checks.URL_B, checked_on=DAY, today=TODAY),
               ["policy", *common, "--url", checks.URL_A, "--source-url", checks.URL_B])
        yield ("contract", lambda: cp.write_contract(self.root, checks.A, vendor="Synthetic vendor", start=DAY,
                                                     end="2027-01-02", source_url=checks.DEAL_A,
                                                     checked_on=DAY, today=TODAY),
               ["contract", *common, "--vendor", "Synthetic vendor", "--start", DAY,
                "--end", "2027-01-02", "--source-url", checks.DEAL_A])
        for command, field, writer, ids in (("policy-check", "alpr_policy", cp.write_policy_check, checks.POLICY_IDS),
                                          ("contract-check", "contract", cp.write_contract_check, checks.CONTRACT_IDS)):
            elements = cp.parse_reading_elements(field, [f"{key}=unclear" for key in ids])
            yield (command, lambda writer=writer, elements=elements: writer(
                self.root, checks.A, checked_on=DAY, elements=elements, today=TODAY),
                [command, *common, *checks.elements(ids, "unclear")])
        for field, command, make in (("funding", "funding", entries.funding), ("reports", "report", entries.report)):
            item = make()
            flags = [part for key, value in item.items() for part in ("--" + key.replace("_", "-"), value)]
            yield (command + " add", lambda field=field, item=item: cp.add_profile_entry(
                self.root, checks.A, field, item, today=TODAY),
                [command, "add", "--agency", checks.A, *flags])
            yield (command + " remove", lambda field=field: cp.remove_profile_entry(
                self.root, checks.A, field, "1", today=TODAY),
                [command, "remove", "--agency", checks.A, "--index", "1"])
        yield ("transparency", lambda: cp.add_transparency_snapshot(self.root, checks.A, DAY, {}, today=TODAY),
               ["transparency", "add", *common])

    def test_real_overdeep_list_text_json_and_api_refuse(self):
        for raw in (deep_value(), deep_value(True),
                    b'[{"agency_id":"ca-alpha-pd","profile":{"unknown":' + deep_value() + b'}}]',
                    b'[{"selected":false,"unknown":' + deep_value(True) + b'}]'):
            with self.subTest(shape=raw[:40]):
                self.assertLess(len(raw), MAX_JSON_BYTES)
                with self.assertRaises(RecursionError):
                    json.loads(raw)  # prove genuine decoder refusal on this interpreter
                self.target.write_bytes(raw)
                before = self.snapshot()
                self.assert_depth(lambda: cp.list_profile_checks(self.root), READ_ERROR)
                for flags in ((), ("--json",)):
                    self.assertEqual(self.command("list", *flags, subprocess_cli=True),
                                     (1, "", "Stopped: " + READ_ERROR + "\n"))
                self.assertEqual(self.snapshot(), before)
                self.assertFalse((self.base / "registry.json").exists())

    def test_every_real_mutation_family_refuses_before_stage(self):
        before = self.snapshot()
        with mock.patch.object(cp, "_install", side_effect=AssertionError("install reached")):
            for name, call, args in self.mutations():
                with self.subTest(command=name):
                    self.assert_depth(call, WRITE_ERROR)
                    self.assertEqual(self.command(*args), (1, "", "Stopped: " + WRITE_ERROR + "\n"))
                    self.assertEqual(self.snapshot(), before)

    def test_injected_decoder_fault_is_private_at_both_parse_sites(self):
        self.target.write_bytes(b"[]")
        with mock.patch.object(cp, "_parse_target", side_effect=RecursionError(CANARY)):
            self.assert_depth(lambda: cp.list_profile_checks(self.root), READ_ERROR)
            change = mock.Mock(side_effect=AssertionError("change reached"))
            for verb in ("recheck", "write", "entry change"):
                self.assert_depth(lambda: cp._transaction(self.root, change, verb), WRITE_ERROR)
            change.assert_not_called()

    def test_non_decoder_recursion_is_not_relabelled(self):
        self.target.write_bytes(b"[]")
        error = RecursionError(CANARY)
        for name, call in (("_read_target", lambda: cp.list_profile_checks(self.root)),
                           ("_read_target", lambda: cp._transaction(self.root, mock.Mock(), "write")),
                           ("load_agencies", lambda: cp.list_profile_checks(self.root))):
            with self.subTest(seam=name), mock.patch.object(cp, name, side_effect=error):
                with self.assertRaises(RecursionError) as caught:
                    call()
                self.assertIs(caught.exception, error)
        with self.assertRaises(RecursionError) as caught:
            cp._transaction(self.root, mock.Mock(side_effect=error), "write")
        self.assertIs(caught.exception, error)
        with mock.patch.object(cp.json, "dumps", side_effect=error):
            with self.assertRaises(RecursionError) as caught:
                cp._transaction(self.root, lambda doc: ({"_document": doc}, True), "write")
        self.assertIs(caught.exception, error)
        with mock.patch.object(cp, "_admit_document", side_effect=error):
            self.target.write_text(json.dumps(checks.agencies()), encoding="utf-8")
            with self.assertRaises(RecursionError) as caught:
                cp.write_policy(self.root, checks.B, url=checks.URL_A, source_url=checks.URL_B,
                                checked_on=DAY, today=TODAY)
        self.assertIs(caught.exception, error)

    def test_real_depth_closes_owned_fds_preserves_campaign_and_never_stages(self):
        before = self.snapshot()
        real_open = os.open
        with self.target.open("rb") as sentinel:
            for mutation in (False, True) * 3:
                opened = []

                def track(*args, **kwargs):
                    self.assertFalse(args[1] & os.O_CREAT, "staging allocation")
                    fd = real_open(*args, **kwargs)
                    opened.append(fd)
                    return fd

                with ExitStack() as stack:
                    stack.enter_context(mock.patch.object(cp.os, "open", side_effect=track))
                    for name in ("chmod", "fchmod", "replace"):
                        stack.enter_context(mock.patch.object(cp.os, name, side_effect=AssertionError(name)))
                    stack.enter_context(mock.patch.object(cp, "_install", side_effect=AssertionError("install")))
                    change = mock.Mock(side_effect=AssertionError("callback"))
                    call = (lambda: cp._transaction(self.root, change, "write")) if mutation else (
                        lambda: cp.list_profile_checks(self.root))
                    self.assert_depth(call, WRITE_ERROR if mutation else READ_ERROR)
                    change.assert_not_called()
                self.assertEqual(len(opened), 3)
                for fd in opened:
                    with self.assertRaises(OSError) as caught:
                        os.fstat(fd)
                    self.assertEqual(caught.exception.errno, errno.EBADF)
                os.fstat(sentinel.fileno())
                self.assertEqual(self.snapshot(), before)

    def test_actual_byte_overflow_still_wins_before_parser(self):
        self.target.write_bytes(deep_value() + b" " * MAX_JSON_BYTES)
        before = self.snapshot()
        with mock.patch.object(cp, "_parse_target", side_effect=AssertionError("parser reached")):
            for call in (lambda: cp.list_profile_checks(self.root),
                         lambda: cp._transaction(self.root, mock.Mock(), "write")):
                with self.assertRaisesRegex(ContentError, "file too large"):
                    call()
        self.assertEqual(self.snapshot(), before)

    def test_operand_and_argparse_refusals_still_precede_depth(self):
        before = self.snapshot()
        for args, status, needle in ((["recheck", "--agency", checks.A, "--field", "alpr_policy",
                                      "--checked-on", "2999-01-01"], 1, "must not be after today"),
                                     (["policy-check", "--agency", checks.A, "--checked-on", DAY],
                                      1, "Missing element ids"),
                                     (["policy"], 2, "required")):
            result = self.command(*args)
            self.assertEqual(result[:2], (status, ""))
            self.assertIn(needle, result[2])
            self.assertNotIn("nesting", result[2])
        self.assertEqual(self.snapshot(), before)

    def test_decodable_metadata_empty_and_missing_inputs_are_unchanged(self):
        for raw in (b"[]", json.dumps([{"agency_id": checks.A, "name": "Fictional Ü Agency", "selected": False,
                                       "profile": {"unknown": [[["Ü"]]]}}], ensure_ascii=False).encode(),
                    json.dumps([{"agency_id": checks.A, "name": "Fictional Ü Agency", "kind": "police",
                                 "profile": {"status": "Ünïcode"}}]).encode()):
            self.target.write_bytes(raw)
            before = self.snapshot()
            self.assertEqual(cp.list_profile_checks(self.root), [])
            self.assertEqual(self.command("list"), (0, "No recorded profile checks.\n", ""))
            self.assertEqual(self.snapshot(), before)
        for raw in (b"{}", b"[null]", b"{", b"\xff"):
            self.target.write_bytes(raw)
            with self.assertRaises(ContentError) as caught:
                cp.list_profile_checks(self.root)
            self.assertNotIn("nesting", str(caught.exception))
        self.target.unlink()
        self.assertEqual(cp.list_profile_checks(self.root), [])
        self.target.parent.rmdir()
        self.assertEqual(cp.list_profile_checks(self.root), [])

    def test_real_successful_transactions_and_noop_keep_storage_contract(self):
        document = transparency.fixture()
        self.target.write_text(json.dumps(document), encoding="utf-8")
        cp.write_policy(self.root, checks.A, url=checks.URL_A, source_url=checks.URL_B,
                        checked_on=DAY, today=TODAY)
        expected_policy = {"url": checks.URL_A, "source_url": checks.URL_B, "checked_on": DAY}
        elements = {key: {"status": "unclear"} for key in checks.POLICY_IDS}
        cp.write_policy_check(self.root, checks.A, checked_on=DAY, elements=elements, today=TODAY)
        cp.add_profile_entry(self.root, checks.A, "funding", entries.funding(), today=TODAY)
        cp.add_transparency_snapshot(self.root, checks.A, DAY, {"searches_last_30_days": 0}, today=TODAY)
        stored = json.loads(self.target.read_bytes())
        expected = dict(document[0]["profile"], alpr_policy=expected_policy,
                        policy_check={"checked_on": DAY, "policy_url": checks.URL_A, "elements": elements},
                        funding=[entries.funding()], transparency=[{"checked_on": DAY,
                        "source_url": transparency.PORTAL + "alpha-flock", "searches_last_30_days": 0}])
        self.assertEqual(stored[0]["profile"], expected)
        self.assertEqual(stored[1:], document[1:])
        self.assertEqual(self.target.read_bytes(), (json.dumps(stored, indent=2, ensure_ascii=False) + "\n").encode())
        before = self.snapshot()
        result = cp.recheck_profile(self.root, checks.A, "alpr_policy", DAY, today=TODAY)
        self.assertEqual(result, {"agency_id": checks.A, "field": "alpr_policy", "index": None,
                                  "old": DAY, "new": DAY, "changed": False})
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
