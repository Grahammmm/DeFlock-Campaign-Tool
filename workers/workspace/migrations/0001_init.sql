-- DeFlock Campaign Tool: D1 schema (single source of truth for campaign state).
-- Originals live in R2 keyed by sha256; D1 holds identities, receipts and state.
-- Every table carries campaign_id so one database can, if ever needed, hold more
-- than one campaign; the workspace Worker always scopes queries by campaign_id.
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS campaign (
  campaign_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  jurisdiction TEXT NOT NULL,            -- us-ca
  county_fips TEXT NOT NULL,
  county_name TEXT NOT NULL,
  place_fips TEXT,
  place_name TEXT,
  public_hostname TEXT,
  workspace_hostname TEXT,
  privacy_tier TEXT NOT NULL DEFAULT 'redacted_cloud' CHECK (privacy_tier IN ('redacted_cloud','strict_local')),
  law_package_status TEXT NOT NULL DEFAULT 'unreviewed',
  external_sends TEXT NOT NULL DEFAULT 'disabled' CHECK (external_sends IN ('disabled','approval_required')),
  schedule_cron TEXT NOT NULL DEFAULT '0 7,13 * * *;30 20 * * *',
  timezone TEXT NOT NULL DEFAULT 'America/Los_Angeles',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS setting (
  campaign_id TEXT NOT NULL REFERENCES campaign(campaign_id),
  key TEXT NOT NULL,
  value_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (campaign_id, key)
);

CREATE TABLE IF NOT EXISTS agency (
  agency_id TEXT NOT NULL,
  campaign_id TEXT NOT NULL REFERENCES campaign(campaign_id),
  name TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('sheriff','police','chp','district_attorney','county_board','city_council','other')),
  jurisdiction_name TEXT NOT NULL,
  records_url TEXT,
  records_email TEXT,
  portal_vendor TEXT NOT NULL DEFAULT 'unknown',
  portal_url TEXT,
  flock_transparency_slug TEXT,
  muckrock_agency_id INTEGER,
  selected INTEGER NOT NULL DEFAULT 1,
  verified INTEGER NOT NULL DEFAULT 0,
  sources_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (campaign_id, agency_id)
);

CREATE TABLE IF NOT EXISTS request (
  request_id TEXT PRIMARY KEY,
  campaign_id TEXT NOT NULL REFERENCES campaign(campaign_id),
  agency_id TEXT NOT NULL,
  scope_id TEXT NOT NULL,
  scope_version INTEGER NOT NULL DEFAULT 1,
  subject TEXT NOT NULL,
  body_md TEXT NOT NULL,
  channel TEXT NOT NULL CHECK (channel IN ('email','muckrock','portal_manual')),
  fee_cap_cents INTEGER NOT NULL DEFAULT 0,
  state TEXT NOT NULL DEFAULT 'draft' CHECK (state IN ('draft','approved','sent','acknowledged','extended','partial','fulfilled','denied','appealed','closed')),
  sent_at TEXT,
  determination_due TEXT,
  extension_claimed_until TEXT,
  last_activity_at TEXT,
  next_action TEXT,
  external_ref TEXT,                     -- MuckRock id, portal number, message-id
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (campaign_id, agency_id) REFERENCES agency(campaign_id, agency_id)
);
CREATE INDEX IF NOT EXISTS request_campaign_state ON request(campaign_id, state);

CREATE TABLE IF NOT EXISTS correspondence (
  correspondence_id TEXT PRIMARY KEY,
  campaign_id TEXT NOT NULL REFERENCES campaign(campaign_id),
  request_id TEXT REFERENCES request(request_id),
  direction TEXT NOT NULL CHECK (direction IN ('inbound','outbound')),
  channel TEXT NOT NULL,
  provider_message_id TEXT,              -- RFC 5322 Message-ID or portal id
  from_addr TEXT,
  to_addr TEXT,
  subject TEXT,
  received_at TEXT NOT NULL,
  raw_sha256 TEXT,                       -- raw MIME stored in R2
  classification TEXT CHECK (classification IN ('acknowledgement','extension','fee_estimate','partial_production','production','denial','clarification','unrelated','unclassified')),
  classification_confidence TEXT,
  summary TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (campaign_id, provider_message_id)
);
CREATE INDEX IF NOT EXISTS correspondence_request ON correspondence(request_id, received_at);

