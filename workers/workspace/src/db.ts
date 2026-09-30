// Typed D1 repository. One method group per table in workers/schema/d1.sql. Every query is
// scoped by campaign_id; identities are minted here per docs/CONTRACTS.md.
import {
  newActionId,
  newCorrespondenceId,
  newEventId,
  newIncidentId,
  newJobId,
  newRequestId,
  newRunId,
  nowIso,
  receiptId,
} from "@deflock/shared/ids";

export interface CampaignRow {
  campaign_id: string;
  name: string;
  jurisdiction: string;
  county_fips: string;
  county_name: string;
  place_fips: string | null;
  place_name: string | null;
  public_hostname: string | null;
  workspace_hostname: string | null;
  privacy_tier: "redacted_cloud" | "strict_local";
  law_package_status: string;
  external_sends: "disabled" | "approval_required";
  schedule_cron: string;
  timezone: string;
  created_at: string;
  updated_at: string;
}

export interface AgencyRow {
  agency_id: string;
  campaign_id: string;
  name: string;
  kind: string;
  jurisdiction_name: string;
  records_url: string | null;
  records_email: string | null;
  portal_vendor: string;
  portal_url: string | null;
  flock_transparency_slug: string | null;
  muckrock_agency_id: number | null;
  selected: number;
  verified: number;
  sources_json: string;
  created_at: string;
  updated_at: string;
}

export type RequestState =
  | "draft" | "approved" | "sent" | "acknowledged" | "extended" | "partial" | "fulfilled" | "denied" | "appealed" | "closed";

export interface RequestRow {
  request_id: string;
  campaign_id: string;
  agency_id: string;
  scope_id: string;
  scope_version: number;
  subject: string;
  body_md: string;
  channel: "email" | "muckrock" | "portal_manual";
  fee_cap_cents: number;
  state: RequestState;
  sent_at: string | null;
  determination_due: string | null;
  extension_claimed_until: string | null;
  last_activity_at: string | null;
  next_action: string | null;
  external_ref: string | null;
  created_at: string;
  updated_at: string;
}

export interface CorrespondenceRow {
  correspondence_id: string;
  campaign_id: string;
  request_id: string | null;
  direction: "inbound" | "outbound";
  channel: string;
  provider_message_id: string | null;
  from_addr: string | null;
  to_addr: string | null;
  subject: string | null;
  received_at: string;
  raw_sha256: string | null;
  classification: string | null;
  classification_confidence: string | null;
  summary: string | null;
  created_at: string;
  updated_at: string;
}

export interface OriginalRow {
  campaign_id: string;
  sha256: string;
  byte_count: number;
  media_type: string | null;
  r2_key: string;
  stored_at: string;
  created_at: string;
  updated_at: string;
}

export interface ReceiptOccurrenceRow {
  receipt_id: string;
  campaign_id: string;
  sha256: string;
  source_id: string;
  correspondence_id: string | null;
  agency_id: string | null;
  request_id: string | null;
  original_name: string;
  received_at: string;
  created_at: string;
  updated_at: string;
}

export interface FindingRow {
  finding_id: string;
  campaign_id: string;
  author: string;
  classification: string;
  confidence: string;
  summary: string;
  finding_json: string;
  content_sha256: string;
  state: string;
  created_at: string;
  updated_at: string;
}

export interface ReviewReceiptRow {
  review_id: string;
  campaign_id: string;
  finding_id: string;
  content_sha256: string;
  reviewer: string;
  role: string;
  decision: string;
  rationale: string;
  reviewed_at: string;
  created_at: string;
  updated_at: string;
}

export interface PublicationRow {
  publication_id: string;
  campaign_id: string;
  finding_id: string | null;
  path: string;
  content_sha256: string;
  published_at: string;
  published_by: string;
  deploy_receipt: string | null;
  created_at: string;
  updated_at: string;
}

export interface SubscriberEventRow {
  event_id: string;
  campaign_id: string;
  provider: string;
  kind: string;
  payload_json: string;
  occurred_at: string;
  created_at: string;
  updated_at: string;
}

