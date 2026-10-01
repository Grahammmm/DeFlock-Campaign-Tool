"""Bounded trusted post-preservation handoff, not a new lifecycle controller."""
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
import hashlib
import re
import time

from .contracts import IntegrationGap, Preserved
from .canonical_mail import CanonicalMailBackend

STAGES = ('preserve', 'extract', 'catalog', 'detect', 'review', 'compare', 'privacy')
DOWNSTREAM = ('detect', 'review', 'compare', 'privacy')


@dataclass(frozen=True)
class PreparedItem:
    """Trusted startup paths/hashes; never build this from record instructions."""
    subject_sha256: str
    original_path: Path
    extraction_receipt: Path
    extraction_receipt_sha256: str
    catalog_artifact: Path | None
    catalog_sha256: str | None
    author_id: str


class PipelineConnection:
    """Real installed adapters; no validator registry, profiles, or finalizer."""
    def __init__(self, *, backend, enrollment, extraction, catalog=None, items=(),
                 max_items=20, max_original_bytes=1024 * 1024, budget_seconds=60):
        from campaign_tool.records.extraction_ledger import ExtractionLedgerAdapter
        from campaign_tool.records.extraction_validation import ExtractionStageAdapter
        if (type(backend) is not CanonicalMailBackend or
                type(enrollment) is not ExtractionLedgerAdapter or
                type(extraction) is not ExtractionStageAdapter):
            raise IntegrationGap('fixed_installed_adapters_required')
        if type(max_items) is not int or not 1 <= max_items <= 20:
            raise ValueError('item_bound')
        if type(max_original_bytes) is not int or not 1 <= max_original_bytes <= 1024 * 1024:
            raise ValueError('original_byte_bound')
        if type(budget_seconds) is not int or not 1 <= budget_seconds <= 60:
            raise ValueError('budget_bound')
        if not isinstance(items, (list, tuple)) or len(items) > max_items:
            raise ValueError('plan_bound')
        plans = {}
        for item in items:
            if type(item) is not PreparedItem:
                raise ValueError('typed_startup_plan_required')
            values = [item.subject_sha256, item.extraction_receipt_sha256]
            if (item.catalog_artifact is None) != (item.catalog_sha256 is None):
                raise ValueError('catalog_plan_pair_required')
            if item.catalog_sha256 is not None:
                values.append(item.catalog_sha256)
            if any(not isinstance(v, str) or not re.fullmatch('[0-9a-f]{64}', v) for v in values):
                raise ValueError('sha256_required')
            if item.subject_sha256 in plans:
                raise ValueError('duplicate_startup_plan')
            if not isinstance(item.author_id, str) or not item.author_id.strip():
                raise ValueError('trusted_author_required')
            plans[item.subject_sha256] = item
        self.backend, self.enrollment = backend, enrollment
        self.extraction, self.catalog = extraction, catalog
        self.database = Path(backend.database).resolve()
        self.plans = MappingProxyType(plans)
        self.max_items, self.max_bytes, self.budget = max_items, max_original_bytes, budget_seconds
        self._authority()

    def _authority(self):
        """Use the real WP4 runner/validator binding; never synthesize an alias."""
        from campaign_tool.records.ledger import stages
        from campaign_tool.records.extraction_validation import ADAPTER_ID, _InstalledExtractionValidator
        runner = self.extraction.runner
        validator = self.extraction.validator
        if type(runner) is not stages.StageRunner or type(validator) is not _InstalledExtractionValidator:
            raise IntegrationGap('installed_extraction_authority_required')
        if runner.test_only or runner.validators.get('extract') != (validator.adapter_id, validator):
            raise IntegrationGap('installed_extract_binding_required')
        paths = (self.backend.database, self.enrollment.database, runner.database, validator.database)
        if any(Path(path).resolve() != self.database for path in paths):
            raise IntegrationGap('canonical_database_mismatch')
        with self.backend.store.ledger(self.database, readonly=True) as con:
            runner._check(con)
            if self.catalog is not None:
                from campaign_tool.records.catalog_stage import CatalogStage, CatalogAdapter
                if type(self.catalog) is not CatalogStage or type(self.catalog.adapter) is not CatalogAdapter:
                    raise IntegrationGap('installed_catalog_adapter_required')
                cr = self.catalog.runner
                ca = self.catalog.adapter
                if type(cr) is not stages.StageRunner or cr.test_only:
                    raise IntegrationGap('installed_catalog_authority_required')
                if Path(cr.database).resolve() != self.database or Path(ca.database).resolve() != self.database:
                    raise IntegrationGap('canonical_database_mismatch')
                binding = cr.validators.get('catalog')
                if type(binding) is not tuple or len(binding) != 2 or binding[0] != ca.adapter_id:
                    raise IntegrationGap('installed_catalog_binding_required')
                installed = binding[1]
                owner = getattr(installed, '__self__', None)
                if (type(owner) is not CatalogAdapter
                        or getattr(installed, '__func__', None) is not CatalogAdapter.validate
                        or owner.database != ca.database or owner.root != ca.root
                        or stages._INSTALLED_VALIDATORS.get(ca.adapter_id) is not installed):
                    raise IntegrationGap('installed_catalog_binding_required')
                if cr.run_id == runner.run_id:
                    raise IntegrationGap('distinct_stage_runs_required')
                cr._check(con)

    def _states(self, subjects):
        counts = {stage: 0 for stage in STAGES}
        receipts = {}
        with self.backend.store.ledger(self.database, readonly=True) as con:
            for subject in subjects:
                rows = con.execute('SELECT stage,status,receipt_sha256 FROM stage_state WHERE original_sha256=?', (subject,)).fetchall()
                for row in rows:
                    if row['stage'] in counts and row['status'] == 'done':
                        counts[row['stage']] += 1
                        receipts[subject + ':' + row['stage']] = row['receipt_sha256']
        return {'done': counts, 'accepted_receipts': receipts}

    def _catalog_subject(self, item):
        # Guard BEFORE process, which may invalidate a previously accepted subject.
        # No error handler may call process on a card that failed this guard.
        from campaign_tool.records.catalog_stage import MAX_CARD_BYTES, _decode
        raw = self.catalog.adapter._read(item.catalog_artifact, MAX_CARD_BYTES)
        if hashlib.sha256(raw).hexdigest() != item.catalog_sha256:
            raise IntegrationGap('catalog_exact_bytes_required')
        card = _decode(raw)
        if (type(card) is not dict or card.get('schema') != 'catalog-card-evidence-v1'
                or card.get('subject_sha256') != item.subject_sha256):
            raise IntegrationGap('catalog_planned_subject_mismatch')

    def advance(self, preserved):
        """Use after successful WP2 preserve; no intake checkpoint/run writes.

        Budget checks admit work between bounded adapters, not mid-call kill.
        A missing catalog adapter or artifact is an explicit capability gap.
        """
        if type(preserved) is not Preserved:
            raise ValueError('preserved_result_required')
        subjects = preserved.documents
        if (not isinstance(subjects, tuple) or len(subjects) > self.max_items or
                any(not isinstance(s, str) or not re.fullmatch('[0-9a-f]{64}', s) for s in subjects)
                or len(set(subjects)) != len(subjects)):
            raise ValueError('subject_bound_or_identity')
        self._authority()
        before = self._states(subjects)
        started = time.monotonic()
        outcomes = []
        for subject in subjects:
            outcome = {'subject_sha256': subject, 'status': 'pending'}
            outcomes.append(outcome)
            if time.monotonic() - started >= self.budget:
                outcome['reason'] = 'slice_admission_budget_exhausted'
                continue
            item = self.plans.get(subject)
            if item is None:
                outcome['reason'] = 'prepared_extraction_capability_missing'
                continue
            try:
                with self.backend.store.ledger(self.database, readonly=True) as con:
                    original = con.execute('SELECT bytes,storage_path FROM originals WHERE sha256=?', (subject,)).fetchone()
                    state = con.execute("SELECT status FROM stage_state WHERE original_sha256=? AND stage='preserve'", (subject,)).fetchone()
                if original is None or state is None or state['status'] != 'done':
                    raise IntegrationGap('accepted_preservation_required')
                if original['bytes'] > self.max_bytes:
                    raise IntegrationGap('original_byte_bound')
                if Path(original['storage_path']) != Path(item.original_path):
                    raise IntegrationGap('original_path_binding')
                enrolled = self.enrollment.enroll(original_path=item.original_path,
                    receipt_path=item.extraction_receipt, receipt_sha256=item.extraction_receipt_sha256)
                if enrolled['original_sha256'] != subject:
                    raise IntegrationGap('extraction_subject_binding')
                extracted = self.extraction.accept(enrolled['import_id'])
                if extracted.get('status') != 'done':
                    outcome.update(status='blocked', reason='extract_not_accepted')
                    continue
                if self.catalog is None or item.catalog_artifact is None:
                    outcome['reason'] = 'catalog_capability_or_artifact_missing'
                    continue
                if time.monotonic() - started >= self.budget:
                    outcome['reason'] = 'catalog_admission_budget_exhausted'
                    continue
                self._catalog_subject(item)
                result = self.catalog.process(item.catalog_artifact, item.catalog_sha256, author_id=item.author_id)
                outcome.update(status=result['status'], reason='canonical_catalog_accepted' if result['status'] == 'done' else 'catalog_not_accepted')
            except Exception as error:
                # Never leak arbitrary private exception messages or paths.
                outcome.update(status='blocked', reason=type(error).__name__)
        after = self._states(subjects)
        return {'schema': 'prepared-pipeline-connection-v1', 'subjects': len(subjects),
            'intake_run_id': (self.backend.run_identity or {}).get('run_id'),
            'extract_run_id': self.extraction.runner.run_id,
            'catalog_run_id': self.catalog.runner.run_id if self.catalog else None,
            'before': before, 'after': after, 'items': outcomes,
            'accepted_receipt_count': len(after['accepted_receipts']),
            'missing_capabilities': list(DOWNSTREAM) + ([] if self.catalog else ['catalog']),
            'release_ready': False, 'publication_ready': False,
            'lifecycle_owned_by': 'existing_wp2_runner', 'fresh_mailbox_coverage': False}
