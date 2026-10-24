"""Executable proposed-contract oracle, NOT a production loader/host stage.

Frozen inputs are checked independently; mutations live under a new candidate
out/ root, never in fixtures. No source acquisition or canonical store access.
"""
import ast
import copy
from contextlib import closing
import hashlib
import inspect
import json
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import uuid

import re


class ValidationError(ValueError):
    pass


class ContractSchemaValidator:
    """Stdlib test oracle for the schema's deliberately small keyword subset.

    Not a general JSON Schema implementation or a production validator. Unknown
    keywords fail rather than being ignored. No new CI dependency is introduced.
    """
    KEYWORDS = {'$schema', '$id', '$defs', '$ref', '$comment', 'title', 'type',
                'additionalProperties', 'properties', 'required', 'const', 'enum',
                'pattern', 'minLength', 'minItems', 'items', 'oneOf', 'allOf', 'if', 'then'}

    def __init__(self, schema, root=None):
        self.schema = schema
        self.root = schema if root is None else root

    @classmethod
    def check_schema(cls, schema):
        require(set(schema) <= cls.KEYWORDS, 'unsupported_schema_keyword')
        for key in ('properties', '$defs'):
            for child in schema.get(key, {}).values():
                cls.check_schema(child)
        for key in ('if', 'then', 'items'):
            if key in schema:
                cls.check_schema(schema[key])
        for key in ('oneOf', 'allOf'):
            for child in schema.get(key, []):
                cls.check_schema(child)

    def is_valid(self, value):
        try:
            self.validate(value)
            return True
        except ValidationError:
            return False

    def validate(self, value):
        s = self.schema
        def fail_unless(ok):
            if not ok:
                raise ValidationError('schema_rejected')
        def child(schema):
            return ContractSchemaValidator(schema, self.root)
        if '$ref' in s:
            fail_unless(s['$ref'].startswith('#/$defs/'))
            child(self.root['$defs'][s['$ref'].split('/')[-1]]).validate(value)
        if 'type' in s:
            types = {'object': dict, 'array': list, 'string': str, 'integer': int, 'null': type(None)}
            fail_unless(type(value) is types[s['type']])
        if 'const' in s:
            fail_unless(type(value) is type(s['const']) and value == s['const'])
        if 'enum' in s:
            fail_unless(any(type(value) is type(v) and value == v for v in s['enum']))
        if isinstance(value, dict):
            fail_unless(set(s.get('required', [])) <= set(value))
            if s.get('additionalProperties') is False:
                fail_unless(set(value) <= set(s.get('properties', {})))
            for key, schema in s.get('properties', {}).items():
                if key in value:
                    child(schema).validate(value[key])
        if isinstance(value, str):
            fail_unless(len(value) >= s.get('minLength', 0))
            if 'pattern' in s:
                fail_unless(re.search(s['pattern'], value) is not None)
        if isinstance(value, list):
            fail_unless(len(value) >= s.get('minItems', 0))
            if 'items' in s:
                for item in value:
                    child(s['items']).validate(item)
        for schema in s.get('allOf', []):
            child(schema).validate(value)
        if 'oneOf' in s:
            fail_unless(sum(child(schema).is_valid(value) for schema in s['oneOf']) == 1)
        if 'if' in s and child(s['if']).is_valid(value) and 'then' in s:
            child(s['then']).validate(value)


REPO = Path(__file__).resolve().parents[1]
FIX = REPO / 'tests/fixtures/comprehension'
TEST_OUT = REPO.parent / 'out'
SCHEMA = json.loads((REPO / 'schemas/comprehension-handoff-v1.json').read_text())
PINS = json.loads((FIX / 'source-pins.json').read_text())


def byte_hash(data):
    return hashlib.sha256(data).hexdigest()


def require(ok, reason):
    if not ok:
        raise ValueError(reason)


# Freeze the complete inspected local import closure BEFORE importing the adapter.
for _path, _hash in PINS['files'].items():
    require(byte_hash((REPO / _path).read_bytes()) == _hash, 'existing_source_changed')
with mock.patch('sqlite3.connect', side_effect=RuntimeError('import_database_forbidden')):
    from campaign_tool.records.detectors import core, pipeline
    from campaign_tool.records.ledger.migrations import v001


VALIDATOR = ContractSchemaValidator(SCHEMA)


