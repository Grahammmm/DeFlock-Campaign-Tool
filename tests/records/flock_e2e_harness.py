"""Flock end-to-end fault-injection harness (T12).

A synthetic end-to-end driver for the ``run-records`` orchestration (T11) that
emits the sanitized run-report v1 (T11a contract) and maintains a private
synthetic ledger, plus the five fault injectors named in the plan:

* docker down      -- bounded Docker wait fails cleanly (exit 75, nothing runs)
* telegram down    -- notify stage fails, run still completes (exit 0)
* portal 404       -- one portal link 404s, portal stage warns, run continues
* corrupt PDF      -- one document fails extraction, extract stage warns
* duplicate receipt-- one receipt identity repeated, adoption deduplicates

This is test infrastructure, not the production run script. T11i wires the real
T4 (adoption), T6 (OCR) and T8 (portal) stages into the same contract; the
harness pins the invariants the real integration must keep, so the assertions
here hold for the real run once T11i lands.

Contracts honoured:

* ``campaign_tool.records.run_report.STAGE_ORDER`` and
  ``run_report.validate_run_report`` -- every report the harness builds is
  validated against ``schemas/records-run-report-v1.json`` before it is
  returned, so the harness can never emit a malformed report.
* The adoption command interface (T4 ``records adopt-mail``): adoption counts
  ``already_adopted``/``pending`` and is idempotent by receipt identity. The
  harness models that same identity-deduplication contract and asserts it.
* Extraction as a pluggable stage (Phase 1): native-text extraction only; a
  document with no native text is queued ``awaiting_ocr`` (durably, in the
  synthetic ledger) and counted (never dropped), a corrupt document is a
  bounded gap. The harness ships a deterministic fake backend, so no real OCR,
  model or network is needed.

The Phase 1 go-live pipeline (G decision 2026-10-19) is: inbox sync -> delta ->
ledger adoption -> portal intake -> native-text extraction -> digest/audit gates
-> outputs -> run summary, 3x/day. Documents that need OCR are queued
``awaiting_ocr`` and counted in the report; OCR itself lands in Phase 2 after
the container swap. This harness models that shape; T11i wires the real stages.

Durable work (T12f): per-document extraction state is persisted in the synthetic
ledger as ``pending`` / ``awaiting_ocr`` / ``failed`` / ``done``. Unresolved
work (anything not ``done``) is re-selected on every run, so a document deferred
or corrupt on one run stays visible on later runs and is retried -- recovered
work is counted exactly once (a ``done`` document is never re-extracted), and a
document that still cannot be processed remains visible as unresolved instead of
silently disappearing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from typing import Callable
import uuid

from campaign_tool.records import run_report

# Exit codes mirror the run-records wrapper contract (T10).
EXIT_DOCKER_UNAVAILABLE = 75
EXIT_COMPLETED = 0
EXIT_COMPLETED_WITH_GAPS = 3

STAGES = tuple(run_report.STAGE_ORDER)  # sync, delta, adoption, portal, extract, digest_gates, outputs, notify

DEFAULT_SCENARIO = {
    "documents": ["doc-1", "doc-2", "doc-3"],
    "receipts": ["recv-1", "recv-2", "recv-3"],
    "portal_links": ["portal-1", "portal-2", "portal-3"],
    "needs_ocr_documents": [],
    "portal_missing_link": "portal-2",
    "corrupt_document": "doc-2",
    "duplicate_receipt": "recv-1",
}


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run_id() -> str:
    return "run_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:8]


def _parse_utc(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# Stage outcome and report construction
# --------------------------------------------------------------------------- #

@dataclass
class StageOutcome:
    name: str
    status: str          # ok | warn | fail | skipped
    input: int = 0
    output: int = 0
    failed: int = 0
    skipped: int = 0
    error_code: str | None = None
    duration_seconds: float | None = 0.0


def _stage_dict(outcome: StageOutcome) -> dict:
    return {
        "name": outcome.name,
        "status": outcome.status,
        "counts": {"input": outcome.input, "output": outcome.output,
                   "failed": outcome.failed, "skipped": outcome.skipped},
        "duration_seconds": outcome.duration_seconds,
        "error_code": outcome.error_code,
    }


def build_run_report(run_id, mode, started_at, finished_at, outcomes, *,
                     new_data=None, review_required_count=0, notify_failed=False,
                     needs_g=None) -> dict:
    """Build a run-report v1 from ``outcomes`` (one per stage, in STAGE_ORDER).

    Always validates the result against the v1 schema and raises ``ValueError``
    on any contract violation, so a caller can never receive a malformed report.
    """
    by_name = {outcome.name: outcome for outcome in outcomes}
    if tuple(by_name) != STAGES:
        raise ValueError("outcomes must cover every stage in STAGE_ORDER")
    statuses = {outcome.status for outcome in outcomes}
    overall = "fail" if "fail" in statuses else "warn" if "warn" in statuses else "ok"
    report = {
        "schema_version": 1,
        "report_kind": "flock-records-run-report",
        "run_id": run_id,
        "mode": mode,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_seconds": round((_parse_utc(finished_at) - _parse_utc(started_at)).total_seconds(), 3),
        "overall_status": overall,
        "stages": [_stage_dict(by_name[name]) for name in STAGES],
        "new_data": new_data or {
            "documents": 0, "pages": 0, "extracted_units": 0,
            "portal_releases": 0, "request_status_changes": 0,
        },
        "review_required_count": review_required_count,
        "notify_failed": notify_failed,
        "needs_g": needs_g or [],
    }
    run_report.validate_run_report(report)
    return report


# --------------------------------------------------------------------------- #
# Synthetic ledger
# --------------------------------------------------------------------------- #

class SyntheticLedger:
    """A minimal private ledger that records the counts the harness asserts.

    Tables mirror the *count* semantics of the real records ledger, not its
    schema: runs, per-run per-stage counts, adopted receipt identities,
    first-seen document identities, and durable per-document extraction state
    (``pending`` / ``awaiting_ocr`` / ``failed`` / ``done``). Idempotent re-runs
    add nothing new, which is exactly what the harness asserts.
    """

    # Durable extraction states. ``done`` is terminal; the others are unresolved
    # and are re-selected on every subsequent run.
    STATE_PENDING = "pending"
    STATE_AWAITING_OCR = "awaiting_ocr"
    STATE_FAILED = "failed"
    STATE_DONE = "done"

    def __init__(self, path: Path):
        self.path = Path(path)
        self.con = sqlite3.connect(self.path)
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY, mode TEXT, started_at TEXT, finished_at TEXT,
                overall_status TEXT, exit_code INTEGER, notify_failed INTEGER);
            CREATE TABLE IF NOT EXISTS stage_counts (
                run_id TEXT, stage TEXT, input INTEGER, output INTEGER,
                failed INTEGER, skipped INTEGER, PRIMARY KEY (run_id, stage));
            CREATE TABLE IF NOT EXISTS adopted (
                identity TEXT PRIMARY KEY, run_id TEXT);
            CREATE TABLE IF NOT EXISTS documents (
                identity TEXT PRIMARY KEY, run_id TEXT);
            CREATE TABLE IF NOT EXISTS releases (
                identity TEXT PRIMARY KEY, run_id TEXT);
            CREATE TABLE IF NOT EXISTS document_state (
                identity TEXT PRIMARY KEY, state TEXT NOT NULL, run_id TEXT NOT NULL);
            """
        )
        self.con.commit()

    def close(self):
        self.con.close()

    def record_run(self, run_id, mode, started_at, finished_at, overall_status, exit_code, notify_failed):
        self.con.execute(
            "INSERT INTO runs VALUES(?,?,?,?,?,?,?)",
            (run_id, mode, started_at, finished_at, overall_status, exit_code, int(notify_failed)))
        self.con.commit()

    def record_stage(self, run_id, outcome: StageOutcome):
        self.con.execute(
            "INSERT INTO stage_counts VALUES(?,?,?,?,?,?)",
            (run_id, outcome.name, outcome.input, outcome.output, outcome.failed, outcome.skipped))
        self.con.commit()

    def adopt(self, identity, run_id) -> bool:
        """Adopt a receipt identity once; return True only on first adoption."""
        try:
            self.con.execute("INSERT INTO adopted VALUES(?,?)", (identity, run_id))
            self.con.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def see_document(self, identity, run_id) -> bool:
        """Record a document identity as first-seen; return True only the first time.

        On first sight the document also enters the durable extraction queue in
        the ``pending`` state. A document already seen leaves its extraction
        state untouched, so unresolved work survives a close/reopen.
        """
        try:
            self.con.execute("INSERT INTO documents VALUES(?,?)", (identity, run_id))
            self.con.execute(
                "INSERT INTO document_state VALUES(?,?,?)",
                (identity, self.STATE_PENDING, run_id))
            self.con.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def release(self, identity, run_id) -> bool:
        """Record a portal release as first-fetched; return True only the first time."""
        try:
            self.con.execute("INSERT INTO releases VALUES(?,?)", (identity, run_id))
            self.con.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def document_state(self, identity) -> str | None:
        """Return the durable extraction state for ``identity`` (None if unseen)."""
        row = self.con.execute(
            "SELECT state FROM document_state WHERE identity=?", (identity,)).fetchone()
        return row[0] if row else None

    def set_document_state(self, identity, state, run_id):
        """Upsert the durable extraction state for ``identity``."""
        self.con.execute(
            "INSERT INTO document_state VALUES(?,?,?) "
            "ON CONFLICT(identity) DO UPDATE SET state=excluded.state, run_id=excluded.run_id",
            (identity, state, run_id))
        self.con.commit()

    def document_state_counts(self):
        """Return a mapping of durable extraction state -> number of documents."""
        rows = self.con.execute(
            "SELECT state, count(*) FROM document_state GROUP BY state").fetchall()
        return {row[0]: row[1] for row in rows}

    def count(self, table):
        return self.con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]

    def adopted_identities(self):
        return {row[0] for row in self.con.execute("SELECT identity FROM adopted")}

    def document_identities(self):
        return {row[0] for row in self.con.execute("SELECT identity FROM documents")}

    def stage_counts(self, run_id):
        rows = self.con.execute(
            "SELECT stage,input,output,failed,skipped FROM stage_counts WHERE run_id=? ORDER BY stage",
            (run_id,)).fetchall()
        return {r[0]: {"input": r[1], "output": r[2], "failed": r[3], "skipped": r[4]} for r in rows}


