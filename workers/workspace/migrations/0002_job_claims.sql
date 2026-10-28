-- Additive hosted-queue upgrade. Never replay/rewrite 0001 on an existing DB.
ALTER TABLE job ADD COLUMN claim_token TEXT;

-- Immutable lease identity and per-attempt result evidence. Accepted results are never
-- overwritten; response_json acknowledges exact replay even after another attempt starts.
CREATE TABLE IF NOT EXISTS job_claim (
  campaign_id TEXT NOT NULL REFERENCES campaign(campaign_id),
  job_id TEXT NOT NULL REFERENCES job(job_id),
  claim_token TEXT NOT NULL,
  attempt INTEGER NOT NULL,
  leased_until TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('leased','expired','done','failed','blocked')),
  result_sha256 TEXT,
  result_json TEXT,
  response_json TEXT,
  completion_id TEXT,
  accepted_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (campaign_id, job_id, claim_token),
  UNIQUE (campaign_id, job_id, attempt)
);
CREATE INDEX IF NOT EXISTS job_claim_completion ON job_claim(campaign_id, completion_id);


-- Old runners have no issued token and cannot complete these claims. Preserve the
-- current row's evidence as a legacy snapshot (not a submitted/replayable result).
-- Existing live leases expire normally; attempts below the cap are then reclaimable.
UPDATE job SET claim_token = 'legacy_' || lower(hex(randomblob(16))) WHERE attempt > 0;
INSERT INTO job_claim (campaign_id, job_id, claim_token, attempt, leased_until, state, result_json, created_at, updated_at)
SELECT campaign_id, job_id, claim_token, attempt, COALESCE(leased_until, updated_at),
  CASE WHEN state = 'queued' THEN 'failed' WHEN state = 'cancelled' THEN 'expired' ELSE state END,
  json_object('legacy_snapshot', json_object('state', state, 'outputs_json', outputs_json, 'error', error, 'finished_at', finished_at)),
  created_at, updated_at FROM job WHERE attempt > 0;
