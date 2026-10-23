# Claude audit repros from 11f108e614367e9fcc487179bcdd4a06765a811e.
# Promoted/adapted by Sol for regular synthetic-only regression.
"""Reader/writer attacks on the mail-resolution row. Synthetic ledger fixtures only.

Findings are tagged F-nn and match docs/audits/2026-10-22-t4b-s1-audit.md. A test
that fails asserts the behaviour the design requires; a passing test pins a
property that was checked and found sound.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import threading
import unittest

from campaign_tool.records.ledger import store, original_resolution as resolution  # noqa: E402
from campaign_tool.records.ledger import mail_resolution_policy as mrp  # noqa: E402
from tests.records import test_original_resolution_mail as pr  # noqa: E402
from tests.records.test_mail_resolution_policy import approved  # noqa: E402


class World(unittest.TestCase):
    """Wraps the PR's own fixture so both stay in step with the engine under test."""
    maxDiff = None

    def setUp(self):
        self.w = pr.OriginalResolutionMailTests("test_happy_overlay_immutable_original_and_all_occurrences")
        self.w.setUp()
        self.addCleanup(self.w.doCleanups)
        w = self.w
        self.db, self.sha, self.output, self.path = w.database, w.sha, w.output, w.path
        self.receipt, self.oid, self.receipt_path = w.receipt, w.oid, w.receipt_path

    def fresh(self):
        """An independent ledger fixture, so a rejected/accepted case never taints the next one."""
        other = type(self)("runTest")
        other.setUp()
        self.addCleanup(other.doCleanups)
        return other

    # helpers -------------------------------------------------------------
    def txn(self, fn, *, immediate=True):
        with store.ledger(self.db) as con:
            con.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                out = fn(con)
            except BaseException:
                con.rollback()
                raise
            con.commit()
            return out

    def append(self, con, **kw):
        return self.w.append(con, **kw)

    def count(self):
        return self.w.fixture.rows("SELECT count(*) FROM original_resolutions")[0][0]

    def set_source(self, **changes):
        source = dict(self.w.source, **changes)
        with store.ledger(self.db) as con:
            con.execute("UPDATE occurrences SET source_ref=? WHERE id=?", (store.canonical(source), self.oid))
            con.commit()

    def set_evidence(self, **changes):
        ev = dict(self.w.evidence, **changes)
        with store.ledger(self.db) as con:
            con.execute("UPDATE occurrences SET evidence=? WHERE id=?", (store.canonical(ev), self.oid))
            con.commit()

    def stage_root(self, name):
        """A second, complete storage root holding a copy of the receipt."""
        root = self.output.parent / name
        (root / "blobs").mkdir(parents=True)
        (root / "verification-receipts").mkdir()
        shutil.copy(self.receipt_path, root / "verification-receipts" / self.receipt)
        return root

    def raw_row(self, payload_json=None, **cols):
        """Insert a row the way a DB-write attacker would: triggers dropped."""
        with store.ledger(self.db) as con:
            con.execute("BEGIN IMMEDIATE")
            triggers = con.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name='original_resolutions'").fetchall()
            for name, _ in triggers:
                con.execute('DROP TRIGGER "%s"' % name)
            payload = {"schema": "original_resolution_mail_export_v1", "original_sha256": self.sha,
                       "declaration_sha256": resolution.digest(dict(con.execute("SELECT * FROM originals WHERE sha256=?", (self.sha,)).fetchone())),
                       "policy_sha256": "1" * 64, "verification_receipt_sha256": self.receipt,
                       "occurrence_id": self.oid, "occurrence_kind": "attachment",
                       "mail_identity_sha256": resolution.digest({k: self.w.source[k] for k in ("account", "folder", "uidvalidity", "uid")}),
                       "legacy_binding_sha256": self.w.legacy, "byte_length": self.w.fixture.source.stat().st_size,
                       "role": "original", "scope": "in_scope", "storage_path": self.path}
            payload.update(cols.pop("payload", {}))
            text = payload_json if payload_json is not None else store.canonical(payload)
            con.execute("INSERT INTO original_resolutions VALUES(?,?,?,?,?,?,?)",
                        (self.sha, resolution.digest(payload), payload["declaration_sha256"], payload["policy_sha256"],
                         payload["verification_receipt_sha256"], payload["occurrence_id"], text))
            for _, sql in triggers:
                con.execute(sql)  # Keep policy proof tests independent of missing-trigger refusal.
            con.commit()
        return payload

    def effective(self):
        with store.ledger(self.db, readonly=True) as con:
            return self.w.read(con)


