"""Private runner-control sidecar, NOT the WP1 canonical ledger."""
VERSION=1
SQL='''
CREATE TABLE runner_schema(version INTEGER PRIMARY KEY, checksum TEXT NOT NULL);
CREATE TABLE runner_runs(id TEXT PRIMARY KEY NOT NULL, started REAL NOT NULL, ended REAL,
 status TEXT NOT NULL, identity TEXT NOT NULL, summary TEXT NOT NULL);
CREATE TABLE runner_folders(account TEXT NOT NULL, folder TEXT NOT NULL, uidvalidity INTEGER NOT NULL,
 highest_uid INTEGER NOT NULL DEFAULT 0, run_id TEXT NOT NULL REFERENCES runner_runs(id),
 PRIMARY KEY(account,folder,uidvalidity));
CREATE TABLE runner_messages(id TEXT PRIMARY KEY NOT NULL, account TEXT NOT NULL, folder TEXT NOT NULL,
 uidvalidity INTEGER NOT NULL, uid INTEGER NOT NULL, eml_sha TEXT NOT NULL, receipt_sha TEXT NOT NULL,
 evidence TEXT NOT NULL, run_id TEXT NOT NULL REFERENCES runner_runs(id),
 UNIQUE(account,folder,uidvalidity,uid));
CREATE TABLE runner_work(subject_sha TEXT NOT NULL,stage TEXT NOT NULL,state TEXT NOT NULL,
 attempts INTEGER NOT NULL DEFAULT 0,lease_until REAL,next_eligible REAL NOT NULL DEFAULT 0,
 run_id TEXT NOT NULL REFERENCES runner_runs(id),receipt_sha TEXT,error_code TEXT,
 PRIMARY KEY(subject_sha,stage));
CREATE TABLE runner_events(sequence INTEGER PRIMARY KEY AUTOINCREMENT,run_id TEXT NOT NULL REFERENCES runner_runs(id),
 kind TEXT NOT NULL,item_key TEXT NOT NULL,code TEXT NOT NULL);
CREATE TRIGGER runner_messages_no_update BEFORE UPDATE ON runner_messages
 BEGIN SELECT RAISE(ABORT,'message evidence immutable'); END;
CREATE TRIGGER runner_messages_no_delete BEFORE DELETE ON runner_messages
 BEGIN SELECT RAISE(ABORT,'message evidence immutable'); END;
CREATE TRIGGER runner_messages_no_replace BEFORE INSERT ON runner_messages WHEN EXISTS(
 SELECT 1 FROM runner_messages WHERE id=NEW.id OR (account=NEW.account AND folder=NEW.folder AND uidvalidity=NEW.uidvalidity AND uid=NEW.uid))
 BEGIN SELECT RAISE(ABORT,'message evidence immutable'); END;
CREATE TRIGGER runner_events_no_update BEFORE UPDATE ON runner_events
 BEGIN SELECT RAISE(ABORT,'run event immutable'); END;
CREATE TRIGGER runner_events_no_delete BEFORE DELETE ON runner_events
 BEGIN SELECT RAISE(ABORT,'run event immutable'); END;
CREATE TRIGGER runner_events_no_replace BEFORE INSERT ON runner_events WHEN EXISTS(
 SELECT 1 FROM runner_events WHERE sequence=NEW.sequence)
 BEGIN SELECT RAISE(ABORT,'run event immutable'); END;
'''


# Additive extension: keep the exact v1 base SQL/checksum and existing profiles.
RELIABILITY_VERSION=1
RELIABILITY_SQL="""
CREATE TABLE runner_reliability_schema(version INTEGER PRIMARY KEY, checksum TEXT NOT NULL);
CREATE TABLE runner_mail_cursor(account TEXT PRIMARY KEY NOT NULL, folder TEXT NOT NULL);
CREATE TABLE runner_lifecycle(
 run_id TEXT PRIMARY KEY NOT NULL REFERENCES runner_runs(id),
 operation TEXT NOT NULL CHECK(operation IN ('recover','finish')),
 identity TEXT NOT NULL, outcome TEXT, summary TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','done')),
 attempts INTEGER NOT NULL DEFAULT 0, next_eligible REAL NOT NULL DEFAULT 0,
 error_code TEXT, acknowledgment TEXT);
CREATE INDEX runner_lifecycle_due ON runner_lifecycle(state,next_eligible,attempts);
CREATE TRIGGER runner_lifecycle_payload_immutable BEFORE UPDATE OF run_id,operation,identity,outcome,summary ON runner_lifecycle
 BEGIN SELECT RAISE(ABORT,'lifecycle intent immutable'); END;
CREATE TRIGGER runner_lifecycle_no_delete BEFORE DELETE ON runner_lifecycle
 BEGIN SELECT RAISE(ABORT,'lifecycle intent immutable'); END;
CREATE TRIGGER runner_lifecycle_no_replace BEFORE INSERT ON runner_lifecycle WHEN EXISTS(
 SELECT 1 FROM runner_lifecycle WHERE run_id=NEW.run_id)
 BEGIN SELECT RAISE(ABORT,'lifecycle intent immutable'); END;
"""
