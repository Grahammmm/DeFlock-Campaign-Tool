"""``records run``: one command that moves every record in a private root through
preserve, extract, catalog, detect, review (with an independent challenge),
compare and privacy, using the installed WP1-WP8 adapters and one ledger.

Nothing here publishes, sends, or contacts a provider. Model calls are
optional and only ever see locally redacted text. Approval and publication
are separate commands (``records approve`` / ``records publish``) so that the
owner's decision stays outside the automated path.
"""
import argparse
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import uuid

from campaign_tool import __version__
from campaign_tool.digest.build import build_digest, redact_units
from campaign_tool.digest.detectors import DETECTOR_VERSION, run_detectors
from campaign_tool.digest.model import ModelConfig
from campaign_tool.digest.redact import PLACEHOLDER
from campaign_tool.law import load_package, rules_in_force
from . import challenge as challenge_mod
from . import extraction_routes
from . import stage_adapters
from . import review_retirement
from .catalog_stage import catalog_run_factory
from .extraction_ledger import ExtractionLedgerAdapter
from .extraction_validation import ExtractionStageAdapter, install_extraction_validator
from .intake import eml_export, folder as intake_folder
from .ledger import stages, store
from .runner.canonical_mail import CanonicalMailBackend
from .runner.contracts import Folder

ENGINE = "records-run-v1"
OWNER = "records-run"
STAGE_ORDER = ("extract", "catalog", "detect", "review", "compare", "privacy")
MAX_UNITS_PER_RECORD = 2000
DOC_TYPE_HINTS = (("policy", "policy"), ("agreement", "contract"), ("contract", "contract"),
                  ("invoice", "invoice"), ("audit", "audit"), ("retention", "retention-setting"),
                  ("denial", "denial-or-extension"), ("extension", "denial-or-extension"))


def sha(data):
    return hashlib.sha256(data).hexdigest()


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def now():
    return datetime.now(timezone.utc).isoformat()


_LOCKED_ROOTS = ContextVar("records_locked_roots", default=())

def require_root_lock(root):
    if str(Path(root).resolve()) not in _LOCKED_ROOTS.get():
        raise RunError("context_root_lock_required")


class RunError(RuntimeError):
    pass


@contextmanager
def root_lock(root):
    """Exclusive, non-blocking writer lock on ``<root>/run.lock``: one intake owner per root.

    A second ``records run`` on the same root fails at once with ``root_locked``
    instead of racing the first one for stage leases.
    """
    path = Path(root) / "run.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RunError("root_locked") from None
        token = _LOCKED_ROOTS.set((*_LOCKED_ROOTS.get(), str(Path(root).resolve())))
        try:
            yield
        finally:
            _LOCKED_ROOTS.reset(token)
    finally:
        os.close(fd)


class Root:
    """Private records root layout. Every directory is owner-only."""

    DIRS = ("mail", "intake", "intake/blobs", "artifacts", "artifacts/evidence", "extract", "ocr",
            "proposals", "proposals/public", "proposals/private", "outbox", "runs")

    def __init__(self, path):
        self.path = Path(os.path.abspath(path))
        if any(parent.is_symlink() for parent in (self.path, *self.path.parents)):
            raise RunError("symlink_root")

    def prepare(self):
        os.umask(0o077)
        self.path.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.path, 0o700)
        for name in self.DIRS:
            (self.path / name).mkdir(mode=0o700, exist_ok=True)
        # First-run initialisation happens under the root lock so two simultaneous
        # first runs cannot race the ledger's atomic link-into-place.
        with root_lock(self.path):
            if not self.ledger.exists():
                store.initialize(self.ledger)
            if not self.intake_db.exists():
                con = sqlite3.connect(self.intake_db)
                try:
                    con.executescript(intake_folder.SCHEMA)
                    con.execute("INSERT INTO runs VALUES(?,?,?,?,?,?)",
                                ("baseline", intake_folder.now(), intake_folder.now(), "inventory", "complete", "{}"))
                    con.execute("INSERT INTO meta VALUES('inventory_run','baseline')")
                    con.commit()
                finally:
                    con.close()
            os.chmod(self.intake_db, 0o600)
        return self

    @property
    def ledger(self):
        return self.path / "ledger.sqlite"

    @property
    def intake(self):
        return self.path / "intake"

    @property
    def intake_db(self):
        return self.intake / "intake.sqlite"

    def sub(self, name):
        return self.path / name


