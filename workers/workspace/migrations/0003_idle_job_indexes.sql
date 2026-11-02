-- Keep token lookup and runnable selection independent of terminal job history.
CREATE INDEX IF NOT EXISTS job_campaign_claim ON job(campaign_id, claim_token);
CREATE INDEX IF NOT EXISTS job_runnable ON job(campaign_id, enqueued_at, job_id)
  WHERE state IN ('queued', 'leased');