# --------------------------------------------------------------------------- #
# Pluggable extraction backend (fake/stub)
# --------------------------------------------------------------------------- #

class CorruptPdf(Exception):
    """A document that cannot be parsed; the extract stage must gap, not crash."""


class NeedsOcr(Exception):
    """A document with no native text; queued ``awaiting_ocr``, never dropped."""


class FakeExtractBackend:
    """Deterministic extraction stub (Phase 1: native-text only).

    Clean documents yield fixed text; a corrupt one raises ``CorruptPdf`` and a
    document with no native text raises ``NeedsOcr`` (queued durably as
    ``awaiting_ocr``, never dropped). T11i swaps this for the real
    extraction/OCR stage; the harness only cares that extraction is a swappable
    stage and that a per-document failure or OCR deferral becomes a bounded,
    counted, durable outcome -- never a run crash or a drop.
    """

    def __init__(self, units_per_document=2, corrupt: set[str] | None = None,
                 needs_ocr: set[str] | None = None):
        self.units_per_document = units_per_document
        self.corrupt = corrupt or set()
        self.needs_ocr = needs_ocr or set()

    def extract(self, document_id):
        if document_id in self.corrupt:
            raise CorruptPdf(document_id)
        if document_id in self.needs_ocr:
            raise NeedsOcr(document_id)
        return {"document_id": document_id, "units": self.units_per_document, "pages": 2}