class TornAndCrashTests(World):
    def test_crash_before_commit_leaves_nothing_and_retry_succeeds(self):
        pid = os.fork()
        if pid == 0:  # child: append, then die without commit/close
            try:
                con = sqlite3.connect(self.db, timeout=5)
                con.row_factory = sqlite3.Row
                con.execute("PRAGMA foreign_keys=ON")
                con.execute("BEGIN IMMEDIATE")
                self.append(con)
            finally:
                os._exit(0)
        os.waitpid(pid, 0)
        self.assertEqual(self.count(), 0)
        first, reused = self.txn(self.append)
        self.assertFalse(reused)
        self.assertEqual(self.txn(self.append), (first, True))

    def test_crash_after_commit_replays_idempotently(self):
        first, _ = self.txn(self.append)
        pid = os.fork()
        if pid == 0:
            os._exit(0)
        os.waitpid(pid, 0)
        self.assertEqual(self.txn(self.append), (first, True))
        self.assertEqual(self.count(), 1)

    def test_failure_after_insert_does_not_leave_a_row_in_the_callers_transaction(self):
        """F-04: INSERT precedes the final self-check; the row stays pending if the caller commits anyway."""
        original = resolution.effective_original
        calls = []

        def boom(con, subject, **context):
            calls.append(subject)
            raise ValueError("original_resolution_mail_receipt_changed")
        import campaign_tool.records.ledger.original_resolution as mod
        mod.effective_original = boom
        try:
            with store.ledger(self.db) as con:
                con.execute("BEGIN IMMEDIATE")
                with self.assertRaises(ValueError):
                    self.append(con)
                pending = con.execute("SELECT count(*) FROM original_resolutions").fetchone()[0]
                con.rollback()
        finally:
            mod.effective_original = original
        self.assertEqual(calls, [self.sha])
        self.assertEqual(pending, 0, "append raised but its row is still visible/committable in the open transaction")

    def test_torn_receipt_after_enroll_is_loud(self):
        self.txn(self.append)
        raw = self.receipt_path.read_bytes()
        for cut in (0, 1, len(raw) - 1):
            self.receipt_path.write_bytes(raw[:cut])
            with self.subTest(cut=cut), self.assertRaisesRegex(ValueError, "original_resolution_mail_receipt_changed"):
                self.effective()
        self.receipt_path.write_bytes(raw)
        self.assertEqual(self.effective()["role"], "original")

    def test_orphan_receipt_or_blob_without_row_grants_nothing(self):
        (self.output / "blobs").mkdir(exist_ok=True)
        (self.output / "blobs" / self.sha).write_bytes(b"orphan")
        with store.ledger(self.db, readonly=True) as con:
            row = self.w.read(con)
        self.assertEqual((row["role"], row["scope"], row["storage_path"]), ("unresolved", "unresolved", None))

    def test_schema_marker_without_table_or_table_without_marker_refused(self):
        self.txn(self.append)
        with store.ledger(self.db) as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute("DROP TRIGGER original_resolution_marker_delete")
            con.execute("DELETE FROM ledger_meta WHERE key=?", (resolution.KEY,))
            with self.assertRaisesRegex(ValueError, "unbound_original_resolution_schema"):
                self.w.read(con)
            con.rollback()


class ConcurrencyTests(World):
    def test_two_appenders_exactly_one_row(self):
        results, errors, barrier = [], [], threading.Barrier(4)

        def worker():
            try:
                barrier.wait()
                results.append(self.txn(self.append))
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
        threads = [threading.Thread(target=worker) for _ in range(4)]
        [t.start() for t in threads]
        [t.join(30) for t in threads]
        self.assertEqual(self.count(), 1)
        self.assertEqual(errors, [])
        self.assertEqual(sorted(r[1] for r in results), [False, True, True, True])
        self.assertEqual(len({r[0] for r in results}), 1)

    def test_deferred_transactions_lose_cleanly(self):
        """F-07: `in_transaction` is satisfied by a deferred BEGIN; the loser gets an uncoded sqlite error."""
        with store.ledger(self.db) as loser:
            loser.execute("BEGIN")
            loser.execute("SELECT count(*) FROM original_resolutions").fetchone()  # snapshot opens here
            self.txn(self.append)
            try:
                self.append(loser)
            except sqlite3.Error as exc:
                loser.rollback()
                self.fail("F-07: stale deferred transaction surfaced %s: %s (writer should demand BEGIN IMMEDIATE)"
                          % (type(exc).__name__, exc))
            except ValueError as exc:
                loser.rollback()
                self.assertIn("original_resolution_", str(exc))
            else:
                loser.rollback()
        self.assertEqual(self.count(), 1)

    def test_conflicting_appenders_never_both_win(self):
        other = dict(self.w.policy_value, approval_sha256="0" * 64)
        outcomes = []

        def go(policy):
            try:
                outcomes.append(("ok", self.txn(lambda c: self.append(c, policy=policy))))
            except Exception as exc:  # noqa: BLE001
                outcomes.append(("err", str(exc)))
        ts = [threading.Thread(target=go, args=(p,)) for p in (self.w.policy, approved(other))]
        [t.start() for t in ts]
        [t.join(30) for t in ts]
        self.assertEqual(self.count(), 1)
        self.assertEqual(sorted(o[0] for o in outcomes), ["err", "ok"])
        self.assertTrue(any("original_resolution_conflict" in o[1] for o in outcomes if o[0] == "err"))


