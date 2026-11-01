"""Synthetic-only correspondence reporting over the existing canonical ledger.

No mailbox, network, model, credentials, scheduler, or outbox is used. The final
test runs the actual canonical IMAP preservation path against an in-memory fake.
"""
from contextlib import redirect_stdout
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from campaign_tool.records import correspondence
from campaign_tool.records.ledger.store import canonical, initialize, ledger
from contextlib import closing


AS_OF = "2026-10-13T12:00:00Z"


class CorrespondenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="synthetic-correspondence-")
        self.addCleanup(self.tmp.cleanup)
        # Canonical path checks correctly reject symlink ancestors, including
        # macOS's /var alias. Resolve the temporary directory before using it.
        self.base = Path(self.tmp.name).resolve()
        self.base.chmod(0o700)
        self.path = self.base / "ledger.sqlite"
        initialize(self.path)

    def execute(self, sql, values=()):
        with ledger(self.path) as con:
            con.execute(sql, values)
            con.commit()

    def agency(self, identity="agency-a"):
        self.execute("INSERT INTO agencies(id,name,jurisdiction) VALUES(?,?,?)",
                     (identity, "Fictional " + identity, "synthetic"))

    def request(self, identity="request-a", agency="agency-a", *, status="unknown", due=None,
                determination=None, production=None):
        self.execute("INSERT INTO requests(id,agency_id,external_ref,submitted_at,determination_due_at,"
                     "determination_at,production_at,status) VALUES(?,?,?,?,?,?,?,?)",
                     (identity, agency, "synthetic-" + identity, "2026-09-01T09:00:00Z", due,
                      determination, production, status))

    def original(self, label="mail-a", mime="message/rfc822", *, role="synthetic", scope="in_scope"):
        raw = ("FICTIONAL TEST ORIGINAL " + label).encode()
        digest = hashlib.sha256(raw).hexdigest()
        self.execute("INSERT INTO originals(sha256,bytes,mime_detected,role,scope,preservation_status,"
                     "provenance_json) VALUES(?,?,?,?,?,?,?)",
                     (digest, len(raw), mime, role, scope, "verified", "{}"))
        return digest

    def occurrence(self, identity, digest, *, kind="mail", parent=None):
        self.execute("INSERT INTO occurrences(id,original_sha256,kind,source_ref,parent_occurrence_id,"
                     "acquired_at,acquisition_method,evidence) VALUES(?,?,?,?,?,?,?,?)",
                     (identity, digest, kind, "synthetic:" + identity, parent, AS_OF, "synthetic_fixture",
                      canonical({"locator": "mime:0"})))
        return identity

    def join(self, identity, digest, *, request="request-a", agency="agency-a", status="typed", evidence=None):
        self.execute("INSERT INTO joins(id,original_sha256,agency_id,request_id,join_type,evidence,status) "
                     "VALUES(?,?,?,?,?,?,?)",
                     (identity, digest, agency, request, "declared_request_reference",
                      canonical({"source_sha256": digest, "locator": "mime:0/header:subject"})
                      if evidence is None else evidence, status))

    def mail(self, identity, digest, *, message_id="auto", account="synthetic-a", folder="INBOX",
             uid=1, fields=(), headers=None):
        if message_id == "auto":
            message_id = "<" + identity + "@synthetic.invalid>"
        projection = {"schema": "verified-eml-headers-v1", "source_original_sha256": digest,
                      "parser_defects": [],
                      "fields": ([["Message-ID", message_id]] if message_id else []) + list(fields)}
        self.execute("INSERT INTO mail_messages(id,account,folder,uidvalidity,uid,message_id,eml_sha256,"
                     "headers,received_at,account_provenance_status) VALUES(?,?,?,?,?,?,?,?,?,?)",
                     (identity, account, folder, 1, uid, message_id, digest,
                      canonical(projection) if headers is None else headers, AS_OF,
                      "configured_namespace_unattested"))

    def report(self, **kwargs):
        return correspondence.report(self.path, as_of=kwargs.pop("as_of", AS_OF), **kwargs)

    def request_row(self, result, identity="request-a"):
        return next(row for row in result["requests"] if row["id"] == identity)

    def seed_request_evidence(self):
        self.agency()
        self.request()
        digest = self.original()
        self.occurrence("mail-a", digest)
        attachment = self.original("attachment-a", "text/plain")
        self.occurrence("attachment-a", attachment, kind="attachment", parent="mail-a")
        self.join("join-a", digest)
        return digest, attachment

    def review(self, *, at=AS_OF):
        return {"schema": "correspondence-review-v1", "snapshot_sha256": self.report()["snapshot_sha256"],
                "requests": [{"request_id": "request-a", "reviewed_by": "synthetic-reviewer",
                              "outstanding_items": [{"item_id": "item-a", "description": "Fictional audit log",
                                                     "disposition": "outstanding", "evidence": [
                                                         {"occurrence_id": "attachment-a", "locator": "line:1"}]}],
                              "follow_up": {"at": at, "reason": "Fictional promise to produce records",
                                            "evidence": [{"occurrence_id": "mail-a", "locator": "mime:0/body:1"}]}}]}

    def database_state(self):
        # Compare canonical data and main DB bytes. SQLite's read-only connection
        # may maintain WAL shared-memory sidecars; they are not an audit artifact.
        with closing(sqlite3.connect(self.path)) as con, con:
            logical = "\n".join(con.iterdump())
        return hashlib.sha256(self.path.read_bytes()).hexdigest(), logical

    def test_empty_canonical_registry_is_reported_without_invented_requests(self):
        result = self.report()
        self.assertEqual(result["counts"], {"requests": 0, "mail_deliveries": 0, "occurrences": 0,
                                           "unique_originals": 0, "scope_excluded_occurrences": 0,
                                           "visible_mail_deliveries": 0})
        self.assertEqual(result["requests"], [])
        self.assertEqual(result["reminder_proposals"], [])
        self.assertEqual(result["visibility"], "private")
        self.assertIn({"key": "acceptance", "value": "candidate_unreconciled"}, result["ledger_meta"])

    def test_missing_or_unknown_registry_is_never_initialized_or_migrated(self):
        missing = self.base / "not-created.sqlite"
        with self.assertRaisesRegex(correspondence.CorrespondenceError,
                                    "^canonical_ledger_unavailable_or_unsupported$"):
            correspondence.report(missing, as_of=AS_OF)
        self.assertFalse(missing.exists())
        unknown = self.base / "unknown.sqlite"
        with closing(sqlite3.connect(unknown)) as con, con:
            con.execute("CREATE TABLE unrelated(value TEXT)")
            con.execute("INSERT INTO unrelated VALUES('synthetic')")
        unknown.chmod(0o600)
        before = unknown.read_bytes()
        with self.assertRaisesRegex(correspondence.CorrespondenceError,
                                    "^canonical_ledger_unavailable_or_unsupported$"):
            correspondence.report(unknown, as_of=AS_OF)
        self.assertEqual(unknown.read_bytes(), before)

    def test_reports_are_deterministic_and_leave_database_unchanged(self):
        self.seed_request_evidence()
        review = self.review()
        before = self.database_state()
        first = self.report(review=review)
        second = self.report(review=deepcopy(review))
        self.assertEqual(canonical(first), canonical(second))
        self.assertEqual(self.database_state(), before)
        equivalent = self.report(as_of="2026-10-13T05:00:00-07:00", review=review)
        self.assertEqual(equivalent, first)
        later = self.report(as_of="2026-10-14T12:00:00Z", review=review)
        self.assertEqual(later["snapshot_sha256"], first["snapshot_sha256"])
        self.assertEqual(later["reminder_proposals"][0]["dedupe_key"],
                         first["reminder_proposals"][0]["dedupe_key"])

    def test_duplicate_originals_keep_every_delivery_and_attachment_ancestry(self):
        digest, attachment = self.seed_request_evidence()
        self.occurrence("mail-copy", digest)
        self.occurrence("attachment-copy", attachment, kind="attachment", parent="mail-copy")
        result = self.report()
        request = self.request_row(result)
        self.assertEqual(request["delivery_occurrences"], 4)
        self.assertEqual(request["unique_originals"], 2)
        self.assertEqual({row["id"] for row in request["occurrences"]},
                         {"mail-a", "mail-copy", "attachment-a", "attachment-copy"})
        self.assertEqual({row["source_ref"] for row in request["occurrences"]},
                         {"synthetic:mail-a", "synthetic:mail-copy", "synthetic:attachment-a", "synthetic:attachment-copy"})
        groups = {row["original_sha256"]: row["occurrence_ids"] for row in result["duplicate_originals"]}
        self.assertEqual(groups[digest], ["mail-a", "mail-copy"])
        self.assertEqual(groups[attachment], ["attachment-a", "attachment-copy"])
        self.assertEqual(request["typed_join_ids"], ["join-a"])
        self.assertIsNone(request["outstanding_items"])
        self.assertEqual(request["fulfillment"], "not_assessed")

    def test_nested_attachment_inherits_only_its_recorded_ancestor_membership(self):
        self.seed_request_evidence()
        nested = self.original("nested", "text/plain")
        self.occurrence("nested", nested, kind="attachment", parent="attachment-a")
        unrelated = self.original("unassigned", "text/plain")
        self.occurrence("unassigned", unrelated, kind="attachment")
        result = self.report()
        self.assertIn("nested", {row["id"] for row in self.request_row(result)["occurrences"]})
        self.assertEqual(result["unassigned_occurrence_ids"], ["unassigned"])
        packet = self.review()
        packet["requests"][0]["outstanding_items"][0]["evidence"][0]["occurrence_id"] = "nested"
        reviewed = self.report(review=packet)
        evidence = self.request_row(reviewed)["outstanding_items"][0]["evidence"][0]
        self.assertEqual(evidence["original_sha256"], nested)

    def test_attachment_ancestry_cycles_fail_closed_without_hanging(self):
        self.seed_request_evidence()
        self.execute("UPDATE occurrences SET parent_occurrence_id='attachment-a' WHERE id='mail-a'")
        result = self.report()
        self.assertEqual(self.request_row(result)["occurrences"], [])
        self.assertEqual(result["unassigned_occurrence_ids"], ["attachment-a", "mail-a"])
        self.assertEqual({gap["occurrence_id"] for gap in result["gaps"]
                          if gap["code"] == "occurrence_ancestry_invalid"}, {"attachment-a", "mail-a"})

    def test_shared_attachment_join_cannot_move_another_requests_delivery(self):
        _, attachment = self.seed_request_evidence()
        self.agency("agency-b")
        self.request("request-b", "agency-b")
        parent_b = self.original("mail-b")
        self.occurrence("mail-b", parent_b)
        self.occurrence("attachment-b", attachment, kind="attachment", parent="mail-b")
        self.join("join-b", parent_b, request="request-b", agency="agency-b")
        self.join("attachment-join-a", attachment)
        result = self.report()
        self.assertEqual({row["id"] for row in self.request_row(result)["occurrences"]},
                         {"mail-a", "attachment-a"})
        self.assertEqual({row["id"] for row in self.request_row(result, "request-b")["occurrences"]}, {"mail-b"})
        self.assertIn("attachment-b", result["unassigned_occurrence_ids"])
        self.assertIn({"code": "request_binding_conflict", "occurrence_id": "attachment-b",
                       "request_ids": ["request-a", "request-b"]}, result["gaps"])
        packet = self.review()
        packet["requests"][0]["outstanding_items"][0]["evidence"][0]["occurrence_id"] = "attachment-b"
        with self.assertRaisesRegex(correspondence.CorrespondenceError, "^evidence_request_binding_missing$"):
            self.report(review=packet)

    def test_shared_attachment_joins_are_disambiguated_by_each_delivery_ancestor(self):
        _, attachment = self.seed_request_evidence()
        self.agency("agency-b")
        self.request("request-b", "agency-b")
        parent_b = self.original("mail-b")
        self.occurrence("mail-b", parent_b)
        self.occurrence("attachment-b", attachment, kind="attachment", parent="mail-b")
        self.join("join-b", parent_b, request="request-b", agency="agency-b")
        self.join("attachment-join-a", attachment)
        self.join("attachment-join-b", attachment, request="request-b", agency="agency-b")
        self.occurrence("attachment-without-parent", attachment, kind="attachment")
        result = self.report()
        self.assertEqual({row["id"] for row in self.request_row(result)["occurrences"]},
                         {"mail-a", "attachment-a"})
        self.assertEqual({row["id"] for row in self.request_row(result, "request-b")["occurrences"]},
                         {"mail-b", "attachment-b"})
        self.assertEqual(result["unassigned_occurrence_ids"], ["attachment-without-parent"])
        self.assertIn({"code": "request_binding_ambiguous", "occurrence_id": "attachment-without-parent",
                       "request_ids": ["request-a", "request-b"]}, result["gaps"])

    def test_scope_excluded_originals_and_descendants_are_hidden_and_cannot_support_review(self):
        self.seed_request_evidence()
        hidden_ids, hidden_shas = [], []
        variants = [{"role": "excluded_unrelated_personal"}, {"scope": "excluded"}, {"scope": "out_of_scope"}]
        for ordinal, options in enumerate(variants, 1):
            identity = "excluded-synthetic-" + str(ordinal)
            parent = self.original(identity, **options)
            self.occurrence(identity, parent)
            self.occurrence(identity + "-copy", parent)
            child = self.original(identity + "-child")
            self.occurrence(identity + "-child", child, kind="attachment", parent=identity)
            nested = self.original(identity + "-nested")
            self.occurrence(identity + "-nested", nested, kind="attachment", parent=identity + "-child")
            self.join(identity + "-join", parent)
            self.mail(identity, parent, uid=ordinal)
            self.mail(identity + "-child", child, uid=10 + ordinal)
            self.mail(identity + "-nested", nested, uid=20 + ordinal,
                      fields=[["In-Reply-To", "<" + identity + "@synthetic.invalid>"]])
            self.execute("INSERT INTO portal_items(id,portal_host,request_id,item_id,notice_occurrence_id,"
                         "retrieval_status) VALUES(?,?,?,?,?,?)",
                         (identity + "-portal", "portal.synthetic.invalid", "external", str(ordinal), identity, "pending"))
            hidden_ids.extend([identity, identity + "-copy", identity + "-child", identity + "-nested"])
            hidden_shas.extend([parent, child, nested])
        before = self.database_state()
        result = self.report()
        self.assertEqual(self.database_state(), before)
        self.assertEqual(result["counts"]["scope_excluded_occurrences"], 12)
        self.assertEqual(result["counts"]["mail_deliveries"], 9)
        self.assertEqual(result["counts"]["visible_mail_deliveries"], 0)
        self.assertEqual({row["id"] for row in self.request_row(result)["occurrences"]}, {"mail-a", "attachment-a"})
        self.assertEqual(result["mail"], [])
        self.assertEqual(result["thread_candidates"], [])
        self.assertEqual(result["duplicate_originals"], [])
        self.assertEqual(result["unassigned_occurrence_ids"], [])
        self.assertEqual(result["unretrieved_portal_items"], [])
        rendered = canonical(result)
        for hidden in hidden_ids + hidden_shas:
            self.assertNotIn(hidden, rendered)
        packet = self.review()
        for identity in hidden_ids:
            bad = deepcopy(packet)
            bad["requests"][0]["outstanding_items"][0]["evidence"][0]["occurrence_id"] = identity
            with self.subTest(identity=identity), self.assertRaisesRegex(
                    correspondence.CorrespondenceError, "^evidence_request_binding_missing$"):
                self.report(review=bad)

    def test_scope_excluded_evidence_source_invalidates_an_otherwise_visible_join(self):
        self.agency()
        self.request()
        visible = self.original("visible")
        self.occurrence("visible", visible)
        excluded = self.original("excluded-evidence-source", scope="excluded")
        self.join("invalid-source-join", visible,
                  evidence=canonical({"source_sha256": excluded, "locator": "line:1"}))
        result = self.report()
        self.assertEqual(self.request_row(result)["occurrences"], [])
        self.assertEqual(self.request_row(result)["typed_join_ids"], [])
        self.assertEqual(result["unassigned_occurrence_ids"], ["visible"])
        self.assertIn({"code": "request_join_scope_excluded"}, result["gaps"])
        self.assertNotIn(excluded, canonical(result))
        packet = {"schema": "correspondence-review-v1", "snapshot_sha256": result["snapshot_sha256"],
                  "requests": [{"request_id": "request-a", "reviewed_by": "synthetic-reviewer",
                                "outstanding_items": [], "follow_up": {"at": AS_OF, "reason": "Synthetic follow up",
                                "evidence": [{"occurrence_id": "visible", "locator": "line:1"}]}}]}
        with self.assertRaisesRegex(correspondence.CorrespondenceError, "^evidence_request_binding_missing$"):
            self.report(review=packet)

    def test_excluded_delivery_of_shared_bytes_does_not_hide_in_scope_delivery(self):
        _, attachment = self.seed_request_evidence()
        excluded = self.original("excluded-parent", scope="out_of_scope")
        self.occurrence("excluded-parent", excluded)
        self.occurrence("excluded-shared-copy", attachment, kind="attachment", parent="excluded-parent")
        result = self.report()
        self.assertEqual(result["counts"]["scope_excluded_occurrences"], 2)
        self.assertEqual({row["id"] for row in self.request_row(result)["occurrences"]}, {"mail-a", "attachment-a"})
        self.assertEqual(result["duplicate_originals"], [])
        self.assertNotIn("excluded-shared-copy", canonical(result))
        reviewed = self.report(review=self.review())
        self.assertEqual(self.request_row(reviewed)["outstanding_items"][0]["evidence"][0]["original_sha256"], attachment)

    def test_unverified_or_wrong_agency_joins_do_not_assign_a_request(self):
        self.agency()
        self.agency("agency-b")
        self.request()
        digest = self.original()
        self.occurrence("mail-a", digest)
        self.join("hint", digest, status="hinted")
        self.join("blocked", digest, status="blocked")
        self.join("wrong-agency", digest, agency="agency-b")
        self.join("empty-evidence", digest, evidence="{}")
        result = self.report()
        self.assertEqual(self.request_row(result)["occurrences"], [])
        self.assertEqual(self.request_row(result)["typed_join_ids"], [])
        self.assertEqual({gap["join_id"] for gap in result["gaps"]
                          if gap["code"] == "request_join_unverified"}, {"hint", "blocked"})
        self.assertEqual({gap["join_id"] for gap in result["gaps"]
                          if gap["code"] == "request_join_invalid"}, {"wrong-agency", "empty-evidence"})

    def test_reply_candidate_retains_duplicate_parent_deliveries_without_assigning_request(self):
        digest, _ = self.seed_request_evidence()
        self.mail("parent-inbox", digest, message_id="<parent@synthetic.invalid>")
        self.mail("parent-archive", digest, message_id="<parent@synthetic.invalid>", folder="Archive")
        reply = self.original("reply")
        self.occurrence("reply", reply)
        self.mail("reply", reply, message_id="<reply@synthetic.invalid>", uid=2,
                  fields=[["In-Reply-To", "<parent@synthetic.invalid>"]])
        result = self.report()
        self.assertEqual(result["thread_candidates"], [{"mail_id": "reply",
                         "parent_mail_ids": ["parent-archive", "parent-inbox"],
                         "basis": "declared_reply_header", "status": "unverified_candidate"}])
        self.assertIn("reply", result["unassigned_occurrence_ids"])
        self.assertNotIn("reply", {row["id"] for row in self.request_row(result)["occurrences"]})
        for row in result["mail"]:
            self.assertNotIn("headers", row)
            self.assertNotIn("message_id", row)

    def test_message_id_collision_with_different_bytes_is_not_a_thread(self):
        for uid in (1, 2):
            digest = self.original("collision-" + str(uid))
            self.mail("parent-" + str(uid), digest, message_id="<same@synthetic.invalid>", uid=uid)
        reply = self.original("reply")
        self.mail("reply", reply, uid=3, fields=[["In-Reply-To", "<same@synthetic.invalid>"]])
        result = self.report()
        self.assertEqual(result["thread_candidates"], [])
        self.assertIn({"code": "message_id_collision", "mail_id": "reply"}, result["gaps"])

    def test_reply_headers_do_not_cross_account_namespaces(self):
        parent = self.original("parent")
        self.mail("parent", parent, message_id="<parent@synthetic.invalid>", account="synthetic-a")
        reply = self.original("reply")
        self.mail("reply", reply, account="synthetic-b", fields=[["In-Reply-To", "<parent@synthetic.invalid>"]])
        result = self.report()
        self.assertEqual(result["thread_candidates"], [])
        self.assertIn({"code": "thread_parent_missing", "mail_id": "reply"}, result["gaps"])

    def test_same_message_id_in_another_account_does_not_poison_a_valid_candidate(self):
        for account in ("synthetic-a", "synthetic-b"):
            parent = self.original(account)
            self.mail("parent-" + account, parent, message_id="<same@synthetic.invalid>", account=account)
        reply = self.original("reply")
        self.mail("reply", reply, uid=2, fields=[["In-Reply-To", "<same@synthetic.invalid>"]])
        result = self.report()
        self.assertEqual(len(result["thread_candidates"]), 1)
        self.assertEqual(result["thread_candidates"][0]["parent_mail_ids"], ["parent-synthetic-a"])

    def test_message_id_without_matching_verified_header_is_not_a_parent(self):
        parent = self.original("parent")
        projection = {"schema": "verified-eml-headers-v1", "source_original_sha256": parent,
                      "parser_defects": [], "fields": [["Message-ID", "<different@synthetic.invalid>"]]}
        self.mail("parent", parent, message_id="<parent@synthetic.invalid>", headers=canonical(projection))
        reply = self.original("reply")
        self.mail("reply", reply, uid=2, fields=[["In-Reply-To", "<parent@synthetic.invalid>"]])
        result = self.report()
        self.assertEqual(result["thread_candidates"], [])
        self.assertIn({"code": "header_message_id_mismatch", "mail_id": "parent"}, result["gaps"])
        self.assertIn({"code": "thread_parent_missing", "mail_id": "reply"}, result["gaps"])

    def test_missing_or_ambiguous_own_message_id_cannot_form_a_thread_candidate(self):
        parent = self.original("parent")
        self.mail("parent", parent, message_id="<parent@synthetic.invalid>")
        for uid, fields in ((2, []), (3, [["Message-ID", "<one@synthetic.invalid>"],
                                        ["Message-ID", "<two@synthetic.invalid>"]])):
            identity = "invalid-id-" + str(uid)
            digest = self.original(identity)
            self.mail(identity, digest, message_id=None, uid=uid,
                      fields=fields + [["In-Reply-To", "<parent@synthetic.invalid>"]])
        result = self.report()
        self.assertEqual(result["thread_candidates"], [])
        self.assertEqual({gap["mail_id"] for gap in result["gaps"]
                          if gap["code"] == "message_id_missing_or_ambiguous"}, {"invalid-id-2", "invalid-id-3"})

    def test_reference_chain_uses_last_parent_and_subject_is_not_identity(self):
        parent = self.original("parent")
        self.mail("parent", parent, message_id="<parent@synthetic.invalid>")
        reply = self.original("reply")
        self.mail("reply", reply, uid=2, fields=[["References", "<older@synthetic.invalid> <parent@synthetic.invalid>"]])
        similar = self.original("similar")
        self.mail("same-subject", similar, uid=3, fields=[["Subject", "Re: Fictional records request"]])
        result = self.report()
        self.assertEqual([row["mail_id"] for row in result["thread_candidates"]], ["reply"])
        self.assertEqual(result["thread_candidates"][0]["parent_mail_ids"], ["parent"])

    def test_unsupported_ambiguous_or_unbound_headers_report_gaps(self):
        parent = self.original("parent")
        self.mail("parent", parent, message_id="<parent@synthetic.invalid>")
        cases = [
            ("unsupported", {}, "thread_headers_unsupported"),
            ("wrong-source", {"schema": "verified-eml-headers-v1", "source_original_sha256": "0" * 64,
                              "fields": []}, "header_projection_invalid"),
            ("defects", {"parser_defects": ["synthetic-defect"]}, "header_projection_invalid"),
            ("duplicate", {"fields": [["In-Reply-To", "<parent@synthetic.invalid>"],
                                      ["in-reply-to", "<parent@synthetic.invalid>"]]}, "ambiguous_reply_headers"),
            ("garbage", {"fields": [["In-Reply-To", "comment <parent@synthetic.invalid>"]]},
             "unsupported_reply_header_syntax"),
        ]
        for uid, (name, replacement, _) in enumerate(cases, 2):
            digest = self.original(name)
            projection = {"schema": "verified-eml-headers-v1", "source_original_sha256": digest,
                          "parser_defects": [], "fields": [["In-Reply-To", "<parent@synthetic.invalid>"]]}
            projection = replacement if name == "unsupported" else {**projection, **replacement}
            if "fields" in projection:
                projection["fields"] = [["Message-ID", "<" + name + "@synthetic.invalid>"]] + projection["fields"]
            self.mail(name, digest, uid=uid, headers=canonical(projection))
        result = self.report()
        self.assertEqual(result["thread_candidates"], [])
        for name, _, code in cases:
            self.assertIn({"code": code, "mail_id": name}, result["gaps"])

    def test_self_reply_reference_is_a_gap(self):
        digest = self.original()
        self.mail("self", digest, message_id="<self@synthetic.invalid>",
                  fields=[["In-Reply-To", "<self@synthetic.invalid>"]])
        result = self.report()
        self.assertEqual(result["thread_candidates"], [])
        self.assertIn({"code": "self_reply_reference", "mail_id": "self"}, result["gaps"])

    def test_valid_review_is_bound_to_snapshot_request_and_original_evidence(self):
        digest, attachment = self.seed_request_evidence()
        packet = self.review()
        result = self.report(review=packet)
        request = self.request_row(result)
        self.assertEqual(request["review"], {"reviewed_by": "synthetic-reviewer",
                         "authority": "reviewer_declaration_only", "status": "proposal"})
        self.assertEqual(request["outstanding_items"][0]["evidence"][0],
                         {"occurrence_id": "attachment-a", "locator": "line:1", "original_sha256": attachment})
        self.assertEqual(request["follow_up"]["evidence"][0]["original_sha256"], digest)
        self.assertEqual(result["review_sha256"], hashlib.sha256(canonical(packet).encode()).hexdigest())
        reminder = result["reminder_proposals"][0]
        self.assertEqual(reminder["kind"], "reviewed_follow_up")
        self.assertEqual(reminder["status"], "proposal_only")
        self.assertTrue(reminder["requires_current_owner_review"])
        self.assertEqual(request["fulfillment"], "not_assessed")

    def test_review_snapshot_goes_stale_when_existing_registry_changes(self):
        self.seed_request_evidence()
        packet = self.review()
        self.execute("UPDATE requests SET status='agency-says-complete' WHERE id='request-a'")
        with self.assertRaisesRegex(correspondence.CorrespondenceError, "^review_snapshot_stale$"):
            self.report(review=packet)

    def test_review_cannot_create_or_duplicate_request_or_omit_author(self):
        self.seed_request_evidence()
        packet = self.review()
        for name in ("unknown", "duplicate", "author"):
            bad = deepcopy(packet)
            if name == "unknown":
                bad["requests"][0]["request_id"] = "invented-request"
            elif name == "duplicate":
                bad["requests"].append(deepcopy(bad["requests"][0]))
            else:
                bad["requests"][0]["reviewed_by"] = "  "
            with self.subTest(name=name), self.assertRaises(correspondence.CorrespondenceError):
                self.report(review=bad)

    def test_review_cannot_use_missing_unassigned_or_other_request_evidence(self):
        self.seed_request_evidence()
        self.agency("agency-b")
        self.request("request-b", "agency-b")
        other = self.original("other")
        self.occurrence("other", other)
        self.join("other-join", other, request="request-b", agency="agency-b")
        unassigned = self.original("unassigned")
        self.occurrence("unassigned", unassigned)
        packet = self.review()
        for identity in ("missing", "unassigned", "other"):
            bad = deepcopy(packet)
            bad["requests"][0]["outstanding_items"][0]["evidence"][0]["occurrence_id"] = identity
            with self.subTest(identity=identity), self.assertRaisesRegex(
                    correspondence.CorrespondenceError, "^evidence_request_binding_missing$"):
                self.report(review=bad)

    def test_malformed_review_items_and_follow_up_fail_closed(self):
        self.seed_request_evidence()
        packet = self.review()
        cases = []
        for mutate, code in (
            (lambda p: p.update(schema="unknown-review-v1"), "review_schema_unsupported"),
            (lambda p: p.update(extra=True), "review_packet_invalid"),
            (lambda p: p["requests"][0]["outstanding_items"][0].update(disposition="complete"), "review_disposition_invalid"),
            (lambda p: p["requests"][0]["outstanding_items"][0].update(evidence=[]), "evidence_required"),
            (lambda p: p["requests"][0]["outstanding_items"][0]["evidence"][0].update(locator=" "), "evidence_locator_required"),
            (lambda p: p["requests"][0]["follow_up"].update(at="2026-10-13T12:00:00"), "timestamp_requires_timezone"),
            (lambda p: p["requests"][0]["follow_up"].update(at="0001-01-01T00:00:00+01:00"), "timestamp_requires_timezone"),
            (lambda p: p["requests"][0]["follow_up"].update(at="9999-12-31T23:59:59-02:00"), "timestamp_requires_timezone"),
            (lambda p: p["requests"][0]["follow_up"].update(evidence=[]), "evidence_required"),
        ):
            bad = deepcopy(packet)
            mutate(bad)
            cases.append((bad, code))
        duplicate = deepcopy(packet)
        duplicate["requests"][0]["outstanding_items"].append(deepcopy(duplicate["requests"][0]["outstanding_items"][0]))
        cases.append((duplicate, "review_item_duplicate"))
        for bad, code in cases:
            with self.subTest(code=code), self.assertRaisesRegex(correspondence.CorrespondenceError, "^" + code + "$"):
                self.report(review=bad)

    def test_due_boundaries_require_timezone_and_completed_determinations_are_skipped(self):
        self.agency()
        requests = [
            ("past", "2026-10-13T11:59:59Z", None),
            ("equal", AS_OF, None),
            ("equal-offset", "2026-10-13T05:00:00-07:00", None),
            ("future", "2026-10-13T12:00:01Z", None),
            ("naive", "2026-10-13T11:00:00", None),
            ("invalid", "not-a-date", None),
            ("underflow", "0001-01-01T00:00:00+01:00", None),
            ("overflow", "9999-12-31T23:59:59-02:00", None),
            ("determined", AS_OF, "2026-10-12T12:00:00Z"),
        ]
        for identity, due, determination in requests:
            self.request(identity, due=due, determination=determination)
        result = self.report()
        self.assertEqual({row["request_id"] for row in result["reminder_proposals"]},
                         {"past", "equal", "equal-offset"})
        self.assertEqual({gap["request_id"] for gap in result["gaps"]
                          if gap["code"] == "determination_due_timezone_or_format_unresolved"},
                         {"naive", "invalid", "underflow", "overflow"})
        for reminder in result["reminder_proposals"]:
            self.assertEqual(reminder["basis"], {"table": "requests", "id": reminder["request_id"],
                                               "field": "determination_due_at"})

    def test_follow_up_boundary_is_inclusive_and_future_review_stays_visible(self):
        self.seed_request_evidence()
        packet = self.review(at="2026-10-13T12:00:01Z")
        before = self.report(review=packet)
        self.assertEqual(before["reminder_proposals"], [])
        self.assertEqual(self.request_row(before)["follow_up"]["at"], "2026-10-13T12:00:01Z")
        at_due = self.report(as_of="2026-10-13T12:00:01Z", review=packet)
        self.assertEqual(len(at_due["reminder_proposals"]), 1)

    def test_unknown_status_and_production_date_do_not_infer_fulfillment(self):
        self.seed_request_evidence()
        self.execute("UPDATE requests SET status='custom-unknown-status',production_at=? WHERE id='request-a'", (AS_OF,))
        packet = self.review()
        packet["requests"][0]["outstanding_items"][0]["disposition"] = "received"
        result = self.report(review=packet)
        request = self.request_row(result)
        self.assertEqual(request["status"], "custom-unknown-status")
        self.assertEqual(request["production_at"], AS_OF)
        self.assertEqual(request["fulfillment"], "not_assessed")
        self.assertEqual(request["outstanding_items"][0]["disposition"], "received")
        self.assertEqual(result["reminder_proposals"][0]["recorded_request_status"], "custom-unknown-status")

    def test_portal_external_request_reference_does_not_create_registry_membership(self):
        digest, _ = self.seed_request_evidence()
        self.execute("INSERT INTO portal_items(id,portal_host,request_id,item_id,notice_occurrence_id,"
                     "retrieval_status) VALUES(?,?,?,?,?,?)",
                     ("portal-pending", "portal.synthetic.invalid", "external-unmapped", "item-1", "mail-a", "pending"))
        self.execute("INSERT INTO portal_items(id,portal_host,request_id,item_id,retrieval_status,original_sha256) "
                     "VALUES(?,?,?,?,?,?)",
                     ("portal-retrieved", "portal.synthetic.invalid", "request-a", "item-2", "retrieved", digest))
        result = self.report()
        self.assertEqual(result["counts"]["requests"], 1)
        self.assertEqual(result["unretrieved_portal_items"], [{"portal_item_id": "portal-pending",
                         "external_request_ref": "external-unmapped", "retrieval_status": "pending",
                         "notice_occurrence_id": "mail-a"}])
        self.assertIn({"code": "portal_bytes_not_received", "portal_item_id": "portal-pending"}, result["gaps"])
        self.assertEqual(self.request_row(result)["fulfillment"], "not_assessed")

    def test_invalid_as_of_and_bounded_snapshot_fail_with_stable_codes(self):
        for value in ("2026-10-13", "2026-10-13T12:00:00", "invalid", None, 123,
                      "0001-01-01T00:00:00+01:00", "9999-12-31T23:59:59-02:00"):
            with self.subTest(value=value), self.assertRaisesRegex(correspondence.CorrespondenceError,
                                                                  "^timestamp_requires_timezone$"):
                self.report(as_of=value)
        self.agency()
        self.agency("agency-b")
        with patch.object(correspondence, "MAX_ROWS", 1), self.assertRaisesRegex(
                correspondence.CorrespondenceError, "^snapshot_row_limit$"):
            self.report()

    def test_review_file_loader_is_bounded_and_rejects_symlinks_and_bad_json(self):
        review_path = self.base / "review.json"
        review_path.write_text('{"schema":"synthetic"}')
        self.assertEqual(correspondence.load_review(review_path), {"schema": "synthetic"})
        link = self.base / "linked-review.json"
        link.symlink_to(review_path)
        with self.assertRaisesRegex(correspondence.CorrespondenceError, "^review_packet_unreadable$"):
            correspondence.load_review(link)
        review_path.write_bytes(b"\xffnot-json")
        with self.assertRaisesRegex(correspondence.CorrespondenceError, "^review_packet_unreadable$"):
            correspondence.load_review(review_path)
        review_path.write_bytes(b" " * (correspondence.MAX_REVIEW_BYTES + 1))
        with self.assertRaisesRegex(correspondence.CorrespondenceError, "^review_packet_too_large$"):
            correspondence.load_review(review_path)

    def test_cli_success_and_errors_are_json_without_source_or_path_leak(self):
        self.seed_request_evidence()
        output = io.StringIO()
        with redirect_stdout(output):
            code = correspondence.main(["--ledger", str(self.path), "--as-of", AS_OF])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue()), self.report())
        private_path = self.base / "private-synthetic-path-do-not-print.sqlite"
        output = io.StringIO()
        with redirect_stdout(output):
            code = correspondence.main(["--ledger", str(private_path), "--as-of", AS_OF])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output.getvalue()), {"schema": "correspondence-report-v1",
                         "error": "canonical_ledger_unavailable_or_unsupported"})
        self.assertNotIn(str(self.base), output.getvalue())

    def test_actual_fake_imap_intake_projects_headers_duplicates_and_attachments(self):
        from campaign_tool.records.run import Pipeline
        from tests.records.test_imap_intake import FakeIMAP
        from tests.records.test_run_slice import synthetic_email

        mail_root = self.base / "intake-root"
        config = self.base / "synthetic-mail.json"
        config.write_text(canonical({"host": "imap.synthetic.invalid", "username": "synthetic",
                                    "password": "synthetic",  # pragma: allowlist secret - in-memory fake only
                                    "account_id": "synthetic-account", "folders": ["INBOX", "Archive"]}))
        config.chmod(0o600)
        parent = synthetic_email(message_id="<parent@synthetic.invalid>", attachment=b"FICTIONAL ATTACHMENT\n")
        reply = b"In-Reply-To: <parent@synthetic.invalid>\r\n" + synthetic_email(
            message_id="<reply@synthetic.invalid>", attachment=None)
        fake = FakeIMAP({"INBOX": {"uidvalidity": 7, "messages": {1: parent, 2: reply}},
                         "Archive": {"uidvalidity": 3, "messages": {4: parent}}})
        pipeline = Pipeline(mail_root, account="synthetic-account")
        intake = pipeline.ingest_mailbox(config, client_factory=lambda _: fake)
        self.assertTrue(fake.logged_out)
        self.assertEqual(intake["preserved"], 3)
        self.assertEqual(intake["failures"], 0)
        self.path = mail_root / "ledger.sqlite"
        self.agency()
        self.request()
        parent_sha = hashlib.sha256(parent).hexdigest()
        self.join("canonical-parent", parent_sha)
        before = self.database_state()
        result = self.report()
        self.assertEqual(self.database_state(), before)
        self.assertEqual(result["counts"]["mail_deliveries"], 3)
        self.assertEqual(result["counts"]["unique_originals"], 3)
        self.assertEqual(len(result["thread_candidates"]), 1)
        self.assertEqual(len(result["thread_candidates"][0]["parent_mail_ids"]), 2)
        request = self.request_row(result)
        self.assertEqual(request["unique_originals"], 2)
        self.assertEqual(request["delivery_occurrences"], 4)
        self.assertEqual({row["kind"] for row in request["occurrences"]}, {"mail", "attachment"})
        self.assertEqual(request["fulfillment"], "not_assessed")
        self.assertNotIn("synthetic-fixture-only", canonical(result))


if __name__ == "__main__":
    unittest.main()
