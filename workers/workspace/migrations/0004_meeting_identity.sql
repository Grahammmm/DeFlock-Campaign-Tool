-- Meeting source identity, an unknown date and immutable revisions (BF-df-meeting-identity).
--
-- SQLite cannot drop the NOT NULL on starts_at in place, so this rebuilds the table in the
-- documented order (new table, copy, drop, rename). D1 applies one migration file as one
-- transaction; foreign keys are deferred to its commit, so no row is lost or orphaned.
-- Every existing row is copied with every value unchanged, date_state = 'known' and
-- revision = 1. The table has no secondary index before this file; the one it gains is below.
--
-- Re-running this file by hand is refused, never data-destroying: the first ALTER adds a
-- column that already exists after the first run, so the whole transaction rolls back.
PRAGMA defer_foreign_keys = on;

ALTER TABLE meeting ADD COLUMN date_state TEXT NOT NULL DEFAULT 'known';

CREATE TABLE meeting_v4 (
  meeting_id TEXT PRIMARY KEY,
  campaign_id TEXT NOT NULL REFERENCES campaign(campaign_id),
  body_name TEXT NOT NULL,
  agency_id TEXT,
  starts_at TEXT,
  date_state TEXT NOT NULL DEFAULT 'known' CHECK (date_state IN ('known','unknown')),
  agenda_url TEXT,
  agenda_item TEXT,
  relevance TEXT,
  comment_kit_md TEXT,
  rsvp_count INTEGER NOT NULL DEFAULT 0,
  source TEXT,
  source_url TEXT,
  item_id TEXT,
  title_sha256 TEXT,
  fetched_at TEXT,
  revision INTEGER NOT NULL DEFAULT 1,
  supersedes TEXT REFERENCES meeting(meeting_id),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  CHECK ((date_state = 'known' AND starts_at IS NOT NULL) OR (date_state = 'unknown' AND starts_at IS NULL))
);

INSERT INTO meeting_v4 (meeting_id, campaign_id, body_name, agency_id, starts_at, date_state, agenda_url, agenda_item,
  relevance, comment_kit_md, rsvp_count, source, created_at, updated_at)
SELECT meeting_id, campaign_id, body_name, agency_id, starts_at, date_state, agenda_url, agenda_item,
  relevance, comment_kit_md, rsvp_count, source, created_at, updated_at
FROM meeting;

DROP TABLE meeting;

ALTER TABLE meeting_v4 RENAME TO meeting;

-- A polled meeting is filed once per revision. Manual meetings (no source_url or no item_id)
-- are outside the index, so any number of them coexist.
CREATE UNIQUE INDEX IF NOT EXISTS meeting_source_identity ON meeting(campaign_id, source_url, item_id, revision)
  WHERE source_url IS NOT NULL AND item_id IS NOT NULL;
