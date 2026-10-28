"""BM1-CORRESPONDENCE-JSON: duplicate JSON members at the serialized-header boundary.

Synthetic only. Every raw serialized projection reaches the real decoder in
``campaign_tool.records.correspondence._header_parent``; nothing is decoded to a
dict first. Projections use the pinned ``verified-eml-headers-v1`` schema and
its ``[name, value]`` field-pair vocabulary. No ledger, mailbox, network, model,
filesystem payload or send API is used. Passing output is REVIEW REQUIRED only:
not message authenticity, request fulfillment or legal acceptance.
"""
from copy import deepcopy
import hashlib
import io
import json
import socket
import sqlite3
import subprocess
import sys
import types
import unittest
from unittest import mock

from campaign_tool.records import correspondence
from campaign_tool.records.ledger.store import canonical

SCHEMA = "verified-eml-headers-v1"
INVALID = "header_projection_invalid"
ROOT_ID = "<root@synthetic.invalid>"
CHILD_ID = "<child@synthetic.invalid>"
ANNOTATION = "x-synthetic-annotation"  # An extra member the decoder ignores.

# The single production decision the A4 gate control reverts in an isolated copy.
GATE_LINE = "obj = json.loads(raw, object_pairs_hook=_reject_duplicate_members)"
PERMISSIVE_LINE = "obj = json.loads(raw)"


def digest(label):
    return hashlib.sha256(("FICTIONAL TEST ORIGINAL " + label).encode()).hexdigest()


def member(key, value):
    return json.dumps(key, ensure_ascii=False) + ":" + canonical(value)


def serialize(pairs):
    """JSON object text from ordered (key, value) pairs; repeated keys stay verbatim."""
    return "{" + ",".join(member(key, value) for key, value in pairs) + "}"


def fields_for(message_id, extra=()):
    return ([["Message-ID", message_id]] if message_id else []) + [list(pair) for pair in extra]


def valid_pairs(sha, fields):
    # Same member order canonical() emits for the projection the ledger stores.
    return [("fields", fields), ("parser_defects", []), ("schema", SCHEMA), ("source_original_sha256", sha)]


def message(identity, *, message_id, sha=None, headers=None, extra=(), account="synthetic-a", folder="INBOX"):
    sha = sha or digest(identity)
    if headers is None:
        headers = serialize(valid_pairs(sha, fields_for(message_id, extra)))
    return {"id": identity, "account": account, "folder": folder, "uidvalidity": 1, "uid": 1,
            "message_id": message_id, "eml_sha256": sha, "headers": headers,
            "received_at": "2026-10-13T12:00:00Z", "account_provenance_status": "configured_namespace_unattested"}


def root():
    return message("root", message_id=ROOT_ID)


def child_sha():
    return digest("child")


def child_fields():
    return fields_for(CHILD_ID, [["In-Reply-To", ROOT_ID]])


def child(headers=None):
    return message("child", message_id=CHILD_ID, sha=child_sha(), headers=headers,
                   extra=[["In-Reply-To", ROOT_ID]])


def with_duplicate(key, first, second, *, sha=None, fields=None):
    """Valid child projection whose ``key`` member is declared twice, in order."""
    sha = sha or child_sha()
    fields = child_fields() if fields is None else fields
    pairs = []
    for name, value in valid_pairs(sha, fields):
        if name == key:
            pairs.extend([(name, first), (name, second)])
        else:
            pairs.append((name, value))
    return serialize(pairs)


def bad_value(key):
    """A value that on its own is a different, typed outcome for ``key``."""
    return {"schema": "unsupported-headers-v0", "source_original_sha256": "0" * 64,
            "parser_defects": ["synthetic-defect"], "fields": fields_for(CHILD_ID)}[key]


def good_value(key):
    return dict(valid_pairs(child_sha(), child_fields()))[key]


def nested(depth, duplicate=True):
    """Object of ``depth`` levels whose innermost object repeats a member (or not)."""
    inner = '{"note":"synthetic","note":"synthetic"}' if duplicate else '{"note":"synthetic"}'
    for level in range(depth):
        inner = '{"level' + str(level) + '":' + inner + "}"
    return inner


def with_annotation(text):
    """Valid child projection plus an ignored top-level member holding raw JSON ``text``."""
    pairs = valid_pairs(child_sha(), child_fields())
    return "{" + ",".join(member(k, v) for k, v in pairs) + "," + json.dumps(ANNOTATION) + ":" + text + "}"


