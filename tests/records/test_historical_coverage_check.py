"""Synthetic tests for tools/historical_coverage_check.py (card t_177ff79b, M-26 checker).

Every diff and attestation used here is synthetic and generated at test time in
a scratch directory; the committed fixture only lists populations. A synthetic
attestation stands in for a custodian document so the binding rule can be
tested; the checker itself never produces or fills one.
"""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import unittest


REPO = Path(__file__).resolve().parents[2]
TOOL_PATH = REPO / "tools" / "historical_coverage_check.py"
FIXTURE = REPO / "tests" / "fixtures" / "n3_historical_check" / "scenarios.json"
SCRATCH_ENV = "N3_HC_SCRATCH"
DELETION_WORDING = re.compile(r"delet|remov|expung|purg|erase|discard|complete", re.I)


def load_tool():
    spec = importlib.util.spec_from_file_location("n3_historical_coverage_check", TOOL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hcc = load_tool()
SPEC = json.loads(FIXTURE.read_text(encoding="utf-8"))


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def record(root, uid, variant="v1"):
    seed = f"{root}:{uid}:{variant}"
    return {
        "identity": [SPEC["account"], f"synthetic-folder-{root}", SPEC["uidvalidity"], str(uid)],
        "original": {"sha256": digest(seed), "bytes": 100 + uid},
        "attachments": [{"part": "2", "sha256": digest(seed + ":part2"), "bytes": 10 + uid}],
    }


def build_diff(name):
    population = SPEC["scenarios"][name]
    added = [record(name, uid) for uid in population["added"]]
    removed = [record(name, uid) for uid in population["removed"]]
    changed = [{"identity": record(name, uid)["identity"], "before": record(name, uid),
                "after": record(name, uid, "v2")} for uid in population["changed"]]
    return {
        "schema": "offline-mail-diff-v1",
        "review_required": True,
        "added": added,
        "removed": removed,
        "changed": changed,
        "counts": {"added": len(added), "removed": len(removed), "changed": len(changed),
                   "unchanged": len(population["unchanged"])},
        "limits": "Synthetic limits text copied in shape only.",
    }


def encode(document):
    return (json.dumps(document, sort_keys=True, indent=2) + "\n").encode("utf-8")


def attestation(label, diff_bytes, **overrides):
    document = {
        "schema": "historical-root-attestation",
        "version": 1,
        "root_label": label,
        "layer": "L-B1",
        "diff_sha256": hashlib.sha256(diff_bytes).hexdigest(),
        "inventory_sha256": None,
        "root_manifest_sha256": None,
        "capture_time_utc": "2026-01-02T03:04:05+00:00",
        "count_unit": "occurrences",
        "producer_statement_sha256": digest("synthetic producer statement " + label),
        "attested_by_role": "custodian",
        "status": "ATTESTED",
    }
    document.update(overrides)
    return document


class HistoricalCoverageCheckTest(unittest.TestCase):

    def setUp(self):
        base = os.environ.get(SCRATCH_ENV) or tempfile.gettempdir()
        self.tmp = Path(tempfile.mkdtemp(prefix="hcc-", dir=base)).resolve()

    def _put(self, name, data=None, link_to=None):
        """The only place this suite writes; every target is inside self.tmp."""
        target = self.tmp / name
        if link_to is not None:
            target.symlink_to(link_to)
        else:
            target.write_bytes(data)
        return target

    def put_diff(self, scenario, document=None, name=None):
        raw = encode(document if document is not None else build_diff(scenario))
        return self._put(name or f"diff-{scenario}.json", raw), raw

    def put_attestation(self, label, diff_raw, name=None, **overrides):
        raw = encode(attestation(label, diff_raw, **overrides))
        return self._put(name or f"attestation-{label}.json", raw)

    def run_tool(self, diffs, attestations=(), output=None):
        output = output or (self.tmp / "verdict.json")
        argv = []
        for path in diffs:
            argv += ["--diff", str(path)]
        for path in attestations:
            argv += ["--attestation", str(path)]
        argv += ["--output", str(output)]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = hcc.main(argv)
        return code, out.getvalue(), err.getvalue(), output

    def assert_refused(self, code, out, err, output, expected):
        self.assertEqual(code, 2)
        self.assertEqual(out, "", "a refusal prints no report")
        self.assertFalse(output.exists(), "a refusal writes no output")
        self.assertEqual(json.loads(err), {"status": "refused", "code": expected})

    def report(self, output):
        return json.loads(output.read_text(encoding="utf-8"))

    def test_historical_only_retained_in_denominator(self):
        diff, raw = self.put_diff("retained")
        att = self.put_attestation("root-alpha", raw)
        code, out, err, output = self.run_tool([diff], [att])
        report = self.report(output)
        root = report["roots"][0]
        self.assertEqual(root["counts"]["historical_denominator"], 2 + 0 + 3,
                         "RETENTION_DENOMINATOR: historical-only occurrences left the denominator")
        self.assertEqual(root["counts"]["historical_only"], 2)
        self.assertEqual(root["counts"]["added"], 1)
        self.assertEqual(root["counts"]["unchanged"], 3)
        self.assertEqual(root["counts"]["content_conflict"], 0)
        self.assertEqual(root["verdict"], "COVERED")
        self.assertEqual(root["root_label"], "root-alpha")
        self.assertEqual(root["capture_time_utc"], "2026-01-02T03:04:05+00:00")
        self.assertEqual(report["overall_verdict"], "COVERED")
        self.assertEqual(report["totals"]["historical_denominator"], 5)
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual(json.loads(out), report)
        self.assertTrue(report["review_required"])

    def test_changed_occurrence_is_content_conflict(self):
        diff, raw = self.put_diff("conflict")
        att = self.put_attestation("root-beta", raw)
        code, _out, _err, output = self.run_tool([diff], [att])
        root = self.report(output)["roots"][0]
        self.assertEqual(root["counts"]["content_conflict"], 1)
        self.assertEqual(root["counts"]["historical_denominator"], 3)
        self.assertEqual(root["verdict"], "NOT_COVERED_CONFLICT")
        self.assertEqual(self.report(output)["overall_verdict"], "NOT_COVERED_CONFLICT")
        self.assertEqual(code, 1)

    def test_mixed_root_counts_conflict_and_retains_historical_only(self):
        diff, raw = self.put_diff("mixed")
        plain, plain_raw = self.put_diff("plain")
        atts = [self.put_attestation("root-mixed", raw), self.put_attestation("root-plain", plain_raw)]
        code, _out, _err, output = self.run_tool([diff, plain], atts)
        report = self.report(output)
        by_label = {r["root_label"]: r for r in report["roots"]}
        self.assertEqual(by_label["root-mixed"]["counts"]["historical_only"], 1)
        self.assertEqual(by_label["root-mixed"]["counts"]["historical_denominator"], 3)
        self.assertEqual(by_label["root-mixed"]["verdict"], "NOT_COVERED_CONFLICT")
        self.assertEqual(by_label["root-plain"]["verdict"], "COVERED")
        self.assertEqual(report["overall_verdict"], "NOT_COVERED_CONFLICT")
        self.assertEqual(report["totals"]["historical_denominator"], 5)
        self.assertEqual(code, 1)

    def test_missing_attestation_is_unsupplied_and_blocks_overall(self):
        diff, _raw = self.put_diff("retained")
        code, _out, _err, output = self.run_tool([diff])
        report = self.report(output)
        self.assertEqual(report["roots"][0]["verdict"], "UNSUPPLIED")
        self.assertEqual(report["roots"][0]["attestation"], "MISSING")
        self.assertIsNone(report["roots"][0]["root_label"])
        self.assertEqual(report["roots"][0]["counts"]["historical_denominator"], 5)
        self.assertEqual(report["overall_verdict"], "UNSUPPLIED")
        self.assertEqual(code, 1)
        covered, covered_raw = self.put_diff("plain")
        att = self.put_attestation("root-plain", covered_raw)
        code, _out, _err, output = self.run_tool([covered, diff], [att], self.tmp / "verdict-2.json")
        report = self.report(output)
        verdicts = sorted(r["verdict"] for r in report["roots"])
        self.assertEqual(verdicts, ["COVERED", "UNSUPPLIED"])
        self.assertEqual(report["overall_verdict"], "UNSUPPLIED")
        self.assertEqual(code, 1)

    def test_mismatched_diff_sha256_is_unsupplied(self):
        diff, raw = self.put_diff("retained")
        att = self.put_attestation("root-alpha", raw + b" ")
        code, _out, _err, output = self.run_tool([diff], [att])
        report = self.report(output)
        self.assertEqual(report["roots"][0]["verdict"], "UNSUPPLIED")
        self.assertEqual(report["roots"][0]["attestation"], "MISSING")
        self.assertEqual([a["root_label"] for a in report["unbound_attestations"]], ["root-alpha"])
        self.assertEqual(report["overall_verdict"], "UNSUPPLIED")
        self.assertEqual(code, 1)

    def test_attestation_for_unsupplied_diff_keeps_overall_unsupplied(self):
        diff, raw = self.put_diff("plain")
        _other, other_raw = self.put_diff("retained")
        atts = [self.put_attestation("root-plain", raw), self.put_attestation("root-missing", other_raw)]
        code, _out, _err, output = self.run_tool([diff], atts)
        report = self.report(output)
        self.assertEqual(report["roots"][0]["verdict"], "COVERED")
        self.assertEqual(report["overall_verdict"], "UNSUPPLIED")
        self.assertEqual(code, 1)

    def test_header_counts_must_equal_list_lengths(self):
        for key in ("added", "removed", "changed"):
            document = build_diff("mixed")
            document["counts"][key] += 1
            diff, _raw = self.put_diff("mixed", document, f"diff-count-{key}.json")
            result = self.run_tool([diff], output=self.tmp / f"out-{key}.json")
            self.assert_refused(*result, "diff_counts_mismatch")
        for bad in (-1, True, 1.0, "1"):
            document = build_diff("mixed")
            document["counts"]["unchanged"] = bad
            diff, _raw = self.put_diff("mixed", document, f"diff-unchanged-{bad!r}.json")
            result = self.run_tool([diff], output=self.tmp / f"out-unchanged-{bad!r}.json")
            self.assert_refused(*result, "diff_counts_shape")

    def test_wrong_diff_schema_is_typed_refusal(self):
        cases = {
            "offline-mail-diff-v2": "diff_schema",
            "offline-mail-inventory-v1": "diff_schema",
            None: "diff_schema",
        }
        for index, (schema, expected) in enumerate(cases.items()):
            document = build_diff("plain")
            if schema is None:
                document.pop("schema")
            else:
                document["schema"] = schema
            diff, _raw = self.put_diff("plain", document, f"diff-schema-{index}.json")
            result = self.run_tool([diff], output=self.tmp / f"out-schema-{index}.json")
            self.assert_refused(*result, expected)
        document = build_diff("plain")
        document["version"] = 1
        diff, _raw = self.put_diff("plain", document, "diff-version.json")
        self.assert_refused(*self.run_tool([diff], output=self.tmp / "out-version.json"), "diff_keys")
        document = build_diff("plain")
        document["review_required"] = False
        diff, _raw = self.put_diff("plain", document, "diff-review.json")
        self.assert_refused(*self.run_tool([diff], output=self.tmp / "out-review.json"), "diff_review_required")
        diff = self._put("diff-dupkey.json", b'{"schema": "offline-mail-diff-v1", "schema": "offline-mail-diff-v1"}')
        self.assert_refused(*self.run_tool([diff], output=self.tmp / "out-dupkey.json"), "json_invalid")
        diff = self._put("diff-text.json", b"not json")
        self.assert_refused(*self.run_tool([diff], output=self.tmp / "out-text.json"), "json_invalid")

    def test_wrong_attestation_schema_or_version_is_typed_refusal(self):
        diff, raw = self.put_diff("plain")
        cases = [
            ({"schema": "historical-root-attestation-v2"}, "attestation_schema"),
            ({"version": 2}, "attestation_version"),
            ({"version": True}, "attestation_version"),
            ({"version": "1"}, "attestation_version"),
            ({"layer": "L-B2"}, "attestation_layer"),
            ({"status": "PROPOSED"}, "attestation_status"),
            ({"attested_by_role": "model"}, "attestation_role"),
            ({"root_label": "Root Alpha"}, "attestation_root_label"),
            ({"diff_sha256": "ABC"}, "attestation_digest"),
            ({"inventory_sha256": "xyz"}, "attestation_digest"),
            ({"capture_time_utc": "2026-01-02T03:04:05"}, "attestation_capture_time"),
            ({"count_unit": "messages"}, "attestation_count_unit"),
            ({"extra": 1}, "attestation_keys"),
        ]
        for index, (override, expected) in enumerate(cases):
            att = self.put_attestation("root-plain", raw, f"att-{index}.json", **override)
            result = self.run_tool([diff], [att], self.tmp / f"out-att-{index}.json")
            self.assert_refused(*result, expected)

    def test_record_shape_and_identity_overlap_refused(self):
        document = build_diff("mixed")
        document["changed"][0]["after"] = document["changed"][0]["before"]
        diff, _raw = self.put_diff("mixed", document, "diff-same.json")
        self.assert_refused(*self.run_tool([diff], output=self.tmp / "o1.json"), "diff_changed_not_different")
        document = build_diff("mixed")
        document["added"] = [document["removed"][0]]
        document["counts"]["added"] = 1
        diff, _raw = self.put_diff("mixed", document, "diff-overlap.json")
        self.assert_refused(*self.run_tool([diff], output=self.tmp / "o2.json"), "diff_identity_overlap")
        document = build_diff("mixed")
        document["removed"][0]["identity"][3] = "0"
        diff, _raw = self.put_diff("mixed", document, "diff-uid.json")
        self.assert_refused(*self.run_tool([diff], output=self.tmp / "o3.json"), "diff_record_shape")

    def test_binding_ambiguity_refused(self):
        diff, raw = self.put_diff("plain")
        result = self.run_tool([diff, diff], output=self.tmp / "o-dup.json")
        self.assert_refused(*result, "duplicate_diff")
        a1 = self.put_attestation("root-one", raw, "a1.json")
        a2 = self.put_attestation("root-two", raw, "a2.json")
        self.assert_refused(*self.run_tool([diff], [a1, a2], self.tmp / "o-bind.json"),
                            "attestation_duplicate_binding")
        _other, other_raw = self.put_diff("retained")
        a3 = self.put_attestation("root-one", other_raw, "a3.json")
        self.assert_refused(*self.run_tool([diff], [a1, a3], self.tmp / "o-label.json"),
                            "attestation_duplicate_label")

    def test_argv_is_exact_and_output_is_new(self):
        diff, raw = self.put_diff("plain")
        existing = self._put("existing.json", b"keep\n")
        out, err = io.StringIO(), io.StringIO()
        bad_argvs = [
            ["--diff", "relative.json", "--output", str(self.tmp / "n1.json")],
            ["--diff", str(diff)],
            ["--diff", str(diff), "--out", str(self.tmp / "n2.json")],
            ["--dif", str(diff), "--output", str(self.tmp / "n3.json")],
            ["--diff", str(diff), "--output", "relative-out.json"],
            ["--diff", str(diff), "--output", str(self.tmp / "n4.json"), "--extra"],
            ["--output", str(self.tmp / "n5.json")],
        ]
        for argv in bad_argvs:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                self.assertEqual(hcc.main(argv), 2)
        self.assertEqual(out.getvalue(), "")
        for name in ("n1.json", "n2.json", "n3.json", "n4.json", "n5.json"):
            self.assertFalse((self.tmp / name).exists())
        result = self.run_tool([diff], output=existing)
        self.assertEqual(result[0], 2)
        self.assertEqual(existing.read_bytes(), b"keep\n")
        self.assertEqual(json.loads(result[2])["code"], "output_exists")
        result = self.run_tool([diff], output=diff)
        self.assertEqual(json.loads(result[2])["code"], "output_exists")
        self.assertEqual(diff.read_bytes(), raw)
        link = self._put("link.json", link_to=diff)
        result = self.run_tool([link], output=self.tmp / "n6.json")
        self.assert_refused(*result, "path_not_canonical")

    def test_output_contains_no_deletion_wording(self):
        texts = []
        for index, scenario in enumerate(("retained", "conflict", "mixed", "plain")):
            diff, raw = self.put_diff(scenario, name=f"wording-{scenario}.json")
            attestations = [self.put_attestation(f"root-{index}", raw, f"wording-att-{index}.json")]
            for suffix, atts in (("bound", attestations), ("missing", [])):
                code, out, err, output = self.run_tool([diff], atts, self.tmp / f"w-{index}-{suffix}.json")
                self.assertIn(code, (0, 1))
                texts += [out, err, output.read_text(encoding="utf-8")]
        joined = "\n".join(texts)
        self.assertGreater(len(joined), 0)
        self.assertIsNone(DELETION_WORDING.search(joined), DELETION_WORDING.findall(joined)[:5])

    def test_output_is_private_and_deterministic(self):
        diff, raw = self.put_diff("retained")
        att = self.put_attestation("root-alpha", raw)
        _code, _out, _err, first = self.run_tool([diff], [att], self.tmp / "d1.json")
        _code, _out, _err, second = self.run_tool([diff], [att], self.tmp / "d2.json")
        self.assertEqual(first.read_bytes(), second.read_bytes())
        self.assertEqual(stat.S_IMODE(first.stat().st_mode), 0o600)
        self.assertNotIn(str(self.tmp), first.read_text(encoding="utf-8"),
                         "the report names diff files by basename only")


if __name__ == "__main__":
    unittest.main()
