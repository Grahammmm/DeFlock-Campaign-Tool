"""Opt-in unattended orchestration; no models, activation or hold-clearing interface."""
import argparse
import hashlib
import importlib
import json
import os
from pathlib import Path
import stat
import sys

from . import run as manual
from . import extraction_routes
from .intake import eml_export, folder, imap_intake
from .ledger import store
from .runner.canonical_mail import CanonicalMailBackend
from .run_safety import EXPECTED_HELDS, EXTRACTION_FAULTS, RunSafety, RunSafetyPolicy, digest, fixed_code


def _private_bytes(path, raw):
    store.checked_path(path, existing=False)
    store.private_parent(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)


def _bounded_read(path):
    with folder.secure_open(path) as stream:
        before = os.fstat(stream.fileno())
        if before.st_uid != os.getuid() or stat.S_IMODE(before.st_mode) & 0o077 or before.st_size > eml_export.MAX_MESSAGE:
            raise ValueError("retention_integrity_failed")
        raw = stream.read(eml_export.MAX_MESSAGE + 1)
        after = os.fstat(stream.fileno())
    fields = ("st_dev", "st_ino", "st_uid", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns")
    if len(raw) > eml_export.MAX_MESSAGE or len(raw) != before.st_size or any(getattr(before, k) != getattr(after, k) for k in fields):
        raise ValueError("retention_integrity_failed")
    return raw


class GuardedIMAPIntake(imap_intake.IMAPIntake):
    def __init__(self, *args, safety, preserve_raw, supported_receipt, **kwargs):
        super().__init__(*args, **kwargs)
        self.safety, self.preserve_raw, self.supported_receipt = safety, preserve_raw, supported_receipt

    def new_uids(self, client, after):
        status, data = client.uid("SEARCH", None, "UID " + str(after + 1) + ":*")
        if status != "OK":
            raise imap_intake.IntakeError("imap_search_failed")
        raw = b" ".join(item for item in (data or []) if isinstance(item, bytes))
        if len(raw) > 4 * 1024 * 1024:
            raise imap_intake.IntakeError("imap_search_failed")
        return sorted({int(value) for value in raw.split() if value.isdigit() and int(value) > after})

    def _checkpoint(self, name):
        with store.ledger(self.ledger) as con:
            checkpoint = super()._checkpoint(con, name)
        return {"uidvalidity": checkpoint[0], "highest_uid": checkpoint[1]} if checkpoint else None

    def _save(self, name, validity, uid, success=True):
        with store.ledger(self.ledger) as con:
            super()._save(con, name, validity, uid, success)

    def run(self):
        report = {"schema": "records-imap-intake-report-v1", "account": self.config["account_id"],
                  "folders": [], "preserved": 0, "failures": 0, "unconfigured_folders": [],
                  "attempted": 0, "deferred": 0, "limit_reached": False, "failure_codes": []}
        client = self.client_factory(self.config)
        try:
            available = self.list_folders(client)
            wanted = self.config["folders"] if self.config["folders"] is not None else available
            report["unconfigured_folders"] = sorted(set(available) - set(wanted))
            for name in wanted:
                entry = {"folder": name, "new": 0, "preserved": 0, "attempted": 0,
                         "deferred": 0, "failed": None, "uidvalidity_reset": False}
                report["folders"].append(entry)
                if name not in available:
                    entry["failed"] = "configured_folder_missing"
                    report["failures"] += 1
                    report["failure_codes"].append(entry["failed"])
                    continue
                if self.safety.remaining_seconds() <= 0:
                    self.safety.check()
                    entry["visibility_deferred"] = "time_budget_exceeded"
                    continue
                try:
                    validity = self.select(client, name)
                    checkpoint = self._checkpoint(name)
                    after = checkpoint["highest_uid"] if checkpoint and checkpoint["uidvalidity"] == validity else 0
                    entry["uidvalidity_reset"] = bool(checkpoint and checkpoint["uidvalidity"] != validity)
                    uids = self.new_uids(client, after)
                    entry["new"] = len(uids)
                    if not checkpoint or entry["uidvalidity_reset"]:
                        self._save(name, validity, 0, success=False)
                except Exception:
                    entry["failed"] = "folder_visibility_failed"
                    report["failures"] += 1
                    report["failure_codes"].append(entry["failed"])
                    continue
                for position, uid in enumerate(uids):
                    identity = digest([self.config["account_id"], name, validity, uid])
                    if not self.safety.begin("intake", identity):
                        entry["deferred"] = len(uids) - position
                        break
                    entry["attempted"] += 1
                    report["attempted"] += 1
                    original = None
                    try:
                        raw = self.fetch(client, uid)
                        original = self.preserve_raw(raw, identity)
                        receipt = eml_export.export_message(raw, mail_root=self.mail_root, account=self.config["account_id"],
                                                            mailbox=name, uidvalidity=validity, uid=uid)
                        self.backend.preserve(receipt, self.config["account_id"], manual.Folder(name, validity), uid)
                        held = self.supported_receipt(receipt)
                        if held:
                            self.safety.finish("held", "decoder_needed", held)
                            entry["failed"] = "decoder_needed"
                            report["failure_codes"].append("decoder_needed")
                            entry["deferred"] = len(uids) - position - 1
                            break
                        self._save(name, validity, uid)
                        self.safety.finish()
                        entry["preserved"] += 1
                        report["preserved"] += 1
                    except Exception as error:
                        code = fixed_code(error)
                        self.safety.finish("held" if code in EXPECTED_HELDS else "fault", code,
                                           (original,) if original else ())
                        entry["failed"] = code
                        report["failure_codes"].append(code)
                        report["failures"] += int(code not in EXPECTED_HELDS)
                        entry["deferred"] = len(uids) - position - 1
                        break
                report["deferred"] += entry["deferred"]
            report["limit_reached"] = self.safety.phases["intake"]["attempted"] >= self.safety.policy.max_fetches
            report["stop_code"] = self.safety.state["hold"]
            return report
        finally:
            try:
                client.logout()
            except Exception:
                pass


class UnattendedPipeline(manual.Pipeline):
    def __init__(self, *args, policy=None, clock=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.policy, self.clock = policy or RunSafetyPolicy(), clock
        self.safety = None

    def _retain(self, raw, identity):
        if not isinstance(raw, bytes) or not raw or len(raw) > eml_export.MAX_MESSAGE:
            raise ValueError("retention_integrity_failed")
        original = hashlib.sha256(raw).hexdigest()
        directory = self.root.sub("mail") / "retained"
        store.checked_path(directory, existing=False)
        directory.mkdir(mode=0o700, exist_ok=True)
        path = directory / (original + ".eml")
        try:
            _private_bytes(path, raw)
        except FileExistsError:
            if hashlib.sha256(_bounded_read(path)).hexdigest() != original:
                raise ValueError("retention_integrity_failed")
        key = "unattended-input:" + identity
        record = {"schema": "retained-mail-input-v1", "identity_sha256": identity,
                  "original_sha256": original, "bytes": len(raw), "retained": True}
        with store.ledger(self.root.ledger) as con:
            prior = con.execute("SELECT value FROM ledger_meta WHERE key=?", (key,)).fetchone()
            if prior and json.loads(prior[0]) != record:
                raise ValueError("retention_identity_conflict")
            con.execute("INSERT OR IGNORE INTO ledger_meta(key,value) VALUES(?,?)", (key, json.dumps(record, sort_keys=True)))
            con.commit()
        return original

    @staticmethod
    def _unsupported(receipt):
        forms = eml_export.attachment_forms(receipt)
        decoder_ready = False
        if "msg" in forms.values():
            try:
                importlib.import_module("extract_msg")
                decoder_ready = True
            except Exception:
                pass
        return tuple(subject for subject, form in forms.items()
                     if form not in extraction_routes.SUPPORTED and form not in extraction_routes.IMAGES
                     and not (form == "msg" and decoder_ready))

    def ingest_mailbox(self, mail_config, client_factory=None):
        config = imap_intake.load_config(mail_config)
        bound = config.get("max_messages_per_run", 200)
        if type(bound) is not int or not 1 <= bound <= 200:
            raise ValueError("invalid_run_safety_policy")
        self.safety.policy = RunSafetyPolicy(self.policy.max_originals, min(self.policy.max_fetches, bound), self.policy.seconds)
        config["timeout"] = min(config["timeout"], max(1, self.safety.remaining_seconds()))
        backend = CanonicalMailBackend(self.root.sub("mail"), self.root.intake, self.root.ledger)
        identity = self._identity()
        identity["run_id"] = "imap-" + self.run_id
        backend.start_run(identity)
        try:
            report = GuardedIMAPIntake(config=config, ledger=self.root.ledger, mail_root=self.root.sub("mail"),
                                       backend=backend, alert=self._alert, client_factory=client_factory,
                                       safety=self.safety, preserve_raw=self._retain, supported_receipt=self._unsupported).run()
        except BaseException:
            backend.finish_run(identity, "failed", {"failures": 1})
            raise
        backend.finish_run(identity, "completed_with_gaps" if report["failures"] or report["stop_code"] else "slice_completed",
                           {"messages": report["attempted"], "preserved": report["preserved"], "failures": report["failures"]})
        return report

    def ingest_inbox(self, inbox):
        files = sorted(Path(inbox).glob("*.eml"))
        report = {"messages": len(files), "preserved": [], "replayed": [], "failures": [], "deferred": 0}
        backend = CanonicalMailBackend(self.root.sub("mail"), self.root.intake, self.root.ledger)
        identity = self._identity()
        identity["run_id"] = "local-" + self.run_id
        backend.start_run(identity)
        try:
            for position, path in enumerate(files):
                binding = digest([self.account, "local", str(path.absolute())])
                if not self.safety.begin("intake", binding):
                    report["deferred"] = len(files) - position
                    break
                original = None
                try:
                    raw = _bounded_read(path)
                    original = self._retain(raw, binding)
                    uid = eml_export.local_uid(original)
                    receipt = eml_export.export_message(raw, mail_root=self.root.sub("mail"), account=self.account,
                                                       mailbox="inbox", uidvalidity=1, uid=uid)
                    seen = self._query("SELECT count(*) AS n FROM mail_messages WHERE account=? AND folder='inbox' AND uidvalidity=1 AND uid=?", (self.account, uid))[0]["n"]
                    backend.preserve(receipt, self.account, manual.Folder("inbox", 1), uid)
                    held = self._unsupported(receipt)
                    if held:
                        self.safety.finish("held", "decoder_needed", held)
                        report["failures"].append({"identity_sha256": binding, "code": "decoder_needed", "kind": "held"})
                        report["deferred"] = len(files) - position - 1
                        break
                    self.safety.finish()
                    report["replayed" if seen else "preserved"].append({"eml_sha256": original})
                except Exception as error:
                    code = fixed_code(error)
                    self.safety.finish("held" if code in EXPECTED_HELDS else "fault", code, (original,) if original else ())
                    report["failures"].append({"identity_sha256": binding, "code": code,
                                               "kind": "held" if code in EXPECTED_HELDS else "fault"})
                    report["deferred"] = len(files) - position - 1
                    if self.safety.stopped:
                        break
        finally:
            backend.finish_run(identity, "completed_with_gaps" if report["failures"] else "slice_completed",
                               {"messages": len(report["preserved"]), "failures": len(report["failures"])})
        return report

    def _advance_resume(self):
        path = self.root.sub("reports") / "unattended-advance-resume.json"
        empty = {"schema": "records-advance-resume-v1", "cursor": None, "frontier": None}
        if not path.exists() and not path.is_symlink():
            return path, empty
        try:
            raw = _bounded_read(path)
            if len(raw) > 4096:
                raise ValueError("invalid_safety_state")
            def pairs(items):
                value = {}
                for key, item in items:
                    if key in value:
                        raise ValueError("invalid_safety_state")
                    value[key] = item
                return value
            state = json.loads(raw, object_pairs_hook=pairs)
            if type(state) is not dict or set(state) != set(empty) or state["schema"] != empty["schema"]:
                raise ValueError("invalid_safety_state")
            for key in ("cursor", "frontier"):
                item = state[key]
                if item is not None and not (
                    type(item) is list and len(item) == 2 and
                    type(item[0]) is str and 0 < len(item[0]) <= 128 and
                    type(item[1]) is str and len(item[1]) == 64 and
                    all(c in "0123456789abcdef" for c in item[1])
                ):
                    raise ValueError("invalid_safety_state")
            if state["cursor"] is not None and (state["frontier"] is None or state["cursor"] > state["frontier"]):
                raise ValueError("invalid_safety_state")
            return path, state
        except Exception:
            self.safety.stop("invalid_safety_state")
            raise manual.RunError("invalid_safety_state") from None

    def _save_advance_resume(self, path, state):
        store.checked_path(path, existing=False)
        temporary = path.with_name(".advance-resume-" + os.urandom(16).hex())
        try:
            _private_bytes(temporary, json.dumps(state, sort_keys=True).encode("ascii"))
            os.replace(temporary, path)
            directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if temporary.exists():
                temporary.unlink()

    def advance_all(self, subjects=None):
        stage = self._open_stage_run()
        self.review_holds_retained = 0
        resume = None
        keys = {}
        outside_sweep = 0
        if subjects is None:
            rows = self._query(
                "SELECT DISTINCT o.sha256,o.first_seen_at FROM originals o JOIN stage_state s ON s.original_sha256=o.sha256 "
                "WHERE o.scope!='out_of_scope' AND s.stage!='preserve' AND s.status NOT IN ('done','inapplicable') "
                "ORDER BY o.first_seen_at,o.sha256")
            if len(rows) > 10000:
                self.safety.stop("invalid_safety_state")
                raise manual.RunError("invalid_safety_state")
            path, resume = self._advance_resume()
            keys = {row["sha256"]: [row["first_seen_at"], row["sha256"]] for row in rows}
            if resume["frontier"] is None and rows:
                resume["frontier"] = max(keys.values())
            selected = [row["sha256"] for row in rows if
                        (resume["cursor"] is None or keys[row["sha256"]] > resume["cursor"]) and
                        (resume["frontier"] is None or keys[row["sha256"]] <= resume["frontier"])]
            outside_sweep = sum(key > resume["frontier"] for key in keys.values()) if resume["frontier"] else 0
        else:
            selected = list(dict.fromkeys(subjects))
        validated_holds = False
        report = []
        for subject in selected:
            # Metadata-only acknowledgement is bounded too, and never clears a hold.
            if len(report) >= 400 or not self.safety.check():
                break
            states = self._states(subject)
            first = next((name for name in manual.STAGE_ORDER
                          if states[name]["status"] not in {"done", "inapplicable"}), None)
            if first is not None and first != "privacy" and states[first]["status"] == "blocked" and states[first]["receipt_sha256"]:
                if not validated_holds:
                    # Validates receipt authority, transition revision and current input bindings.
                    manual.stages.counts(self.root.ledger)
                    validated_holds = True
                self.review_holds_retained += 1
                report.append({"subject_sha256": subject, "stages": {first: "held:review_required"},
                               "outcome": "review_required", "failure_code": "review_required", "attempted": False})
                if resume is not None:
                    resume["cursor"] = keys[subject]
                continue
            if not self.safety.begin("advance", subject):
                break
            outcome = {"subject_sha256": subject, "stages": {}}
            result_kind, code = "success", None
            for name in manual.STAGE_ORDER:
                if not self.safety.check():
                    result_kind, code = "fault", "time_budget_exceeded"
                    break
                state = self._states(subject)[name]
                if state["status"] in {"done", "inapplicable"}:
                    continue
                if state["status"] == "blocked" and state["receipt_sha256"]:
                    result_kind, code = "review_required", "review_required"
                    outcome["stages"][name] = "held:review_required"
                    break
                try:
                    if name == "extract" and not self.ocr_tools and self._form(subject, self._original(subject)) in extraction_routes.IMAGES:
                        result_kind, code = "review_required", "review_required"
                        outcome["stages"][name] = "held:image_review_required"
                        break
                    self.extraction_timeout = min(self.extraction_timeout, max(1, self.safety.remaining_seconds()))
                    result = getattr(self, "stage_" + name)(subject, stage)
                    status = result.get("status")
                    if status not in {"done", "inapplicable", "pending", "blocked"}:
                        result_kind, code = "fault", "invalid_stage_result"
                        break
                    outcome["stages"][name] = status
                    if status in {"done", "inapplicable"}:
                        continue
                    if name == "privacy":
                        result_kind, code = "fault", "privacy_redaction_fault"
                    elif name == "extract":
                        reason = result.get("reason", "")
                        tokens = reason.split(":", 1)[-1].split(",") if type(reason) is str else []
                        known = next((item for item in tokens if item in EXTRACTION_FAULTS), None)
                        if "decoder_needed" in tokens:
                            result_kind, code = "held", "decoder_needed"
                        elif known:
                            result_kind, code = "fault", known
                        else:
                            result_kind, code = "review_required", "review_required"
                    else:
                        result_kind, code = "review_required", "review_required"
                    break
                except Exception:
                    result_kind, code = "fault", "privacy_redaction_fault" if name in {"review", "privacy"} else "stage_failed"
                    outcome["stages"][name] = "error:" + code
                    break
            self.safety.finish(result_kind, code, (subject,) if result_kind != "success" else ())
            if code:
                outcome["failure_code"] = code
                outcome["outcome"] = result_kind
            report.append(outcome)
            if resume is not None:
                resume["cursor"] = keys[subject]
        self.originals_deferred = len(selected) - len(report) + outside_sweep
        if resume is not None:
            if len(report) == len(selected):
                resume["cursor"] = resume["frontier"] = None
            self._save_advance_resume(path, resume)
        return report

    def run(self, inbox=None, mail_config=None, client_factory=None):
        with manual.root_lock(self.root.path):
            started = manual.now()
            self.safety = RunSafety(self.root.ledger, self.policy, **({"clock": self.clock} if self.clock else {}))
            self._open_stage_run()
            intake, mailbox, progress = None, None, []
            self.originals_deferred = 0
            self.review_holds_retained = 0
            interrupted = None
            try:
                if self.model is not None or self.challenge is not None:
                    self.safety.stop("model_configuration_forbidden")
                if self.safety.check() and inbox:
                    intake = self.ingest_inbox(inbox)
                if self.safety.check() and mail_config:
                    mailbox = self.ingest_mailbox(mail_config, client_factory)
                if self.safety.check():
                    progress = self.advance_all()
            except (KeyboardInterrupt, SystemExit) as error:
                self.safety.stop("interrupted_attempt")
                interrupted = error
            except Exception:
                self.safety.stop("unexpected_run_fault")
            held = self.safety.state["hold"] in EXPECTED_HELDS
            gaps = bool(self.review_holds_retained) or any(p["failed"] or p["held"] or p["review_required"] for p in self.safety.phases.values())
            gaps = gaps or bool(mailbox and mailbox["failures"]) or bool(intake and intake["failures"])
            status = "interrupted" if interrupted else "held" if held else "failed" if self.safety.stopped else "completed_with_gaps" if gaps else "completed"
            summary = manual.stages.counts(self.root.ledger)
            counts = summary["stages"]
            intake_deferred = (intake or {}).get("deferred", 0) + (mailbox or {}).get("deferred", 0)
            report = {"schema": "records-run-report-v1", "engine": manual.ENGINE, "run_id": self.run_id,
                      "started_at": started, "ended_at": manual.now(), "status": status,
                      "exit_code": 2 if self.safety.stopped else 3 if gaps else 0,
                      "intake": intake, "mailbox": mailbox, "subjects": progress, "counts": counts,
                      "version": manual.__version__, "config_sha256": self.config_sha256,
                      "recovery": {"interrupted_runs": [], "recovered_leases": []},
                      "originals": summary["originals"],
                      "proposals_awaiting_owner": len(self._query("SELECT id FROM proposals WHERE owner_approval='none'")),
                      "safety": self.safety.report(), "originals_deferred": self.originals_deferred,
                      "intake_deferred": intake_deferred,
                      "end_to_end_complete": summary["candidate_seven_stage_complete"],
                      "review_holds_retained": self.review_holds_retained,
                      "model_id": None, "challenge_model_id": None}
            report_raw = manual.encoded(report)
            report_sha256 = None
            try:
                _private_bytes(self.root.sub("runs") / (self.run_id + ".json"), report_raw)
                report_sha256 = hashlib.sha256(report_raw).hexdigest()
            except Exception:
                # Never leave completed ledger evidence for a missing/partial final receipt.
                self.safety.stop("report_write_failed")
                status = "interrupted" if interrupted else "failed"
                report.update(status=status, exit_code=2,
                              failure_code="report_write_failed", receipt_status="write_failed",
                              safety=self.safety.report())
            with store.ledger(self.root.ledger) as con:
                con.execute("UPDATE runs SET status=?,ended_at=?,summary=? WHERE run_id=?",
                            (status, report["ended_at"], json.dumps({"status": status, "safety": report["safety"],
                                                                  "subjects": len(progress), "originals_deferred": self.originals_deferred,
                                                                  "report_sha256": report_sha256,
                                                                  "failure_code": report.get("failure_code")}, sort_keys=True), self.run_id))
                con.commit()
            if interrupted:
                raise interrupted
            return report


def main(argv=None):
    parser = argparse.ArgumentParser(prog="records run --unattended")
    parser.add_argument("--unattended", action="store_true")
    parser.add_argument("--root", required=True)
    parser.add_argument("--inbox")
    parser.add_argument("--mail-config")
    parser.add_argument("--jurisdiction", default="us-ca")
    parser.add_argument("--account", default="local")
    parser.add_argument("--event-date")
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--max-originals-per-run", type=int, default=200)
    parser.add_argument("--ocr", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        policy = RunSafetyPolicy(max_originals=args.max_originals_per_run)
        tools, signature = manual.local_ocr_tools() if args.ocr else (None, None)
        pipeline = UnattendedPipeline(args.root, jurisdiction=args.jurisdiction, account=args.account,
                                      event_date=manual.date.fromisoformat(args.event_date) if args.event_date else None,
                                      extraction_timeout=args.timeout, policy=policy,
                                      ocr_tools=tools, ocr_tool_signature=signature)
        # Check configuration presence, never inspect or report environment values.
        forbidden_model = any(os.environ.get(prefix + "MODEL_BASE_URL") for prefix in ("", "CHALLENGE_"))
        if forbidden_model or (args.ocr and tools is None):
            with manual.root_lock(pipeline.root.path):
                safety = RunSafety(pipeline.root.ledger, policy)
                safety.stop("model_configuration_forbidden" if forbidden_model else "ocr_runtime_unavailable")
        report = pipeline.run(args.inbox, args.mail_config)
    except Exception:
        print(json.dumps({"status": "failed", "exit_code": 2, "failure_code": "unattended_setup_failed"}))
        return 2
    print(json.dumps(report, sort_keys=True) if args.json else "records run: " + report["status"])
    return report["exit_code"]