def synthetic_output_root():
    # Validate ancestry BEFORE mkdir/mkdtemp; never redirect through a symlink.
    for ancestor in [*reversed(TEST_OUT.parents), TEST_OUT]:
        if ancestor.exists() or ancestor.is_symlink():
            require(not stat.S_ISLNK(ancestor.lstat().st_mode), 'symlink_forbidden')
    TEST_OUT.mkdir(exist_ok=True)
    return TEST_OUT


def build_runtime_fixture(root):
    """Build only in a fresh admitted synthetic out/ child, never in fixtures.

    SQL preserves the frozen database's native schema/rows, not its version-
    dependent SQLite page layout. Only the runtime copies of the producer and
    envelope are rebound to the generated database bytes; source pins stay fixed.
    """
    root = Path(root).absolute()
    require(root.parent == TEST_OUT, 'fixture_root_outside_synthetic_out')
    for ancestor in [*reversed(root.parents), root]:
        require(not stat.S_ISLNK(ancestor.lstat().st_mode), 'symlink_forbidden')
    require(not any(root.iterdir()), 'fixture_root_not_empty')
    for name, expected in PINS['fixture_files'].items():
        require(byte_hash((FIX / name).read_bytes()) == expected, 'fixture_source_changed')
    shutil.copytree(FIX, root, dirs_exist_ok=True)
    with closing(sqlite3.connect(root / 'canonical.sqlite')) as db:
        db.executescript((root / 'canonical.sql').read_text())
    envelope = json.loads((root / 'comprehension-handoff-v1.json').read_text())
    envelope['canonical_copy']['sha256'] = byte_hash((root / 'canonical.sqlite').read_bytes())
    producer = json.loads((root / envelope['source_manifest']).read_text())
    producer['canonical_copy'] = copy.deepcopy(envelope['canonical_copy'])
    producer_bytes = (json.dumps(producer, sort_keys=True) + '\n').encode()
    (root / envelope['source_manifest']).write_bytes(producer_bytes)
    envelope['source_manifest_sha256'] = byte_hash(producer_bytes)
    (root / 'comprehension-handoff-v1.json').write_text(
        json.dumps(envelope, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False) + '\n')
    return envelope


def checked_path(root, relative):
    """Oracle for immutable admitted trees; not a hostile-filesystem sandbox."""
    require(type(relative) is str, 'unsafe_relative_path')
    # Same portable grammar as the schema, with no OS-specific normalization.
    import re
    require(re.fullmatch(SCHEMA['$defs']['reference']['properties']['path']['pattern'], relative) is not None,
            'unsafe_relative_path')
    root = Path(root).absolute()
    for ancestor in [*reversed(root.parents), root]:
        require(not stat.S_ISLNK(ancestor.lstat().st_mode), 'symlink_forbidden')
    current = root
    parts = relative.split('/')
    for index, component in enumerate(parts):
        current = current / component
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError as error:
            raise ValueError('referenced_file_missing') from error
        require(not stat.S_ISLNK(mode), 'symlink_forbidden')
        require(stat.S_ISREG(mode) if index == len(parts) - 1 else stat.S_ISDIR(mode),
                'referenced_file_type_invalid')
    return current