def padded_child(total_bytes, prefix=""):
    """Valid child projection padded with an ordinary header pair to an exact UTF-8 size."""
    def text(count):
        return serialize(valid_pairs(child_sha(), child_fields() + [["X-Synthetic-Padding", prefix + "p" * count]]))
    count = total_bytes - len(text(0).encode("utf-8"))
    if count < 0:
        raise ValueError("padding cannot reach the exact size")
    result = text(count)
    if len(result.encode("utf-8")) != total_bytes:
        raise ValueError("padding size mismatch")
    return result


DUPLICATE_KEYS = ("schema", "source_original_sha256", "parser_defects", "fields")


class _Subject(unittest.TestCase):
    """Tests target ``module``; the gate control rebinds it to an isolated copy (no tests here)."""
    module = correspondence

    def parent(self, mail):
        return self.module._header_parent(mail)

    def threads(self, messages):
        gaps = []
        candidates = self.module._threads(messages, gaps)
        return candidates, gaps

    def candidate(self, mail_id="child", parents=("root",)):
        return {"mail_id": mail_id, "parent_mail_ids": sorted(parents), "basis": "declared_reply_header",
                "status": "unverified_candidate"}

    def assert_rejected(self, headers):
        bad = child(headers)
        self.assertEqual(self.parent(bad), (None, INVALID))
        candidates, gaps = self.threads([root(), bad])
        self.assertEqual(candidates, [])
        self.assertEqual(gaps, [{"code": INVALID, "mail_id": "child"}])


class PositiveHeaderTests(_Subject, unittest.TestCase):
    """A1: legitimate projections keep their pinned outcomes."""

    def test_serializer_matches_canonical_ledger_projection(self):
        projection = {"schema": SCHEMA, "source_original_sha256": child_sha(), "parser_defects": [],
                      "fields": child_fields()}
        self.assertEqual(child()["headers"], canonical(projection))

    def test_valid_root_and_child_form_one_unverified_candidate(self):
        self.assertEqual(self.parent(root()), (None, None))
        self.assertEqual(self.parent(child()), (ROOT_ID, None))
        candidates, gaps = self.threads([root(), child()])
        self.assertEqual(candidates, [self.candidate()])
        self.assertEqual(gaps, [])

    def test_no_reply_header_has_no_parent_and_no_gap(self):
        mail = message("plain", message_id="<plain@synthetic.invalid>", extra=[["Subject", "Re: Fictional"]])
        self.assertEqual(self.parent(mail), (None, None))
        self.assertEqual(self.threads([mail]), ([], []))

    def test_duplicate_delivery_keeps_every_parent_delivery(self):
        inbox = root()
        archive = dict(root(), id="root-archive", folder="Archive")
        candidates, gaps = self.threads([inbox, archive, child()])
        self.assertEqual(candidates, [self.candidate(parents=("root", "root-archive"))])
        self.assertEqual(gaps, [])

    def test_unsupported_schema_keeps_its_typed_gap(self):
        sha = child_sha()
        headers = serialize([("fields", child_fields()), ("parser_defects", []),
                             ("schema", "unsupported-headers-v0"), ("source_original_sha256", sha)])
        self.assertEqual(self.parent(child(headers)), (None, "thread_headers_unsupported"))

    def test_distinct_repeated_header_pairs_are_not_duplicate_members(self):
        extra = [["Received", "from synthetic-a"], ["Received", "from synthetic-b"], ["In-Reply-To", ROOT_ID]]
        mail = message("child", message_id=CHILD_ID, sha=child_sha(), extra=extra)
        self.assertEqual(self.parent(mail), (ROOT_ID, None))
        for name, pairs in (("in-reply-to", [["In-Reply-To", ROOT_ID], ["in-reply-to", ROOT_ID]]),
                            ("references", [["References", ROOT_ID], ["References", ROOT_ID]])):
            with self.subTest(name=name):
                mail = message("child", message_id=CHILD_ID, sha=child_sha(), extra=pairs)
                self.assertEqual(self.parent(mail), (None, "ambiguous_reply_headers"))

    def test_reference_chain_uses_last_parent(self):
        mail = message("child", message_id=CHILD_ID, sha=child_sha(),
                       extra=[["References", "<older@synthetic.invalid> " + ROOT_ID]])
        self.assertEqual(self.parent(mail), (ROOT_ID, None))

    def test_message_id_collision_is_not_a_thread(self):
        first = message("root-1", message_id=ROOT_ID)
        second = message("root-2", message_id=ROOT_ID)
        candidates, gaps = self.threads([first, second, child()])
        self.assertEqual(candidates, [])
        self.assertEqual(gaps, [{"code": "message_id_collision", "mail_id": "child"}])