class Pipeline:
    def __init__(self, root, *, jurisdiction="us-ca", account="local", model=None, challenge=None,
                 redaction_allowlist=(), redaction_denylist=(), event_date=None, opener=None,
                 ocr_tools=None, ocr_tool_signature=None, extraction_timeout=180, native_workbook_policy=None,
                 ocr_vision=None):
        self.root = Root(root).prepare()
        self.jurisdiction, self.account = jurisdiction, account
        self.model, self.challenge, self.opener = model, challenge, opener
        self.allowlist, self.denylist = tuple(redaction_allowlist), tuple(redaction_denylist)
        self.event_date = event_date
        self.ocr_tools, self.ocr_tool_signature = ocr_tools, ocr_tool_signature
        if ocr_vision is not None and not ocr_tools:
            raise ValueError("ocr_vision_requires_ocr_tools")
        self.ocr_vision = ocr_vision
        self.extraction_timeout = extraction_timeout
        self.native_workbook_policy = native_workbook_policy
        self.package = load_package(jurisdiction)
        self.config = {"engine": ENGINE, "version": __version__, "jurisdiction": jurisdiction,
                       "law_package_sha256": sha(encoded(self.package)),
                       "event_date_declaration": event_date.isoformat() if event_date else None,
                       "detector_code_sha256": store.file_hash(__import__("campaign_tool.digest.detectors", fromlist=[""]).__file__),
                       "prompt_code_sha256": store.file_hash(__import__("campaign_tool.digest.model", fromlist=[""]).__file__),
                       "model_id": model.model_id if model else None,
                       "challenge_model_id": challenge.model.model_id if challenge else None,
                       "ocr": bool(ocr_tools)}
        if ocr_vision is not None:
            self.config["ocr_vision"] = {key: ocr_vision[key] for key in ("engine", "model", "signature")}
        if native_workbook_policy is not None:
            from .workbook_policy import NativeWorkbookPolicy
            if type(native_workbook_policy) is not NativeWorkbookPolicy:
                raise ValueError("trusted_workbook_policy_required")
            self.config["native_workbook_policy"] = native_workbook_policy.binding
        self.config_sha256 = sha(encoded(self.config))
        self.run_id = "run-" + uuid.uuid4().hex
        self._stage = None
        self._contexts = {}
        self.review_retirement = {"state": "complete", "processed": 0}

    # ----- intake -------------------------------------------------------------------------
    def _identity(self):
        return {"run_id": "mail-" + self.run_id, "config_sha256": self.config_sha256, "mode": "preparation",
                "runtime": {"version": ENGINE + "/" + __version__, "image_digest": "local:" + sys.platform,
                            "code_sha256": sha(Path(__file__).read_bytes()),
                            "image_verification": {"state": "local_process_unverified"}}}

    def ingest_inbox(self, inbox):
        """Export every ``*.eml`` under ``inbox`` and preserve it through the canonical mail backend."""
        inbox = Path(inbox)
        files = sorted(p for p in inbox.iterdir() if p.is_file() and p.suffix.lower() in {".eml", ".msg"}) if inbox.is_dir() else []
        backend = CanonicalMailBackend(self.root.sub("mail"), self.root.intake, self.root.ledger)
        identity = self._identity()
        backend.start_run(identity)
        preserved, replayed, failures = [], [], []
        try:
            for path in files:
                with intake_folder.secure_open(path) as stream:
                    raw = stream.read(64*1024**2 + 1)
                uid = eml_export.local_uid(sha(raw))
                try:
                    receipt = eml_export.export_message(raw, mail_root=self.root.sub("mail"), account=self.account,
                                                        mailbox="inbox", uidvalidity=1, uid=uid,
                                                        original_format=path.suffix.lower()[1:])
                    seen = self._query("SELECT count(*) AS n FROM mail_messages WHERE account=? AND folder='inbox' "
                                       "AND uidvalidity=1 AND uid=?", (self.account, uid))[0]["n"]
                    result = backend.preserve(receipt, self.account, Folder("inbox", 1), uid)
                    entry = {"file": path.name, "eml_sha256": result.eml_sha256, "documents": list(result.documents)}
                    (replayed if seen else preserved).append(entry)
                except Exception as error:  # one bad message never stops the run
                    failures.append({"file": path.name, "error": type(error).__name__, "detail": "fetch_or_preserve_failed"})
            status = "slice_completed" if not failures else "completed_with_gaps"
            backend.finish_run(identity, status, {"messages": len(files), "preserved": len(preserved),
                                                 "replayed": len(replayed), "failures": len(failures)})
        except BaseException:
            backend.finish_run(identity, "failed", {"messages": len(files), "preserved": len(preserved)})
            raise
        return {"messages": len(files), "preserved": preserved, "replayed": replayed, "failures": failures}

    def ingest_mailbox(self, mail_config, *, client_factory=None):
        """Fetch new messages over IMAP (owner-only config file) and preserve them.

        Checkpoints live in the ledger per (account, folder) and advance only past
        messages that were fully preserved. Credentials never enter the report.
        """
        from .intake import imap_intake
        config = imap_intake.load_config(mail_config)
        backend = CanonicalMailBackend(self.root.sub("mail"), self.root.intake, self.root.ledger)
        identity = self._identity()
        identity["run_id"] = "imap-" + self.run_id
        identity["mail_config_sha256"] = imap_intake.fingerprint(config)
        backend.start_run(identity)
        intake = imap_intake.IMAPIntake(config, ledger=self.root.ledger, mail_root=self.root.sub("mail"),
                                        backend=backend, alert=self._alert, client_factory=client_factory)
        try:
            report = intake.run()
        except BaseException:
            backend.finish_run(identity, "failed", {"messages": 0, "preserved": 0})
            raise
        status = "slice_completed" if not report["failures"] else "completed_with_gaps"
        backend.finish_run(identity, status, {"messages": sum(f["new"] for f in report["folders"]),
                                             "preserved": report["preserved"], "failures": report["failures"]})
        return report

    def ingest_exports(self, exports):
        """Explicit bounded legacy receipts; no mailbox/configuration access."""
        from .export_bridge import enroll_export
        require_root_lock(self.root.path)
        if not isinstance(exports, dict) or set(exports) != {"root", "receipts", "inventory"}:
            raise RunError("invalid_export_selection")
        receipts = exports["receipts"]
        inventory = exports["inventory"]
        if not isinstance(receipts, (list, tuple)) or not 1 <= len(receipts) <= 60:
            raise RunError("export_receipt_bound")
        if inventory.queue.root != self.root.path / "portal":
            raise RunError("export_inventory_root_mismatch")
        backend = CanonicalMailBackend(Path(os.path.abspath(exports["root"])), self.root.intake, self.root.ledger)
        identity = self._identity()
        identity["run_id"] = "export-" + self.run_id
        backend.start_run(identity)
        report = {"messages": len(receipts), "preserved": [], "replayed": [], "failures": [], "events": []}
        try:
            for receipt in receipts:
                try:
                    from .intake import mail_delta
                    path = mail_delta.source_path(backend.mail_root, str(receipt), backend.output)
                    _, scope, _ = mail_delta.load_receipt(path)
                    seen = self._query("SELECT id FROM mail_messages WHERE account=? AND folder=? AND uidvalidity=? AND uid=?",
                                       (scope[0], scope[1], int(scope[2]), int(scope[3])))
                    value = enroll_export(path, export_root=backend.mail_root, backend=backend, inventory=inventory)
                    entry = {"event": value.message_id, "eml_sha256": value.eml_sha256, "documents": list(value.documents)}
                    report["replayed" if seen else "preserved"].append(entry)
                    report["events"].append(inventory.readiness(value.message_id))
                except Exception:
                    # Never place private paths, account names, URLs or arbitrary
                    # exception text in the run report.
                    report["failures"].append({"detail": "export_enrollment_or_discovery_failed"})
            backend.finish_run(identity, "completed_with_gaps" if report["failures"] else "slice_completed",
                               {"messages": len(receipts), "failures": len(report["failures"])})
        except BaseException:
            backend.finish_run(identity, "failed", {"messages": len(receipts)})
            raise
        return report

    # ----- stage runner ------------------------------------------------------------------
    def _open_stage_run(self):
        if self._stage is not None:
            return self._stage
        database = self.root.ledger
        with store.ledger(database) as con:
            con.execute("INSERT INTO runs(run_id,kind,started_at,engine_version,image_digest,config_sha256,status,summary) "
                        "VALUES(?,?,?,?,?,?,?,?)", (self.run_id, "stage-runner", now(), ENGINE, "local",
                                                   self.config_sha256, "running", "{}"))
            con.commit()
        artifacts = self.root.sub("artifacts")
        evidence, blobs = artifacts / "evidence", self.root.intake / "blobs"
        validators = {"extract": install_extraction_validator(database=database, evidence_root=evidence, original_root=blobs, native_workbook_policy=self.native_workbook_policy)}
        validators.update(stage_adapters.install_validators(database))
        profile = "records-run:" + sha(encoded({"db": str(database), "config": self.config_sha256}))[:32]
        catalog = catalog_run_factory(database, artifacts, run_id=self.run_id, owner=OWNER, profile_id=profile,
                                      engine_version=ENGINE, config_sha256=self.config_sha256,
                                      additional_adapters=validators)
        extraction = ExtractionStageAdapter(database=database, evidence_root=evidence, original_root=blobs,
                                            run_id=self.run_id, owner=OWNER, profile_id=profile, native_workbook_policy=self.native_workbook_policy)
        enrollment = ExtractionLedgerAdapter(database=database, evidence_root=evidence)
        self._stage = {"catalog": catalog, "runner": catalog.runner, "extraction": extraction,
                       "enrollment": enrollment, "profile": profile}
        return self._stage

    def _close_stage_run(self, summary):
        if self._stage is None:
            return
        with store.ledger(self.root.ledger) as con:
            con.execute("UPDATE runs SET status=?,ended_at=?,summary=? WHERE run_id=?",
                        ("completed", now(), json.dumps(summary, sort_keys=True), self.run_id))
            con.commit()

    def _query(self, sql, values=()):
        with store.ledger(self.root.ledger, readonly=True) as con:
            return [dict(row) for row in con.execute(sql, values)]

    def _states(self, subject):
        return stage_adapters.current_states(self.root.ledger, subject)

    def _alert(self, key, *, owner):
        """Keyed, deduplicated alert in the ledger: one row per key, counted on repeat."""
        stamp = now()
        with store.ledger(self.root.ledger) as con:
            con.execute("INSERT INTO alerts(id,key,first_seen,last_seen,count,owner,state) VALUES(?,?,?,?,1,?,'open') "
                        "ON CONFLICT(key) DO UPDATE SET last_seen=excluded.last_seen,count=alerts.count+1,state='open'",
                        (sha(key.encode())[:32], key, stamp, stamp, owner))
            con.commit()

    def _resolve_alert(self, key):
        """Mark one open keyed alert resolved (a later repeat reopens it via ``_alert``)."""
        with store.ledger(self.root.ledger) as con:
            con.execute("UPDATE alerts SET state='resolved',last_seen=? WHERE key=? AND state='open'", (now(), key))
            con.commit()

    def subjects(self):
        return [row["sha256"] for row in self._query("SELECT sha256 FROM originals WHERE scope!='out_of_scope' ORDER BY first_seen_at,sha256")]

    def advance_all(self, subjects=None):
        """Advance every subject through the stage order; never raise for one record."""
        stage = self._open_stage_run()
        if not self._reconcile_review_authority(stage):
            return []
        report = []
        for subject in (self.subjects() if subjects is None else subjects):
            outcome = {"subject_sha256": subject, "stages": {}}
            report.append(outcome)
            for name in STAGE_ORDER:
                if name == "catalog":
                    from .context_runtime import reconcile
                    try:
                        current, plan = reconcile(self, subject, stage["runner"])
                        self._contexts[subject] = current
                        outcome["context_revision_sha256"] = current["revision"]["revision_sha256"]
                        outcome["context_invalidated"] = plan["applied_stages"]
                    except Exception:
                        outcome["stages"]["context"] = "blocked:context_inputs_unresolved"
                        break
                state = self._states(subject)[name]
                if state["status"] in ("done", "inapplicable"):
                    outcome["stages"][name] = state["status"]
                    continue
                if state["status"] == "blocked" and state["receipt_sha256"]:
                    outcome["stages"][name] = "blocked:" + str(state["reason"])[:120]
                    break
                try:
                    result = getattr(self, "stage_" + name)(subject, stage)
                except Exception as error:
                    outcome["stages"][name] = "error:stage_failed"
                    break
                outcome["stages"][name] = result.get("status", "pending")
                if name == "extract" and result.get("extraction_gaps"):
                    outcome["extraction_gaps"] = result["extraction_gaps"]
                if result.get("reason"):
                    outcome["stages"][name] += ":" + str(result["reason"])[:160]
                if result.get("status") not in ("done", "inapplicable"):
                    break
        return report

    def _reconcile_review_authority(self, stage):
        """Retire cached automated/test-only review before completed-stage reuse.

        No source text is needed: the protected receipt issuer and validation
        authority identify prior automated passes. Root lock belongs to run().
        Genuine independent receipt submission remains a separate review seam.
        """
        self._arm_review_retirement()
        safety = getattr(self, "safety", None)
        limit = min(review_retirement.LIMIT, safety.policy.max_originals) if safety else review_retirement.LIMIT
        rows = self._query("SELECT s.original_sha256,s.receipt_sha256 " + review_retirement.ELIGIBLE +
                           " ORDER BY s.original_sha256 LIMIT ?", (limit,))
        for row in rows:
            if safety and not safety.begin("advance", sha(encoded([
                    "review-retirement", row["original_sha256"], row["receipt_sha256"]]))):
                break
            # Revoke first and durably. An interrupted reconciliation can only
            # leave authority more restrictive; it cannot restore old approval.
            with store.ledger(self.root.ledger) as con:
                con.execute("UPDATE proposals SET owner_approval='none',approved_content_sha256=NULL "
                            "WHERE independent_receipt_sha256=?", (row["receipt_sha256"],))
                con.commit()
            try:
                stage["runner"].invalidate(row["original_sha256"], "review",
                                           reason="independent_factual_review_required:cached_automated_or_test_review")
            except Exception:
                self.review_retirement.update(state="pending", failure_code="review_authority_retirement_failed")
                if safety:
                    safety.finish("held", "review_authority_retirement_failed", (row["original_sha256"],))
                raise RunError("review_authority_retirement_failed") from None
            self.review_retirement["processed"] += 1
            if safety:
                safety.finish()
        with store.ledger(self.root.ledger) as con:
            complete = review_retirement.finish(con)
        self.review_retirement["state"] = "complete" if complete else "pending"
        return complete

    def _arm_review_retirement(self):
        with store.ledger(self.root.ledger) as con:
            held = review_retirement.arm(con)
        self.review_retirement["state"] = "pending" if held else "complete"

    def _start_review_retirement(self):
        with store.ledger(self.root.ledger) as con:
            review_retirement.start(con)
        self.review_retirement["state"] = "pending"

    # ----- helpers ---------------------------------------------------------------------
    def _original(self, subject):
        with store.ledger(self.root.ledger,readonly=True) as con:
            original = store.effective_original(con,subject)
        if original is None:
            raise RunError("original_missing")
        return original

    def ingest_documents(self, documents, manifest_sha256):
        from .intake.document_drop import ingest
        return ingest(self, documents, manifest_sha256)

    def _form(self, subject, original):
        from .ledger.occurrence_classification import validate_declarations
        with store.ledger(self.root.ledger, readonly=True) as con:
            validate_declarations(con, subject)
        if original.get("mime_detected") == "application/vnd.ms-outlook":
            return "msg"
        if original.get("mime_detected") == "message/rfc822":
            return "eml"
        for occurrence in self._query("SELECT * FROM occurrences WHERE original_sha256=?", (subject,)):
            evidence = json.loads(occurrence["evidence"])
            if occurrence["kind"] == "local":
                method = occurrence["acquisition_method"]
                if method.startswith("captured_legacy_"):
                    continue  # A captured declaration is not a document-drop receipt.
                if method == "verified_local_document_drop":
                    from .intake.document_drop import verify_local_occurrence
                    return verify_local_occurrence(occurrence, self.root.intake, subject)[1]
                if method == "verified_local_capture":
                    from .intake.local_capture import verify_occurrence
                    verify_occurrence(occurrence, self.root.intake, subject)
                    name = json.loads(occurrence["source_ref"])["path"]
                    with intake_folder.secure_open(original["storage_path"]) as source:
                        head = source.read(16384)
                    return intake_folder.fmt(name, head, original["storage_path"])
                raise RunError("local_acquisition_method_unsupported")
            receipt = evidence.get("export_receipt_path")
            if receipt and Path(receipt).exists():
                forms = eml_export.attachment_forms(Path(receipt))
                if subject in forms:
                    return forms[subject]
        with intake_folder.secure_open(original["storage_path"]) as source:
            head = source.read(16384)
        return intake_folder.fmt(original["storage_path"], head)

    def _unit_view(self, subject):
        from .runner_coverage import unit_view
        return unit_view(self, subject, MAX_UNITS_PER_RECORD)

    def _units(self, subject):
        return self._unit_view(subject)[0]

    def _headers(self, subject):
        rows = self._query("SELECT headers FROM mail_messages WHERE eml_sha256=?", (subject,))
        if not rows:
            parent = self._query("SELECT m.headers FROM occurrences o JOIN occurrences p ON o.parent_occurrence_id=p.id "
                                 "JOIN mail_messages m ON m.id=p.id WHERE o.original_sha256=?", (subject,))
            rows = parent
        if not rows:
            return {}
        fields = json.loads(rows[0]["headers"]).get("fields", [])
        return {name.lower(): value for name, value in fields}

    def _event_date(self, subject):
        # Header/receipt dates and CLI declarations are not verified conduct dates.
        return None

    # ----- stages ----------------------------------------------------------------------
    def stage_extract(self, subject, stage):
        original = self._original(subject)
        form = self._form(subject, original)
        ocr_root = self.root.sub("ocr") if self.ocr_tools else None
        result = extraction_routes.extract(original["storage_path"], subject, self.root.sub("extract"), form=form,
                                           timeout=self.extraction_timeout, ocr_output_root=ocr_root,
                                           ocr_tools=self.ocr_tools, tool_signature=self.ocr_tool_signature,
                                           ocr_vision=self.ocr_vision)
        receipt = Path(result["run_path"]) / "extraction.json"
        codes = [i.get("code", "?") for i in result.get("issues", [])]
        # A plain workbook is "partial" by construction (the reader hold below). It may go on to
        # enrollment only when trusted startup loaded the native workbook policy; the installed
        # extraction check then qualifies it under that policy or refuses it. Nothing is waived here.
        workbook = (form == "xlsx" and self.native_workbook_policy is not None and result["status"] == "partial"
                    and codes == ["workbook_cached_values_and_objects_not_reviewed"] and not result.get("ocr_receipts"))
        if result["status"] != "complete" and not workbook and not (result["status"] == "partial" and result.get("ocr_receipts")):
            # Not acceptable yet (no decoder, a parser failure on a damaged file, timeout, or
            # image-only pages with no OCR runtime). Record a keyed alert so the gap is visible
            # in `records status`; nothing is faked and nothing unacceptable is enrolled.
            reason = "extraction_" + result["status"] + ":" + ",".join(codes)
            self._alert("extract:" + subject + ":" + ",".join(sorted(set(codes))), owner="runtime")
            return {"status": "blocked", "reason": reason,
                    "extraction_gaps": _extraction_report_gaps(subject, result)}
        enrolled = stage["enrollment"].enroll(original_path=Path(original["storage_path"]), receipt_path=receipt,
                                              receipt_sha256=result["receipt_sha256"])
        accepted = stage["extraction"].accept(enrolled["import_id"])
        return {"status": accepted.get("status"), "import_id": enrolled["import_id"], "form": form,
                "units": enrolled.get("units"),
                **({"extraction_gaps": _extraction_report_gaps(subject, result)} if result["status"] == "partial" else {})}

    def stage_catalog(self, subject, stage):
        units = [u for u in self._units(subject) if u["text"].strip()]
        if not units:
            return self._inapplicable(subject, stage, "catalog", "no_text_units")
        headers = self._headers(subject)
        original = self._original(subject)
        is_mail = original.get("mime_detected") == "message/rfc822"
        first = next((u for u in units if u["unit_type"] == "email_body"), units[0])
        quote = first["text"].strip()[:400]
        type_match = None if is_mail else next(
            ((u, match.start(), kind) for key, kind in DOC_TYPE_HINTS for u in units[:50]
             if (match := re.search(re.escape(key), u["text"], flags=re.IGNORECASE | re.ASCII))), None)
        doc_type = "correspondence" if is_mail else type_match[2] if type_match else "other"
        title = headers.get("subject") if is_mail else None
        title_unit = next((u for u in units if title and title in u["text"]), None)
        if title_unit is None:
            title = quote.splitlines()[0][:120] if quote else None
            title_unit = first
        event = None  # Source-date adapter unavailable; never copy a mail date.
        metadata = {"title": title, "doc_type": doc_type, "summary": quote[:300] or None,
                    "date_from": event, "date_to": event}
        metadata = {k: v for k, v in metadata.items() if v not in (None, "")}
        field_units = {"title": title_unit, "summary": first,
                       "doc_type": type_match[0] if type_match else first}
        field_support = {k: [field_units[k]["unit_id"]] for k in metadata}
        supports = {}
        for field in ("summary", "title", "doc_type"):
            if field not in metadata:
                continue
            unit = field_units[field]
            if field == "doc_type" and not type_match and unit["unit_id"] in supports:
                continue
            text = unit["text"]
            # Keep an exact source substring near the evidence used for this
            # field, including type hints beyond the first display excerpt.
            start = type_match[1] if field == "doc_type" and type_match else (
                text.find(title) if field == "title" else 0)
            supports[unit["unit_id"]] = {
                "unit_id": unit["unit_id"], "source_sha256": subject,
                "locator": unit["locator_text"], "artifact_sha256": unit["artifact_sha256"],
                "quote": text[max(0, start):max(0, start) + 400]}
        metadata["evidence"] = [{"source_sha256": subject, "locator": p["locator"]}
                                for p in supports.values()]
        card = {"schema": "catalog-card-evidence-v1", "subject_sha256": subject, "metadata": metadata,
                "supports": list(supports.values()), "field_support": field_support}
        path = self.root.sub("artifacts") / (subject + "-card.json")
        raw = encoded(card)
        if not path.exists() or path.read_bytes() != raw:
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(raw)
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        result = stage["catalog"].process(path, sha(raw), author_id="records-cataloger")
        return {"status": result["status"], "doc_type": doc_type}

    def _inapplicable(self, subject, stage, name, reason):
        runner = stage["runner"]
        states = self._states(subject)
        content = {"schema": stage_adapters.SCHEMAS[name], "subject_sha256": subject, "inapplicable": reason}
        if name == "detect":
            content.update(detector_version=DETECTOR_VERSION, unit_count=0, text_leads=[],
                           structured={n: {"status": "skipped", "reason": reason} for n in stage_adapters.STRUCTURED_DETECTORS})
        elif name == "compare":
            return self._unresolved_comparison(subject, stage, "comparison_applicability_unresolved")
        elif name == "catalog":
            # Catalog has its own adapter; an empty record is cataloged as "other" with no supports is
            # impossible (evidence required), so record the gap on the stage via a blocked receipt.
            return {"status": "blocked", "reason": reason}
        else:
            raise RunError("inapplicable_unsupported:" + name)
        content_bytes = encoded(content)
        registered = runner.set_content(subject, name, content_bytes, author_id="records-" + name, tier="A")
        states = self._states(subject)
        claim = runner.claim(subject, name, supersedes=states[name]["receipt_sha256"])
        inputs = {"original": subject}
        for prerequisite in stages.PREREQUISITES[name]:
            inputs[prerequisite] = states[prerequisite]["receipt_sha256"]
        receipt = {"schema": "ledger-stage-receipt-v1", "subject_sha256": subject, "stage": name,
                   "content_sha256": registered["content_sha256"], "author_id": "records-" + name, "tier": "A",
                   "reviewer_id": OWNER, "role": stages.ROLES[name], "verdict": "inapplicable", "reason": reason,
                   "coverage": {"denominator": {"kind": "items", "total": 0}, "covered": 0, "scope": "selected"},
                   "locators": ["stage:" + name], "rationale": "Stage not applicable: " + reason,
                   "model_or_tool": ENGINE, "created_at_tz": now(), "input_hashes": inputs}
        if states[name]["receipt_sha256"]:
            receipt["supersedes"] = states[name]["receipt_sha256"]
        return runner.promote(subject, name, encoded(receipt), claim_id=claim["claim_id"])

    def stage_detect(self, subject, stage):
        units, inventory, extraction = self._unit_view(subject)
        text_units = [{"locator": u["locator"], "text": u["text"]} for u in units]
        leads = run_detectors(text_units, self.package) if text_units else []
        structured = {name: {"status": "skipped", "reason": "no_structured_units_of_required_type"}
                      for name in stage_adapters.STRUCTURED_DETECTORS}
        content = {"schema": stage_adapters.SCHEMAS["detect"], "subject_sha256": subject,
                   "detector_version": DETECTOR_VERSION, "unit_count": len(units),
                   "text_leads": leads, "structured": structured, "rules_version": self.package.get("schema_version"),
                   "inventory_total": len(inventory), "selection_capped": len(inventory) > MAX_UNITS_PER_RECORD,
                   "coverage_gaps": [u for u in inventory if u.get("blocked_reason") or
                                     u["unit_id"] not in {v["unit_id"] for v in units}]}
        coverage = {"denominator": {"kind": "items", "total": len(inventory)},
                    "covered": sum(bool(u["text"].strip()) for u in units), "scope": "selected"}
        locators = [u["locator_text"] for u in units] or ["stage:detect"]
        return stage_adapters.promote(stage["runner"], subject, "detect", content, author_id="records-detect",
                                      coverage=coverage, locators=locators[:1024])

    def stage_review(self, subject, stage):
        units, inventory, extraction = self._unit_view(subject)
        text_units = [{"locator": u["locator"], "text": u["text"]} for u in units]
        if not text_units:
            return {"status": "blocked", "reason": "no_text_units"}
        tier = "strict_local" if (self.model and self.model.privacy_tier == "strict_local") else "redacted_cloud"
        digest = build_digest(subject, text_units, self.package, self.jurisdiction,
                              str(self.package.get("schema_version")), tier, model_config=None if self._event_date(subject) is None else self.model,
                              allowlist=self.allowlist, denylist=self.denylist,
                              event_date=self._event_date(subject), opener=self.opener)
        ocr_pages = sorted({u["locator"].get("page") for u in units if u["ocr"]} - {None})
        if ocr_pages:
            digest["limitations"].append(
                "Page(s) " + ", ".join(str(p) for p in ocr_pages) +
                " were read by local OCR and have not been visually reviewed; verify quotes against the page image.")
        redacted, _ = redact_units(text_units, allowlist=self.allowlist, denylist=self.denylist)
        record = challenge_mod.challenge_review(digest, redacted, self.package, config=None if self._event_date(subject) is None else self.challenge, opener=self.opener,
                                                allowlist=self.allowlist, denylist=self.denylist)
        if self._event_date(subject) is None:
            digest["limitations"].append("Conduct date and rule applicability are unresolved; no dated model narrative was requested.")
        from .runner_coverage import processing_coverage
        progress = processing_coverage(self, subject, units, inventory, extraction, stage="review")
        digest["limitations"].append(
            "Coverage is selected: unit and model-prompt caps may omit text. Automated output "
            "accepts no semantic-review ranges; independent factual review remains required.")
        content = {"schema": stage_adapters.SCHEMAS["review"], "subject_sha256": subject,
                   "digest": digest, "challenge": record, "processing_coverage": progress}
        coverage = progress["summary"]["receipt_coverage"]
        locators = [u["locator_text"] for u in units][:1024]
        result = stage_adapters.promote(stage["runner"], subject, "review", content, author_id="records-digest",
                                        coverage=coverage, locators=locators, challenges=[("factual", record)])
        return result

    def _unresolved_comparison(self, subject, stage, reason):
        from .context_revision import UNRESOLVED
        record = {"kind": "compare", "verdict": "challenge",
                  "disputes": [{"field": "applicability", "index": 0, "reason": reason}]}
        content = {"schema": stage_adapters.SCHEMAS["compare"], "subject_sha256": subject,
                   "event_date": None, "comparisons": [], "classification": UNRESOLVED,
                   "applicability_status": "unknown"}
        return stage_adapters.promote(stage["runner"], subject, "compare", content,
            author_id="records-compare", coverage={"denominator": {"kind": "items", "total": 0},
            "covered": 0, "scope": "selected"}, locators=["context:unresolved"],
            challenges=[("legal", record)], reason=reason)

    def stage_compare(self, subject, stage):
        states = self._states(subject)
        review = self._content(subject, "review")
        digest = review["digest"]
        event_date = self._event_date(subject)
        if event_date is None:
            return self._unresolved_comparison(subject, stage, "conduct_date_or_applicability_unresolved")
        in_force = {rule["rule_id"]: rule for rule in rules_in_force(self.package, event_date)}
        rows = []
        for duty in digest.get("duties", []):
            rule = in_force.get(duty["rule_id"])
            if rule is None:
                continue
            evidence = [{"sha256": subject, "locator": loc} for loc in duty["locators"][:10]]
            source = (rule.get("sources") or [{}])[0]
            # Automated passes never upgrade a legal conclusion: the attorney label is fixed here.
            # What the first model concluded about this rule travels with the row as observations.
            observations = [{"text": c["text"], "confidence": c["confidence"], "model_id": digest.get("model_id")}
                            for c in digest.get("conclusions", [])
                            if any(src.get("rule_id") == rule["rule_id"] for src in c.get("sources", []))]
            confidence = "needs_attorney_review"
            classification = "LEGAL_REVIEW_REQUIRED"
            rows.append({"rule_id": rule["rule_id"], "citation": rule["citation"], "duty": rule["duty"],
                         "actor": rule.get("actor"), "effective_from": rule.get("effective_from"),
                         "effective_to": rule.get("effective_to"),
                         "primary_source": {"citation": rule["citation"], "title": source.get("title"),
                                            "url": source.get("url"), "accessed": source.get("accessed")},
                         "rule_review_label": rule.get("review", "needs_attorney_review"),
                         "classification": classification, "confidence": confidence,
                         "observation": "Record text references the subject matter of this rule (see evidence).",
                         "model_observations": observations,
                         "evidence": evidence, "counterevidence": [],
                         "next_action": "Attorney review of applicability and compliance on the event date."})
        if not rows:
            return self._unresolved_comparison(subject, stage, "no_matched_duties_is_not_inapplicability")
        record = challenge_mod.challenge_compare(rows, self.package, event_date)
        content = {"schema": stage_adapters.SCHEMAS["compare"], "subject_sha256": subject,
                   "event_date": event_date.isoformat(), "comparisons": rows,
                   "review_content_sha256": states["review"]["receipt_sha256"], "challenge": record}
        coverage = {"denominator": {"kind": "items", "total": len(rows)}, "covered": len(rows), "scope": "selected"}
        locators = ["rule:" + row["rule_id"] for row in rows]
        return stage_adapters.promote(stage["runner"], subject, "compare", content, author_id="records-compare",
                                      coverage=coverage, locators=locators, challenges=[("legal", record)])

    def stage_privacy(self, subject, stage):
        states = self._states(subject)
        review = self._content(subject, "review")
        digest = review["digest"]
        compare = self._content(subject, "compare") if states["compare"]["status"] == "done" else {"comparisons": []}
        card = self._catalog_metadata(subject)
        title = card.get("title") or "Public record"
        lines = ["# " + title, "", "Document type: " + str(card.get("doc_type") or "other") + ".", ""]
        if card.get("date_from"):
            lines += ["Event date: " + card["date_from"] + ".", ""]
        lines += ["## What the record says", ""]
        seen = set()
        for item in digest.get("statements", [])[:20]:
            text = PLACEHOLDER.sub("[redacted]", item["text"]).strip()
            if text in seen:
                continue
            seen.add(text)
            lines.append("- " + text + " (" + _public_locator(item["locator"]) + ")")
        if not digest.get("statements"):
            lines.append("- No statements were extracted that are suitable for publication.")
        lines += ["", "## Rules referenced", ""]
        for row in compare.get("comparisons", []):
            lines.append("- " + row["citation"] + ": " + row["classification"].replace("_", " ").lower() +
                         " (" + row["confidence"].replace("_", " ") + "). Source: " + str(row["primary_source"].get("url")))
        if not compare.get("comparisons"):
            lines.append("- No rule comparison applied to this record.")
        lines += ["", "## Limitations", ""] + ["- " + text for text in digest.get("limitations", [])]
        public_markdown = "\n".join(lines) + "\n"
        record = challenge_mod.challenge_privacy(public_markdown, denylist=self.denylist)
        manifest = {"schema": "records-proposal-manifest-v1", "original_sha256": subject, "tier": "A",
                    "review_receipt_sha256": states["review"]["receipt_sha256"],
                    "compare_receipt_sha256": states["compare"]["receipt_sha256"],
                    "catalog_receipt_sha256": states["catalog"]["receipt_sha256"],
                    "digest_locators": [item["locator"] for item in digest.get("statements", [])],
                    "jurisdiction": self.jurisdiction, "generated_by": ENGINE}
        content = {"schema": stage_adapters.SCHEMAS["privacy"], "subject_sha256": subject,
                   "public_markdown": public_markdown, "public_content_sha256": sha(public_markdown.encode()),
                   "manifest": manifest, "manifest_sha256": sha(encoded(manifest)), "challenge": record}
        coverage = {"denominator": {"kind": "bytes", "total": len(public_markdown.encode())},
                    "covered": len(public_markdown.encode()), "scope": "full_text"}
        result = stage_adapters.promote(stage["runner"], subject, "privacy", content, author_id="records-proposal",
                                        coverage=coverage, locators=["public:full_text"],
                                        challenges=[("factual", review["challenge"]), ("privacy", record)])
        if result.get("status") == "done":
            self._record_proposal(subject, content, states)
        return result

    def _content(self, subject, name):
        rows = self._query("SELECT a.payload FROM stage_content c JOIN stage_artifacts a ON a.sha256=c.content_sha256 "
                           "WHERE c.subject_sha256=? AND c.stage=? ORDER BY c.revision DESC LIMIT 1", (subject, name))
        if not rows:
            raise RunError("stage_content_missing:" + name)
        return json.loads(bytes(rows[0]["payload"]))

    def _catalog_metadata(self, subject):
        try:
            return self._content(subject, "catalog").get("metadata", {})
        except (RunError, ValueError):
            return {}

    def _record_proposal(self, subject, content, states):
        """Persist a proposal under the caller's root lock; provenance can change without text."""
        import stat

        proposal_id = "prop_" + subject[:16]
        public, manifest = content["public_markdown"].encode(), encoded(content["manifest"])
        if sha(public) != content["public_content_sha256"] or sha(manifest) != content["manifest_sha256"]:
            raise RunError("proposal_content_hash_mismatch")
        privacy_receipt = self._states(subject)["privacy"]["receipt_sha256"]
        bindings = (content["public_content_sha256"], content["manifest_sha256"], privacy_receipt,
                    states["review"]["receipt_sha256"])
        fields = ("public_content_sha256", "manifest_sha256", "privacy_receipt_sha256", "independent_receipt_sha256")
        revision_id = sha(encoded({"proposal_id": proposal_id, "bindings": bindings}))
        directories = []
        try:
            # Pin destination directories without following symlinks. Unique exclusive
            # temporary files cannot follow a planted, predictable .tmp symlink either.
            if any(path.is_symlink() for path in (self.root.path, *self.root.path.parents)):
                raise RunError("symlink_root")
            directories.append(os.open(self.root.path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW))
            for part in self.root.path.parts[1:]:
                following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directories[0])
                os.close(directories[0])
                directories[0] = following
            directories.append(os.open("proposals", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                       dir_fd=directories[0]))
            for name in ("public", "private"):
                directories.append(os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                           dir_fd=directories[1]))
            outputs = ((directories[2], proposal_id + ".md", public),
                       (directories[3], proposal_id + ".manifest.json", manifest))
            for directory, name, data in outputs:
                try:
                    mode = os.stat(name, dir_fd=directory, follow_symlinks=False).st_mode
                except FileNotFoundError:
                    continue
                if not stat.S_ISREG(mode):
                    raise RunError("proposal_file_not_regular")
            with store.ledger(self.root.ledger) as con:
                con.execute("BEGIN IMMEDIATE")
                con.execute("CREATE TABLE IF NOT EXISTS proposal_revisions("
                            "revision_id TEXT PRIMARY KEY, proposal_id TEXT NOT NULL REFERENCES proposals(id),"
                            "public_content_sha256 TEXT NOT NULL, manifest_sha256 TEXT NOT NULL,"
                            "privacy_receipt_sha256 TEXT, independent_receipt_sha256 TEXT,"
                            "public_content BLOB NOT NULL, manifest_content BLOB NOT NULL,"
                            "state TEXT NOT NULL CHECK(state IN ('pending','complete','superseded')))")
                con.execute("CREATE TRIGGER IF NOT EXISTS proposal_revision_immutable BEFORE UPDATE OF "
                            "revision_id,proposal_id,public_content_sha256,manifest_sha256,privacy_receipt_sha256,"
                            "independent_receipt_sha256,public_content,manifest_content ON proposal_revisions "
                            "BEGIN SELECT RAISE(ABORT,'proposal revision is immutable'); END")
                con.execute("CREATE TRIGGER IF NOT EXISTS proposal_revision_no_delete BEFORE DELETE ON proposal_revisions "
                            "BEGIN SELECT RAISE(ABORT,'proposal revision is immutable'); END")
                con.execute("CREATE TRIGGER IF NOT EXISTS proposal_revision_no_replace BEFORE INSERT ON proposal_revisions "
                            "WHEN EXISTS(SELECT 1 FROM proposal_revisions WHERE revision_id=NEW.revision_id) "
                            "BEGIN SELECT RAISE(ABORT,'proposal revision is immutable'); END")
                existing = con.execute("SELECT * FROM proposals WHERE id=?", (proposal_id,)).fetchone()
                if existing is None:
                    con.execute("INSERT INTO proposals(id,tier,agency_id,public_content_sha256,manifest_sha256,"
                                "privacy_receipt_sha256,independent_receipt_sha256,owner_approval,approved_content_sha256,supersedes) "
                                "VALUES(?,?,?,?,?,?,?,?,?,?)", (proposal_id, "A", None, *bindings, "none", None, None))
                elif tuple(existing[field] for field in fields) != bindings:
                    con.execute("UPDATE proposals SET public_content_sha256=?,manifest_sha256=?,privacy_receipt_sha256=?,"
                                "independent_receipt_sha256=?,owner_approval='none',approved_content_sha256=NULL WHERE id=?",
                                (*bindings, proposal_id))
                revision = con.execute("SELECT * FROM proposal_revisions WHERE revision_id=?", (revision_id,)).fetchone()
                if revision is not None and (tuple(revision[field] for field in fields) != bindings or
                                             revision["proposal_id"] != proposal_id or
                                             bytes(revision["public_content"]) != public or
                                             bytes(revision["manifest_content"]) != manifest):
                    raise RunError("proposal_revision_changed")
                if revision is None:
                    con.execute("INSERT INTO proposal_revisions VALUES(?,?,?,?,?,?,?,?,?)",
                                (revision_id, proposal_id, *bindings, public, manifest, "pending"))
                else:
                    con.execute("UPDATE proposal_revisions SET state='pending' WHERE revision_id=?", (revision_id,))
                con.execute("UPDATE proposal_revisions SET state='superseded' "
                            "WHERE proposal_id=? AND revision_id!=? AND state='pending'", (proposal_id, revision_id))
                # Commit intent and revoked authority before replacing either file.
                # Publication/reopen can recover an interrupted write from this journal.
                con.commit()
            for directory, name, data in outputs:
                temporary = "." + name + "." + uuid.uuid4().hex + ".tmp"
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=directory)
                try:
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(data)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
                    os.fsync(directory)
                finally:
                    try:
                        os.unlink(temporary, dir_fd=directory)
                    except FileNotFoundError:
                        pass
            with store.ledger(self.root.ledger) as con:
                con.execute("UPDATE proposal_revisions SET state='complete' WHERE revision_id=?", (revision_id,))
                con.commit()
        finally:
            for directory in reversed(directories):
                os.close(directory)
        return proposal_id

    # ----- whole run -------------------------------------------------------------------
    def recover(self):
        """Under the root lock: finalise runs a dead process left open and free their leases."""
        stamp = now()
        with store.ledger(self.root.ledger) as con:
            dead = [row["run_id"] for row in con.execute(
                "SELECT run_id FROM runs WHERE kind='stage-runner' AND status='running' AND ended_at IS NULL")]
            for run_id in dead:
                con.execute("UPDATE runs SET status='interrupted',ended_at=?,summary=? WHERE run_id=?",
                            (stamp, json.dumps({"interrupted_by": self.run_id}), run_id))
            con.commit()
        leases = stages.recover_abandoned_leases(self.root.ledger, owner=OWNER)
        for subject, stage in leases:
            self._alert("run:lease_recovered:" + subject + ":" + stage, owner="runtime")
        return {"interrupted_runs": dead, "recovered_leases": [list(pair) for pair in leases]}

    def run(self, inbox=None, mail_config=None, *, client_factory=None, documents=None, document_manifest_sha256=None,
            exports=None):
        from .export_selection import ExportSelection
        if bool(documents) != bool(document_manifest_sha256) or documents and (inbox or mail_config):
            raise RunError('invalid_input_selection')
        if exports is not None and (inbox or mail_config or documents):
            raise RunError('invalid_input_selection')
        with root_lock(self.root.path):
            if type(exports) is ExportSelection:
                with exports.open(self.root.path) as selected:
                    return self._run_locked(inbox, mail_config, client_factory=client_factory, documents=documents,
                                            document_manifest_sha256=document_manifest_sha256, exports=selected)
            return self._run_locked(inbox, mail_config, client_factory=client_factory, documents=documents,
                                    document_manifest_sha256=document_manifest_sha256, exports=exports)

    def _run_locked(self, *args, **kwargs):
        # Recovery precedes this invocation's run row, so it cannot interrupt itself.
        try:
            self._start_review_retirement()
            report = self._run_locked_unchecked(*args, **kwargs)
        except Exception as error:
            code = error.args[0] if isinstance(error, RunError) and error.args == ("review_authority_retirement_failed",) else "run_failed"
            with store.ledger(self.root.ledger) as con:
                con.execute("UPDATE runs SET status='failed',ended_at=?,summary=? WHERE run_id=?",
                            (now(), encoded({"failure_code": code}).decode(), self.run_id))
                con.commit()
            raise RunError(code) from None
        outcomes = [value for item in report.get("subjects", []) for value in item.get("stages", {}).values()]
        technical = any(value.startswith("error:") for value in outcomes)
        gaps = technical or bool(report.get("intake", {}).get("failures")) or bool((report.get("mailbox") or {}).get("failures"))
        gaps = gaps or any(not event["retrieval_complete"] for event in report["intake"].get("events", []))
        gaps = gaps or any(value.startswith(("blocked", "pending")) for value in outcomes)
        gaps = gaps or report["review_retirement"]["state"] == "pending"
        status = "failed" if technical else "completed_with_gaps" if gaps else "completed"
        report.update(status=status, exit_code=2 if technical else 3 if gaps else 0)
        report.setdefault("substantive_complete", False)
        report.setdefault("substantive_review_status", "queued" if report["originals"] else "not_applicable")
        with store.ledger(self.root.ledger) as con:
            con.execute("UPDATE runs SET status=?,summary=? WHERE run_id=?",
                        (status, encoded({"status": status, "subjects": len(report.get("subjects", [])), "counts": report["counts"]}).decode(), self.run_id))
            con.commit()
        path = self.root.sub("runs") / (self.run_id + ".json")
        path.write_bytes(encoded(report))
        os.chmod(path, 0o600)
        return report

    def _run_locked_unchecked(self, inbox, mail_config, *, client_factory=None, documents=None, document_manifest_sha256=None,
                              exports=None):
        started = now()
        recovery = self.recover()
        self._open_stage_run()
        self._arm_review_retirement()
        intake = self.ingest_exports(exports) if exports is not None else self.ingest_documents(documents, document_manifest_sha256) if documents else self.ingest_inbox(inbox) if inbox else {"messages": 0, "preserved": [], "replayed": [], "failures": []}
        mailbox = self.ingest_mailbox(mail_config, client_factory=client_factory) if mail_config else None
        progress = self.advance_all()
        summary = stages.counts(self.root.ledger)
        report = {"schema": "records-run-report-v1", "run_id": self.run_id, "started_at": started, "ended_at": now(),
                  "engine": ENGINE, "version": __version__, "config_sha256": self.config_sha256,
                  "recovery": recovery, "intake": intake, "mailbox": mailbox, "subjects": progress, "counts": summary["stages"],
                  "originals": summary["originals"], "end_to_end_complete": summary["candidate_seven_stage_complete"],
                  "proposals_awaiting_owner": len(self._query("SELECT id FROM proposals WHERE owner_approval='none'")),
                  "model_id": self.model.model_id if self.model else None,
                  "challenge_model_id": self.challenge.model.model_id if self.challenge else None}
        report["review_retirement"] = dict(self.review_retirement)
        self._close_stage_run({"subjects": len(progress), "counts": summary["stages"]})
        path = self.root.sub("runs") / (self.run_id + ".json")
        path.write_bytes(json.dumps(report, sort_keys=True, indent=1).encode())
        os.chmod(path, 0o600)
        return report


