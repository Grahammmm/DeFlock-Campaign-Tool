"""Disjoint immutable receipt capture schema; no canonical stage writes."""
VERSION = 'receipt-evidence-2'
SQL = r'''
CREATE TABLE rp_schema(version TEXT PRIMARY KEY NOT NULL, checksum TEXT NOT NULL);
CREATE TABLE rp_manifests(id TEXT PRIMARY KEY NOT NULL, root TEXT NOT NULL, body BLOB NOT NULL,
 CHECK(id=rp_sha(body)));
CREATE TABLE rp_sources(id TEXT PRIMARY KEY NOT NULL, manifest_id TEXT NOT NULL REFERENCES rp_manifests(id),
 ordinal INTEGER NOT NULL, path TEXT NOT NULL, mapping TEXT NOT NULL, expected_sha TEXT NOT NULL,
 observed_sha TEXT, size INTEGER, body BLOB, error TEXT,
 UNIQUE(manifest_id,ordinal),
 CHECK((body IS NULL AND error IS NOT NULL) OR
       (body IS NOT NULL AND observed_sha IS NOT NULL AND size IS NOT NULL AND observed_sha=rp_sha(body) AND size=length(body))));
CREATE TABLE rp_artifacts(sha TEXT PRIMARY KEY NOT NULL, size INTEGER NOT NULL, body BLOB NOT NULL,
 CHECK(sha=rp_sha(body) AND size=length(body)));
CREATE TABLE rp_items(id TEXT PRIMARY KEY NOT NULL, manifest_id TEXT NOT NULL REFERENCES rp_manifests(id),
 source_id TEXT NOT NULL REFERENCES rp_sources(id), line INTEGER NOT NULL CHECK(line>0),
 raw BLOB NOT NULL, plan TEXT NOT NULL, artifact_sha TEXT REFERENCES rp_artifacts(sha),
 capture_error TEXT, original_sha TEXT REFERENCES originals(sha256),
 binding_verified INTEGER NOT NULL CHECK(binding_verified IN (0,1)), evidence_sha TEXT NOT NULL,
 UNIQUE(source_id,line), UNIQUE(id,evidence_sha));
CREATE TABLE rp_gaps(id TEXT PRIMARY KEY NOT NULL, source_id TEXT NOT NULL REFERENCES rp_sources(id),
 item_id TEXT REFERENCES rp_items(id), reason TEXT NOT NULL, owner TEXT NOT NULL DEFAULT 'caller');
CREATE TABLE rp_steps(manifest_id TEXT NOT NULL REFERENCES rp_manifests(id), sequence INTEGER NOT NULL,
 item_id TEXT NOT NULL UNIQUE, evidence_sha TEXT NOT NULL,
 PRIMARY KEY(manifest_id,sequence),
 FOREIGN KEY(item_id,evidence_sha) REFERENCES rp_items(id,evidence_sha));
CREATE TRIGGER rp_source_consistency BEFORE INSERT ON rp_sources BEGIN
 SELECT CASE WHEN rp_source_valid(NEW.id,NEW.manifest_id,NEW.ordinal,NEW.path,NEW.mapping,NEW.expected_sha,
 (SELECT body FROM rp_manifests WHERE id=NEW.manifest_id))!=1
 OR (NEW.body IS NOT NULL AND NEW.observed_sha!=NEW.expected_sha AND NEW.error IS NOT 'index_hash_mismatch')
 OR (NEW.body IS NOT NULL AND NEW.observed_sha=NEW.expected_sha AND NEW.error IS NOT NULL AND NEW.error!='changing_input')
 OR (NEW.body IS NULL AND NEW.error NOT IN ('missing_file','path_escape','unsafe_or_unreadable_path','not_regular_file','oversize','changing_input'))
 THEN RAISE(ABORT,'source mapping mismatch') END;
END;
CREATE TRIGGER rp_item_consistency BEFORE INSERT ON rp_items BEGIN
 SELECT CASE WHEN NEW.id!=rp_item_id(NEW.source_id,NEW.line)
 OR NEW.manifest_id!=(SELECT manifest_id FROM rp_sources WHERE id=NEW.source_id)
 OR rp_line(NEW.source_id,NEW.line) IS NULL
 OR NEW.raw IS NOT rp_line(NEW.source_id,NEW.line)
 OR NEW.plan!=rp_plan(NEW.raw,(SELECT mapping FROM rp_sources WHERE id=NEW.source_id))
 OR NEW.evidence_sha!=rp_evidence(NEW.id,NEW.manifest_id,NEW.source_id,NEW.line,NEW.raw,NEW.plan,
 NEW.artifact_sha,NEW.capture_error,NEW.original_sha,NEW.binding_verified)
 OR NEW.original_sha IS NOT (SELECT sha256 FROM originals WHERE sha256=json_extract(NEW.plan,'$.original_hash'))
 OR (NEW.artifact_sha IS NULL AND NEW.capture_error IS NULL AND json_extract(NEW.plan,'$.artifact_path') IS NOT NULL)
 OR (NEW.capture_error IS NOT NULL AND NEW.capture_error NOT IN ('path_escape','missing_file','unsafe_or_unreadable_path','not_regular_file','oversize','changing_input'))
 OR NEW.binding_verified!=rp_bound(NEW.plan,NEW.artifact_sha,NEW.capture_error,NEW.original_sha)
 THEN RAISE(ABORT,'item evidence mismatch') END;
END;
CREATE TRIGGER rp_gap_consistency BEFORE INSERT ON rp_gaps BEGIN
 SELECT CASE WHEN NEW.id!=rp_gap_id(NEW.source_id,NEW.item_id,NEW.reason)
 OR (NEW.item_id IS NULL AND NEW.reason IS NOT (SELECT error FROM rp_sources WHERE id=NEW.source_id))
 OR (NEW.item_id IS NOT NULL AND (NEW.source_id!=(SELECT source_id FROM rp_items WHERE id=NEW.item_id)
 OR NOT EXISTS(SELECT 1 FROM json_each(rp_reasons(
 (SELECT plan FROM rp_items WHERE id=NEW.item_id),(SELECT artifact_sha FROM rp_items WHERE id=NEW.item_id),
 (SELECT capture_error FROM rp_items WHERE id=NEW.item_id),(SELECT original_sha FROM rp_items WHERE id=NEW.item_id))) WHERE value=NEW.reason)))
 THEN RAISE(ABORT,'gap evidence mismatch') END;
END;
CREATE TRIGGER rp_step_consistency BEFORE INSERT ON rp_steps BEGIN
 SELECT CASE WHEN NEW.sequence!=1+coalesce((SELECT max(sequence) FROM rp_steps WHERE manifest_id=NEW.manifest_id),0)
 OR NEW.manifest_id!=(SELECT manifest_id FROM rp_items WHERE id=NEW.item_id)
 THEN RAISE(ABORT,'checkpoint identity mismatch') END;
END;
'''
KEYS = {'rp_schema':('version',), 'rp_manifests':('id',), 'rp_sources':('id',),
        'rp_artifacts':('sha',), 'rp_items':('id',), 'rp_gaps':('id',),
        'rp_steps':('manifest_id','sequence')}
for table, keys in KEYS.items():
    SQL += f"CREATE TRIGGER {table}_no_replace BEFORE INSERT ON {table} WHEN EXISTS(SELECT 1 FROM {table} WHERE "
    SQL += ' AND '.join(f'{key}=NEW.{key}' for key in keys)
    SQL += ") BEGIN SELECT RAISE(ABORT,'append-only replacement'); END;\n"
    for op in ('UPDATE','DELETE'):
        # Unconditional UPDATE protects OLD and NEW keys, including collision REPLACE.
        SQL += f"CREATE TRIGGER {table}_no_{op.lower()} BEFORE {op} ON {table} BEGIN SELECT RAISE(ABORT,'append-only evidence'); END;\n"