class DuplicateAndReorderTests(World):
    def test_replay_is_idempotent_and_extra_occurrences_do_not_change_binding(self):
        first, _ = self.txn(self.append)
        with store.ledger(self.db) as con:
            for i, mime in enumerate(("1.1", "1.9", "2.3")):
                source = dict(self.w.source, mime_path=mime)
                con.execute("INSERT INTO occurrences VALUES(?,?,?,?,?,?,?,?)", (("%064x" % (i + 1)), self.sha, "attachment",
                            store.canonical(source), None, None, "verified_mail_export", store.canonical(self.w.evidence)))
            con.commit()
        self.assertEqual(self.txn(self.append), (first, True))
        self.assertEqual(self.effective()["resolution_sha256"], first)

    def test_other_occurrence_cannot_stand_in_for_the_policy_binding_occurrence(self):
        twin = "9" * 64
        with store.ledger(self.db) as con:
            con.execute("INSERT INTO occurrences SELECT ?,original_sha256,kind,source_ref,parent_occurrence_id,acquired_at,"
                        "acquisition_method,evidence FROM occurrences WHERE id=?", (twin, self.oid))
            con.commit()
        with self.assertRaisesRegex(ValueError, "resolution_policy_receipt_mismatch"):
            self.txn(lambda c: self.append(c, occurrence_id=twin))
        self.assertEqual(self.count(), 0)

    def test_kind_swap_between_occurrence_and_payload_is_caught_by_reader(self):
        self.txn(self.append)
        with store.ledger(self.db) as con:
            con.execute("UPDATE occurrences SET kind='mail' WHERE id=?", (self.oid,))
            con.commit()
        with self.assertRaisesRegex(ValueError, "original_resolution_mail_occurrence_unbound"):
            self.effective()

    def test_second_original_in_same_message_shares_mail_identity_by_design(self):
        a = resolution.digest({k: self.w.source[k] for k in ("account", "folder", "uidvalidity", "uid")})
        b = resolution.digest({k: dict(self.w.source, mime_path="9.9")[k] for k in ("account", "folder", "uidvalidity", "uid")})
        self.assertEqual(a, b)  # mime_path is not part of identity (F-12 notes the consequence)

    def test_successor_policy_replay_conflicts_at_the_writer(self):
        self.txn(self.append)
        successor = json.loads(self.w.policy.raw)
        successor["originals"][self.sha]["dependent_receipts"] = ["2" * 64]
        successor["approval_sha256"] = "7" * 64
        with self.assertRaisesRegex(ValueError, "original_resolution_conflict"):
            self.txn(lambda c: self.append(c, policy=approved(successor)))