def _extraction_report_gaps(subject, result):
    """Report coordinates without copying free-form source labels or errors.

    The locator hash refers to the exact locator in preserved extraction.json;
    only numeric coordinates are repeated here. Neither source metadata nor
    ledger identities are rewritten by this presentation projection.
    """
    gaps = []
    issues = list(result.get("issues") or [])
    issues.extend({"code": page.get("reason"), "locator": {"page": page["page_no"]}}
                  for page in result.get("pages", []) if page.get("status") in {"partial", "blocked"})
    for issue in issues or [{"code": "extraction_not_complete"}]:
        locator = issue.get("locator")
        coordinates = {key: value for key, value in (locator.items() if isinstance(locator, dict) else [])
                       if key in {"page", "line", "row", "column", "ordinal", "line_start", "line_end", "record", "frame", "image", "altchunk"}
                       and type(value) is int and value >= 0}
        code = issue.get("code")
        gaps.append({"original_sha256": subject, "extraction_status": result["status"],
                     "reason": code if isinstance(code, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,79}", code) else "extraction_issue",
                     "locator": coordinates or None,
                     "locator_sha256": sha(encoded(locator)) if locator is not None else None})
    return gaps


def _public_locator(locator):
    if not isinstance(locator, dict):
        return "document"
    parts = [f"{key} {value}" for key, value in sorted(locator.items()) if key in ("page", "line", "row", "sheet", "part") and not isinstance(value, dict)]
    return ", ".join(parts) or "document"