export interface MeetingRow {
  meeting_id: string;
  campaign_id: string;
  body_name: string;
  agency_id: string | null;
  starts_at: string;
  agenda_url: string | null;
  agenda_item: string | null;
  relevance: string | null;
  comment_kit_md: string | null;
  rsvp_count: number;
  source: string | null;
  created_at: string;
  updated_at: string;
}

export type JobKind =
  | "intake" | "extract" | "digest" | "classify_mail" | "send_request" | "draft_followup" | "build_site" | "newsletter_draft" | "backup";
export type JobState = "queued" | "leased" | "done" | "failed" | "blocked" | "cancelled";

export interface JobRow {
  job_id: string;
  campaign_id: string;
  kind: JobKind;
  idempotency_key: string;
  inputs_json: string;
  state: JobState;
  attempt: number;
  max_attempts: number;
  leased_until: string | null;
  outputs_json: string | null;
  error: string | null;
  enqueued_at: string;
  finished_at: string | null;
  created_at: string;
  updated_at: string;
}

export type ActionKind =
  | "send_request" | "send_followup" | "pay_fee" | "publish_finding" | "send_newsletter" | "post_social" | "deploy_site";
export type ActionState = "proposed" | "approved" | "rejected" | "executing" | "executed" | "failed";

export interface ExternalActionRow {
  action_id: string;
  campaign_id: string;
  kind: ActionKind;
  subject_id: string | null;
  proposal_json: string;
  idempotency_key: string;
  state: ActionState;
  proposed_by: string;
  approved_by: string | null;
  approved_at: string | null;
  executed_at: string | null;
  provider_receipt: string | null;
  error: string | null;
  created_at: string;
  updated_at: string;
}

export interface IncidentRow {
  incident_id: string;
  campaign_id: string;
  fingerprint: string;
  severity: "info" | "warning" | "critical";
  message: string;
  first_seen: string;
  last_seen: string;
  count: number;
  resolved_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface RunReceiptRow {
  run_id: string;
  campaign_id: string;
  started_at: string;
  finished_at: string | null;
  trigger: string;
  jobs_created: number;
  jobs_done: number;
  jobs_failed: number;
  summary_json: string | null;
  created_at: string;
  updated_at: string;
}

export interface SettingRow {
  campaign_id: string;
  key: string;
  value_json: string;
  created_at: string;
  updated_at: string;
}

export const ACTION_KINDS: readonly ActionKind[] = [
  "send_request", "send_followup", "pay_fee", "publish_finding", "send_newsletter", "post_social", "deploy_site",
];
export const JOB_KINDS: readonly JobKind[] = [
  "intake", "extract", "digest", "classify_mail", "send_request", "draft_followup", "build_site", "newsletter_draft", "backup",
];

type Insertable<T> = Omit<T, "created_at" | "updated_at">;

export class Repo {
  constructor(
    readonly db: D1Database,
    readonly campaignId: string,
  ) {}

  private async insert<T extends object>(table: string, row: Insertable<T>): Promise<T> {
    const now = nowIso();
    const full = { ...row, created_at: now, updated_at: now } as unknown as Record<string, unknown>;
    const cols = Object.keys(full);
    const sql = `INSERT INTO ${table} (${cols.join(",")}) VALUES (${cols.map(() => "?").join(",")})`;
    await this.db.prepare(sql).bind(...cols.map((c) => full[c] ?? null)).run();
    return full as unknown as T;
  }

  private async insertIgnore<T extends object>(table: string, row: Insertable<T>): Promise<{ row: T; inserted: boolean }> {
    const now = nowIso();
    const full = { ...row, created_at: now, updated_at: now } as unknown as Record<string, unknown>;
    const cols = Object.keys(full);
    const sql = `INSERT OR IGNORE INTO ${table} (${cols.join(",")}) VALUES (${cols.map(() => "?").join(",")})`;
    const res = await this.db.prepare(sql).bind(...cols.map((c) => full[c] ?? null)).run();
    return { row: full as unknown as T, inserted: (res.meta.changes ?? 0) > 0 };
  }

  private one<T>(sql: string, ...args: unknown[]): Promise<T | null> {
    return this.db.prepare(sql).bind(...args).first<T>();
  }