class DuplicateMemberTests(_Subject, unittest.TestCase):
    """A2: any repeated JSON member at this boundary is header_projection_invalid."""

    def test_conflicting_parser_defects_valid_last_is_invalid(self):
        # The measured premise: a nonempty declaration silently replaced by [].
        self.assert_rejected(with_duplicate("parser_defects", ["synthetic-defect"], []))

    def test_conflicting_duplicates_each_order_are_invalid(self):
        for key in DUPLICATE_KEYS:
            for order, (first, second) in (("valid_last", (bad_value(key), good_value(key))),
                                           ("valid_first", (good_value(key), bad_value(key)))):
                with self.subTest(key=key, order=order):
                    self.assert_rejected(with_duplicate(key, first, second))

    def test_equal_value_duplicates_are_invalid(self):
        for key in DUPLICATE_KEYS:
            with self.subTest(key=key):
                self.assert_rejected(with_duplicate(key, good_value(key), good_value(key)))

    def test_ignored_nested_object_duplicate_is_invalid(self):
        for name, text in (("equal", '{"note":"synthetic","note":"synthetic"}'),
                           ("conflicting", '{"note":"a","note":"b"}')):
            with self.subTest(name=name):
                self.assert_rejected(with_annotation(text))

    def test_escaped_key_duplicate_is_invalid(self):
        text = with_duplicate("schema", SCHEMA, SCHEMA).replace('"schema"', '"\\u0073chema"', 1)
        self.assertEqual(text.count('"\\u0073chema"'), 1)
        self.assert_rejected(text)

    def test_duplicate_child_contributes_no_candidate_through_threads(self):
        bad = dict(child(with_duplicate("parser_defects", ["synthetic-defect"], [])), id="child-duplicate")
        candidates, gaps = self.threads([root(), child(), bad])
        self.assertEqual(candidates, [self.candidate()])
        self.assertEqual(gaps, [{"code": INVALID, "mail_id": "child-duplicate"}])

    def test_duplicate_root_projection_contributes_no_parent(self):
        sha = digest("root")
        headers = serialize([("fields", fields_for(ROOT_ID)), ("parser_defects", []), ("schema", SCHEMA),
                             ("schema", SCHEMA), ("source_original_sha256", sha)])
        bad_root = message("root", message_id=ROOT_ID, sha=sha, headers=headers)
        self.assertEqual(self.parent(bad_root), (None, INVALID))
        candidates, gaps = self.threads([bad_root, child()])
        self.assertEqual(candidates, [])
        self.assertEqual(gaps, [{"code": INVALID, "mail_id": "root"},
                                {"code": "thread_parent_missing", "mail_id": "child"}])

    def test_controls_without_duplicates_are_valid(self):
        # Isolates the deciding gate: each single declaration and the ignored
        # member itself are accepted; only repetition is rejected.
        self.assertEqual(self.parent(child(with_annotation(nested(3, duplicate=False)))), (ROOT_ID, None))
        for key in DUPLICATE_KEYS:
            with self.subTest(key=key):
                pairs = [(name, good_value(name) if name != key else good_value(key))
                         for name, _ in valid_pairs(child_sha(), child_fields())]
                self.assertEqual(self.parent(child(serialize(pairs))), (ROOT_ID, None))

    def test_prior_typed_outcomes_are_preserved(self):
        sha = child_sha()
        cases = [
            ("invalid_json", '{"schema":', INVALID),
            ("not_an_object", "[]", "thread_headers_unsupported"),
            ("missing_fields", serialize([("parser_defects", []), ("schema", SCHEMA),
                                          ("source_original_sha256", sha)]), INVALID),
            ("fields_not_pairs", serialize([("fields", [["Message-ID"]]), ("parser_defects", []),
                                            ("schema", SCHEMA), ("source_original_sha256", sha)]), INVALID),
            ("source_hash_mismatch", serialize(valid_pairs("0" * 64, child_fields())), INVALID),
            ("parser_defects", serialize([("fields", child_fields()), ("parser_defects", ["synthetic-defect"]),
                                          ("schema", SCHEMA), ("source_original_sha256", sha)]), INVALID),
        ]
        for name, headers, code in cases:
            with self.subTest(name=name):
                self.assertEqual(self.parent(child(headers)), (None, code))
        self.assertEqual(self.parent(child(headers=None) | {"headers": None}), (None, INVALID))
        self.assertEqual(self.parent(child(headers=None) | {"headers": b"{}"}), (None, INVALID))