CREATE TABLE IF NOT EXISTS original (
  campaign_id TEXT NOT NULL REFERENCES campaign(campaign_id),
  sha256 TEXT NOT NULL,
  byte_count INTEGER NOT NULL,
  media_type TEXT,
  r2_key TEXT NOT NULL,
  stored_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (campaign_id, sha256)
);

CREATE TABLE IF NOT EXISTS receipt_occurrence (
  receipt_id TEXT PRIMARY KEY,           -- sha256([source_id, sha256])
  campaign_id TEXT NOT NULL REFERENCES campaign(campaign_id),
  sha256 TEXT NOT NULL,
  source_id TEXT NOT NULL,               -- message-id + attachment index, portal file id, manual
  correspondence_id TEXT REFERENCES correspondence(correspondence_id),
  agency_id TEXT,
  request_id TEXT REFERENCES request(request_id),
  original_name TEXT NOT NULL,
  received_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (campaign_id, sha256) REFERENCES original(campaign_id, sha256)
);

CREATE TABLE IF NOT EXISTS extraction (
  extraction_id TEXT PRIMARY KEY,
  campaign_id TEXT NOT NULL REFERENCES campaign(campaign_id),
  sha256 TEXT NOT NULL,
  extractor TEXT NOT NULL,               -- pypdf 6.10.0, openpyxl, eml, text
  status TEXT NOT NULL CHECK (status IN ('complete','partial','failed','unsupported')),
  page_count INTEGER,
  pages_with_text INTEGER,
  text_sha256 TEXT,                      -- derived text object in R2 (private)
  notes TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (campaign_id, sha256) REFERENCES original(campaign_id, sha256)
);

CREATE TABLE IF NOT EXISTS digest (
  digest_id TEXT PRIMARY KEY,
  campaign_id TEXT NOT NULL REFERENCES campaign(campaign_id),
  sha256 TEXT NOT NULL,
  law_package_version TEXT NOT NULL,
  privacy_tier TEXT NOT NULL,
  redaction_count INTEGER NOT NULL DEFAULT 0,
  model_id TEXT,
  digest_json TEXT NOT NULL,             -- scope, actors, dates, duties, statements, omissions, counterevidence, locators
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (campaign_id, sha256) REFERENCES original(campaign_id, sha256)
);