  private async many<T>(sql: string, ...args: unknown[]): Promise<T[]> {
    const res = await this.db.prepare(sql).bind(...args).all<T>();
    return res.results;
  }

  private async update(table: string, key: string, id: string, patch: Record<string, unknown>): Promise<void> {
    const cols = Object.keys(patch);
    if (!cols.length) return;
    const sql = `UPDATE ${table} SET ${cols.map((c) => `${c} = ?`).join(", ")}, updated_at = ? WHERE ${key} = ? AND campaign_id = ?`;
    await this.db.prepare(sql).bind(...cols.map((c) => patch[c] ?? null), nowIso(), id, this.campaignId).run();
  }

  // campaign -----------------------------------------------------------------------
  campaign(): Promise<CampaignRow | null> {
    return this.one<CampaignRow>("SELECT * FROM campaign WHERE campaign_id = ?", this.campaignId);
  }

  createCampaign(row: Omit<Insertable<CampaignRow>, "campaign_id">): Promise<CampaignRow> {
    return this.insert<CampaignRow>("campaign", { ...row, campaign_id: this.campaignId });
  }

  // setting ------------------------------------------------------------------------
  async setting<T = unknown>(key: string): Promise<T | null> {
    const row = await this.one<SettingRow>("SELECT * FROM setting WHERE campaign_id = ? AND key = ?", this.campaignId, key);
    return row ? (JSON.parse(row.value_json) as T) : null;
  }

  async putSetting(key: string, value: unknown): Promise<void> {
    const now = nowIso();
    await this.db
      .prepare(
        "INSERT INTO setting (campaign_id, key, value_json, created_at, updated_at) VALUES (?,?,?,?,?) " +
          "ON CONFLICT(campaign_id, key) DO UPDATE SET value_json = excluded.value_json, updated_at = excluded.updated_at",
      )
      .bind(this.campaignId, key, JSON.stringify(value), now, now)
      .run();
  }

  settings(): Promise<SettingRow[]> {
    return this.many<SettingRow>("SELECT * FROM setting WHERE campaign_id = ? ORDER BY key", this.campaignId);
  }

  // agency -------------------------------------------------------------------------
  agencies(): Promise<AgencyRow[]> {
    return this.many<AgencyRow>("SELECT * FROM agency WHERE campaign_id = ? ORDER BY name", this.campaignId);
  }

  agency(agencyId: string): Promise<AgencyRow | null> {
    return this.one<AgencyRow>("SELECT * FROM agency WHERE campaign_id = ? AND agency_id = ?", this.campaignId, agencyId);
  }

  createAgency(row: Omit<Insertable<AgencyRow>, "campaign_id">): Promise<AgencyRow> {
    return this.insert<AgencyRow>("agency", { ...row, campaign_id: this.campaignId });
  }

  // request ------------------------------------------------------------------------
  requests(): Promise<RequestRow[]> {
    return this.many<RequestRow>("SELECT * FROM request WHERE campaign_id = ? ORDER BY created_at", this.campaignId);
  }

  request(requestId: string): Promise<RequestRow | null> {
    return this.one<RequestRow>("SELECT * FROM request WHERE campaign_id = ? AND request_id = ?", this.campaignId, requestId);
  }

  createRequest(row: Omit<Insertable<RequestRow>, "campaign_id" | "request_id">): Promise<RequestRow> {
    return this.insert<RequestRow>("request", { ...row, campaign_id: this.campaignId, request_id: newRequestId() });
  }

  updateRequest(requestId: string, patch: Partial<RequestRow>): Promise<void> {
    return this.update("request", "request_id", requestId, patch);
  }

  /** Sent or extended requests whose determination date has passed with no inbound reply since. */
  overdueRequests(today: string): Promise<RequestRow[]> {
    return this.many<RequestRow>(
      "SELECT r.* FROM request r WHERE r.campaign_id = ? AND r.state IN ('sent','extended','acknowledged') " +
        "AND COALESCE(r.extension_claimed_until, r.determination_due) < ? " +
        "AND NOT EXISTS (SELECT 1 FROM correspondence c WHERE c.request_id = r.request_id AND c.direction = 'inbound' " +
        "AND c.received_at > COALESCE(r.sent_at, r.created_at) AND c.classification IN ('production','partial_production','denial'))",
      this.campaignId,
      today,
    );
  }