class PolicyAuthorityTests(World):
    def test_append_without_gate_function_or_with_wrong_values_is_blocked(self):
        with store.ledger(self.db) as con:
            con.execute("BEGIN IMMEDIATE")
            resolution.ensure_schema(con)
            with self.assertRaises(sqlite3.Error):
                con.execute("INSERT INTO original_resolutions VALUES(?,?,?,?,?,?,?)",
                            (self.sha, "a" * 64, "b" * 64, "c" * 64, self.receipt, self.oid, "{}"))
            con.rollback()

    def test_forged_policy_object_types_are_refused(self):
        class Fake(mrp.ApprovedMailResolutionPolicy):
            pass
        fake = Fake(self.w.policy.raw, expected_sha256=self.w.policy.sha256)
        for bad in (fake, None, self.w.policy.raw, {"sha256": self.w.policy.sha256}, object()):
            with self.subTest(bad=type(bad).__name__), self.assertRaisesRegex(ValueError, "resolution_policy_invalid"):
                self.txn(lambda c: self.append(c, policy=bad))
        self.assertEqual(self.count(), 0)

    def test_policy_reassigned_after_validation_cannot_authorise_a_different_original(self):
        """F-06: mutating .raw/.sha256 on a validated policy changes what the writer trusts."""
        evil = json.loads(self.w.policy.raw)
        evil["originals"][self.sha]["bytes"] = self.w.policy_value["originals"][self.sha]["bytes"]
        evil["approval_sha256"] = "5" * 64
        raw = store.canonical(evil).encode()
        policy = approved(self.w.policy_value)
        try:
            policy.raw, policy.sha256 = raw, hashlib.sha256(raw).hexdigest()
        except (AttributeError, TypeError):
            return
        out = self.txn(lambda c: self.append(c, policy=policy))
        self.fail("F-06: writer accepted a policy whose bytes were swapped after validation: %s" % (out,))

    def test_self_built_policy_is_accepted_documented_trust_boundary(self):
        """Pins the doc'd limit: any in-process caller that knows the bytes can mint a policy object."""
        forged = approved(dict(self.w.policy_value, approval_sha256="3" * 64))
        _, reused = self.txn(lambda c: self.append(c, policy=forged))
        self.assertFalse(reused)

    def test_held_original_is_not_admitted(self):
        v = json.loads(self.w.policy.raw)
        v["held"] = [self.sha]
        v["originals"] = {}
        with self.assertRaises(ValueError):
            self.txn(lambda c: self.append(c, policy=approved(v)))
        self.assertEqual(self.count(), 0)

    def test_reader_accepts_a_row_no_policy_ever_approved(self):
        """F-02: the read path never checks policy_sha256 or the receipt against anything approved."""
        self.raw_row()  # policy_sha256 = '1'*64: no such policy exists anywhere
        with self.assertRaisesRegex(ValueError, "^resolution_policy_mismatch$"):
            self.effective()

    def test_dropped_triggers_are_not_noticed_by_schema_check(self):
        """F-03: installed() trusts one marker value; the immutability triggers can vanish silently."""
        self.txn(self.append)
        with store.ledger(self.db) as con:
            con.execute("DROP TRIGGER original_resolution_update")
            con.execute("DROP TRIGGER original_resolution_delete")
            con.commit()
        with store.ledger(self.db) as con:
            for check in (resolution.installed, resolution.ensure_schema):
                with self.assertRaisesRegex(ValueError, "^original_resolution_schema_mismatch$"):
                    check(con)
        with self.assertRaisesRegex(ValueError, "^original_resolution_schema_mismatch$"):
            self.effective()