class BoundaryReplayTests(_Subject, unittest.TestCase):
    """A3: byte boundary, depth, replay independence, purity and no side effects."""

    def test_exact_max_header_bytes_is_accepted_and_one_over_is_invalid(self):
        limit = correspondence.MAX_HEADER_BYTES
        exact = padded_child(limit)
        self.assertEqual(len(exact.encode("utf-8")), limit)
        self.assertEqual(self.parent(child(exact)), (ROOT_ID, None))
        over = padded_child(limit + 1)
        self.assertEqual(len(over.encode("utf-8")), limit + 1)
        self.assertEqual(self.parent(child(over)), (None, INVALID))
        # UTF-8 bytes, not characters, are counted: a two-byte character at the edge.
        wide = padded_child(limit, prefix="\u00e9")
        self.assertEqual(len(wide.encode("utf-8")), limit)
        self.assertEqual(self.parent(child(wide)), (ROOT_ID, None))
        wider = padded_child(limit + 1, prefix="\u00e9")
        self.assertEqual(len(wider.encode("utf-8")), limit + 1)
        self.assertEqual(len(wider), limit)
        self.assertEqual(self.parent(child(wider)), (None, INVALID))

    def test_duplicate_at_exact_max_header_bytes_is_invalid(self):
        limit = correspondence.MAX_HEADER_BYTES
        clean = padded_child(limit - len(',"schema":"' + SCHEMA + '"'))
        text = clean[:-1] + ',"schema":"' + SCHEMA + '"}'
        self.assertEqual(len(text.encode("utf-8")), limit)
        self.assertEqual(self.parent(child(clean)), (ROOT_ID, None))
        self.assert_rejected(text)

    def test_duplicates_at_every_depth_are_invalid(self):
        self.assert_rejected(with_duplicate("schema", SCHEMA, SCHEMA))
        for depth in (0, 1, 3, 8):
            with self.subTest(depth=depth):
                self.assertEqual(self.parent(child(with_annotation(nested(depth, duplicate=False)))), (ROOT_ID, None))
                self.assert_rejected(with_annotation(nested(depth)))

    def test_repeated_clean_and_invalid_calls_are_independent(self):
        bad = child(with_duplicate("parser_defects", ["synthetic-defect"], []))
        expected_threads = ([self.candidate()], [])
        for _ in range(5):
            self.assertEqual(self.parent(child()), (ROOT_ID, None))
            self.assertEqual(self.parent(bad), (None, INVALID))
            self.assertEqual(self.threads([root(), child()]), expected_threads)
            self.assertEqual(self.threads([root(), bad]), ([], [{"code": INVALID, "mail_id": "child"}]))

    def test_inputs_are_not_mutated(self):
        bad = child(with_duplicate("fields", child_fields(), child_fields()))
        messages = [root(), child(), bad]
        before = deepcopy(messages)
        for mail in messages:
            self.parent(mail)
        self.threads(messages)
        self.assertEqual(messages, before)

    def test_no_db_mailbox_filesystem_network_model_or_send_api(self):
        import builtins
        import imaplib
        import os
        import pathlib
        import smtplib
        import urllib.request
        calls = []

        def sentinel(name):
            def refuse(*_args, **_kwargs):
                calls.append(name)
                raise AssertionError("side effect attempted: " + name)
            return refuse

        targets = [(self.module, "ledger"), (self.module, "snapshot"), (self.module, "checked_path"),
                   (self.module, "load_review"), (sqlite3, "connect"), (socket.socket, "connect"),
                   (socket, "create_connection"), (socket, "getaddrinfo"), (builtins, "open"), (io, "open"),
                   (os, "open"), (pathlib.Path, "open"), (subprocess, "Popen"), (smtplib, "SMTP"),
                   (smtplib, "SMTP_SSL"), (imaplib, "IMAP4"), (imaplib, "IMAP4_SSL"), (urllib.request, "urlopen")]
        bad = child(with_duplicate("parser_defects", ["synthetic-defect"], []))
        modules_before = set(sys.modules)
        patches = [mock.patch.object(owner, name, sentinel(owner.__name__ + "." + name)) for owner, name in targets]
        for patcher in patches:
            patcher.start()
        try:
            results = [self.parent(root()), self.parent(child()), self.parent(bad),
                       self.threads([root(), child(), bad])]
        finally:
            for patcher in reversed(patches):
                patcher.stop()
        self.assertEqual(calls, [])
        self.assertEqual(set(sys.modules) - modules_before, set())
        self.assertEqual(results[2], (None, INVALID))