  // correspondence -----------------------------------------------------------------
  correspondenceFor(requestId: string): Promise<CorrespondenceRow[]> {
    return this.many<CorrespondenceRow>(
      "SELECT * FROM correspondence WHERE campaign_id = ? AND request_id = ? ORDER BY received_at",
      this.campaignId,
      requestId,
    );
  }

  inbox(limit = 200): Promise<CorrespondenceRow[]> {
    return this.many<CorrespondenceRow>(
      "SELECT * FROM correspondence WHERE campaign_id = ? ORDER BY received_at DESC LIMIT ?",
      this.campaignId,
      limit,
    );
  }

  correspondence(id: string): Promise<CorrespondenceRow | null> {
    return this.one<CorrespondenceRow>("SELECT * FROM correspondence WHERE campaign_id = ? AND correspondence_id = ?", this.campaignId, id);
  }

  correspondenceByMessageId(providerMessageId: string): Promise<CorrespondenceRow | null> {
    return this.one<CorrespondenceRow>(
      "SELECT * FROM correspondence WHERE campaign_id = ? AND provider_message_id = ?",
      this.campaignId,
      providerMessageId,
    );
  }

  /** Insert, deduplicating on (campaign_id, provider_message_id). Returns the existing row when deduped. */
  async createCorrespondence(
    row: Omit<Insertable<CorrespondenceRow>, "campaign_id" | "correspondence_id">,
  ): Promise<{ row: CorrespondenceRow; inserted: boolean }> {
    const candidate = { ...row, campaign_id: this.campaignId, correspondence_id: newCorrespondenceId() };
    if (row.provider_message_id) {
      const existing = await this.correspondenceByMessageId(row.provider_message_id);
      if (existing) return { row: existing, inserted: false };
    }
    const res = await this.insertIgnore<CorrespondenceRow>("correspondence", candidate);
    if (res.inserted || !row.provider_message_id) return res;
    const existing = await this.correspondenceByMessageId(row.provider_message_id);
    return { row: existing ?? res.row, inserted: false };
  }

  updateCorrespondence(id: string, patch: Partial<CorrespondenceRow>): Promise<void> {
    return this.update("correspondence", "correspondence_id", id, patch);
  }

  // original + receipt_occurrence --------------------------------------------------
  original(sha256: string): Promise<OriginalRow | null> {
    return this.one<OriginalRow>("SELECT * FROM original WHERE campaign_id = ? AND sha256 = ?", this.campaignId, sha256);
  }

  originals(limit = 500): Promise<OriginalRow[]> {
    return this.many<OriginalRow>("SELECT * FROM original WHERE campaign_id = ? ORDER BY stored_at DESC LIMIT ?", this.campaignId, limit);
  }

  async putOriginal(row: Omit<Insertable<OriginalRow>, "campaign_id">): Promise<{ row: OriginalRow; inserted: boolean }> {
    return this.insertIgnore<OriginalRow>("original", { ...row, campaign_id: this.campaignId });
  }

  receiptsFor(sha256: string): Promise<ReceiptOccurrenceRow[]> {
    return this.many<ReceiptOccurrenceRow>(
      "SELECT * FROM receipt_occurrence WHERE campaign_id = ? AND sha256 = ? ORDER BY received_at",
      this.campaignId,
      sha256,
    );
  }

  /** receipt_id = sha256([source_id, sha256]); the same pair never duplicates. */
  async recordReceipt(
    row: Omit<Insertable<ReceiptOccurrenceRow>, "campaign_id" | "receipt_id">,
  ): Promise<{ row: ReceiptOccurrenceRow; inserted: boolean }> {
    const id = await receiptId(row.source_id, row.sha256);
    const res = await this.insertIgnore<ReceiptOccurrenceRow>("receipt_occurrence", { ...row, campaign_id: this.campaignId, receipt_id: id });
    if (res.inserted) return res;
    const existing = await this.one<ReceiptOccurrenceRow>("SELECT * FROM receipt_occurrence WHERE receipt_id = ?", id);
    return { row: existing ?? res.row, inserted: false };
  }