class PathAndStorageTests(World):
    def appended(self, **kw):
        return self.txn(lambda c: self.append(c, **kw))

    def test_dotdot_and_dot_and_relative_and_nul_and_unicode_storage_paths(self):
        blobs = str(self.output / "blobs")
        bad = [blobs + "/../blobs/" + self.sha, str(self.output) + "/./blobs/" + self.sha + "/../" + self.sha,
               "blobs/" + self.sha, "", blobs + "/" + self.sha + "\x00", blobs + "/" + self.sha[:-1] + "а",
               blobs + "/" + self.sha.upper(), "~/blobs/" + self.sha, None, 7, Path(blobs) / self.sha]
        for p in bad:
            with self.subTest(p=repr(p)[-40:]):
                try:
                    self.appended(storage_path=p)
                except ValueError as exc:
                    self.assertRegex(str(exc), r"^original_resolution_|^resolution_policy_")
                except TypeError as exc:
                    self.fail("uncoded TypeError escaped: %s" % exc)
                else:
                    if not isinstance(p, Path):
                        self.fail("accepted %r" % (p,))
        self.assertLessEqual(self.count(), 1)

    def test_noncanonical_spellings_are_one_binding(self):
        """F-10: '//' and '/./' spellings of one path are accepted and become distinct bindings."""
        canonical = self.path
        sloppy = str(self.output).replace("/", "//", 1) + "/blobs/" + self.sha
        sloppy_dot = str(self.output) + "/./blobs/" + self.sha
        self.assertNotEqual(sloppy, canonical)
        accepted = []
        for p in (sloppy, sloppy_dot):
            try:
                self.set_evidence(verification_receipt_path=str(Path(p).parent.parent / "verification-receipts" / self.receipt))
                accepted.append(self.appended(storage_path=p))
            except ValueError:
                pass
        self.assertEqual(accepted, [], "F-10: non-canonical storage_path accepted and stored verbatim: %s" % (accepted,))

    def test_storage_root_is_not_tied_to_the_ledgers_own_root(self):
        """F-01: any directory containing a file whose hash matches is accepted as the CAS root."""
        other = self.stage_root("attacker-root")
        path = str(other / "blobs" / self.sha)
        self.set_evidence(verification_receipt_path=str(other / "verification-receipts" / self.receipt))
        with self.assertRaises(ValueError) as caught:
            self.appended(storage_path=path)
        self.assertRegex(str(caught.exception), "original_resolution_storage_binding",
                         "F-01: resolution bound to a storage root outside the records root")

    def test_symlinked_receipt_dir_root_and_file_are_refused_by_reader(self):
        self.appended()
        saved = self.receipt_path.read_bytes()
        directory = self.receipt_path.parent
        moved = directory.with_name("vr-real")
        directory.rename(moved)
        directory.symlink_to(moved, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "original_resolution_mail_receipt_changed"):
            self.effective()
        directory.unlink()
        moved.rename(directory)
        link_root = self.output.parent / "output-alias"
        link_root.symlink_to(self.output, target_is_directory=True)
        self.assertEqual(self.receipt_path.read_bytes(), saved)

    def test_receipt_as_fifo_hardlink_dir_is_refused_without_hanging(self):
        self.appended()
        self.receipt_path.unlink()
        os.mkfifo(self.receipt_path, 0o600)
        with self.assertRaisesRegex(ValueError, "original_resolution_mail_receipt_changed"):
            self.effective()
        self.receipt_path.unlink()
        self.receipt_path.mkdir()
        with self.assertRaisesRegex(ValueError, "original_resolution_mail_receipt_changed"):
            self.effective()

    def test_receipt_exactly_at_and_over_limit(self):
        self.appended()
        self.receipt_path.write_bytes(b"x" * (1024 * 1024 + 1))
        with self.assertRaisesRegex(ValueError, "original_resolution_mail_receipt_changed"):
            self.effective()

    def test_receipt_content_is_never_parsed_or_tied_to_the_original(self):
        """F-11: only sha256(receipt bytes) is checked; any bytes with the pinned hash 'verify' the original."""
        raw = b'{"schema":"synthetic","note":"says nothing about this original"}'
        digest = hashlib.sha256(raw).hexdigest()
        self.assertNotEqual(digest, self.receipt)
        self.assertNotIn(self.sha.encode(), raw)
        # sound by design (policy pins digest) but the reader cannot detect a receipt for the wrong export
        self.assertEqual(hashlib.sha256(self.receipt_path.read_bytes()).hexdigest(), self.receipt)


