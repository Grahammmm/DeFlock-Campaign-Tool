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
        yield
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
                 ocr_tools=None, ocr_tool_signature=None, extraction_timeout=180):
        self.root = Root(root).prepare()
        self.jurisdiction, self.account = jurisdiction, account
        self.model, self.challenge, self.opener = model, challenge, opener
        self.allowlist, self.denylist = tuple(redaction_allowlist), tuple(redaction_denylist)
        self.event_date = event_date
        self.ocr_tools, self.ocr_tool_signature = ocr_tools, ocr_tool_signature
        self.extraction_timeout = extraction_timeout
        self.package = load_package(jurisdiction)
        self.config = {"engine": ENGINE, "version": __version__, "jurisdiction": jurisdiction,
                       "law_sources": sorted(r["rule_id"] for r in self.package["rules"]),
                       "model_id": model.model_id if model else None,
                       "challenge_model_id": challenge.model.model_id if challenge else None,
                       "ocr": bool(ocr_tools)}
        self.config_sha256 = sha(encoded(self.config))
        self.run_id = "run-" + uuid.uuid4().hex
        self._stage = None

    # ----- intake -------------------------------------------------------------------------
    def _identity(self):
        return {"run_id": "mail-" + self.run_id, "config_sha256": self.config_sha256, "mode": "preparation",
                "runtime": {"version": ENGINE + "/" + __version__, "image_digest": "local:" + sys.platform,
                            "code_sha256": sha(Path(__file__).read_bytes()),
                            "image_verification": {"state": "local_process_unverified"}}}

    def ingest_inbox(self, inbox):
        """Export every ``*.eml`` under ``inbox`` and preserve it through the canonical mail backend."""
        inbox = Path(inbox)
        files = sorted(p for p in inbox.iterdir() if p.is_file() and p.suffix.lower() == ".eml") if inbox.is_dir() else []
        backend = CanonicalMailBackend(self.root.sub("mail"), self.root.intake, self.root.ledger)
        identity = self._identity()
        backend.start_run(identity)
        preserved, replayed, failures = [], [], []
        try:
            for path in files:
                raw = path.read_bytes()
                uid = eml_export.local_uid(sha(raw))
                try:
                    receipt = eml_export.export_message(raw, mail_root=self.root.sub("mail"), account=self.account,
                                                        mailbox="inbox", uidvalidity=1, uid=uid)
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
        validators = {"extract": install_extraction_validator(database=database, evidence_root=evidence, original_root=blobs)}
        validators.update(stage_adapters.install_validators(database))
        profile = "records-run:" + sha(encoded({"db": str(database), "config": self.config_sha256}))[:32]
        catalog = catalog_run_factory(database, artifacts, run_id=self.run_id, owner=OWNER, profile_id=profile,
                                      engine_version=ENGINE, config_sha256=self.config_sha256,
                                      additional_adapters=validators)
        extraction = ExtractionStageAdapter(database=database, evidence_root=evidence, original_root=blobs,
                                            run_id=self.run_id, owner=OWNER, profile_id=profile)
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

    def subjects(self):
        return [row["sha256"] for row in self._query("SELECT sha256 FROM originals WHERE scope!='out_of_scope' ORDER BY first_seen_at,sha256")]

    def advance_all(self, subjects=None):
        """Advance every subject through the stage order; never raise for one record."""
        stage = self._open_stage_run()
        report = []
        for subject in (self.subjects() if subjects is None else subjects):
            outcome = {"subject_sha256": subject, "stages": {}}
            report.append(outcome)
            for name in STAGE_ORDER:
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
                if result.get("reason"):
                    outcome["stages"][name] += ":" + str(result["reason"])[:160]
                if result.get("status") not in ("done", "inapplicable"):
                    break
        return report

    # ----- helpers ---------------------------------------------------------------------
    def _original(self, subject):
        rows = self._query("SELECT * FROM originals WHERE sha256=?", (subject,))
        if not rows:
            raise RunError("original_missing")
        return rows[0]

    def _form(self, subject, original):
        if original.get("mime_detected") == "message/rfc822":
            return "eml"
        for occurrence in self._query("SELECT * FROM occurrences WHERE original_sha256=?", (subject,)):
            evidence = json.loads(occurrence["evidence"])
            receipt = evidence.get("export_receipt_path")
            if receipt and Path(receipt).exists():
                forms = eml_export.attachment_forms(Path(receipt))
                if subject in forms:
                    return forms[subject]
        with intake_folder.secure_open(original["storage_path"]) as source:
            head = source.read(16384)
        return intake_folder.fmt(original["storage_path"], head)

    def _units(self, subject):
        rows = self._query("SELECT * FROM units WHERE original_sha256=? ORDER BY legacy_ordinal, id", (subject,))
        units = []
        for row in rows[:MAX_UNITS_PER_RECORD]:
            raw = Path(row["derived_path"]).read_bytes()
            try:
                item = json.loads(raw)
            except ValueError:
                continue
            text = item.get("text") if isinstance(item, dict) else None
            if not isinstance(text, str):
                continue
            data = item.get("data") if isinstance(item.get("data"), dict) else {}
            units.append({"unit_id": row["id"], "locator": json.loads(row["locator"]), "locator_text": row["locator"],
                          "text": text, "artifact_sha256": sha(raw), "status": row["status"],
                          "unit_type": row["unit_type"], "ocr": "ocr_receipt_id" in data})
        return units

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
        if self.event_date:
            return self.event_date
        header = self._headers(subject).get("date")
        if header:
            from email.utils import parsedate_to_datetime
            try:
                return parsedate_to_datetime(header).date()
            except (TypeError, ValueError):
                pass
        return date.today()

    # ----- stages ----------------------------------------------------------------------
    def stage_extract(self, subject, stage):
        original = self._original(subject)
        form = self._form(subject, original)
        ocr_root = self.root.sub("ocr") if self.ocr_tools else None
        result = extraction_routes.extract(original["storage_path"], subject, self.root.sub("extract"), form=form,
                                           timeout=self.extraction_timeout, ocr_output_root=ocr_root,
                                           ocr_tools=self.ocr_tools, tool_signature=self.ocr_tool_signature)
        receipt = Path(result["run_path"]) / "extraction.json"
        codes = [i.get("code", "?") for i in result.get("issues", [])]
        if result["status"] == "blocked" or (result["status"] == "partial" and not result.get("ocr_receipts")):
            # Not acceptable yet (no decoder, timeout, or image-only pages with no OCR runtime).
            # Record a keyed alert so the gap is visible in `records status`; nothing is faked.
            reason = "extraction_" + result["status"] + ":" + ",".join(codes)
            self._alert("extract:" + subject + ":" + ",".join(sorted(set(codes))), owner="runtime")
            return {"status": "blocked", "reason": reason}
        enrolled = stage["enrollment"].enroll(original_path=Path(original["storage_path"]), receipt_path=receipt,
                                              receipt_sha256=result["receipt_sha256"])
        accepted = stage["extraction"].accept(enrolled["import_id"])
        return {"status": accepted.get("status"), "import_id": enrolled["import_id"], "form": form,
                "units": enrolled.get("units")}

    def stage_catalog(self, subject, stage):
        units = [u for u in self._units(subject) if u["text"].strip()]
        if not units:
            return self._inapplicable(subject, stage, "catalog", "no_text_units")
        headers = self._headers(subject)
        original = self._original(subject)
        is_mail = original.get("mime_detected") == "message/rfc822"
        first = next((u for u in units if u["unit_type"] == "email_body"), units[0])
        quote = first["text"].strip()[:400]
        proof = {"unit_id": first["unit_id"], "source_sha256": subject, "locator": first["locator_text"],
                 "artifact_sha256": first["artifact_sha256"], "quote": quote}
        text_blob = " ".join(u["text"] for u in units[:50]).lower()
        doc_type = "correspondence" if is_mail else next((t for key, t in DOC_TYPE_HINTS if key in text_blob), "other")
        title = headers.get("subject") if is_mail else None
        if not title:
            title = quote.splitlines()[0][:120] if quote else None
        event = self._event_date(subject).isoformat() if headers.get("date") else None
        metadata = {"title": title, "doc_type": doc_type, "summary": quote[:300] or None,
                    "date_from": event, "date_to": event}
        metadata = {k: v for k, v in metadata.items() if v not in (None, "")}
        field_support = {k: [first["unit_id"]] for k in metadata}
        metadata["evidence"] = [{"source_sha256": subject, "locator": first["locator_text"]}]
        card = {"schema": "catalog-card-evidence-v1", "subject_sha256": subject, "metadata": metadata,
                "supports": [proof], "field_support": field_support}
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
            content.update(comparisons=[], event_date=self._event_date(subject).isoformat())
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
        units = self._units(subject)
        text_units = [{"locator": u["locator"], "text": u["text"]} for u in units]
        leads = run_detectors(text_units, self.package) if text_units else []
        structured = {name: {"status": "skipped", "reason": "no_structured_units_of_required_type"}
                      for name in stage_adapters.STRUCTURED_DETECTORS}
        content = {"schema": stage_adapters.SCHEMAS["detect"], "subject_sha256": subject,
                   "detector_version": DETECTOR_VERSION, "unit_count": len(units),
                   "text_leads": leads, "structured": structured, "rules_version": self.package.get("schema_version")}
        coverage = {"denominator": {"kind": "items", "total": len(units)}, "covered": len(units), "scope": "full_text"}
        locators = [u["locator_text"] for u in units] or ["stage:detect"]
        return stage_adapters.promote(stage["runner"], subject, "detect", content, author_id="records-detect",
                                      coverage=coverage, locators=locators[:1024])

    def stage_review(self, subject, stage):
        units = self._units(subject)
        text_units = [{"locator": u["locator"], "text": u["text"]} for u in units]
        if not text_units:
            return {"status": "blocked", "reason": "no_text_units"}
        tier = "strict_local" if (self.model and self.model.privacy_tier == "strict_local") else "redacted_cloud"
        digest = build_digest(subject, text_units, self.package, self.jurisdiction,
                              str(self.package.get("schema_version")), tier, model_config=self.model,
                              allowlist=self.allowlist, denylist=self.denylist,
                              event_date=self._event_date(subject), opener=self.opener)
        ocr_pages = sorted({u["locator"].get("page") for u in units if u["ocr"]} - {None})
        if ocr_pages:
            digest["limitations"].append(
                "Page(s) " + ", ".join(str(p) for p in ocr_pages) +
                " were read by local OCR and have not been visually reviewed; verify quotes against the page image.")
        redacted, _ = redact_units(text_units, allowlist=self.allowlist, denylist=self.denylist)
        record = challenge_mod.challenge_review(digest, redacted, self.package, config=self.challenge, opener=self.opener)
        content = {"schema": stage_adapters.SCHEMAS["review"], "subject_sha256": subject,
                   "digest": digest, "challenge": record}
        coverage = {"denominator": {"kind": "items", "total": len(units)}, "covered": len(units), "scope": "full_text"}
        locators = [u["locator_text"] for u in units][:1024]
        result = stage_adapters.promote(stage["runner"], subject, "review", content, author_id="records-digest",
                                        coverage=coverage, locators=locators, challenges=[("factual", record)])
        return result

    def stage_compare(self, subject, stage):
        states = self._states(subject)
        review = self._content(subject, "review")
        digest = review["digest"]
        event_date = self._event_date(subject)
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
            return self._inapplicable(subject, stage, "compare", "no_rule_in_force_matched_digest_duties")
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
        proposal_id = "prop_" + subject[:16]
        public = self.root.sub("proposals/public") / (proposal_id + ".md")
        private = self.root.sub("proposals/private") / (proposal_id + ".manifest.json")
        for path, data in ((public, content["public_markdown"].encode()), (private, encoded(content["manifest"]))):
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_bytes(data)
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        privacy_receipt = self._states(subject)["privacy"]["receipt_sha256"]
        with store.ledger(self.root.ledger) as con:
            existing = con.execute("SELECT * FROM proposals WHERE id=?", (proposal_id,)).fetchone()
            if existing is None:
                con.execute("INSERT INTO proposals(id,tier,agency_id,public_content_sha256,manifest_sha256,"
                            "privacy_receipt_sha256,independent_receipt_sha256,owner_approval,approved_content_sha256,supersedes) "
                            "VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (proposal_id, "A", None, content["public_content_sha256"], content["manifest_sha256"],
                             privacy_receipt, states["review"]["receipt_sha256"], "none", None, None))
            elif existing["public_content_sha256"] != content["public_content_sha256"]:
                # Content changed after approval state was recorded: reset approval, keep history by supersession.
                con.execute("UPDATE proposals SET public_content_sha256=?,manifest_sha256=?,privacy_receipt_sha256=?,"
                            "independent_receipt_sha256=?,owner_approval='none',approved_content_sha256=NULL WHERE id=?",
                            (content["public_content_sha256"], content["manifest_sha256"], privacy_receipt,
                             states["review"]["receipt_sha256"], proposal_id))
            con.commit()
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

    def run(self, inbox=None, mail_config=None, *, client_factory=None):
        with root_lock(self.root.path):
            return self._run_locked(inbox, mail_config, client_factory=client_factory)

    def _run_locked(self, *args, **kwargs):
        # Recovery precedes this invocation's run row, so it cannot interrupt itself.
        try:
            report = self._run_locked_unchecked(*args, **kwargs)
        except Exception:
            with store.ledger(self.root.ledger) as con:
                con.execute("UPDATE runs SET status='failed',ended_at=?,summary=? WHERE run_id=?",
                            (now(), encoded({"failure_code": "run_failed"}).decode(), self.run_id))
                con.commit()
            raise RunError("run_failed") from None
        outcomes = [value for item in report.get("subjects", []) for value in item.get("stages", {}).values()]
        technical = any(value.startswith("error:") for value in outcomes)
        gaps = technical or bool(report.get("intake", {}).get("failures")) or bool((report.get("mailbox") or {}).get("failures"))
        gaps = gaps or any(value.startswith(("blocked", "pending")) for value in outcomes)
        status = "failed" if technical else "completed_with_gaps" if gaps else "completed"
        report.update(status=status, exit_code=2 if technical else 3 if gaps else 0)
        with store.ledger(self.root.ledger) as con:
            con.execute("UPDATE runs SET status=?,summary=? WHERE run_id=?",
                        (status, encoded({"status": status, "subjects": len(report.get("subjects", [])), "counts": report["counts"]}).decode(), self.run_id))
            con.commit()
        path = self.root.sub("runs") / (self.run_id + ".json")
        path.write_bytes(encoded(report))
        os.chmod(path, 0o600)
        return report

    def _run_locked_unchecked(self, inbox, mail_config, *, client_factory=None):
        started = now()
        recovery = self.recover()
        self._open_stage_run()
        intake = self.ingest_inbox(inbox) if inbox else {"messages": 0, "preserved": [], "replayed": [], "failures": []}
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
        self._close_stage_run({"subjects": len(progress), "counts": summary["stages"]})
        path = self.root.sub("runs") / (self.run_id + ".json")
        path.write_bytes(json.dumps(report, sort_keys=True, indent=1).encode())
        os.chmod(path, 0o600)
        return report


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
    from .publish import drifted_proposals
    return {"root": str(root.path), "ledger": True, "originals": summary["originals"], "stages": summary["stages"],
            "end_to_end_complete": summary["candidate_seven_stage_complete"], "proposals": proposals,
            "drifted_proposals": drifted_proposals(root.path),
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
    return Pipeline(args.root, jurisdiction=args.jurisdiction, account=args.account, model=model, challenge=challenge,
                    redaction_allowlist=[x for x in env.get("REDACTION_ALLOWLIST", "").split(",") if x.strip()],
                    redaction_denylist=[x for x in env.get("REDACTION_DENYLIST", "").split(",") if x.strip()],
                    event_date=event, ocr_tools=ocr_tools,
                    ocr_tool_signature=(env.get("OCR_TOOL_SIGNATURE") or ocr_signature) if ocr_tools else None,
                    extraction_timeout=args.timeout)


def main(argv=None):
    if "--unattended" in (sys.argv[1:] if argv is None else argv):
        from .unattended import main as unattended_main
        return unattended_main(argv)
    parser = argparse.ArgumentParser(prog="records run", description=__doc__)
    parser.add_argument("--root", required=True, help="private records root (created if missing)")
    parser.add_argument("--inbox", help="directory of .eml files to preserve before advancing stages")
    parser.add_argument("--mail-config", dest="mail_config",
                        help="owner-only JSON (0600) naming the IMAP host/account; new messages are fetched and preserved")
    parser.add_argument("--jurisdiction", default="us-ca")
    parser.add_argument("--account", default="local")
    parser.add_argument("--event-date", dest="event_date", help="YYYY-MM-DD override for rule applicability")
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--ocr", action="store_true", help="use local tesseract/pdftoppm for image-only PDF pages when installed")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    pipeline = build_pipeline(args)
    report = pipeline.run(args.inbox, args.mail_config)
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