# Pinned outcomes measured through the unchanged module at the staged base.
PINNED = {
    "ordinary_valid": ((ROOT_ID, None), [{"mail_id": "child", "parent_mail_ids": ["root"],
                                         "basis": "declared_reply_header", "status": "unverified_candidate"}], []),
    "unsupported_schema": ((None, "thread_headers_unsupported"), [],
                           [{"code": "thread_headers_unsupported", "mail_id": "child"}]),
    "missing_message_id": ((None, "message_id_missing_or_ambiguous"), [],
                           [{"code": "message_id_missing_or_ambiguous", "mail_id": "child"}]),
    "conflicting_header": ((None, "ambiguous_reply_headers"), [],
                           [{"code": "ambiguous_reply_headers", "mail_id": "child"}]),
    "source_hash_mismatch": ((None, INVALID), [], [{"code": INVALID, "mail_id": "child"}]),
}


def compatibility_cases():
    sha = child_sha()
    missing = message("child", message_id=None, sha=sha, extra=[["In-Reply-To", ROOT_ID]])
    return {
        "ordinary_valid": child(),
        "unsupported_schema": child(serialize([("fields", child_fields()), ("parser_defects", []),
                                               ("schema", "unsupported-headers-v0"),
                                               ("source_original_sha256", sha)])),
        "missing_message_id": missing,
        "conflicting_header": message("child", message_id=CHILD_ID, sha=sha,
                                      extra=[["In-Reply-To", ROOT_ID], ["In-Reply-To", "<other@synthetic.invalid>"]]),
        "source_hash_mismatch": child(serialize(valid_pairs("0" * 64, child_fields()))),
    }


def observe(module, mail):
    gaps = []
    candidates = module._threads([root(), mail], gaps)
    return module._header_parent(mail), candidates, gaps


class Compatibility(_Subject, unittest.TestCase):
    """A5: pinned behavior for ordinary and typed-gap cases through the real module."""

    def test_pinned_cases_keep_their_outcomes(self):
        cases = compatibility_cases()
        self.assertEqual(set(cases), set(PINNED))
        for name, mail in sorted(cases.items()):
            with self.subTest(name=name):
                self.assertEqual(observe(self.module, mail), PINNED[name])

    def test_constants_and_public_entry_points_are_unchanged(self):
        self.assertEqual(correspondence.MAX_HEADER_BYTES, 131072)
        self.assertEqual(correspondence.SCHEMA, "correspondence-report-v1")
        self.assertEqual(correspondence.MESSAGE_ID.pattern, r"<[^<>\s]{1,2048}>")
        for name in ("report", "load_review", "snapshot", "main", "_threads", "_header_parent"):
            self.assertTrue(callable(getattr(correspondence, name)))