  // finding / review ---------------------------------------------------------------
  findings(): Promise<FindingRow[]> {
    return this.many<FindingRow>("SELECT * FROM finding WHERE campaign_id = ? ORDER BY updated_at DESC", this.campaignId);
  }

  finding(findingId: string): Promise<FindingRow | null> {
    return this.one<FindingRow>("SELECT * FROM finding WHERE campaign_id = ? AND finding_id = ?", this.campaignId, findingId);
  }

  createFinding(row: Omit<Insertable<FindingRow>, "campaign_id">): Promise<FindingRow> {
    return this.insert<FindingRow>("finding", { ...row, campaign_id: this.campaignId });
  }

  updateFinding(findingId: string, patch: Partial<FindingRow>): Promise<void> {
    return this.update("finding", "finding_id", findingId, patch);
  }

  reviewReceipts(findingId: string): Promise<ReviewReceiptRow[]> {
    return this.many<ReviewReceiptRow>(
      "SELECT * FROM review_receipt WHERE campaign_id = ? AND finding_id = ? ORDER BY reviewed_at",
      this.campaignId,
      findingId,
    );
  }

  createReviewReceipt(row: Omit<Insertable<ReviewReceiptRow>, "campaign_id">): Promise<ReviewReceiptRow> {
    return this.insert<ReviewReceiptRow>("review_receipt", { ...row, campaign_id: this.campaignId });
  }

  publications(): Promise<PublicationRow[]> {
    return this.many<PublicationRow>("SELECT * FROM publication WHERE campaign_id = ? ORDER BY published_at DESC", this.campaignId);
  }

  createPublication(row: Omit<Insertable<PublicationRow>, "campaign_id">): Promise<PublicationRow> {
    return this.insert<PublicationRow>("publication", { ...row, campaign_id: this.campaignId });
  }

  // subscriber_event (counts only; identities stay with the provider) ---------------
  subscriberEvents(limit = 50): Promise<SubscriberEventRow[]> {
    return this.many<SubscriberEventRow>(
      "SELECT * FROM subscriber_event WHERE campaign_id = ? ORDER BY occurred_at DESC LIMIT ?",
      this.campaignId,
      limit,
    );
  }

  createSubscriberEvent(row: Omit<Insertable<SubscriberEventRow>, "campaign_id" | "event_id">): Promise<SubscriberEventRow> {
    return this.insert<SubscriberEventRow>("subscriber_event", { ...row, campaign_id: this.campaignId, event_id: newEventId() });
  }

  // meeting ------------------------------------------------------------------------
  meetings(): Promise<MeetingRow[]> {
    return this.many<MeetingRow>("SELECT * FROM meeting WHERE campaign_id = ? ORDER BY starts_at DESC", this.campaignId);
  }

  createMeeting(row: Omit<Insertable<MeetingRow>, "campaign_id">): Promise<MeetingRow> {
    return this.insert<MeetingRow>("meeting", { ...row, campaign_id: this.campaignId });
  }

  // job ----------------------------------------------------------------------------
  jobs(limit = 200): Promise<JobRow[]> {
    return this.many<JobRow>("SELECT * FROM job WHERE campaign_id = ? ORDER BY enqueued_at DESC LIMIT ?", this.campaignId, limit);
  }

  job(jobId: string): Promise<JobRow | null> {
    return this.one<JobRow>("SELECT * FROM job WHERE campaign_id = ? AND job_id = ?", this.campaignId, jobId);
  }

  /** Idempotent enqueue: the same (campaign_id, idempotency_key) returns the existing job. */
  async enqueueJob(kind: JobKind, idempotencyKey: string, inputs: unknown, maxAttempts = 3): Promise<{ row: JobRow; inserted: boolean }> {
    const res = await this.insertIgnore<JobRow>("job", {
      job_id: newJobId(),
      campaign_id: this.campaignId,
      kind,
      idempotency_key: idempotencyKey,
      inputs_json: JSON.stringify(inputs ?? {}),
      state: "queued",
      attempt: 0,
      max_attempts: maxAttempts,
      leased_until: null,
      outputs_json: null,
      error: null,
      enqueued_at: nowIso(),
      finished_at: null,
    });
    if (res.inserted) return res;
    const existing = await this.one<JobRow>(
      "SELECT * FROM job WHERE campaign_id = ? AND idempotency_key = ?",
      this.campaignId,
      idempotencyKey,
    );
    return { row: existing ?? res.row, inserted: false };
  }