def status(root):
    root = Root(root)
    if not root.ledger.exists():
        return {"root": str(root.path), "ledger": False}
    summary = stages.counts(root.ledger)
    with store.ledger(root.ledger, readonly=True) as con:
        proposals = [dict(row) for row in con.execute("SELECT id,owner_approval,public_content_sha256 FROM proposals ORDER BY id")]
        blocked = [dict(row) for row in con.execute(
            "SELECT original_sha256,stage,reason FROM stage_state WHERE status='blocked' ORDER BY original_sha256,stage")]
        runs = [dict(row) for row in con.execute("SELECT run_id,kind,started_at,ended_at,status FROM runs ORDER BY started_at DESC LIMIT 10")]
        alerts = [dict(row) for row in con.execute("SELECT key,first_seen,last_seen,count,owner,state FROM alerts WHERE state='open' ORDER BY last_seen DESC")]
        retirement_pending = review_retirement.pending(con)
    from .publish import drifted_proposals
    return {"root": str(root.path), "ledger": True, "originals": summary["originals"], "stages": summary["stages"],
            "end_to_end_complete": summary["candidate_seven_stage_complete"], "proposals": proposals,
            "drifted_proposals": drifted_proposals(root.path),
            "review_retirement_pending": retirement_pending,
            "blocked": blocked, "alerts": alerts, "recent_runs": runs}