# --------------------------------------------------------------------------- #
# Fault injectors
# --------------------------------------------------------------------------- #

@dataclass
class Fault:
    name: str
    install: Callable[["Orchestrator"], None]

    def apply(self, orchestrator):
        self.install(orchestrator)


def docker_down() -> Fault:
    """The Docker daemon never becomes ready within the bounded retries."""
    def install(orchestrator):
        orchestrator.docker_probe = lambda: False
    return Fault("docker_down", install)


def telegram_down() -> Fault:
    """Telegram delivery fails; the run must still complete with exit 0."""
    def install(orchestrator):
        orchestrator.notify = lambda report: False
    return Fault("telegram_down", install)


def portal_404() -> Fault:
    """One portal link returns HTTP 404; the portal stage warns and continues."""
    def install(orchestrator):
        original = orchestrator.portal_fetch

        def fetching(link):
            if link == orchestrator.scenario["portal_missing_link"]:
                return "http_404"
            return original(link)
        orchestrator.portal_fetch = fetching
    return Fault("portal_404", install)


def corrupt_pdf() -> Fault:
    """One document is corrupt; extract gaps it and the run continues."""
    def install(orchestrator):
        orchestrator.extract.corrupt = {orchestrator.scenario["corrupt_document"]}
    return Fault("corrupt_pdf", install)