DECIDING_TESTS = (
    "DuplicateMemberTests.test_conflicting_parser_defects_valid_last_is_invalid",
    "DuplicateMemberTests.test_conflicting_duplicates_each_order_are_invalid",
    "DuplicateMemberTests.test_equal_value_duplicates_are_invalid",
    "DuplicateMemberTests.test_ignored_nested_object_duplicate_is_invalid",
    "DuplicateMemberTests.test_escaped_key_duplicate_is_invalid",
    "DuplicateMemberTests.test_duplicate_child_contributes_no_candidate_through_threads",
    "DuplicateMemberTests.test_duplicate_root_projection_contributes_no_parent",
    "BoundaryReplayTests.test_duplicate_at_exact_max_header_bytes_is_invalid",
    "BoundaryReplayTests.test_duplicates_at_every_depth_are_invalid",
    "BoundaryReplayTests.test_repeated_clean_and_invalid_calls_are_independent",
    "BoundaryReplayTests.test_no_db_mailbox_filesystem_network_model_or_send_api",
)
POSITIVE_CONTROLS = (
    "PositiveHeaderTests",
    "DuplicateMemberTests.test_controls_without_duplicates_are_valid",
    "DuplicateMemberTests.test_prior_typed_outcomes_are_preserved",
    "BoundaryReplayTests.test_exact_max_header_bytes_is_accepted_and_one_over_is_invalid",
    "BoundaryReplayTests.test_inputs_are_not_mutated",
    "Compatibility.test_pinned_cases_keep_their_outcomes",
)


def permissive_source(source):
    """Complete module text with ONLY the duplicate-member decode decision reverted."""
    if source.count(GATE_LINE) != 1:
        raise ValueError("gate line not found exactly once")
    return source.replace(GATE_LINE, PERMISSIVE_LINE, 1)


def module_from_source(source, name, filename):
    module = types.ModuleType("campaign_tool.records." + name)
    module.__package__ = "campaign_tool.records"
    module.__file__ = filename
    exec(compile(source, filename, "exec"), module.__dict__)
    return module


def bound_suite(module, names):
    """Load ``Class`` or ``Class.method`` names with ``module`` as the tested subject."""
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    this = sys.modules[__name__]
    for name in names:
        class_name, _, method = name.partition(".")
        base = getattr(this, class_name)
        bound = type(class_name, (base,), {"module": module, "__module__": __name__,
                                            "__qualname__": class_name})
        suite.addTests(loader.loadTestsFromName(method, bound) if method else loader.loadTestsFromTestCase(bound))
    return suite


def run_suite(suite):
    stream = io.StringIO()
    outcome = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)

    def ident(case):
        case = getattr(case, "test_case", case)
        return case.id().split(__name__ + ".", 1)[-1]
    return {"tests_run": outcome.testsRun,
            "failed": sorted({ident(case) for case, _ in outcome.failures + outcome.errors}),
            "errors": sorted({ident(case) for case, _ in outcome.errors}),
            "skipped": sorted({ident(case) for case, _ in outcome.skipped}),
            "successful": outcome.wasSuccessful()}, stream.getvalue()


class GateControl(unittest.TestCase):
    """A4: the regression tests fail when only the duplicate decision is reverted."""

    def production_bytes(self):
        with open(correspondence.__file__, "rb") as handle:
            return handle.read()

    def test_permissive_copy_fails_deciding_assertions_and_keeps_positives(self):
        before = self.production_bytes()
        mutant = module_from_source(permissive_source(before.decode("utf-8")), "_bm1_permissive_control",
                                    "<bm1 permissive gate control>")
        self.assertIsNot(mutant, correspondence)
        red, _ = run_suite(bound_suite(mutant, DECIDING_TESTS))
        self.assertEqual(red["tests_run"], len(DECIDING_TESTS))
        self.assertEqual(red["failed"], sorted(DECIDING_TESTS))
        self.assertEqual(red["errors"], [])
        green, text = run_suite(bound_suite(mutant, POSITIVE_CONTROLS))
        self.assertTrue(green["successful"], text)
        self.assertGreater(green["tests_run"], len(POSITIVE_CONTROLS))
        restored, text = run_suite(bound_suite(correspondence, DECIDING_TESTS + POSITIVE_CONTROLS))
        self.assertTrue(restored["successful"], text)
        self.assertEqual(self.production_bytes(), before)

    def test_mutant_reproduces_the_measured_last_declaration_premise(self):
        mutant = module_from_source(permissive_source(self.production_bytes().decode("utf-8")),
                                    "_bm1_permissive_premise", "<bm1 permissive premise>")
        bad = child(with_duplicate("parser_defects", ["synthetic-defect"], []))
        self.assertEqual(mutant._header_parent(bad), (ROOT_ID, None))
        self.assertEqual(correspondence._header_parent(bad), (None, INVALID))
        self.assertEqual(mutant._header_parent(child()), correspondence._header_parent(child()))


if __name__ == "__main__":
    unittest.main()