def validate_handoff(envelope, root, *, expected_run_id, expected_source_manifest_sha256):
    """Test-only relational/byte oracle. Schema alone cannot perform these checks."""
    VALIDATOR.validate(envelope)
    require(envelope['run_id'] == expected_run_id, 'producer_run_mismatch')
    require(envelope['source_manifest_sha256'] == expected_source_manifest_sha256,
            'producer_manifest_mismatch')
    rows = envelope['artifacts']
    identities = [row['artifact_sha256'] for row in rows]
    require(len(set(identities)) == len(identities), 'duplicate_artifact_identity')
    require(identities == sorted(identities), 'artifacts_not_sorted')
    refs = [dict(path=envelope['source_manifest'], sha256=envelope['source_manifest_sha256']),
            envelope['inventory'], envelope['canonical_copy'], envelope['rules']]
    for row in rows:
        require(row['derived_receipt_sha256'] == row['derived_receipt']['sha256'],
                'derived_receipt_identity_mismatch')
        refs.extend(row[key] for key in ('derived_receipt', 'kind_receipt', 'units') if row[key] is not None)
        require(row['reason'] != 'missing_kind_receipt' or row['kind_receipt'] is None,
                'status_reference_mismatch')
        require(row['reason'] != 'missing_units' or row['units'] is None, 'status_reference_mismatch')
    # Check EVERY path before the first referenced-content read. No writes.
    resolved = [(ref, checked_path(root, ref['path'])) for ref in refs]
    paths = [ref['path'] for ref in refs]
    require(len(set(paths)) == len(paths), 'duplicate_reference_path')
    data = {}
    for ref, path in resolved:
        value = path.read_bytes()
        require(byte_hash(value) == ref['sha256'], 'referenced_content_hash_mismatch')
        data[ref['path']] = value
    # This producer manifest is a synthetic identity-adapter seam, not a declared
    # replacement for the coordinator's current manifest schema.
    producer = json.loads(data[envelope['source_manifest']])
    for key in ('run_id', 'inventory', 'canonical_copy', 'rules', 'artifacts'):
        require(producer[key] == envelope[key], 'producer_binding_mismatch')
    inventory = json.loads(data[envelope['inventory']['path']])
    sources = pipeline._sources(inventory)  # existing source-binding shape oracle
    require(set(identities) == {row['original_sha256'] for row in inventory},
            'inventory_artifact_population_mismatch')
    rules = json.loads(data[envelope['rules']['path']])
    require(rules.get('version') == envelope['rules']['version'], 'rules_version_mismatch')
    normalized_units = []
    for row in rows:
        if row['status'] != 'ready':
            continue  # no successful empty normalization for blocked evidence
        units = json.loads(data[row['units']['path']])
        require(type(units) is list, 'units_shape_invalid')
        for item in units:
            require(item['record']['original_sha256'] == row['artifact_sha256'],
                    'artifact_units_mismatch')
            normalized_units.append(item)
    # Do not deserialize CanonicalJoins from a file. Take a fresh snapshot from
    # the hash-verified synthetic, rollback-journal canonical copy only.
    canonical_path = checked_path(root, envelope['canonical_copy']['path'])
    with closing(sqlite3.connect(canonical_path.as_uri() + '?mode=ro', uri=True)) as con:
        require(con.execute('PRAGMA user_version').fetchone()[0] == 1, 'canonical_schema_mismatch')
        require(con.execute('SELECT version,checksum FROM schema_migrations').fetchall() ==
                [(1, byte_hash(v001.SQL.encode()))], 'canonical_schema_mismatch')
        joins = pipeline.canonical_joins(con, inventory)
    normalized = {'schema': pipeline.NORMALIZED_SCHEMA, 'version': 'synthetic-mapper-v1',
                  'complete': all(row['status'] == 'ready' for row in rows),
                  'source_inventory_sha256': core.digest(inventory), 'units': normalized_units}
    return {'source_bindings': list(sources.values()), 'rules': rules, 'joins': joins,
            'normalized': normalized, 'config': {}}


