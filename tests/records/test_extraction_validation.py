"""Synthetic trust tests. No production corpus acceptance/count claims."""
import importlib
import importlib.metadata
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from campaign_tool.records import extraction_validation as validation
from campaign_tool.records import extraction_ledger as enrollment
from campaign_tool.records import extraction_routes as routes
from tests.records import test_extraction_ledger as fixture_module


class ExtractionValidationTests(unittest.TestCase):
    setUpClass = classmethod(fixture_module.ExtractionLedgerTests.setUpClass.__func__)
    write = fixture_module.ExtractionLedgerTests.write
    bundle = fixture_module.ExtractionLedgerTests.bundle
    enroll = fixture_module.ExtractionLedgerTests.enroll
    rewrite = fixture_module.ExtractionLedgerTests.rewrite
    ocr_bundle = fixture_module.ExtractionLedgerTests.ocr_bundle

    def setUp(self):
        fixture_module.ExtractionLedgerTests.setUp(self)
        self.addCleanup(self.temp.cleanup)
        self.stages = importlib.import_module('campaign_tool.records.ledger.stages')
        self.addCleanup(patch.stopall)
        patch.dict(self.stages._INSTALLED_VALIDATORS, {}, clear=True).start()
        patch.dict(self.stages._INSTALLED_PROFILES, {}, clear=True).start()
        self.config = enrollment.sha(b'synthetic trust configuration')

    def actual(self, pdf=False):
        if pdf:
            from pypdf import PdfWriter
            from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
            import io
            writer = PdfWriter()
            for text in ('Synthetic page one', 'Synthetic page two'):
                page = writer.add_blank_page(width=200, height=200)
                font = DictionaryObject({NameObject('/Type'): NameObject('/Font'),
                    NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
                page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'):
                    DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
                stream = DecodedStreamObject()
                stream.set_data(('BT /F1 12 Tf 10 100 Td (' + text + ') Tj ET').encode())
                page[NameObject('/Contents')] = writer._add_object(stream)
            output = io.BytesIO(); writer.write(output)
            self.source = self.root / 'original.pdf'
            self.write(self.source, output.getvalue())
            self.subject = enrollment.sha(output.getvalue())
            with self.store.ledger(self.database) as con:
                con.execute('INSERT INTO originals VALUES(?,?,?,?,?,?,?,?,?,?)',
                    (self.subject, len(output.getvalue()), 'application/pdf', None, 'original', 'unresolved',
                     str(self.source), 'receipt_validation_pending', None, '{}'))
                con.execute('INSERT INTO occurrences VALUES(?,?,?,?,?,?,?,?)',
                    ('pdf-source', self.subject, 'local', 'synthetic-pdf', None, None, 'fixture', '{}'))
                for stage in self.store.STAGES:
                    con.execute('INSERT INTO stage_state VALUES(?,?,?,?,?,?,?,?)',
                        (self.subject, stage, 'pending', None, 'fixture', self.store.now(), 'intake', None))
                con.commit()
        out = self.root / 'actual'; out.mkdir(mode=0o700)
        result = routes.extract(self.source, self.subject, out, 'pdf' if pdf else 'txt', timeout=20)
        self.path = Path(result['run_path']) / 'extraction.json'
        if pdf:
            # Genuine installed distribution metadata only. A module __version__
            # string does not stand in for missing distribution provenance.
            try:
                distribution = importlib.metadata.distribution('pypdf')
            except importlib.metadata.PackageNotFoundError:
                self.assertIn(result.get('parser_version'), ('unknown', 'unavailable', 'unverified', None))
                with self.assertRaisesRegex(enrollment.ExtractionBindingError, 'parser_provenance_unavailable'):
                    self.enroll(self.path)
                self.skipTest('PDF-success gap: genuine installed pypdf distribution metadata unavailable; fail-closed enrollment verified; no metadata fabricated')
            self.assertTrue(distribution.read_text('METADATA'))
            self.assertEqual(distribution.metadata['Name'].lower(), 'pypdf')
            self.assertEqual(distribution.version, result['parser_version'])
        self.enrolled = self.enroll(self.path)
        return self.enrolled

    def install(self):
        adapter_id = validation.install_extraction_validator(database=self.database,
            evidence_root=self.adapter.evidence_root, original_root=self.root)
        self.assertEqual(adapter_id, self.expected_id())
        self.adapter_id = adapter_id
        self.validator = self.stages._INSTALLED_VALIDATORS[adapter_id]
        return self.validator

    def expected_id(self):
        binding = validation._InstalledExtractionValidator(enrollment.checked_path(self.database),
            enrollment.private_dir(self.adapter.evidence_root), enrollment.private_dir(self.root))
        self.assertTrue(binding.adapter_id.startswith(validation.ADAPTER_ID + ':'))
        return binding.adapter_id

    def run_row(self, name='synthetic-run'):
        with self.store.ledger(self.database) as con:
            con.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?)',
                (name, 'stage-runner', self.store.now(), None, 'synthetic-v1', None,
                 self.config, None, 'running', '{}'))
            con.commit()
        return name

    def runner(self):
        self.run_row()
        def preserve(context):
            original = context['original']
            raw = enrollment.read_file(Path(original['storage_path']), validation.MAX_ACCEPT_SOURCE)
            return (context['stage'] == 'preserve' and raw == context['content'] and
                    enrollment.sha(raw) == original['sha256'] and len(raw) == original['bytes'])
        self.test_runner = self.stages.testing_runner(self.database, run_id='synthetic-run',
            owner='fixture', engine_version='synthetic-v1', config_sha256=self.config,
            validators={'preserve': preserve, 'extract': self.validator}, version='extraction-trust-fixture')
        raw = self.source.read_bytes()
        self.test_runner.set_content(self.subject, 'preserve', raw, author_id='fixture', tier='A')
        packet = self.packet('preserve', raw, {'denominator': {'kind': 'bytes', 'total': len(raw)},
            'covered': len(raw), 'scope': 'full_text'})
        claim = self.test_runner.claim(self.subject, 'preserve')
        self.test_runner.promote(self.subject, 'preserve', validation.encoded(packet), claim_id=claim['claim_id'])
        return self.test_runner

    def packet(self, stage, content, coverage):
        inputs = {'original': self.subject}
        if stage == 'extract':
            with self.store.ledger(self.database, readonly=True) as con:
                inputs['preserve'] = con.execute("SELECT receipt_sha256 FROM stage_state WHERE original_sha256=? AND stage='preserve'",
                    (self.subject,)).fetchone()[0]
        return {'schema': 'ledger-stage-receipt-v1', 'subject_sha256': self.subject, 'stage': stage,
            'content_sha256': enrollment.sha(content), 'author_id': 'fixture', 'tier': 'A',
            'reviewer_id': 'fixture', 'role': 'extractor' if stage == 'extract' else 'preserver',
            'verdict': 'pass', 'coverage': coverage,
            'locators': ['extraction-import:' + self.enrolled['import_id']] if stage == 'extract' else ['synthetic original'],
            'rationale': 'synthetic functional extraction only', 'model_or_tool': validation.ADAPTER_ID,
            'created_at_tz': self.store.now(), 'input_hashes': inputs}

    def prepared(self, pdf=False):
        self.actual(pdf=pdf); self.install(); self.runner()
        content, original, coverage = self.validator.evidence(self.enrolled['import_id'])
        self.test_runner.set_content(self.subject, 'extract', content, author_id='fixture', tier='A')
        self.claim = self.test_runner.claim(self.subject, 'extract')['claim_id']
        self.content = content
        self.original = original
        self.receipt = self.packet('extract', content, coverage)

    def context(self, receipt=None, content=None):
        return {'stage': 'extract', 'subject_sha256': self.subject, 'original': self.original,
            'content': content if content is not None else self.content,
            'receipt': receipt if receipt is not None else self.receipt}

    def test_complete_text_passes_test_only_extract_no_review_promotion(self):
        self.prepared()
        raw = validation.encoded(self.receipt)
        result = self.test_runner.promote(self.subject, 'extract', raw, claim_id=self.claim)
        self.assertEqual(result['status'], 'done')
        replay = self.test_runner.promote(self.subject, 'extract', raw)
        self.assertTrue(replay['reused'])
        counts = self.stages.counts(self.database)
        self.assertEqual(counts['end_to_end_complete'], 0)
        self.assertEqual(counts['verified_seven_stage_complete'], 0)
        self.assertEqual(counts['stages']['review']['pending'], 1)
        with self.store.ledger(self.database, readonly=True) as con:
            self.assertTrue(all(row[0] == 1 for row in con.execute('SELECT test_only FROM stage_validation_authority')))

    def test_complete_pdf_passes_functional_extract_preserves_visual_hold(self):
        self.prepared(pdf=True)
        result = self.test_runner.promote(self.subject, 'extract', validation.encoded(self.receipt), claim_id=self.claim)
        self.assertEqual(result['status'], 'done')
        self.assertEqual(self.receipt['coverage']['denominator'], {'kind': 'pages', 'total': 2})
        with self.store.ledger(self.database, readonly=True) as con:
            pages = con.execute('SELECT needs_visual_review FROM page_state WHERE original_sha256=?', (self.subject,)).fetchall()
            self.assertEqual([r[0] for r in pages], [1, 1])
        self.assertEqual(self.stages.counts(self.database)['verified_seven_stage_complete'], 0)

    def test_enrollment_remains_pending_without_installed_opt_in(self):
        self.actual()
        self.assertEqual(self.enrolled['stage_promotions'], 0)
        self.assertEqual(self.store.counts(self.database)['stages']['extract']['pending'], 1)
        self.assertFalse(any(key.startswith(validation.ADAPTER_ID) for key in self.stages._INSTALLED_VALIDATORS))

    def test_installed_profile_has_fixed_binding_and_explicit_identity(self):
        self.actual(); self.install(); self.run_row()
        self.stages.configure_installed_profile('fixture-profile', engine_version='synthetic-v1',
            config_sha256=self.config, validators={'extract': self.adapter_id})
        adapter = validation.ExtractionStageAdapter(database=self.database, evidence_root=self.adapter.evidence_root,
            original_root=self.root, run_id='synthetic-run', owner='fixture', profile_id='fixture-profile')
        self.assertEqual(adapter.runner.validators['extract'][0], self.adapter_id)
        self.assertEqual(adapter.runner.run_id, 'synthetic-run')
        self.assertEqual(adapter.runner.owner, 'fixture')
        with self.assertRaises(self.stages.StageError):
            self.stages.configure_installed_profile('callback-profile', engine_version='synthetic-v1',
                config_sha256=self.config, validators={'extract': lambda c: True})

    def test_installed_profile_cannot_promote_synthetic_prerequisite(self):
        # No extract claim by the synthetic run: isolate prerequisite authority,
        # not refusal caused by another run's competing work lease.
        self.actual(); self.install(); self.runner()
        self.run_row('installed-contract-run')
        self.stages.configure_installed_profile('installed-contract', engine_version='synthetic-v1',
            config_sha256=self.config, validators={'extract': self.adapter_id})
        adapter = validation.ExtractionStageAdapter(database=self.database, evidence_root=self.adapter.evidence_root,
            original_root=self.root, run_id='installed-contract-run', owner='fixture', profile_id='installed-contract')
        with self.assertRaisesRegex(self.stages.StageError, 'synthetic'):
            adapter.accept(self.enrolled['import_id'])
        self.assertEqual(self.stages.counts(self.database)['stages']['extract']['done'], 0)
        with self.store.ledger(self.database, readonly=True) as con:
            self.assertEqual(con.execute(
                "SELECT count(*) FROM stage_validation_authority WHERE test_only=0"
            ).fetchone()[0], 0)

    def test_registration_cannot_replace_other_installed_callback(self):
        self.stages._INSTALLED_VALIDATORS[self.expected_id()] = lambda context: True
        with self.assertRaisesRegex(enrollment.ExtractionBindingError, 'binding_conflict'):
            self.install()

    def test_registration_reuse_and_root_change_gets_distinct_identity(self):
        first = self.install(); self.assertEqual(first, self.install())
        other = self.root / 'other'; other.mkdir(mode=0o700)
        # A different root is a different installed binding, never a silent replacement.
        second = validation.install_extraction_validator(database=self.database,
            evidence_root=other, original_root=self.root)
        self.assertNotEqual(second, self.adapter_id)
        self.assertEqual(self.stages._INSTALLED_VALIDATORS[self.adapter_id], first)
        with self.assertRaisesRegex(enrollment.ExtractionBindingError, 'binding_conflict'):
            self.stages._INSTALLED_VALIDATORS[second] = lambda context: True
            validation.install_extraction_validator(database=self.database, evidence_root=other, original_root=self.root)

    def test_caller_paths_and_callbacks_not_in_api(self):
        with self.assertRaises(TypeError):
            validation.install_extraction_validator(database=self.database, evidence_root=self.adapter.evidence_root,
                original_root=self.root, validator=lambda context: True)
        self.prepared()
        payload = json.loads(self.content); payload['receipt_path'] = '/synthetic/untrusted'
        with self.assertRaises(enrollment.ExtractionBindingError):
            self.validator(self.context(content=validation.encoded(payload)))

    def test_oversize_and_duplicate_key_envelopes_rejected(self):
        self.prepared()
        for raw in (b' ' * (validation.MAX_CONTENT + 1), b'{"schema":1,"schema":2}'):
            with self.subTest(size=len(raw)), self.assertRaises(enrollment.ExtractionBindingError):
                self.validator(self.context(content=raw))

    def test_wrong_coverage_scope_and_denominator_rejected(self):
        self.prepared()
        for changed in ({'scope': 'full_visual'}, {'covered': 1}, {'denominator': {'kind': 'items', 'total': 1}}):
            packet = json.loads(json.dumps(self.receipt)); packet['coverage'].update(changed)
            with self.subTest(changed=changed), self.assertRaises(enrollment.ExtractionBindingError):
                self.validator(self.context(receipt=packet))

    def test_receipt_validator_and_profile_override_rejected(self):
        self.prepared()
        for field in ('validator', 'profile_id', 'run_id', 'owner'):
            packet = dict(self.receipt); packet[field] = 'caller-choice'
            with self.subTest(field=field), self.assertRaises(self.stages.StageError):
                self.test_runner.promote(self.subject, 'extract', validation.encoded(packet), claim_id=self.claim)

    def test_original_tampering_blocks_acceptance(self):
        self.prepared(); self.source.chmod(0o600); self.source.write_bytes(b'tampered')
        with self.assertRaises(enrollment.ExtractionBindingError):
            self.validator(self.context())

    def test_cas_tampering_blocks_acceptance(self):
        self.prepared()
        target = self.adapter.evidence_root / self.enrolled['manifest_sha256']
        target.chmod(0o600); target.write_bytes(b'{}')
        with self.assertRaises(enrollment.ExtractionBindingError): self.validator(self.context())

    def test_canonical_unit_tamper_blocks_acceptance(self):
        self.prepared()
        with self.store.ledger(self.database) as con:
            con.execute("UPDATE units SET parser_version='tampered'"); con.commit()
        with self.assertRaises(enrollment.ExtractionBindingError): self.validator(self.context())

    def test_missing_canonical_unit_blocks_acceptance(self):
        self.prepared()
        with self.store.ledger(self.database) as con:
            con.execute('DELETE FROM units WHERE legacy_ordinal=2'); con.commit()
        with self.assertRaises(enrollment.ExtractionBindingError): self.validator(self.context())

    def test_missing_pages_partial_and_unknown_parser_fail_closed(self):
        self.enroll(self.bundle(pdf=True)); self.install()
        with self.store.ledger(self.database, readonly=True) as con:
            import_id = con.execute('SELECT id FROM extraction_adapter_imports').fetchone()[0]
        with self.assertRaisesRegex(enrollment.ExtractionBindingError, 'extraction_incomplete'):
            self.validator.evidence(import_id)

    def test_per_page_ocr_import_is_accepted_with_visual_hold_recorded(self):
        source, receipt, result, _ = self.ocr_bundle()
        enrolled = self.adapter.enroll(original_path=source, receipt_path=receipt,
                                       receipt_sha256=enrollment.sha(receipt.read_bytes()))
        self.assertIn('ocr_visual_review_required', enrolled['gaps'])
        self.install()
        raw, _, coverage = self.validator.evidence(enrolled['import_id'])
        envelope = json.loads(raw)
        self.assertEqual(envelope['ocr']['pages'], [2])
        self.assertTrue(envelope['ocr']['visual_review_pending'])
        self.assertEqual(coverage['denominator'], {'kind': 'pages', 'total': 2})
        # Evidence alone never promotes; the hold stays on page_state for the reviewer.
        with self.store.ledger(self.database, readonly=True) as con:
            row = con.execute("SELECT status FROM stage_state WHERE original_sha256=? AND stage='extract'",
                              (result['original_sha256'],)).fetchone()
            holds = con.execute("SELECT page_no,needs_visual_review FROM page_state WHERE original_sha256=? ORDER BY page_no",
                                (result['original_sha256'],)).fetchall()
        self.assertEqual(row[0], 'pending')
        self.assertEqual([tuple(h) for h in holds], [(1, 1), (2, 1)])

    def test_ocr_receipt_page_marker_refuses_acceptance_hold(self):
        self.prepared()
        receipt = json.loads(self.path.read_text())
        self.assertFalse(validation.ocr_evidence_present(receipt))
        page_marked = json.loads(json.dumps(receipt)); page_marked['pages'] = [{'page_no': 1, 'ocr_receipt_id': 'a' * 64}]
        self.assertTrue(validation.ocr_evidence_present(page_marked))
        self.assertTrue(validation.ocr_evidence_present(dict(receipt, ocr_receipts=[{'page': 1}])))
        self.assertTrue(validation.ocr_evidence_present(dict(receipt, ocr_derivative_sha256='b' * 64)))

    def test_unknown_complete_parser_fails_closed(self):
        result = self.enroll(self.bundle()); self.install()
        with self.assertRaises(enrollment.ExtractionBindingError): self.validator.evidence(result['import_id'])

    def test_parser_reported_count_mismatch_fails_closed(self):
        self.actual()
        # Create a separately hashed, internally bound candidate with a false denominator.
        self.rewrite(self.path.parent / 'derived' / 'digest.json', lambda d: d['counts'].update(units=99))
        self.rewrite(self.path, lambda d: d.update(synthetic_new_attempt=True))
        new = self.enroll(self.path); self.install()
        with self.assertRaisesRegex(enrollment.ExtractionBindingError, 'parser_unit_count_mismatch'):
            self.validator.evidence(new['import_id'])

    def test_verified_rows_cannot_omit_original_line(self):
        self.actual()
        receipt = json.loads(self.path.read_text()); receipt['units'] = receipt['units'][:1]
        raw_units = (json.dumps(receipt['units'][0], sort_keys=True) + '\n').encode()
        self.write(self.path.parent / 'derived' / 'units.jsonl', raw_units)
        metadata = json.loads((self.path.parent / 'derived' / 'digest.json').read_text())
        metadata['counts']['units'] = 1; metadata['counts']['text_line'] = 1
        metadata['artifacts']['units.jsonl'] = enrollment.sha(raw_units)
        self.write(self.path.parent / 'derived' / 'digest.json', json.dumps(metadata).encode())
        self.write(self.path, json.dumps(receipt).encode())
        new = self.enroll(self.path); self.install()
        with self.assertRaisesRegex(enrollment.ExtractionBindingError, 'source_text_denominator_or_content'):
            self.validator.evidence(new['import_id'])

    def test_historical_candidate_cannot_be_accepted(self):
        self.actual(); original_id = self.enrolled['import_id']
        self.rewrite(self.path, lambda d: d.update(synthetic_new_attempt=True))
        self.enroll(self.path); self.install()
        with self.assertRaisesRegex(enrollment.ExtractionBindingError, 'not_current_extraction'):
            self.validator.evidence(original_id)

    def test_enrollment_after_controller_install_does_not_write_stage_authority(self):
        self.actual(); self.install(); self.runner()
        # The controller now protects state writes. Another enrollment stays pending.
        self.rewrite(self.path, lambda d: d.update(synthetic_new_attempt=True))
        new = self.enroll(self.path)
        self.assertEqual(new['stage_promotions'], 0)
        self.assertEqual(self.stages.counts(self.database)['stages']['extract']['pending'], 1)

    def test_extra_canonical_current_import_unit_rejected(self):
        self.prepared()
        with self.store.ledger(self.database) as con:
            row = list(con.execute("SELECT * FROM units LIMIT 1").fetchone())
            row[0] = enrollment.sha(b"synthetic additional current-import unit")
            con.execute("INSERT INTO units VALUES(" + ",".join("?" for _ in row) + ")", row)
            con.commit()
        with self.assertRaisesRegex(enrollment.ExtractionBindingError, "canonical_unit_set_mismatch"):
            self.validator(self.context())
        self.assertEqual(self.stages.counts(self.database)["stages"]["extract"]["done"], 0)

    def test_forged_installed_parser_components_rejected(self):
        self.actual()
        components = {"intake": "synthetic-forged-component"}
        self.rewrite(self.path, lambda d: d.update(parser_components=components))
        self.rewrite(self.path.parent / "derived" / "digest.json", lambda d: d.update(parser_components=components))
        self.enrolled = self.enroll(self.path); self.install()
        with self.assertRaisesRegex(enrollment.ExtractionBindingError, "installed_parser_components_mismatch"):
            self.validator.evidence(self.enrolled["import_id"])

    def test_forged_declared_runtime_hash_rejected(self):
        self.actual()
        forged = enrollment.sha(b"synthetic forged runtime")
        self.rewrite(self.path, lambda d: d.update(parser_runtime_sha256=forged))
        self.rewrite(self.path.parent / "derived" / "digest.json", lambda d: d.update(parser_runtime_sha256=forged))
        self.enrolled = self.enroll(self.path); self.install()
        with self.assertRaisesRegex(enrollment.ExtractionBindingError, "installed_parser_runtime_mismatch"):
            self.validator.evidence(self.enrolled["import_id"])

    def test_runtime_change_invalidates_exact_content(self):
        self.prepared()
        components, _ = validation.installed_parser_identity("txt")
        with patch.object(validation, "installed_parser_identity",
                          return_value=(components, enrollment.sha(b"changed synthetic runtime"))):
            with self.assertRaisesRegex(enrollment.ExtractionBindingError, "canonical_content_binding"):
                self.validator(self.context())

    def test_competing_owner_rejected_before_content_or_lease_mutation(self):
        self.prepared()
        self.run_row("competing-installed-run")
        self.stages.configure_installed_profile("competing-installed", engine_version="synthetic-v1",
            config_sha256=self.config, validators={"extract": self.adapter_id})
        adapter = validation.ExtractionStageAdapter(database=self.database, evidence_root=self.adapter.evidence_root,
            original_root=self.root, run_id="competing-installed-run", owner="different-owner", profile_id="competing-installed")
        with self.store.ledger(self.database, readonly=True) as con:
            before = {table: [tuple(row) for row in con.execute("SELECT * FROM " + table)]
                      for table in ("work_leases", "stage_content", "stage_state", "stage_claims", "stage_artifacts")}
        with self.assertRaisesRegex(self.stages.StageError, "competing_live_lease"):
            adapter.accept(self.enrolled["import_id"])
        with self.store.ledger(self.database, readonly=True) as con:
            after = {table: [tuple(row) for row in con.execute("SELECT * FROM " + table)] for table in before}
        self.assertEqual(after, before)

    def test_same_owner_foreign_run_rejected_before_mutation(self):
        self.prepared(); self.run_row("foreign-run")
        self.stages.configure_installed_profile("foreign-profile", engine_version="synthetic-v1",
            config_sha256=self.config, validators={"extract": self.adapter_id})
        adapter = validation.ExtractionStageAdapter(database=self.database, evidence_root=self.adapter.evidence_root,
            original_root=self.root, run_id="foreign-run", owner="fixture", profile_id="foreign-profile")
        with self.store.ledger(self.database, readonly=True) as con:
            before = tuple(con.execute("SELECT * FROM work_leases WHERE stage=?", ("extract",)).fetchone())
        with self.assertRaisesRegex(self.stages.StageError, "competing_live_lease"):
            adapter.accept(self.enrolled["import_id"])
        with self.store.ledger(self.database, readonly=True) as con:
            self.assertEqual(tuple(con.execute("SELECT * FROM work_leases WHERE stage=?", ("extract",)).fetchone()), before)

    def test_guarded_registration_without_preserve_does_not_mutate(self):
        self.actual(); self.install(); self.run_row("unpreserved-run")
        self.stages.configure_installed_profile("unpreserved-profile", engine_version="synthetic-v1",
            config_sha256=self.config, validators={"extract": self.adapter_id})
        adapter = validation.ExtractionStageAdapter(database=self.database, evidence_root=self.adapter.evidence_root,
            original_root=self.root, run_id="unpreserved-run", owner="fixture", profile_id="unpreserved-profile")
        with self.assertRaisesRegex(enrollment.ExtractionBindingError, "preservation_acceptance_required"):
            adapter.accept(self.enrolled["import_id"])
        with self.store.ledger(self.database, readonly=True) as con:
            self.assertEqual(con.execute("SELECT count(*) FROM stage_content WHERE stage=?", ("extract",)).fetchone()[0], 0)
            self.assertEqual(con.execute("SELECT count(*) FROM work_leases").fetchone()[0], 0)

    def test_historical_import_rows_do_not_inflate_current_unit_set(self):
        self.actual()
        self.rewrite(self.path, lambda d: d.update(synthetic_new_attempt=True))
        current = self.enroll(self.path); self.install()
        content, _, coverage = self.validator.evidence(current["import_id"])
        self.assertEqual(coverage["covered"], 2)
        self.assertEqual(json.loads(content)["unit_count"], 2)