  /**
   * Atomically lease the oldest runnable job: queued, or leased with an expired lease
   * (requeue). One UPDATE ... RETURNING statement, so two runners cannot lease the same job.
   */
  async leaseJob(leaseSeconds: number, now: Date = new Date()): Promise<JobRow | null> {
    const nowIsoStr = now.toISOString();
    const until = new Date(now.getTime() + leaseSeconds * 1000).toISOString();
    const res = await this.db
      .prepare(
        "UPDATE job SET state = 'leased', leased_until = ?, attempt = attempt + 1, updated_at = ? " +
          "WHERE job_id = (SELECT job_id FROM job WHERE campaign_id = ? AND attempt < max_attempts " +
          "AND (state = 'queued' OR (state = 'leased' AND leased_until < ?)) ORDER BY enqueued_at, job_id LIMIT 1) " +
          "RETURNING *",
      )
      .bind(until, nowIsoStr, this.campaignId, nowIsoStr)
      .first<JobRow>();
    return res ?? null;
  }

  /** Record a runner result. failed with attempts left goes back to queued. */
  async finishJob(jobId: string, status: "done" | "failed" | "blocked", outputs: unknown, error: string | null): Promise<JobRow | null> {
    const job = await this.job(jobId);
    if (!job) return null;
    let state: JobState = status;
    if (status === "failed" && job.attempt < job.max_attempts) state = "queued";
    const finished = state === "queued" ? null : nowIso();
    await this.update("job", "job_id", jobId, {
      state,
      leased_until: null,
      outputs_json: outputs === undefined ? null : JSON.stringify(outputs),
      error,
      finished_at: finished,
    });
    return this.job(jobId);
  }

  // external_action ----------------------------------------------------------------
  actions(state?: ActionState): Promise<ExternalActionRow[]> {
    return state
      ? this.many<ExternalActionRow>(
          "SELECT * FROM external_action WHERE campaign_id = ? AND state = ? ORDER BY created_at DESC",
          this.campaignId,
          state,
        )
      : this.many<ExternalActionRow>("SELECT * FROM external_action WHERE campaign_id = ? ORDER BY created_at DESC", this.campaignId);
  }

  action(actionId: string): Promise<ExternalActionRow | null> {
    return this.one<ExternalActionRow>("SELECT * FROM external_action WHERE campaign_id = ? AND action_id = ?", this.campaignId, actionId);
  }

  /** Idempotent proposal: the same idempotency_key returns the existing row untouched. */
  async propose(
    kind: ActionKind,
    subjectId: string | null,
    proposal: unknown,
    idempotencyKey: string,
    proposedBy: string,
  ): Promise<{ row: ExternalActionRow; inserted: boolean }> {
    const res = await this.insertIgnore<ExternalActionRow>("external_action", {
      action_id: newActionId(),
      campaign_id: this.campaignId,
      kind,
      subject_id: subjectId,
      proposal_json: JSON.stringify(proposal ?? {}),
      idempotency_key: idempotencyKey,
      state: "proposed",
      proposed_by: proposedBy,
      approved_by: null,
      approved_at: null,
      executed_at: null,
      provider_receipt: null,
      error: null,
    });
    if (res.inserted) return res;
    const existing = await this.one<ExternalActionRow>(
      "SELECT * FROM external_action WHERE campaign_id = ? AND idempotency_key = ?",
      this.campaignId,
      idempotencyKey,
    );
    return { row: existing ?? res.row, inserted: false };
  }

  updateAction(actionId: string, patch: Partial<ExternalActionRow>): Promise<void> {
    return this.update("external_action", "action_id", actionId, patch);
  }