class ComprehensionContractTests(unittest.TestCase):
    def setUp(self):
        # Retain fresh temporary roots like the existing mutation outputs: no
        # cleanup/deletion authority is inferred by this test-only change.
        self.root = Path(tempfile.mkdtemp(prefix='contract-runtime-', dir=synthetic_output_root()))
        self.envelope = build_runtime_fixture(self.root)
        self.expected = {'expected_run_id': self.envelope['run_id'],
                         'expected_source_manifest_sha256': self.envelope['source_manifest_sha256']}

    def check(self, envelope=None):
        return validate_handoff(self.envelope if envelope is None else envelope, self.root, **self.expected)

    def mutable_copy(self):
        target = synthetic_output_root() / ('contract-' + uuid.uuid4().hex)
        target.parent.mkdir(exist_ok=True)
        shutil.copytree(self.root, target)
        self.root = target
        return target

    def rebind_synthetic_producer(self):
        """Admit a NEW synthetic producer identity for isolated semantic negatives."""
        producer = json.loads((self.root / self.envelope['source_manifest']).read_text())
        for key in ('run_id', 'inventory', 'canonical_copy', 'rules', 'artifacts'):
            producer[key] = copy.deepcopy(self.envelope[key])
        data = (json.dumps(producer, sort_keys=True) + '\n').encode()
        (self.root / self.envelope['source_manifest']).write_bytes(data)
        value = byte_hash(data)
        self.envelope['source_manifest_sha256'] = value
        self.expected['expected_source_manifest_sha256'] = value

    def mutate_json_ref(self, binding, mutate):
        path = self.root / binding['path']
        value = json.loads(path.read_text())
        mutate(value)
        data = (json.dumps(value, sort_keys=True) + '\n').encode()
        path.write_bytes(data)
        binding['sha256'] = byte_hash(data)
        self.rebind_synthetic_producer()

    def test_schema_and_independently_frozen_fixture_bytes(self):
        ContractSchemaValidator.check_schema(SCHEMA)
        for name, expected in PINS['fixture_files'].items():
            self.assertEqual(byte_hash((FIX / name).read_bytes()), expected, name)
        for path, expected in PINS['files'].items():
            self.assertEqual(byte_hash((REPO / path).read_bytes()), expected, path)
        VALIDATOR.validate(self.envelope)

    def test_runtime_database_preserves_frozen_native_schema_and_content(self):
        pins = PINS['canonical_database']
        def digest(value):
            return byte_hash(json.dumps(value, sort_keys=True, separators=(',', ':')).encode())
        path = self.root / 'canonical.sqlite'
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
            schema = db.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name').fetchall()
            tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            data = {name: db.execute('SELECT * FROM "' + name.replace('"', '""') + '" ORDER BY rowid').fetchall()
                    for name in tables}
            self.assertEqual(digest(schema), pins['schema_sha256'])
            self.assertEqual(digest(data), pins['data_sha256'])
            self.assertEqual(byte_hash(('\n'.join(db.iterdump()) + '\n').encode()), pins['dump_sha256'])
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], pins['user_version'])
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchall(), [('ok',)])
            self.assertEqual(db.execute('PRAGMA journal_mode').fetchone()[0], 'delete')
        frozen = json.loads((FIX / 'comprehension-handoff-v1.json').read_text())
        self.assertEqual(frozen['canonical_copy']['sha256'], pins['original_binary_sha256'])
        self.assertEqual(self.envelope['canonical_copy']['sha256'], byte_hash(path.read_bytes()))
        self.assertFalse((FIX / 'canonical.sqlite').exists())
        self.check()

    def test_runtime_builder_refuses_outside_root_before_any_write(self):
        for root in (FIX, REPO, TEST_OUT, self.root / 'nested', TEST_OUT / '..' / 'escape'):
            with self.subTest(root=str(root)):
                with mock.patch('sqlite3.connect') as connect, mock.patch.object(shutil, 'copytree') as copytree:
                    with mock.patch.object(Path, 'read_bytes', side_effect=AssertionError('unexpected source read')):
                        with self.assertRaisesRegex(ValueError, '^fixture_root_outside_synthetic_out$'):
                            build_runtime_fixture(root)
                    connect.assert_not_called()
                    copytree.assert_not_called()
        alias = TEST_OUT / ('contract-alias-' + uuid.uuid4().hex)
        alias.symlink_to(self.root, target_is_directory=True)
        for root, reason in ((alias, 'symlink_forbidden'), (self.root, 'fixture_root_not_empty')):
            with self.subTest(root=str(root)):
                with mock.patch('sqlite3.connect') as connect, mock.patch.object(shutil, 'copytree') as copytree:
                    with self.assertRaisesRegex(ValueError, '^' + reason + '$'):
                        build_runtime_fixture(root)
                    connect.assert_not_called()
                    copytree.assert_not_called()

    def test_public_scanner_still_rejects_database_paths_and_private_content(self):
        from campaign_tool.records.public_scan import violations
        self.assertEqual(violations('tests/fixtures/comprehension/canonical.sqlite', b''),
                         [('tests/fixtures/comprehension/canonical.sqlite', 0, 'private_path')])
        self.assertEqual(violations('tests/fixtures/comprehension/canonical.sql', (FIX / 'canonical.sql').read_bytes()), [])
        private_marker = b'-----BEGIN ' + b'PRIVATE KEY-----'
        self.assertEqual(violations('synthetic.sql', private_marker), [('synthetic.sql', 1, 'private_key')])

    def test_fresh_import_has_no_database_or_fixture_write(self):
        result = subprocess.run([sys.executable, '-B', str(FIX / 'import-isolation.py')],
                                cwd=REPO, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout, 'PASS pre-import isolation: no database or fixture write\n')
        self.assertEqual(result.stderr, '')

    def test_real_adapter_positive_and_canonical_bytes_unchanged(self):
        before = {p.name: (p.stat().st_ino, byte_hash(p.read_bytes())) for p in self.root.iterdir()}
        source_before = {p.name: (p.stat().st_ino, byte_hash(p.read_bytes())) for p in FIX.iterdir()}
        kwargs = self.check()
        subject = next(item['record']['original_sha256'] for item in kwargs['normalized']['units']
                       if item['record']['record_type'] == 'retention-setting')
        result = pipeline.evaluate_structured(subject_sha256=subject, **kwargs)
        entry = result['structured']['retention-over-policy']
        self.assertEqual((entry['status'], entry['reason'], entry['hit_count']),
                         ('evaluated', 'supplied_context_evaluated', 1))
        self.assertFalse(result['publication_ready'])
        self.assertEqual(result['stage_promotions'], 0)
        self.assertEqual(result, pipeline.evaluate_structured(subject_sha256=subject, **kwargs))
        self.assertEqual(before, {p.name: (p.stat().st_ino, byte_hash(p.read_bytes())) for p in self.root.iterdir()})
        self.assertEqual(source_before, {p.name: (p.stat().st_ino, byte_hash(p.read_bytes())) for p in FIX.iterdir()})

    def test_actual_api_signatures_not_a_second_format(self):
        sig = inspect.signature(pipeline.evaluate_structured)
        self.assertEqual(list(sig.parameters), ['subject_sha256', 'normalized', 'source_bindings', 'joins', 'rules', 'config'])
        self.assertTrue(all(p.kind == inspect.Parameter.KEYWORD_ONLY for p in sig.parameters.values()))
        self.assertEqual(str(inspect.signature(pipeline.canonical_joins)), '(connection, source_bindings)')
        self.assertEqual(list(pipeline.CanonicalJoins.__dataclass_fields__),
                         ['source_bindings_sha256', 'rows_json', 'revision_sha256'])
        self.assertFalse(hasattr(core, 'CURRENT_CORE_UNIT'))  # descriptive name, not an invented source symbol
        self.assertEqual(pipeline.NORMALIZED_SCHEMA, 'structured-normalization-v1')
        self.assertEqual(core.VERSION, 'wp6-pure-detectors-v2')

    def test_missing_inventory_canonical_rules_rejected_before_read(self):
        for key in ('inventory', 'canonical_copy', 'rules'):
            with self.subTest(key=key):
                value = copy.deepcopy(self.envelope)
                del value[key]
                with mock.patch.object(Path, 'read_bytes', side_effect=AssertionError('unexpected read')):
                    with self.assertRaises(ValidationError):
                        self.check(value)

    def test_duplicate_identity_and_unsorted_artifacts(self):
        duplicate = copy.deepcopy(self.envelope)
        duplicate['artifacts'].append(copy.deepcopy(duplicate['artifacts'][0]))
        with self.assertRaisesRegex(ValueError, '^duplicate_artifact_identity$'):
            self.check(duplicate)
        self.envelope['artifacts'].reverse()
        with self.assertRaisesRegex(ValueError, '^artifacts_not_sorted$'):
            self.check()

    def test_altered_bytes_and_inert_broken_hash_gate_control(self):
        root = self.mutable_copy()
        path = root / self.envelope['artifacts'][0]['units']['path']
        path.write_bytes(path.read_bytes() + b' ')
        with self.assertRaisesRegex(ValueError, '^referenced_content_hash_mismatch$'):
            self.check()
        # Gate-disabled oracle accepts harmless whitespace: proves this precise
        # byte-integrity control, not some unrelated malformed JSON prerequisite.
        real_hash = byte_hash
        old_hash = self.envelope['artifacts'][0]['units']['sha256']
        with mock.patch(__name__ + '.byte_hash', side_effect=lambda b: old_hash if b == path.read_bytes() else real_hash(b)):
            self.check()

    def test_unsafe_paths_rejected_by_schema(self):
        for path in ('/absolute', '../parent', 'a/../b', './x', 'a//b', 'a/', 'a\\b', 'C:/x', 'https://host/x', '%2e%2e/x', 'x\x00y', '.'):
            with self.subTest(path=path):
                value = copy.deepcopy(self.envelope)
                value['inventory']['path'] = path
                with self.assertRaises(ValidationError):
                    self.check(value)
        for path in ('inventory.json', 'sub-dir/file_v1.json', 'v1/receipt.2.json'):
            self.assertTrue(ContractSchemaValidator(SCHEMA['$defs']['reference']['properties']['path']).is_valid(path))

    def test_symlink_leaf_parent_root_and_nonregular_before_read(self):
        root = self.mutable_copy()
        leaf = root / 'linked.json'
        leaf.symlink_to(root / 'inventory.json')
        parent = root / 'linked-dir'
        parent.symlink_to(root, target_is_directory=True)
        rootlink = root.parent / (root.name + '-alias')
        rootlink.symlink_to(root, target_is_directory=True)
        for tree, relative in ((root, 'linked.json'), (root, 'linked-dir/inventory.json'), (rootlink, 'inventory.json')):
            with self.subTest(tree=str(tree), relative=relative):
                with self.assertRaisesRegex(ValueError, '^symlink_forbidden$'):
                    checked_path(tree, relative)
        with self.assertRaisesRegex(ValueError, '^referenced_file_type_invalid$'):
            checked_path(root.parent, root.name)

    def test_unknown_versions_flags_extra_fields_and_empty_run(self):
        mutations = [('schema_version', 2), ('schema_version', True), ('run_id', ''), ('run_id', '  '),
                     ('publication_ready', True), ('stage_promotions', 1), ('review_required', False),
                     ('qualified', True), ('activation_allowed', True), ('raw_document_text', 'not allowed')]
        for key, value in mutations:
            with self.subTest(key=key, value=value):
                candidate = copy.deepcopy(self.envelope)
                candidate[key] = value
                with self.assertRaises(ValidationError):
                    self.check(candidate)

    def test_external_source_and_run_pin_cannot_self_authenticate(self):
        self.envelope['source_manifest_sha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, '^producer_manifest_mismatch$'):
            self.check()
        self.envelope['source_manifest_sha256'] = self.expected['expected_source_manifest_sha256']
        self.envelope['run_id'] = 'regenerated-downstream'
        with self.assertRaisesRegex(ValueError, '^producer_run_mismatch$'):
            self.check()

    def test_unresolved_rule_source_is_existing_typed_blocker(self):
        self.mutable_copy()
        self.mutate_json_ref(self.envelope['rules'], lambda rules: rules['entries'][0].update(source_sha256='0' * 64))
        kwargs = self.check()
        subject = next(item['record']['original_sha256'] for item in kwargs['normalized']['units'] if item['record']['record_type'] == 'retention-setting')
        entry = pipeline.evaluate_structured(subject_sha256=subject, **kwargs)['structured']['retention-over-policy']
        self.assertEqual((entry['status'], entry['reason'], entry['hit_count']), ('blocked', 'rule_source_unbound', 0))

    def test_unverified_rule_and_missing_inventory_keep_existing_blockers(self):
        kwargs = self.check()
        subject = next(item['record']['original_sha256'] for item in kwargs['normalized']['units']
                       if item['record']['record_type'] == 'retention-setting')
        kwargs['rules']['entries'][0]['version_verified'] = False
        result = pipeline.evaluate_structured(subject_sha256=subject, **kwargs)
        self.assertEqual(result['structured']['retention-over-policy']['reason'], 'rule_version_or_applicability_unresolved')
        kwargs['source_bindings'] = None
        result = pipeline.evaluate_structured(subject_sha256=subject, **kwargs)
        self.assertEqual({row['status'] for row in result['structured'].values()}, {'blocked'})
        self.assertEqual({row['reason'] for row in result['structured'].values()}, {'source_bindings_required'})

    def test_blocked_and_inapplicable_routing_are_not_detector_acceptance(self):
        self.mutable_copy()
        for row in self.envelope['artifacts']:
            row.update(status='blocked', reason='missing_units', units=None)
        self.rebind_synthetic_producer()
        kwargs = self.check()
        self.assertFalse(kwargs['normalized']['complete'])
        result = pipeline.evaluate_structured(subject_sha256=self.envelope['artifacts'][0]['artifact_sha256'], **kwargs)
        self.assertEqual({r['reason'] for r in result['structured'].values()}, {'normalization_incomplete'})
        for row in self.envelope['artifacts']:
            row.update(status='inapplicable', reason='classified_non_target')
        self.rebind_synthetic_producer()
        self.assertFalse(self.check()['normalized']['complete'])
        self.envelope['artifacts'][0]['status'] = 'made-up'
        with self.assertRaises(ValidationError):
            self.check()

    def test_status_reason_and_receipt_binding_mismatch(self):
        self.envelope['artifacts'][0]['derived_receipt_sha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, '^derived_receipt_identity_mismatch$'):
            self.check()
        self.envelope['artifacts'][0]['derived_receipt_sha256'] = self.envelope['artifacts'][0]['derived_receipt']['sha256']
        self.envelope['artifacts'][0].update(status='blocked', reason='missing_units')
        with self.assertRaisesRegex(ValueError, '^status_reference_mismatch$'):
            self.check()

    def test_inventory_population_cannot_be_authored_from_output(self):
        self.mutable_copy()
        self.envelope['artifacts'].pop()
        self.rebind_synthetic_producer()
        with self.assertRaisesRegex(ValueError, '^inventory_artifact_population_mismatch$'):
            self.check()

    def test_rules_version_and_normalized_source_identity(self):
        self.mutable_copy()
        self.envelope['rules']['version'] = 'invented-rule-version'
        self.rebind_synthetic_producer()
        with self.assertRaisesRegex(ValueError, '^rules_version_mismatch$'):
            self.check()
        self.envelope['rules']['version'] = 'synthetic-wp6-rules-v1'
        self.rebind_synthetic_producer()
        row = self.envelope['artifacts'][0]
        self.mutate_json_ref(row['units'], lambda units: units[0]['source'].update(artifact_sha256='0' * 64))
        kwargs = self.check()
        result = pipeline.evaluate_structured(subject_sha256=row['artifact_sha256'], **kwargs)
        self.assertEqual({r['reason'] for r in result['structured'].values()}, {'normalized_source_missing_or_changed'})

    def test_missing_file_producer_binding_and_strict_hash_grammar(self):
        self.mutable_copy()
        value = copy.deepcopy(self.envelope)
        value['inventory']['path'] = 'not-present.json'
        with self.assertRaisesRegex(ValueError, '^referenced_file_missing$'):
            self.check(value)
        self.envelope['rules']['version'] = 'same-bytes-different-routing'
        with self.assertRaisesRegex(ValueError, '^producer_binding_mismatch$'):
            self.check()
        for value in ('A' * 64, 'a' * 63, 'a' * 65, 'a' * 64 + '\n'):
            candidate = copy.deepcopy(self.envelope)
            candidate['source_manifest_sha256'] = value
            with self.assertRaises(ValidationError):
                self.check(candidate)
        candidate = copy.deepcopy(self.envelope)
        candidate['inventory']['path'] = 'inventory.json\n'
        with self.assertRaises(ValidationError):
            self.check(candidate)

    def test_canonical_schema_mismatch_isolated_and_unchanged_on_failure(self):
        self.mutable_copy()
        path = self.root / self.envelope['canonical_copy']['path']
        with closing(sqlite3.connect(path)) as db:
            db.execute('PRAGMA user_version=2')
        self.envelope['canonical_copy']['sha256'] = byte_hash(path.read_bytes())
        self.rebind_synthetic_producer()
        before = {p.name: (p.stat().st_ino, byte_hash(p.read_bytes())) for p in self.root.iterdir()}
        with self.assertRaisesRegex(ValueError, '^canonical_schema_mismatch$'):
            self.check()
        self.assertEqual(before, {p.name: (p.stat().st_ino, byte_hash(p.read_bytes())) for p in self.root.iterdir()})

    def test_canonical_serialization_is_byte_stable(self):
        expected = json.dumps(self.envelope, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False) + '\n'
        self.assertEqual(expected.encode(), (self.root / 'comprehension-handoff-v1.json').read_bytes())
        reordered = dict(reversed(list(self.envelope.items())))
        self.assertEqual(expected, json.dumps(reordered, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False) + '\n')
        frozen = json.loads((FIX / 'comprehension-handoff-v1.json').read_text())
        self.assertEqual((json.dumps(frozen, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False) + '\n').encode(),
                         (FIX / 'comprehension-handoff-v1.json').read_bytes())

    def test_host_mapping_is_static_and_unchanged(self):
        source = REPO / 'campaign_tool/records/run_stages.py'
        tree = ast.parse(source.read_text())
        stages = next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'STAGES' for t in n.targets))
        self.assertEqual(stages, ('sync', 'delta', 'adoption', 'portal', 'extract', 'digest_gates', 'outputs'))
        functions = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
        self.assertIn('stage_outputs', functions)
        self.assertIn('stage_digest_gates', functions)
        self.assertNotIn('stage_comprehension', functions)
        self.assertEqual([a.arg for a in functions['stage_outputs'].args.args], ['args'])


if __name__ == '__main__':
    unittest.main()