def local_ocr_tools():
    """Doctor-resolved local OCR tools and a version signature, or (None, None)."""
    from .extract import ocr as page_ocr
    status_ = page_ocr.dependency_status()
    if not status_.get("ready"):
        return None, None
    tools = status_["tools"]
    signature = re.sub(r"[^\w .+/-]", "", "/".join(
        str(tools[name].get("version") or name) for name in ("tesseract", "pdftoppm")))[:120]
    return tools, signature or "local-ocr"


def _model_from_env(prefix, env):
    base = (env.get(prefix + "MODEL_BASE_URL") or "").strip()
    if not base:
        return None
    return ModelConfig(base_url=base, api_key=env.get(prefix + "MODEL_API_KEY", ""),
                       model_id=env.get(prefix + "MODEL_ID", "local"),
                       privacy_tier=env.get("PRIVACY_TIER", "redacted_cloud"),
                       timeout=int(env.get("MODEL_TIMEOUT", "120")))


def build_pipeline(args, env=None):
    env = os.environ if env is None else env
    model = _model_from_env("", env)
    challenge = None
    second = _model_from_env("CHALLENGE_", env)
    if second is not None:
        challenge = challenge_mod.ChallengeConfig(second, primary_model_id=model.model_id if model else None,
                                                  fresh_context=env.get("CHALLENGE_FRESH_CONTEXT", "") == "1")
    ocr_tools, ocr_signature = None, None
    if args.ocr:
        ocr_tools, ocr_signature = local_ocr_tools()
        if ocr_tools is None:
            print("records run: --ocr requested but tesseract/pdftoppm/pypdf are not all installed; "
                  "image-only pages will be held", file=sys.stderr)
    event = date.fromisoformat(args.event_date) if getattr(args, "event_date", None) else None
    vision_config = getattr(args, "ocr_vision_config", None)
    ocr_vision = None
    if vision_config:
        if ocr_tools is None:
            raise ValueError("ocr_vision_requires_ocr_tools")
        from .extract import vision as page_vision
        ocr_vision = page_vision.load_config(vision_config)
    return Pipeline(args.root, jurisdiction=args.jurisdiction, account=args.account, model=model, challenge=challenge,
                    redaction_allowlist=[x for x in env.get("REDACTION_ALLOWLIST", "").split(",") if x.strip()],
                    redaction_denylist=[x for x in env.get("REDACTION_DENYLIST", "").split(",") if x.strip()],
                    event_date=event, ocr_tools=ocr_tools,
                    ocr_tool_signature=(env.get("OCR_TOOL_SIGNATURE") or ocr_signature) if ocr_tools else None,
                    extraction_timeout=args.timeout, ocr_vision=ocr_vision)