  /** Approved -> executing only if still approved (guards double execution). */
  async claimForExecution(actionId: string): Promise<ExternalActionRow | null> {
    const row = await this.db
      .prepare(
        "UPDATE external_action SET state = 'executing', updated_at = ? WHERE campaign_id = ? AND action_id = ? AND state = 'approved' RETURNING *",
      )
      .bind(nowIso(), this.campaignId, actionId)
      .first<ExternalActionRow>();
    return row ?? null;
  }

  // incident -----------------------------------------------------------------------
  incidents(): Promise<IncidentRow[]> {
    return this.many<IncidentRow>(
      "SELECT * FROM incident WHERE campaign_id = ? AND resolved_at IS NULL ORDER BY last_seen DESC",
      this.campaignId,
    );
  }

  async raiseIncident(fingerprint: string, severity: IncidentRow["severity"], message: string): Promise<void> {
    const now = nowIso();
    await this.db
      .prepare(
        "INSERT INTO incident (incident_id, campaign_id, fingerprint, severity, message, first_seen, last_seen, count, resolved_at, created_at, updated_at) " +
          "VALUES (?,?,?,?,?,?,?,1,NULL,?,?) ON CONFLICT(campaign_id, fingerprint) DO UPDATE SET last_seen = excluded.last_seen, " +
          "count = incident.count + 1, message = excluded.message, resolved_at = NULL, updated_at = excluded.updated_at",
      )
      .bind(newIncidentId(), this.campaignId, fingerprint, severity, message, now, now, now, now)
      .run();
  }

  // run_receipt --------------------------------------------------------------------
  runs(limit = 20): Promise<RunReceiptRow[]> {
    return this.many<RunReceiptRow>("SELECT * FROM run_receipt WHERE campaign_id = ? ORDER BY started_at DESC LIMIT ?", this.campaignId, limit);
  }

  startRun(trigger: string): Promise<RunReceiptRow> {
    return this.insert<RunReceiptRow>("run_receipt", {
      run_id: newRunId(),
      campaign_id: this.campaignId,
      started_at: nowIso(),
      finished_at: null,
      trigger,
      jobs_created: 0,
      jobs_done: 0,
      jobs_failed: 0,
      summary_json: null,
    });
  }

  finishRun(runId: string, patch: Partial<RunReceiptRow>): Promise<void> {
    return this.update("run_receipt", "run_id", runId, { ...patch, finished_at: nowIso() });
  }

  // counts for the dashboard ---------------------------------------------------------
  async counts(): Promise<Record<string, number>> {
    const q = async (sql: string) => {
      const r = await this.db.prepare(sql).bind(this.campaignId).first<{ n: number }>();
      return r?.n ?? 0;
    };
    return {
      agencies: await q("SELECT COUNT(*) n FROM agency WHERE campaign_id = ? AND selected = 1"),
      requests: await q("SELECT COUNT(*) n FROM request WHERE campaign_id = ?"),
      requests_open: await q("SELECT COUNT(*) n FROM request WHERE campaign_id = ? AND state IN ('sent','acknowledged','extended','partial')"),
      inbox: await q("SELECT COUNT(*) n FROM correspondence WHERE campaign_id = ? AND direction = 'inbound'"),
      unclassified: await q("SELECT COUNT(*) n FROM correspondence WHERE campaign_id = ? AND direction = 'inbound' AND (classification IS NULL OR classification = 'unclassified')"),
      originals: await q("SELECT COUNT(*) n FROM original WHERE campaign_id = ?"),
      findings: await q("SELECT COUNT(*) n FROM finding WHERE campaign_id = ?"),
      approvals_pending: await q("SELECT COUNT(*) n FROM external_action WHERE campaign_id = ? AND state = 'proposed'"),
      jobs_queued: await q("SELECT COUNT(*) n FROM job WHERE campaign_id = ? AND state IN ('queued','leased')"),
      jobs_failed: await q("SELECT COUNT(*) n FROM job WHERE campaign_id = ? AND state = 'failed'"),
      incidents: await q("SELECT COUNT(*) n FROM incident WHERE campaign_id = ? AND resolved_at IS NULL"),
    };
  }
}