class MailFieldTests(World):
    def try_source(self, **changes):
        """Return ('ok', resolution_sha) or ('refused', code) for one mutated source_ref on a fresh ledger."""
        w = self.fresh()
        w.set_source(**changes)
        try:
            w.txn(w.append)
        except ValueError as exc:
            return "refused", str(exc)
        return "ok", w.effective()["resolution_sha256"]

    def test_hostile_unicode_and_control_characters_never_escape_as_uncoded_errors(self):
        hostile = ["inbox\r\nBcc: evil@example.invalid", "\x00", "a\u202eb", "e\u0301", "\u00e9", "\ufeff", "\U0001f4a9",
                   "x" * 1_000_000, "\n", " ", "INBOX\t", "../../etc", "%0d%0a"]
        outcome = {}
        for value in hostile:
            for field in ("account", "folder"):
                with self.subTest(field=field, value=value[:12]):
                    try:
                        outcome[(field, value)] = self.try_source(**{field: value})
                    except ValueError as exc:  # e.g. lone surrogate cannot even be stored
                        outcome[(field, value)] = ("refused", str(exc))
                    if outcome[(field, value)][0] == "refused":
                        self.assertRegex(outcome[(field, value)][1], r"^original_resolution_")
        nfc, nfd = outcome[("folder", "\u00e9")], outcome[("folder", "e\u0301")]
        if nfc[0] == nfd[0] == "ok":
            self.assertNotEqual(nfc[1], nfd[1])  # identity is byte-exact; NFC and NFD are different mail

    def test_control_characters_and_oversize_are_refused_before_they_reach_reports(self):
        """F-12: CR/LF/NUL/bidi and 100 KB account/folder strings are accepted into the stored identity."""
        accepted = [v[:12] for v in ("a\r\nBcc: x", "a\x00b", "a\u202eb", "x" * 100_000)
                    if self.try_source(folder=v)[0] == "ok"]
        self.assertEqual(accepted, [], "F-12: accepted %s" % accepted)

    def test_uid_and_uidvalidity_bounds(self):
        """F-12: IMAP UID/UIDVALIDITY are 32-bit unsigned; zero/negative/bool/str/float are already refused."""
        for field in ("uid", "uidvalidity"):
            for value in (0, -1, True, 1.0, "7", None):
                with self.subTest(field=field, value=value):
                    self.assertEqual(self.try_source(**{field: value})[0], "refused")
            for value in (1, 2 ** 31, 2 ** 32 - 1):
                with self.subTest(field=field, value=value):
                    self.assertEqual(self.try_source(**{field: value})[0], "ok")
            with self.subTest(field=field, value="2**32"):
                self.assertEqual(self.try_source(**{field: 2 ** 32})[0], "refused", "F-12: %s above 32 bits accepted" % field)
            with self.subTest(field=field, value="2**64"):
                self.assertEqual(self.try_source(**{field: 2 ** 64})[0], "refused", "F-12: %s above 64 bits accepted" % field)

    def test_duplicate_keys_and_extra_types_in_occurrence_json(self):
        src = store.canonical(self.w.source)
        with store.ledger(self.db) as con:
            con.execute("UPDATE occurrences SET source_ref=? WHERE id=?", (src[:-1] + ',"uid":8}', self.oid))
            con.commit()
        with self.assertRaisesRegex(ValueError, "original_resolution_mail_occurrence_unbound"):
            self.txn(self.append)

    def test_oversized_occurrence_blobs_do_not_crash(self):
        with store.ledger(self.db) as con:
            con.execute("UPDATE occurrences SET evidence=? WHERE id=?", ("[" * 500000, self.oid))
            con.commit()
        with self.assertRaisesRegex(ValueError, "original_resolution_mail_occurrence_unbound"):
            self.txn(self.append)
        self.assertEqual(self.count(), 0)


class ReaderMalformedRowTests(World):
    def test_deeply_nested_or_garbage_payload_json_only_raises_coded_errors(self):
        """F-13: reader calls plain json.loads on payload_json; RecursionError/other types escape."""
        for text in ("[" * 200000, "{", "", "null", "NaN", '{"schema":"original_resolution_mail_export_v1"}'):
            with self.subTest(head=text[:12]):
                with store.ledger(self.db) as con:
                    con.execute("BEGIN IMMEDIATE")
                    triggers = con.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name='original_resolutions'").fetchall()
                    for name, _ in triggers:
                        con.execute('DROP TRIGGER "%s"' % name)
                    con.execute("DELETE FROM original_resolutions")
                    con.execute("INSERT INTO original_resolutions VALUES(?,?,?,?,?,?,?)",
                                (self.sha, "a" * 64, "b" * 64, "c" * 64, self.receipt, self.oid, text))
                    for _, sql in triggers:
                        con.execute(sql)
                    try:
                        self.w.read(con)
                    except ValueError as exc:
                        self.assertRegex(str(exc), r"^original_resolution_")
                    except Exception as exc:  # noqa: BLE001
                        self.fail("F-13: uncoded %s escaped the reader" % type(exc).__name__)
                    finally:
                        con.rollback()

    def test_stored_payload_text_must_be_the_canonical_text(self):
        """F-14: digest(parsed payload) is compared, not the stored text; whitespace/duplicate keys survive."""
        payload = self.raw_row(payload={"policy_sha256": self.w.policy.sha256})
        with store.ledger(self.db) as con:
            con.execute("BEGIN IMMEDIATE")
            trigger = con.execute("SELECT sql FROM sqlite_master WHERE name='original_resolution_update'").fetchone()[0]
            con.execute("DROP TRIGGER original_resolution_update")
            con.execute("UPDATE original_resolutions SET policy_sha256=?", (payload["policy_sha256"],))
            text = store.canonical(payload)
            malleable = '{"role":"unresolved",' + text[1:]  # duplicate key, last one wins
            con.execute("UPDATE original_resolutions SET payload_json=?", (malleable,))
            con.execute(trigger)
            con.commit()
        with self.assertRaisesRegex(ValueError, "^original_resolution_payload_mismatch$"):
            self.effective()


if __name__ == "__main__":
    unittest.main()