def duplicate_receipt() -> Fault:
    """The same receipt identity is presented twice; adoption deduplicates."""
    def install(orchestrator):
        orchestrator.receipts = list(orchestrator.receipts) + [orchestrator.scenario["duplicate_receipt"]]
    return Fault("duplicate_receipt", install)


# --------------------------------------------------------------------------- #
# Orchestrator (synthetic run-records double) and the harness
# --------------------------------------------------------------------------- #

@dataclass
class RunOutcome:
    exit_code: int
    outcome: str           # completed | completed_with_gaps | docker_unavailable
    report: dict | None
    attempts: int
    run_id: str | None
    ledger: SyntheticLedger


class Orchestrator:
    """A test double for the T11 run-records orchestration.

    Seams are plain callables so the harness (and later T11i) can substitute the
    real stages. Every stage is idempotent against the ledger: re-running a
    clean scenario produces no new data, and unresolved extraction work (not yet
    ``done``) is retried on each run rather than dropped.
    """

    def __init__(self, ledger: SyntheticLedger, *, mode="test", scenario=None,
                 docker_probe=None, notify=None, portal_fetch=None,
                 extract: FakeExtractBackend | None = None, docker_wait_attempts=3,
                 docker_wait_delay=0.0):
        self.ledger = ledger
        self.mode = mode
        self.scenario = {**DEFAULT_SCENARIO, **(scenario or {})}
        self.documents = list(self.scenario["documents"])
        self.receipts = list(self.scenario["receipts"])
        self.portal_links = list(self.scenario["portal_links"])
        self.docker_probe = docker_probe or (lambda: True)
        self.notify = notify or (lambda report: True)
        self.portal_fetch = portal_fetch or (lambda link: "ok")
        self.extract = extract or FakeExtractBackend(
            needs_ocr=set(self.scenario.get("needs_ocr_documents", [])))
        self.docker_wait_attempts = docker_wait_attempts
        self.docker_wait_delay = docker_wait_delay

    # -- docker readiness -------------------------------------------------- #
    def wait_for_docker(self) -> int:
        for attempt in range(1, self.docker_wait_attempts + 1):
            if self.docker_probe():
                return attempt
        raise _DockerUnavailable(self.docker_wait_attempts)

    # -- stages ------------------------------------------------------------ #
    def _stage_sync(self):
        new, skipped = 0, 0
        for doc in self.documents:
            if self.ledger.see_document(doc, self._current_run):
                new += 1
            else:
                skipped += 1
        return StageOutcome("sync", "ok", input=len(self.documents), output=new, skipped=skipped)

    def _stage_delta(self, new_docs):
        return StageOutcome("delta", "ok", input=new_docs, output=new_docs)

    def _stage_adoption(self):
        adopted = skipped = 0
        for receipt in self.receipts:
            if self.ledger.adopt(receipt, self._current_run):
                adopted += 1
            else:
                skipped += 1
        return StageOutcome("adoption", "ok", input=len(self.receipts),
                            output=adopted, skipped=skipped)

    def _stage_portal(self):
        fetched = failed = skipped = 0
        for link in self.portal_links:
            if self.portal_fetch(link) == "ok":
                if self.ledger.release(link, self._current_run):
                    fetched += 1
                else:
                    skipped += 1
            else:
                failed += 1
        status = "warn" if failed else "ok"
        return StageOutcome("portal", status, input=len(self.portal_links),
                            output=fetched, failed=failed, skipped=skipped,
                            error_code="PORTAL_HTTP_404" if failed else None)

    def _unresolved_documents(self):
        """Documents not yet ``done`` -- retried on every run so unresolved work
        stays visible and recovered work is processed exactly once."""
        return [d for d in self.documents if self.ledger.document_state(d) != SyntheticLedger.STATE_DONE]

    def _stage_extract(self):
        extracted = failed = skipped = 0
        pages = units = 0
        unresolved = self._unresolved_documents()
        for doc in unresolved:
            try:
                result = self.extract.extract(doc)
            except NeedsOcr:
                skipped += 1  # queued awaiting_ocr durably, never dropped
                self.ledger.set_document_state(doc, SyntheticLedger.STATE_AWAITING_OCR, self._current_run)
                continue
            except CorruptPdf:
                failed += 1
                self.ledger.set_document_state(doc, SyntheticLedger.STATE_FAILED, self._current_run)
                continue
            extracted += 1
            pages += result["pages"]
            units += result["units"]
            self.ledger.set_document_state(doc, SyntheticLedger.STATE_DONE, self._current_run)
        status = "warn" if failed else "ok"
        return StageOutcome("extract", status, input=len(unresolved),
                            output=extracted, failed=failed, skipped=skipped,
                            error_code="EXTRACT_CORRUPT_PDF" if failed else None,
                            ), {"pages": pages, "units": units, "awaiting_ocr": skipped}

    def _stage_digest_gates(self, extracted):
        review_required = extracted  # every extracted unit still awaits independent review
        return StageOutcome("digest_gates", "ok", input=extracted, output=extracted), review_required

    def _stage_outputs(self, gated):
        return StageOutcome("outputs", "ok", input=gated, output=gated)

    # -- full run ---------------------------------------------------------- #
    def run(self) -> RunOutcome:
        self._current_run = _run_id()
        try:
            attempts = self.wait_for_docker()
        except _DockerUnavailable:
            return RunOutcome(EXIT_DOCKER_UNAVAILABLE, "docker_unavailable", None,
                              self.docker_wait_attempts, None, self.ledger)

        started_at = _utcnow()
        outcomes = []
        new_data = {"documents": 0, "pages": 0, "extracted_units": 0,
                    "portal_releases": 0, "request_status_changes": 0}

        sync = self._stage_sync()
        outcomes.append(sync)
        new_data["documents"] = sync.output

        delta = self._stage_delta(sync.output)
        outcomes.append(delta)

        adoption = self._stage_adoption()
        outcomes.append(adoption)
        new_data["request_status_changes"] = adoption.output

        portal = self._stage_portal()
        outcomes.append(portal)
        new_data["portal_releases"] = portal.output

        extract, extraction = self._stage_extract()
        outcomes.append(extract)
        new_data["pages"] = extraction["pages"]
        new_data["extracted_units"] = extraction["units"]

        gates, review_required = self._stage_digest_gates(extract.output)
        outcomes.append(gates)

        outputs = self._stage_outputs(gates.output)
        outcomes.append(outputs)

        # Build a provisional, schema-valid report (notify optimistically "ok")
        # and hand it to notify as an explicit pre-delivery snapshot, so the
        # callback always receives a non-null, sanitized, schema-valid payload.
        provisional_notify = StageOutcome("notify", "ok", input=1, output=1)
        pre_delivery = build_run_report(
            self._current_run, self.mode, started_at, _utcnow(),
            outcomes + [provisional_notify],
            new_data=new_data, review_required_count=review_required,
            notify_failed=False)
        delivered = self.notify(pre_delivery)

        # Finalize the notify stage and the final report consistently: delivery
        # success keeps the provisional "ok"; delivery failure is recorded as a
        # non-fatal fail (the run still completes).
        if delivered:
            notify = provisional_notify
        else:
            notify = StageOutcome("notify", "fail", input=1, output=0, failed=1,
                                  error_code="NOTIFY_SEND_FAILED")
        outcomes.append(notify)

        finished_at = _utcnow()
        pipeline_gaps = extract.failed > 0 or portal.failed > 0
        exit_code = EXIT_COMPLETED_WITH_GAPS if pipeline_gaps else EXIT_COMPLETED
        notify_failed = notify.status == "fail"

        report = build_run_report(
            self._current_run, self.mode, started_at, finished_at, outcomes,
            new_data=new_data, review_required_count=review_required,
            notify_failed=notify_failed)

        self.ledger.record_run(self._current_run, self.mode, started_at, finished_at,
                               report["overall_status"], exit_code, notify_failed)
        for outcome in outcomes:
            self.ledger.record_stage(self._current_run, outcome)

        return RunOutcome(exit_code, "completed_with_gaps" if pipeline_gaps else "completed",
                          report, attempts, self._current_run, self.ledger)


class _DockerUnavailable(Exception):
    def __init__(self, attempts):
        super().__init__("docker_unavailable")
        self.attempts = attempts


class Harness:
    """Runs the orchestration with faults and asserts the contract."""

    def __init__(self, base: Path, scenario=None):
        self.base = base
        self.scenario = scenario
        self.ledger_path = base / "ledger.sqlite"
        self.ledger = SyntheticLedger(self.ledger_path)

    def run(self, faults=(), notify=None) -> RunOutcome:
        orchestrator = Orchestrator(self.ledger, scenario=self.scenario, notify=notify)
        for fault in faults:
            fault.apply(orchestrator)
        return orchestrator.run()

    def run_consecutive(self, n=3, faults=()) -> list[RunOutcome]:
        return [self.run(faults) for _ in range(n)]
