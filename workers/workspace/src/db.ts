// Typed D1 repository. One method group per table in workers/schema/d1.sql. Every query is
// scoped by campaign_id; identities are minted here per docs/CONTRACTS.md.
import {
  newActionId,
  newCorrectionId,
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

export interface CorrectionRow {
  correction_id: string;
  campaign_id: string;
  publication_id: string;
  reason: string;
  replacement_finding_id: string | null;
  corrected_at: string;
  corrected_by: string;
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

/** Tables exported by /api/export.json, in dependency order (parents before children). */
export const EXPORT_TABLES = [
  "campaign", "setting", "agency", "request", "correspondence", "original", "receipt_occurrence", "extraction", "digest",
  "finding", "review_receipt", "publication", "correction", "subscriber_event", "meeting", "job", "external_action", "incident",
  "run_receipt",
] as const;
export type ExportTable = (typeof EXPORT_TABLES)[number];

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

  selectedAgencies(): Promise<AgencyRow[]> {
    return this.many<AgencyRow>("SELECT * FROM agency WHERE campaign_id = ? AND selected = 1 ORDER BY name", this.campaignId);
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

  publication(publicationId: string): Promise<PublicationRow | null> {
    return this.one<PublicationRow>("SELECT * FROM publication WHERE campaign_id = ? AND publication_id = ?", this.campaignId, publicationId);
  }

  /** Latest publication row for a finding (a finding is published once per content hash). */
  publicationForFinding(findingId: string): Promise<PublicationRow | null> {
    return this.one<PublicationRow>(
      "SELECT * FROM publication WHERE campaign_id = ? AND finding_id = ? ORDER BY published_at DESC LIMIT 1",
      this.campaignId,
      findingId,
    );
  }

  updatePublication(publicationId: string, patch: Partial<PublicationRow>): Promise<void> {
    return this.update("publication", "publication_id", publicationId, patch);
  }

  findingsByState(states: readonly string[]): Promise<FindingRow[]> {
    if (!states.length) return Promise.resolve([]);
    return this.many<FindingRow>(
      `SELECT * FROM finding WHERE campaign_id = ? AND state IN (${states.map(() => "?").join(",")}) ORDER BY updated_at DESC`,
      this.campaignId,
      ...states,
    );
  }

  // correction ---------------------------------------------------------------------
  corrections(): Promise<CorrectionRow[]> {
    return this.many<CorrectionRow>("SELECT * FROM correction WHERE campaign_id = ? ORDER BY corrected_at DESC", this.campaignId);
  }

  correctionsFor(publicationId: string): Promise<CorrectionRow[]> {
    return this.many<CorrectionRow>(
      "SELECT * FROM correction WHERE campaign_id = ? AND publication_id = ? ORDER BY corrected_at",
      this.campaignId,
      publicationId,
    );
  }

  createCorrection(row: Omit<Insertable<CorrectionRow>, "campaign_id" | "correction_id">): Promise<CorrectionRow> {
    return this.insert<CorrectionRow>("correction", { ...row, campaign_id: this.campaignId, correction_id: newCorrectionId() });
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

  latestSubscriberEvent(kind: string): Promise<SubscriberEventRow | null> {
    return this.one<SubscriberEventRow>(
      "SELECT * FROM subscriber_event WHERE campaign_id = ? AND kind = ? ORDER BY occurred_at DESC LIMIT 1",
      this.campaignId,
      kind,
    );
  }

  // meeting ------------------------------------------------------------------------
  meetings(): Promise<MeetingRow[]> {
    return this.many<MeetingRow>("SELECT * FROM meeting WHERE campaign_id = ? ORDER BY starts_at DESC", this.campaignId);
  }

  meeting(meetingId: string): Promise<MeetingRow | null> {
    return this.one<MeetingRow>("SELECT * FROM meeting WHERE campaign_id = ? AND meeting_id = ?", this.campaignId, meetingId);
  }

  createMeeting(row: Omit<Insertable<MeetingRow>, "campaign_id">): Promise<MeetingRow> {
    return this.insert<MeetingRow>("meeting", { ...row, campaign_id: this.campaignId });
  }

  updateMeeting(meetingId: string, patch: Partial<MeetingRow>): Promise<void> {
    return this.update("meeting", "meeting_id", meetingId, patch);
  }

  upcomingMeetings(fromIso: string): Promise<MeetingRow[]> {
    return this.many<MeetingRow>("SELECT * FROM meeting WHERE campaign_id = ? AND starts_at >= ? ORDER BY starts_at", this.campaignId, fromIso);
  }

  // job ----------------------------------------------------------------------------
  jobs(limit = 200): Promise<JobRow[]> {
    return this.many<JobRow>("SELECT * FROM job WHERE campaign_id = ? ORDER BY enqueued_at DESC LIMIT ?", this.campaignId, limit);
  }

  job(jobId: string): Promise<JobRow | null> {
    return this.one<JobRow>("SELECT * FROM job WHERE campaign_id = ? AND job_id = ?", this.campaignId, jobId);
  }

  jobsByKind(kind: JobKind, limit = 50): Promise<JobRow[]> {
    return this.many<JobRow>("SELECT * FROM job WHERE campaign_id = ? AND kind = ? ORDER BY enqueued_at DESC LIMIT ?", this.campaignId, kind, limit);
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

  /**
   * Idempotent proposal: the same idempotency_key returns the existing row untouched,
   * except a conclusively failed card, which is re-opened as `proposed` so a
   * transient provider failure does not retire the key for good (approvals.ts: "a failed
   * card must be proposed again"). Ambiguous delivery stays held for reconciliation.
   */
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
    if (existing?.state === "failed" && !requiresDeliveryReconciliation(existing)) {
      const reopened = await this.db
        .prepare(
          "UPDATE external_action SET state = 'proposed', proposal_json = ?, proposed_by = ?, approved_by = NULL, approved_at = NULL, executed_at = NULL, provider_receipt = NULL, error = ? WHERE campaign_id = ? AND action_id = ? AND state = 'failed' AND error IS ? AND provider_receipt IS ?",
        )
        .bind(JSON.stringify(proposal ?? {}), proposedBy, "re-proposed after: " + (existing.error ?? "failure"), this.campaignId, existing.action_id, existing.error, existing.provider_receipt)
        .run();
      if (reopened.meta.changes === 1) return { row: (await this.action(existing.action_id))!, inserted: false };
    }
    return { row: existing ?? res.row, inserted: false };
  }

  /** Apply an operator's evidence only to the exact failed row they inspected. */
  async reconcileDelivery(action: ExternalActionRow, delivered: boolean, receipt: string, deliveredAt: string | null = null, mailMessageId: string | null = null): Promise<boolean> {
    const clock = nowIso();
    const provider = action.kind === "send_newsletter" ? "brevo" : "mail";
    const eventId = newEventId();
    const repairMail = delivered && (action.kind === "send_request" || action.kind === "send_followup") && action.subject_id !== null;
    const proposal = repairMail ? JSON.parse(action.proposal_json) as Record<string, unknown> : {};
    if (repairMail) {
      if (!mailMessageId || !deliveredAt || !["email", "muckrock", "portal_manual"].includes(String(proposal.channel)) ||
          typeof proposal.subject !== "string" || !proposal.subject.trim()) throw new Error("invalid mail reconciliation evidence");
      const request = await this.request(action.subject_id!);
      if (!request) throw new Error("reconciled request is missing from this campaign");
      if (action.kind === "send_request" && request.external_ref && request.external_ref !== mailMessageId) {
        throw new Error("request already has a different provider receipt");
      }
    }
    const update = this.db.prepare(
      "UPDATE external_action SET state = ?, approved_by = ?, approved_at = ?, executed_at = ?, provider_receipt = ?, error = NULL, updated_at = ? WHERE campaign_id = ? AND action_id = ? AND state = 'failed' AND error IS ? AND provider_receipt IS ?" +
      (repairMail ? " AND EXISTS (SELECT 1 FROM request WHERE campaign_id = ? AND request_id = ?" +
        (action.kind === "send_request" ? " AND (external_ref IS NULL OR external_ref = ?)" : "") + ")" : ""),
    ).bind(delivered ? "executed" : "proposed", delivered ? action.approved_by : null,
      delivered ? action.approved_at : null, delivered ? deliveredAt : null, receipt, clock,
      this.campaignId, action.action_id, action.error, action.provider_receipt,
      ...(repairMail ? [this.campaignId, action.subject_id, ...(action.kind === "send_request" ? [mailMessageId] : [])] : []));
    // D1 batches are transactional. The event exists only if the conditional UPDATE won.
    const event = this.db.prepare(
      "INSERT INTO subscriber_event (event_id, campaign_id, provider, kind, payload_json, occurred_at, created_at, updated_at) SELECT ?, ?, ?, ?, ?, ?, ?, ? WHERE changes() = 1",
    ).bind(eventId, this.campaignId, provider, delivered ? (provider === "brevo" ? "campaign_sent" : "mail_delivery_reconciled") : "action_reconciled", receipt, delivered ? deliveredAt : clock, clock, clock);
    const statements = [update, event];
    if (repairMail) {
      // This unique transaction-local audit event exists only if our exact action won.
      const won = "EXISTS (SELECT 1 FROM subscriber_event WHERE event_id = ? AND campaign_id = ?)";
      const initial = action.kind === "send_request";
      statements.push(this.db.prepare(
        "UPDATE request SET " + (initial ? "state = CASE WHEN state IN ('draft','approved') THEN 'sent' ELSE state END, sent_at = COALESCE(sent_at, ?), external_ref = COALESCE(external_ref, ?), " : "") +
        "last_activity_at = CASE WHEN last_activity_at IS NULL OR julianday(last_activity_at) < julianday(?) THEN ? ELSE last_activity_at END, updated_at = ? " +
        "WHERE campaign_id = ? AND request_id = ? AND " + won,
      ).bind(...(initial ? [deliveredAt, mailMessageId] : []), deliveredAt, deliveredAt, clock, this.campaignId, action.subject_id, eventId, this.campaignId));
      statements.push(this.db.prepare(
        "INSERT INTO correspondence (correspondence_id, campaign_id, request_id, direction, channel, provider_message_id, from_addr, to_addr, subject, received_at, raw_sha256, classification, classification_confidence, summary, created_at, updated_at) " +
        "SELECT ?, ?, ?, 'outbound', ?, ?, NULL, ?, ?, ?, NULL, NULL, NULL, ?, ?, ? WHERE " + won +
        " AND NOT EXISTS (SELECT 1 FROM correspondence WHERE campaign_id = ? AND provider_message_id = ? AND request_id = ? AND direction = 'outbound' AND channel = ? AND subject = ?)",
      ).bind(newCorrespondenceId(), this.campaignId, action.subject_id, proposal.channel, mailMessageId,
        typeof proposal.to === "string" ? proposal.to : null, proposal.subject, deliveredAt,
        initial ? "records request sent" : "follow-up sent", clock, clock, eventId, this.campaignId,
        this.campaignId, mailMessageId, action.subject_id, proposal.channel, proposal.subject));
    }
    const results = await this.db.batch(statements);
    return results[0].meta.changes === 1;
  }

  updateAction(actionId: string, patch: Partial<ExternalActionRow>): Promise<void> {
    return this.update("external_action", "action_id", actionId, patch);
  }

  /** Check the immutable claim marker, not an elapsed-time lease. */
  async assertExecuting(action: ExternalActionRow): Promise<void> {
    const current = await this.action(action.action_id);
    if (!executionClaimId(action) || current?.state !== "executing" || current.error !== action.error) {
      throw new Error("execution claim is no longer active");
    }
  }

  /** A retired executor cannot overwrite a held/reconciled card or a later claim. */
  async updateExecutingAction(action: ExternalActionRow, patch: Partial<Pick<ExternalActionRow,
    "state" | "executed_at" | "provider_receipt" | "error">>): Promise<boolean> {
    const keys = Object.keys(patch) as Array<keyof typeof patch>;
    if (!executionClaimId(action) || !keys.length || keys.some(key =>
      !["state", "executed_at", "provider_receipt", "error"].includes(key))) throw new Error("invalid execution update");
    const result = await this.db.prepare(
      `UPDATE external_action SET ${keys.map(key => `${key} = ?`).join(", ")}, updated_at = ? ` +
      "WHERE campaign_id = ? AND action_id = ? AND state = 'executing' AND error = ?",
    ).bind(...keys.map(key => patch[key] ?? null), nowIso(), this.campaignId, action.action_id, action.error).run();
    return result.meta.changes === 1;
  }

  async createExecutionEvent(action: ExternalActionRow, row: Omit<Insertable<SubscriberEventRow>, "campaign_id" | "event_id">): Promise<SubscriberEventRow | null> {
    const clock = nowIso();
    return this.db.prepare(
      "INSERT INTO subscriber_event (event_id, campaign_id, provider, kind, payload_json, occurred_at, created_at, updated_at) " +
      "SELECT ?, ?, ?, ?, ?, ?, ?, ? WHERE EXISTS (SELECT 1 FROM external_action " +
      "WHERE campaign_id = ? AND action_id = ? AND state = 'executing' AND error = ?) RETURNING *",
    ).bind(newEventId(), this.campaignId, row.provider, row.kind, row.payload_json, row.occurred_at,
      clock, clock, this.campaignId, action.action_id, action.error).first<SubscriberEventRow>();
  }

  /** Fence an interrupted newsletter and journal its hold atomically; never retry it. */
  async holdExecution(action: ExternalActionRow, evidence: string): Promise<boolean> {
    const claimId = executionClaimId(action);
    if (!claimId || action.kind !== "send_newsletter" || action.state !== "executing") return false;
    const clock = nowIso();
    const update = this.db.prepare(
      "UPDATE external_action SET state = 'failed', error = ?, updated_at = ? WHERE campaign_id = ? " +
      "AND action_id = ? AND state = 'executing' AND error = ? AND updated_at = ? AND provider_receipt IS ?",
    ).bind("brevo_execution_interrupted: " + claimId, clock, this.campaignId, action.action_id,
      action.error, action.updated_at, action.provider_receipt);
    const event = this.db.prepare(
      "INSERT INTO subscriber_event (event_id, campaign_id, provider, kind, payload_json, occurred_at, created_at, updated_at) " +
      "SELECT ?, ?, 'brevo', 'action_execution_held', ?, ?, ?, ? WHERE changes() = 1",
    ).bind(newEventId(), this.campaignId, evidence, clock, clock, clock);
    const results = await this.db.batch([update, event]);
    return results[0].meta.changes === 1;
  }

  /** Bounded, action-specific recovery evidence; private organizer access only. */
  actionExecutionEvidence(actionId: string): Promise<SubscriberEventRow[]> {
    return this.many<SubscriberEventRow>(
      "SELECT * FROM subscriber_event WHERE campaign_id = ? AND kind IN ('action_execution_held', 'late_execution_receipt', 'action_reconciliation_attempt') " +
      "AND json_extract(payload_json, '$.action_id') = ? ORDER BY occurred_at DESC, event_id DESC LIMIT 51",
      this.campaignId, actionId);
  }

  /** Operator transitions may change only the exact card snapshot inspected. */
  async transitionAction(action: ExternalActionRow, patch: Partial<Pick<ExternalActionRow,
    "state" | "proposal_json" | "approved_by" | "approved_at" | "error">>): Promise<ExternalActionRow | null> {
    const keys = Object.keys(patch) as Array<keyof typeof patch>;
    const allowed = new Set(["state", "proposal_json", "approved_by", "approved_at", "error"]);
    if (!keys.length || keys.some(key => !allowed.has(key))) throw new Error("invalid action transition fields");
    return this.db.prepare(
      `UPDATE external_action SET ${keys.map(key => `${key} = ?`).join(", ")}, updated_at = ? ` +
      "WHERE campaign_id = ? AND action_id = ? AND state = ? AND proposal_json = ? AND updated_at = ? " +
      "AND approved_by IS ? AND approved_at IS ? AND error IS ? AND provider_receipt IS ? RETURNING *",
    ).bind(...keys.map(key => patch[key] ?? null), nowIso(), this.campaignId, action.action_id,
      action.state, action.proposal_json, action.updated_at, action.approved_by, action.approved_at,
      action.error, action.provider_receipt).first<ExternalActionRow>();
  }

  /** Approved -> executing only if still approved (guards double execution). */
  async claimForExecution(actionId: string): Promise<ExternalActionRow | null> {
    const row = await this.db
      .prepare(
        "UPDATE external_action SET state = 'executing', error = ?, updated_at = ? WHERE campaign_id = ? AND action_id = ? AND state = 'approved' " +
        "AND (kind != 'send_request' OR subject_id IS NULL OR (" +
        "NOT EXISTS (SELECT 1 FROM external_action prior WHERE prior.campaign_id = external_action.campaign_id " +
        "AND prior.subject_id = external_action.subject_id AND prior.kind = 'send_request' AND prior.action_id != external_action.action_id " +
        "AND (prior.state IN ('executing','executed') OR (prior.state = 'failed' AND (prior.provider_receipt IS NOT NULL OR prior.error LIKE 'mail_send_ambiguous:%')))) " +
        "AND NOT EXISTS (SELECT 1 FROM request sent WHERE sent.campaign_id = external_action.campaign_id " +
        "AND sent.request_id = external_action.subject_id AND sent.sent_at IS NOT NULL))) RETURNING *",
      )
      .bind("execution_claim: " + crypto.randomUUID(), nowIso(), this.campaignId, actionId)
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

  // export ---------------------------------------------------------------------------
  /** Every row of one table for this campaign, in primary-key order; used by /api/export.json. */
  exportTable(table: ExportTable): Promise<Record<string, unknown>[]> {
    return this.many<Record<string, unknown>>(`SELECT * FROM ${table} WHERE campaign_id = ? ORDER BY created_at, rowid`, this.campaignId);
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

/** A provider object or ambiguous network result must not be retried as a new send. */
export function requiresDeliveryReconciliation(action: ExternalActionRow): boolean {
  if ((action.kind === "send_request" || action.kind === "send_followup") && action.state === "failed") {
    return action.provider_receipt !== null || /^mail_send_ambiguous:/.test(action.error ?? "");
  }
  return action.kind === "send_newsletter" && action.state === "failed" &&
    (action.provider_receipt !== null || /^brevo_(?:create|send)_ambiguous:/.test(action.error ?? "") || requiresExecutionQuiescence(action));
}

export function executionClaimId(action: ExternalActionRow): string | null {
  return /^execution_claim: ([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})$/.exec(action.error ?? "")?.[1] ?? null;
}

export function requiresExecutionQuiescence(action: ExternalActionRow): boolean {
  return /^brevo_execution_interrupted: /.test(action.error ?? "");
}
