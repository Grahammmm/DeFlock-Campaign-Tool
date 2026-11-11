"""RF0211-4: an unparseable header in a listed CSV is a fixed, path-scoped catalogue problem, not a traceback.

Synthetic content only (a temp copy of the fictional example); sockets are blocked, nothing is fetched.
"""
import contextlib
import csv
import hashlib
import io
import subprocess
import sys
import unittest
from unittest import mock

import tests.test_site_data_catalogue as base
from campaign_tool.content import ContentError
from campaign_tool.site_check import check_data_catalogue
from campaign_tool.site_data_catalogue import csv_columns, verify_data_package

REASON = "CSV header cannot be parsed"
CANARY = "CANARYCELL"
CSV_PATHS = ("data/findings.csv", "data/sources.csv", "data/agencies.csv", "data/meetings.csv",
             "data/officials.csv", "data/cameras.csv")


def overlarge_header(quoted=False):
    """A header field one byte past the interpreter's current CSV field limit (default 131072)."""
    field = CANARY + "\u00e9" * (csv.field_size_limit() + 1)
    return ('"' + field + '",id\n' if quoted else field + ",id\n").encode("utf-8")


class CsvHeaderErrorTests(base.Case):
    def setUp(self):
        super().setUp()
        self.add_county()
        body = {"agency_id": base.SHERIFF, "officials": [base.official()]}
        (self.content / "officials.json").write_text(base.json.dumps({"schema_version": 1, "bodies": [body]}), encoding="utf-8")
        base.build_site(self.root, today=base.DAY)
        self.pristine = self.snapshot()

    def corrupt(self, path, data, matching=True):
        """Replace a listed file; with ``matching`` the descriptor and the HTML row agree with the new bytes."""
        old = (self.public / path).read_bytes()
        (self.public / path).write_bytes(data)
        if not matching:
            return
        old_hash, new_hash = hashlib.sha256(old).hexdigest(), hashlib.sha256(data).hexdigest()
        descriptor = self.descriptor()
        for resource in descriptor["resources"]:
            if "data/" + resource["path"] == path:
                resource["bytes"], resource["hash"] = len(data), "sha256:" + new_hash
        (self.public / "data" / "datapackage.json").write_text(base.json.dumps(descriptor, indent=2), encoding="utf-8")
        page = (self.public / "data.html").read_text(encoding="utf-8")
        start = page.index(f'<tr data-path="{path}"')
        end = page.index("</tr>", start)
        row = page[start:end].replace(old_hash, new_hash).replace(f'"{len(old)}"', f'"{len(data)}"').replace(f">{len(old)}<", f">{len(data)}<")
        page = page[:start] + row + page[end:]
        (self.public / "data.html").write_text(page, encoding="utf-8")

    def run_check(self):
        return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "build", "--directory", str(self.root), "--check"],
                              cwd=base.REPO_ROOT, capture_output=True, text=True, timeout=60)

    def test_real_default_limit_header_reports_problem_quoted_and_unquoted(self):
        for quoted in (False, True):
            self.corrupt("data/agencies.csv", overlarge_header(quoted))
            with self.assertRaises(csv.Error):  # a genuine parser failure, not a mock
                csv_columns((self.public / "data/agencies.csv").read_bytes())
            problems = verify_data_package(self.public)
            self.assertEqual(problems, [("data/agencies.csv", REASON)])
            self.assertNotIn(CANARY, repr(problems))
            self.assertNotIn("131072", repr(problems))

    def test_every_registered_csv_family_uses_the_boundary(self):
        listed = [p for p in CSV_PATHS if (self.public / p).exists()]
        self.assertIn("data/cameras.csv", listed)
        self.assertGreaterEqual(len(listed), 5)
        for path in listed:
            with self.subTest(path=path):
                self.corrupt(path, overlarge_header())
                problems = verify_data_package(self.public)
                self.assertEqual(len(problems), 1, problems)
                self.assertIn(path, repr(problems))
                self.assertIn(REASON, repr(problems))
                self.assertNotIn(CANARY, repr(problems))
                self.corrupt(path, self.pristine[path])
                self.assertEqual(verify_data_package(self.public), [])

    def test_stale_metadata_and_later_problems_survive(self):
        self.corrupt("data/agencies.csv", overlarge_header(), matching=False)
        self.corrupt("data/findings.csv", self.pristine["data/findings.csv"] + b"x", matching=False)
        text = repr(verify_data_package(self.public))
        self.assertIn(REASON, text)
        self.assertIn("sha256 changed", text)
        self.assertIn("size changed", text)
        self.assertNotIn("schema does not match", text.split("agencies.csv")[1].split(")")[0])
        # an invalid camera licence on the broken file still reports alongside the header reason
        self.corrupt("data/cameras.csv", overlarge_header())
        descriptor = self.descriptor()
        for resource in descriptor["resources"]:
            if resource["path"] == "cameras.csv":
                resource["licenses"] = []
        (self.public / "data" / "datapackage.json").write_text(base.json.dumps(descriptor, indent=2), encoding="utf-8")
        problems = verify_data_package(self.public)
        self.assertEqual(problems, sorted(set(problems)))
        self.assertTrue(any("cameras.csv" in p[0] and p[1] == REASON for p in problems))
        self.assertTrue(any("cameras.csv" in p[0] and "ODbL" in p[1] for p in problems))
        self.assertTrue(any("findings.csv" in p[0] for p in problems))

    def test_checker_and_real_checked_cli_refuse_before_rebuild(self):
        self.corrupt("data/agencies.csv", overlarge_header())
        with self.assertRaises(ContentError) as raised:
            check_data_catalogue(self.public)
        self.assertIn(REASON, str(raised.exception))
        self.assertNotIn(CANARY, str(raised.exception))
        before = self.snapshot(self.root)
        result = self.run_check()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertNotIn("Traceback", result.stderr)
        self.assertIn(REASON, result.stderr)
        self.assertNotIn(CANARY, result.stderr)
        self.assertEqual(self.snapshot(self.root), before)
        with mock.patch.object(base.site_module, "build_site", side_effect=AssertionError("rebuilt")):
            message, _ = self.check_cli()
        self.assertIn(REASON, message or "")

    def test_header_only_scope_and_utf8_reason_survive(self):
        header = self.pristine["data/agencies.csv"].split(b"\n", 1)[0] + b"\n"
        body = header + (b"x" * (csv.field_size_limit() + 5)) + b",y\n"
        self.corrupt("data/agencies.csv", body)
        self.assertEqual(verify_data_package(self.public), [])  # body fields are never parsed
        self.corrupt("data/agencies.csv", header + b"\xff\xfe\n")
        problems = verify_data_package(self.public)
        self.assertEqual(problems, [("data/agencies.csv", "CSV is not UTF-8")])

    def test_catch_is_local_and_field_policy_is_untouched(self):
        limit = csv.field_size_limit()
        self.corrupt("data/agencies.csv", overlarge_header())
        verify_data_package(self.public)
        self.assertEqual(csv.field_size_limit(), limit)
        try:  # a caller's larger limit is respected, not bypassed
            csv.field_size_limit(limit * 4)
            self.assertEqual(verify_data_package(self.public), [("data/agencies.csv", "schema does not match the file")])
        finally:
            csv.field_size_limit(limit)
        for error in (ValueError("x"), MemoryError()):
            with mock.patch("campaign_tool.site_data_catalogue._expected_schema", side_effect=error):
                with self.assertRaises(type(error)):
                    verify_data_package(self.public)
        with mock.patch("campaign_tool.site_data_catalogue._read_regular", side_effect=csv.Error("elsewhere")):
            with self.assertRaises(csv.Error):
                verify_data_package(self.public)

    def test_healthy_tree_and_explicit_rebuild_remain_compatible(self):
        self.assertEqual(verify_data_package(self.public), [])
        self.assertEqual(self.run_check().returncode, 0)
        self.corrupt("data/agencies.csv", overlarge_header())
        self.assertEqual(self.run_check().returncode, 1)
        with contextlib.redirect_stdout(io.StringIO()):
            base.build_site(self.root, today=base.DAY)
        self.assertEqual(self.snapshot(), self.pristine)
        self.assertEqual(self.run_check().returncode, 0)


if __name__ == "__main__":
    unittest.main()