def main(argv=None):
    if "--unattended" in (sys.argv[1:] if argv is None else argv):
        from .unattended import main as unattended_main
        return unattended_main(argv)
    parser = argparse.ArgumentParser(prog="records run", description=__doc__)
    parser.add_argument("--root", required=True, help="private records root (created if missing)")
    parser.add_argument("--inbox", help="directory of .eml files to preserve before advancing stages")
    parser.add_argument("--mail-config", dest="mail_config",
                        help="owner-only JSON (0600) naming the IMAP host/account; new messages are fetched and preserved")
    parser.add_argument("--documents", help="read-only flat PDF/XLSX drop")
    parser.add_argument("--document-manifest-sha256")
    parser.add_argument("--export-root", help="existing local export root; does not fetch mail")
    parser.add_argument("--export-receipt", action="append", help="explicit receipt path under --export-root; repeat up to 60 times")
    parser.add_argument("--collection-host", action="append", help="host to classify for collection inventory; does not authorize retrieval")
    parser.add_argument("--collection-exclude-host", action="append", help="explicit non-record host exclusion for this inventory scope")
    parser.add_argument("--jurisdiction", default="us-ca")
    parser.add_argument("--account", default="local")
    parser.add_argument("--event-date", dest="event_date", help="YYYY-MM-DD override for rule applicability")
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--ocr", action="store_true", help="use local tesseract/pdftoppm for image-only PDF pages when installed")
    parser.add_argument("--ocr-vision-config", dest="ocr_vision_config",
                        help="with --ocr: owner-only local-vision-config-v1 JSON naming a local vision command for "
                             "pages Tesseract leaves empty or low confidence")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if bool(args.documents) != bool(args.document_manifest_sha256) or args.documents and (args.inbox or args.mail_config):
        parser.error('invalid_input_selection')
    from .export_selection import from_cli
    try:
        exports = from_cli(args)
    except ValueError:
        parser.error('invalid_export_selection')
    from .runtime import require
    if not require(args.root):
        return 2
    if args.ocr_vision_config and not args.ocr:
        parser.error("--ocr-vision-config requires --ocr")
    try:
        pipeline = build_pipeline(args)
    except ValueError as error:
        if str(error).startswith(("vision_config", "ocr_vision", "vision adapter")):
            print("records run: " + str(error).split(":")[0], file=sys.stderr)
            return 2
        raise
    selection = {"exports": exports} if exports is not None else {}
    report = pipeline.run(args.inbox, args.mail_config, documents=args.documents,
                          document_manifest_sha256=args.document_manifest_sha256, **selection)
    if args.json:
        print(json.dumps(report, sort_keys=True))
    else:
        print(f"run {report['run_id']}: {report['originals']} originals; intake {report['intake']['messages']} messages, "
              f"{len(report['intake']['preserved'])} preserved, {len(report['intake'].get('replayed', []))} replayed, "
              f"{len(report['intake']['failures'])} failed")
        if report["recovery"]["interrupted_runs"] or report["recovery"]["recovered_leases"]:
            print(f"  recovered: {len(report['recovery']['interrupted_runs'])} interrupted run(s), "
                  f"{len(report['recovery']['recovered_leases'])} abandoned lease(s)")
        if report.get("mailbox"):
            m = report["mailbox"]
            print(f"  mailbox {m['account']}: {len(m['folders'])} folders, {m['preserved']} preserved, "
                  f"{m['failures']} failed, {len(m['unconfigured_folders'])} unconfigured")
        for name, counts in report["counts"].items():
            print(f"  {name:9} done={counts['done']} pending={counts['pending']} blocked={counts['blocked']} "
                  f"inapplicable={counts['inapplicable']}")
        for item in report.get("subjects", []):
            for gap in item.get("extraction_gaps", []):
                print("  extraction gap: " + json.dumps(gap, sort_keys=True))
        print(f"  complete through privacy: {report['end_to_end_complete']}; proposals awaiting owner: "
              f"{report['proposals_awaiting_owner']}")
    return report["exit_code"]


def status_main(argv=None):
    parser = argparse.ArgumentParser(prog="records status")
    parser.add_argument("--root", required=True)
    args = parser.parse_args(argv)
    print(json.dumps(status(args.root), sort_keys=True, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