CREATE TABLE IF NOT EXISTS finding (
  finding_id TEXT PRIMARY KEY,
  campaign_id TEXT NOT NULL REFERENCES campaign(campaign_id),
  author TEXT NOT NULL,
  classification TEXT NOT NULL CHECK (classification IN ('documented_fact','apparent_conflict','confirmed_conflict','information_gap','redaction','agency_assertion','no_conflict')),
  confidence TEXT NOT NULL CHECK (confidence IN ('verified','likely','needs_attorney_review')),
  summary TEXT NOT NULL,
  finding_json TEXT NOT NULL,            -- full review.py finding: sources[], limitations, counterevidence, event_date, rule_version, duty, exceptions
  content_sha256 TEXT NOT NULL,          -- review.content_hash(finding_json)
  state TEXT NOT NULL DEFAULT 'draft' CHECK (state IN ('draft','in_review','blocked','ready','published','corrected','withdrawn')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS review_receipt (
  review_id TEXT PRIMARY KEY,
  campaign_id TEXT NOT NULL REFERENCES campaign(campaign_id),
  finding_id TEXT NOT NULL REFERENCES finding(finding_id),
  content_sha256 TEXT NOT NULL,
  reviewer TEXT NOT NULL,                -- Access identity email
  role TEXT NOT NULL CHECK (role IN ('factual','legal','privacy')),
  decision TEXT NOT NULL CHECK (decision IN ('approve','reject','changes_requested')),
  rationale TEXT NOT NULL,
  reviewed_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS publication (
  publication_id TEXT PRIMARY KEY,
  campaign_id TEXT NOT NULL REFERENCES campaign(campaign_id),
  finding_id TEXT REFERENCES finding(finding_id),
  path TEXT NOT NULL,                    -- /findings/<slug>.html
  content_sha256 TEXT NOT NULL,
  published_at TEXT NOT NULL,
  published_by TEXT NOT NULL,
  deploy_receipt TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS correction (
  correction_id TEXT PRIMARY KEY,
  campaign_id TEXT NOT NULL REFERENCES campaign(campaign_id),
  publication_id TEXT NOT NULL REFERENCES publication(publication_id),
  reason TEXT NOT NULL,
  replacement_finding_id TEXT REFERENCES finding(finding_id),
  corrected_at TEXT NOT NULL,
  corrected_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

-- Subscriber identities stay with the newsletter provider; only events and counts land here.
CREATE TABLE IF NOT EXISTS subscriber_event (
  event_id TEXT PRIMARY KEY,
  campaign_id TEXT NOT NULL REFERENCES campaign(campaign_id),
  provider TEXT NOT NULL,                -- brevo
  kind TEXT NOT NULL,                    -- list_count, campaign_sent, webhook
  payload_json TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meeting (
  meeting_id TEXT PRIMARY KEY,
  campaign_id TEXT NOT NULL REFERENCES campaign(campaign_id),
  body_name TEXT NOT NULL,
  agency_id TEXT,
  starts_at TEXT NOT NULL,
  agenda_url TEXT,
  agenda_item TEXT,
  relevance TEXT,                        -- alpr_item, budget, consent_calendar, none
  comment_kit_md TEXT,
  rsvp_count INTEGER NOT NULL DEFAULT 0,
  source TEXT,                           -- legistar, granicus, manual
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS job (
  job_id TEXT PRIMARY KEY,
  campaign_id TEXT NOT NULL REFERENCES campaign(campaign_id),
  kind TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  inputs_json TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'queued' CHECK (state IN ('queued','leased','done','failed','blocked','cancelled')),
  attempt INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 3,
  leased_until TEXT,
  outputs_json TEXT,
  error TEXT,
  enqueued_at TEXT NOT NULL,
  finished_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (campaign_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS job_campaign_state ON job(campaign_id, state, enqueued_at);

-- Every proposed external effect. Nothing leaves the system without state = approved.
CREATE TABLE IF NOT EXISTS external_action (
  action_id TEXT PRIMARY KEY,
  campaign_id TEXT NOT NULL REFERENCES campaign(campaign_id),
  kind TEXT NOT NULL CHECK (kind IN ('send_request','send_followup','pay_fee','publish_finding','send_newsletter','post_social','deploy_site')),
  subject_id TEXT,                       -- request_id, finding_id, etc.
  proposal_json TEXT NOT NULL,           -- what will happen, verbatim
  idempotency_key TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'proposed' CHECK (state IN ('proposed','approved','rejected','executing','executed','failed')),
  proposed_by TEXT NOT NULL,             -- runner job id or identity
  approved_by TEXT,
  approved_at TEXT,
  executed_at TEXT,
  provider_receipt TEXT,
  error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (campaign_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS external_action_state ON external_action(campaign_id, state, created_at);

CREATE TABLE IF NOT EXISTS incident (
  incident_id TEXT PRIMARY KEY,
  campaign_id TEXT NOT NULL REFERENCES campaign(campaign_id),
  fingerprint TEXT NOT NULL,             -- dedup key
  severity TEXT NOT NULL CHECK (severity IN ('info','warning','critical')),
  message TEXT NOT NULL,
  first_seen TEXT NOT NULL,
  last_seen TEXT NOT NULL,
  count INTEGER NOT NULL DEFAULT 1,
  resolved_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (campaign_id, fingerprint)
);

CREATE TABLE IF NOT EXISTS run_receipt (
  run_id TEXT PRIMARY KEY,
  campaign_id TEXT NOT NULL REFERENCES campaign(campaign_id),
  started_at TEXT NOT NULL,
  finished_at TEXT,
  trigger TEXT NOT NULL,                 -- cron, manual, email
  jobs_created INTEGER NOT NULL DEFAULT 0,
  jobs_done INTEGER NOT NULL DEFAULT 0,
  jobs_failed INTEGER NOT NULL DEFAULT 0,
  summary_json TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
