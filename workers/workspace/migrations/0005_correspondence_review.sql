-- Organizer classification review (PANEL-INBOX-REVIEW). Who confirmed or corrected a
-- message's classification, when, and what the runner first proposed. All three are NULL on
-- every existing row (never reviewed); a reviewed row is never overwritten by classify_mail.
--
-- Re-running this file by hand is refused, never data-destroying: the first ALTER adds a
-- column that already exists after the first run, so the whole transaction rolls back.
ALTER TABLE correspondence ADD COLUMN reviewed_by TEXT;
ALTER TABLE correspondence ADD COLUMN reviewed_at TEXT;
ALTER TABLE correspondence ADD COLUMN proposed_classification TEXT;
