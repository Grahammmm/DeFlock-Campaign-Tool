import { requiresDeliveryReconciliation } from "../db.ts";
// Server-rendered workspace screens. Every value is escaped by the html tag.
import { fmtDate, html, jsonBlock, raw, type Safe } from "@deflock/shared/html";
import { reviewBlockers, type JsonObject } from "@deflock/shared/review";
import type {
  AgencyRow, CampaignRow, CorrectionRow, CorrespondenceRow, ExternalActionRow, FindingRow, IncidentRow, JobRow, MeetingRow, OriginalRow,
  PublicationRow, ReceiptOccurrenceRow, RequestRow, ReviewReceiptRow, RunReceiptRow, SettingRow, SubscriberEventRow,
} from "../db.ts";
import type { BrevoSettings } from "../manifest.ts";
import { statePill } from "./layout.ts";

export function dashboard(campaign: CampaignRow | null, counts: Record<string, number>, incidents: IncidentRow[], runs: RunReceiptRow[], jobs: JobRow[]): Safe {
  const stat = (label: string, key: string) => html`<div class="card stat"><strong>${counts[key] ?? 0}</strong><span class="meta">${label}</span></div>`;
  return html`<h1>${campaign?.name ?? "Campaign"}</h1>
<p class="meta">${campaign ? `${campaign.county_name} County, ${campaign.jurisdiction.toUpperCase()} - privacy tier ${campaign.privacy_tier} - external sends ${campaign.external_sends} - law package ${campaign.law_package_status}` : "No campaign row yet; the wizard writes it at launch."}</p>
<div class="cards">${stat("Selected agencies", "agencies")}${stat("Requests", "requests")}${stat("Open requests", "requests_open")}${stat("Inbound mail", "inbox")}${stat("Unclassified mail", "unclassified")}${stat("Preserved originals", "originals")}${stat("Findings", "findings")}${stat("Approvals waiting", "approvals_pending")}${stat("Jobs queued", "jobs_queued")}${stat("Jobs failed", "jobs_failed")}${stat("Open incidents", "incidents")}</div>
<h2>Open incidents</h2>
${incidents.length ? html`<table class="data"><tr><th>Severity</th><th>Message</th><th>Count</th><th>Last seen</th></tr>${incidents.map((i) => html`<tr><td>${statePill(i.severity)}</td><td>${i.message}</td><td>${i.count}</td><td>${i.last_seen}</td></tr>`)}</table>` : html`<p>None.</p>`}
<h2>Recent runs</h2>
<table class="data"><tr><th>Run</th><th>Trigger</th><th>Started</th><th>Jobs created</th></tr>${runs.map((r) => html`<tr><td>${r.run_id}</td><td>${r.trigger}</td><td>${r.started_at}</td><td>${r.jobs_created}</td></tr>`)}</table>
<h2>Recent jobs</h2>
<table class="data"><tr><th>Job</th><th>Kind</th><th>State</th><th>Attempt</th><th>Enqueued</th><th>Error</th></tr>${jobs.slice(0, 30).map((j) => html`<tr><td>${j.job_id}</td><td>${j.kind}</td><td>${statePill(j.state)}</td><td>${j.attempt}/${j.max_attempts}</td><td>${j.enqueued_at}</td><td>${j.error ?? ""}</td></tr>`)}</table>`;
}

export function requestsList(requests: RequestRow[], agencies: Map<string, AgencyRow>): Safe {
  return html`<h1>Records requests</h1>
<p>Drafts are never sent from this screen. Sending is a <a href="/approvals">proposed action</a> that an organizer approves.</p>
<table class="data"><tr><th>Agency</th><th>Channel</th><th>State</th><th>Sent</th><th>Determination due</th><th>Next action</th></tr>
${requests.map((r) => html`<tr><td><a href="/requests/${r.request_id}">${agencies.get(r.agency_id)?.name ?? r.agency_id}</a></td><td>${r.channel}</td><td>${statePill(r.state)}</td><td>${fmtDate(r.sent_at)}</td><td>${r.determination_due ?? ""}</td><td>${r.next_action ?? ""}</td></tr>`)}
</table>${requests.length ? "" : html`<p>No requests yet.</p>`}`;
}

export function requestDetail(r: RequestRow, agency: AgencyRow | null, timeline: CorrespondenceRow[], followupJob: JobRow | null): Safe {
  return html`<h1>${agency?.name ?? r.agency_id}</h1>
<p>${statePill(r.state)} channel ${r.channel} - fee cap $${(r.fee_cap_cents / 100).toFixed(2)} - sent ${fmtDate(r.sent_at) || "never"} - determination due ${r.determination_due ?? "n/a"}${r.extension_claimed_until ? ` (extension claimed until ${r.extension_claimed_until})` : ""}</p>
<h2>Correspondence timeline</h2>
${timeline.length ? html`<ul class="timeline">${timeline.map((c) => html`<li class="${c.direction}"><strong>${c.direction}</strong> ${c.received_at} - ${c.subject ?? "(no subject)"} <span class="pill">${c.classification ?? "unclassified"}</span>${c.summary ? html`<br><span class="meta">${c.summary}</span>` : ""}${c.raw_sha256 ? html`<br><a href="/records/${c.raw_sha256}">raw message ${c.raw_sha256.slice(0, 12)}</a>` : ""}</li>`)}</ul>` : html`<p>No correspondence recorded.</p>`}
<form method="post" action="/requests/${r.request_id}/draft-followup" class="stack"><button class="secondary" type="submit">Draft follow-up</button> <span class="hint">Enqueues a draft_followup job for the runner; the resulting draft appears as an approval card, nothing is sent.</span></form>
${followupJob ? html`<p class="notice">Follow-up job ${followupJob.job_id} is ${statePill(followupJob.state)}.</p>` : ""}
<h2>Subject</h2><p>${r.subject}</p>
<h2>Body (Markdown)</h2><pre class="json">${r.body_md}</pre>`;
}

export function inbox(rows: CorrespondenceRow[], requests: Map<string, RequestRow>, agencies: Map<string, AgencyRow>): Safe {
  return html`<h1>Inbox</h1>
<p>Raw messages are preserved by hash before anything else happens. Classification is proposed by the runner and reviewed here.</p>
<table class="data"><tr><th>Received</th><th>Direction</th><th>From</th><th>Subject</th><th>Classification</th><th>Request</th><th>Raw</th></tr>
${rows.map((c) => { const req = c.request_id ? requests.get(c.request_id) : null; return html`<tr><td>${c.received_at}</td><td>${c.direction}</td><td>${c.from_addr ?? ""}</td><td>${c.subject ?? ""}</td><td>${c.classification ?? "unclassified"}</td><td>${req ? html`<a href="/requests/${req.request_id}">${agencies.get(req.agency_id)?.name ?? req.agency_id}</a>` : ""}</td><td>${c.raw_sha256 ? html`<a href="/records/${c.raw_sha256}">${c.raw_sha256.slice(0, 12)}</a>` : ""}</td></tr>`; })}
</table>${rows.length ? "" : html`<p>Nothing received yet.</p>`}`;
}

export function records(rows: OriginalRow[]): Safe {
  return html`<h1>Records</h1>
<p>Originals are stored under their SHA-256 in the private bucket. Downloads are served by this Worker through short-lived signed links; the bucket has no public URL.</p>
<table class="data"><tr><th>SHA-256</th><th>Bytes</th><th>Type</th><th>Stored</th><th></th></tr>
${rows.map((o) => html`<tr><td><a href="/records/${o.sha256}">${o.sha256}</a></td><td>${o.byte_count}</td><td>${o.media_type ?? ""}</td><td>${o.stored_at}</td><td><form method="post" action="/records/${o.sha256}/link"><button class="secondary" type="submit">Signed link (10 min)</button></form></td></tr>`)}
</table>${rows.length ? "" : html`<p>No originals preserved yet.</p>`}`;
}

export function recordDetail(o: OriginalRow, receipts: ReceiptOccurrenceRow[], link: string | null): Safe {
  return html`<h1>Original ${o.sha256.slice(0, 16)}</h1>
<p>${o.byte_count} bytes - ${o.media_type ?? "unknown type"} - stored ${o.stored_at} - key ${o.r2_key}</p>
${link ? html`<p class="notice">Signed download link (expires in 10 minutes, not stored): <a href="${link}">${link}</a></p>` : ""}
<form method="post" action="/records/${o.sha256}/link"><button class="secondary" type="submit">Create signed link</button></form>
<h2>Receipts (where this exact document arrived)</h2>
<table class="data"><tr><th>Receipt</th><th>Source</th><th>Original name</th><th>Received</th><th>Agency</th><th>Request</th></tr>
${receipts.map((r) => html`<tr><td>${r.receipt_id.slice(0, 16)}</td><td>${r.source_id}</td><td>${r.original_name}</td><td>${r.received_at}</td><td>${r.agency_id ?? ""}</td><td>${r.request_id ? html`<a href="/requests/${r.request_id}">${r.request_id}</a>` : ""}</td></tr>`)}
</table><p class="meta">A hash establishes byte identity, not authenticity or truth.</p>`;
}

export async function findings(rows: { finding: FindingRow; receipts: ReviewReceiptRow[] }[]): Promise<Safe> {
  const items: Safe[] = [];
  for (const { finding, receipts } of rows) {
    const blockers = await reviewBlockers(JSON.parse(finding.finding_json) as JsonObject, receipts.map((r) => ({
      content_sha256: r.content_sha256, decision: r.decision, reviewer: r.reviewer, role: r.role, rationale: r.rationale, reviewed_at: r.reviewed_at,
    })));
    items.push(html`<tr><td><a href="/findings/${finding.finding_id}">${finding.summary}</a></td><td>${finding.classification}</td><td>${finding.confidence}</td><td>${statePill(finding.state)}</td><td>${receipts.length}</td><td>${blockers.length ? html`<span class="pill bad">${blockers.length} blocker(s)</span> ${blockers.join(", ")}` : html`<span class="pill ok">publishable</span>`}</td></tr>`);
  }
  return html`<h1>Findings</h1>
<p>Publication requires factual, legal and privacy approvals from two independent reviewers bound to the exact content hash; <code>needs_attorney_review</code> blocks publish_finding.</p>
<table class="data"><tr><th>Summary</th><th>Classification</th><th>Confidence</th><th>State</th><th>Receipts</th><th>Review gate</th></tr>${items}</table>${rows.length ? "" : html`<p>No findings yet.</p>`}`;
}

export function findingDetail(
  f: FindingRow,
  receipts: ReviewReceiptRow[],
  blockers: string[],
  reviewer: string,
  publication: PublicationRow | null,
  corrections: CorrectionRow[],
  actions: ExternalActionRow[],
  notice: string | null,
): Safe {
  const isAuthor = reviewer.toLowerCase() === f.author.toLowerCase();
  const published = f.state === "published" || f.state === "corrected";
  const terminal = published || f.state === "withdrawn";
  return html`<h1>${f.summary}</h1>
${notice ? html`<p class="notice">${notice}</p>` : ""}
<p>${statePill(f.state)} ${f.classification} - confidence ${f.confidence} - author ${f.author} - content ${f.content_sha256.slice(0, 16)}</p>
<p>${blockers.length ? html`<span class="pill bad">blocked</span> ${blockers.join(", ")}` : html`<span class="pill ok">review gate satisfied</span>`}${f.confidence === "needs_attorney_review" ? html` <span class="pill bad">needs_attorney_review blocks publication</span>` : ""}</p>
${publication ? html`<p class="notice">Published ${publication.published_at} by ${publication.published_by} at <code>${publication.path}</code> (content ${publication.content_sha256.slice(0, 16)})${publication.deploy_receipt ? html`; deployed` : html`; <strong>not yet deployed</strong>`}.</p>` : ""}
${corrections.length ? html`<h2>Corrections</h2><table class="data"><tr><th>When</th><th>By</th><th>Reason</th><th>Replacement</th></tr>${corrections.map((c) => html`<tr><td>${c.corrected_at}</td><td>${c.corrected_by}</td><td>${c.reason}</td><td>${c.replacement_finding_id ? html`<a href="/findings/${c.replacement_finding_id}">${c.replacement_finding_id}</a>` : ""}</td></tr>`)}</table>` : ""}
<h2>Review receipts</h2>
<table class="data"><tr><th>Reviewer</th><th>Role</th><th>Decision</th><th>Rationale</th><th>Reviewed</th><th>Bound to</th></tr>${receipts.map((r) => html`<tr><td>${r.reviewer}</td><td>${r.role}</td><td>${r.decision}</td><td>${r.rationale}</td><td>${r.reviewed_at}</td><td>${r.content_sha256 === f.content_sha256 ? html`<span class="pill ok">current</span>` : html`<span class="pill warn">stale</span>`}</td></tr>`)}</table>
${terminal ? "" : isAuthor ? html`<h2>Review</h2><p class="notice warn">You are the author of this finding; authors cannot review their own work. Ask two independent reviewers to cover the factual, legal and privacy roles.</p>` : html`<h2>Submit review receipt (as ${reviewer})</h2>
<form method="post" action="/findings/${f.finding_id}/review" class="stack">
<label class="field">Role <select name="role"><option>factual</option><option>legal</option><option>privacy</option></select></label>
<label class="field">Decision <select name="decision"><option>approve</option><option>changes_requested</option><option>reject</option></select></label>
<label class="field">Rationale <textarea name="rationale" required style="min-height:6rem"></textarea></label>
<p class="meta">The receipt binds your Access identity to content hash ${f.content_sha256.slice(0, 16)}; editing the finding invalidates it.</p>
<button class="primary" type="submit">Record review receipt</button></form>`}
${f.state === "ready" && f.confidence !== "needs_attorney_review" ? html`<h2>Publication</h2><form method="post" action="/findings/${f.finding_id}/propose" class="stack"><p>Creates a <code>publish_finding</code> approval card. Nothing is published until an organizer approves and executes it; execution re-checks the review gate.</p><button class="primary" type="submit">Propose publication</button></form>` : ""}
${published ? html`<h2>Issue correction</h2>
<form method="post" action="/findings/${f.finding_id}/correct" class="stack"><label class="field">Reason (published with the finding)<textarea name="reason" required style="min-height:5rem"></textarea></label><label class="field">Replacement finding id (optional, must be published)<input type="text" name="replacement_finding_id" pattern="fnd_[0-9a-f]{16}"></label><button class="secondary" type="submit">Record correction and rebuild site</button></form>
<h2>Withdraw</h2>
<form method="post" action="/findings/${f.finding_id}/withdraw" class="stack"><label class="field">Reason<input type="text" name="reason" required></label><button class="danger" type="submit">Withdraw and rebuild site</button></form>` : ""}
${actions.length ? html`<h2>Approval cards</h2><table class="data"><tr><th>Card</th><th>Kind</th><th>State</th><th>Proposed</th><th>Result</th></tr>${actions.map((a) => html`<tr><td><a href="/approvals">${a.action_id}</a></td><td>${a.kind}</td><td>${statePill(a.state)}</td><td>${a.created_at}</td><td>${a.error ?? a.provider_receipt ?? ""}</td></tr>`)}</table>` : ""}
<h2>Finding JSON</h2>${jsonBlock(JSON.parse(f.finding_json))}`;
}

export function approvals(rows: ExternalActionRow[], notice: string | null): Safe {
  const proposed = rows.filter((a) => a.state === "proposed");
  const other = rows.filter((a) => a.state !== "proposed");
  const card = (a: ExternalActionRow) => html`<div class="card"><h3>${a.kind} ${statePill(a.state)}</h3><p class="meta">${a.action_id} - subject ${a.subject_id ?? "n/a"} - proposed by ${a.proposed_by} ${a.created_at}${a.approved_by ? html`<br>approved by ${a.approved_by} ${a.approved_at}` : ""}${a.executed_at ? html`<br>executed ${a.executed_at}` : ""}${a.error ? html`<br><span class="pill bad">${a.error}</span>` : ""}</p>
${jsonBlock(JSON.parse(a.proposal_json))}
${a.state === "proposed" ? html`<form method="post" action="/approvals/${a.action_id}/approve" style="display:inline"><button class="primary" type="submit">Approve</button></form>
<form method="post" action="/approvals/${a.action_id}/edit" class="stack" style="margin-top:.6rem"><label class="field">Edit proposal (JSON merged over the current one)<textarea name="proposal" style="min-height:5rem">{}</textarea></label><button class="secondary" type="submit">Save edit</button></form>
<form method="post" action="/approvals/${a.action_id}/reject" class="stack" style="margin-top:.6rem"><label class="field">Reason<input type="text" name="reason"></label><button class="danger" type="submit">Reject</button></form>` : ""}
${requiresDeliveryReconciliation(a) ? html`<p>Delivery is unresolved. Check this action and its provider receipt before retrying. Reconciliation does not send anything.</p>
${a.provider_receipt ? jsonBlock(JSON.parse(a.provider_receipt)) : ""}
<form method="post" action="/approvals/${a.action_id}/reconcile" class="stack"><label class="field">Provider-confirmed outcome<select name="outcome" required><option value="">Choose after checking provider</option><option value="delivered">Delivered — retain as executed</option><option value="not_delivered">Not delivered — require fresh approval</option></select></label><label class="field">Provider-check reference<input name="reference" maxlength="500" required></label><label class="field">Provider delivery time (UTC ISO, delivered only)<input name="delivered_at" placeholder="2026-01-01T12:00:00Z"></label><button class="secondary" type="submit">Record delivery check</button></form>` : ""}
${a.state === "approved" ? html`<form method="post" action="/approvals/${a.action_id}/execute" style="display:inline"><button class="primary" type="submit">Execute now</button></form>` : ""}
</div>`;
  return html`<h1>Approvals</h1>
${notice ? html`<p class="notice">${notice}</p>` : ""}
<p>Every proposed external effect is a card. Only approved cards with an approver identity from Cloudflare Access are executed, and execution records a provider receipt. Executing an executed card again returns the existing receipt. send_request and send_followup fail with <code>sender_not_configured</code> until an outbox sender is registered; post_social is <code>not_implemented</code>; pay_fee is <code>manual_only</code>.</p>
<h2>Waiting (${proposed.length})</h2><div class="cards">${proposed.map(card)}</div>
<h2>History</h2><div class="cards">${other.map(card)}</div>`;
}

export function publish(publications: PublicationRow[], siteVersion: string | null, previous: string | null, campaign: CampaignRow | null): Safe {
  return html`<h1>Publish</h1>
<p>The public site serves the R2 prefix <code>sites/${siteVersion ?? "(none)"}/</code>${previous ? html` (previous: <code>${previous}</code>)` : ""}${campaign?.public_hostname ? html` at <code>${campaign.public_hostname}</code>` : ""}. A version switch is a <code>deploy_site</code> approval card.</p>
<form method="post" action="/publish/propose" class="stack"><label class="field">Propose serving site version <input type="text" name="site_version" pattern="[a-z0-9._-]{1,64}" required></label><button class="secondary" type="submit">Create deploy_site card</button></form>
<h2>Publications</h2>
<table class="data"><tr><th>Path</th><th>Finding</th><th>Published</th><th>By</th><th>Content</th></tr>${publications.map((p) => html`<tr><td>${p.path}</td><td>${p.finding_id ?? ""}</td><td>${p.published_at}</td><td>${p.published_by}</td><td>${p.content_sha256.slice(0, 16)}</td></tr>`)}</table>`;
}

export function subscribers(events: SubscriberEventRow[], drafts: JobRow[], sends: ExternalActionRow[], brevo: BrevoSettings | null, notice: string | null): Safe {
  const latestCount = events.find((e) => e.kind === "list_count");
  const draftCard = (j: JobRow) => {
    const outputs = j.outputs_json ? (JSON.parse(j.outputs_json) as { draft?: Record<string, unknown>; subject?: unknown }) : null;
    const draft = (outputs?.draft ?? outputs) as Record<string, unknown> | null;
    const subject = draft && typeof draft.subject === "string" ? draft.subject : "(no draft yet)";
    const text = draft && typeof draft.text === "string" ? draft.text : "";
    const inputs = JSON.parse(j.inputs_json) as { trigger?: string; month?: string };
    return html`<div class="card"><h3>${subject} ${statePill(j.state)}</h3><p class="meta">${j.job_id} - ${inputs.trigger ?? "?"}${inputs.month ? " " + inputs.month : ""} - enqueued ${j.enqueued_at}${j.error ? html`<br><span class="pill bad">${j.error}</span>` : ""}</p>
${text ? html`<pre class="json">${text.slice(0, 3000)}</pre>` : ""}
${j.state === "done" && draft ? html`<form method="post" action="/subscribers/drafts/${j.job_id}/propose"><button class="primary" type="submit">Approve &amp; send (creates a send_newsletter card)</button></form>` : html`<p class="meta">The runner builds the draft; it appears here when the job is done.</p>`}</div>`;
  };
  return html`<h1>Subscribers</h1>
${notice ? html`<p class="notice">${notice}</p>` : ""}
<p>Subscriber identities stay with the newsletter provider. This screen shows counts and events only; the workspace never imports, exports or edits contacts. ${brevo?.list_id ? html`Sending to Brevo list <code>${brevo.list_id}</code>.` : html`No Brevo list configured yet (<a href="/settings">Settings</a>).`}</p>
<div class="cards"><div class="card stat"><strong>${latestCount ? String((JSON.parse(latestCount.payload_json) as { count?: number }).count ?? "?") : "-"}</strong><span class="meta">Latest list count${latestCount ? " at " + latestCount.occurred_at : ""}</span></div></div>
<h2>Newsletter drafts</h2>
<p>A draft is built on the first run of each month and after every publication, from published findings and upcoming meetings only. Approve &amp; send creates a <code>send_newsletter</code> card; the send happens only when that card is approved and executed.</p>
<div class="cards">${drafts.map(draftCard)}</div>${drafts.length ? "" : html`<p>No drafts yet.</p>`}
<h2>Sends</h2><table class="data"><tr><th>Card</th><th>State</th><th>Subject</th><th>Receipt</th></tr>${sends.map((a) => html`<tr><td>${a.action_id}</td><td>${statePill(a.state)}</td><td>${(JSON.parse(a.proposal_json) as { subject?: string }).subject ?? ""}</td><td>${a.provider_receipt ?? a.error ?? ""}</td></tr>`)}</table>
<h2>Events</h2><table class="data"><tr><th>When</th><th>Provider</th><th>Kind</th><th>Payload</th></tr>${events.map((e) => html`<tr><td>${e.occurred_at}</td><td>${e.provider}</td><td>${e.kind}</td><td>${e.payload_json}</td></tr>`)}</table>`;
}

export function meetings(rows: MeetingRow[], notice: string | null): Safe {
  return html`<h1>Meetings</h1>
${notice ? html`<p class="notice">${notice}</p>` : ""}
<p>Meetings come from the Legistar import (<code>campaign_tool meetings</code>, kit/meetings.json) or are added here. A comment kit is generated from published findings only and is rendered on the public meetings page after the next build.</p>
<table class="data"><tr><th>Body</th><th>Starts</th><th>Agenda item</th><th>Relevance</th><th>RSVPs</th><th>Source</th><th>Comment kit</th></tr>${rows.map((m) => html`<tr><td>${m.body_name}</td><td>${m.starts_at}</td><td>${m.agenda_url ? html`<a href="${m.agenda_url}" rel="noopener">${m.agenda_item ?? "agenda"}</a>` : m.agenda_item ?? ""}</td><td>${m.relevance ?? ""}</td><td>${m.rsvp_count}</td><td>${m.source ?? ""}</td><td><form method="post" action="/meetings/${m.meeting_id}/comment-kit"><button class="secondary" type="submit">${m.comment_kit_md ? "Regenerate kit" : "Generate kit"}</button></form></td></tr>${m.comment_kit_md ? html`<tr><td colspan="7"><details><summary>Comment kit (Markdown)</summary><pre class="json">${m.comment_kit_md}</pre></details></td></tr>` : ""}`)}</table>
<h2>Add a meeting</h2>
<form method="post" action="/meetings" class="stack"><label class="field">Body name<input type="text" name="body_name" required></label><label class="field">Starts (ISO-8601)<input type="text" name="starts_at" required placeholder="2026-10-14T18:00:00-07:00"></label><label class="field">Agenda URL (https)<input type="url" name="agenda_url"></label><label class="field">Agenda item<input type="text" name="agenda_item"></label><label class="field">Agency id (optional)<input type="text" name="agency_id" pattern="[a-z]{2}-[a-z0-9-]{1,80}"></label><label class="field">Relevance<select name="relevance"><option>alpr_item</option><option>budget</option><option>consent_calendar</option><option>none</option></select></label><button class="primary" type="submit">Add</button></form>`;
}

export interface SecretPresence {
  name: string;
  present: boolean;
  note: string;
}

export const SIGNUP_TEST_CHECKLIST = [
  "Use exactly one approved test address that you control; never a subscriber's or a colleague's.",
  "Submit the hosted signup form from the public site and confirm the double opt-in email arrives.",
  "Confirm the welcome message, then check the contact shows in the Brevo list with consent recorded.",
  "Unsubscribe from that message and confirm the contact is marked unsubscribed in Brevo.",
  "Do not reactivate, re-import or re-subscribe any suppressed or unsubscribed contact; suppression stays in Brevo.",
  "Record the date, the address's last four characters and the result in the launch receipt; then delete the test contact.",
] as const;

export function settings(campaign: CampaignRow | null, rows: SettingRow[], secrets: SecretPresence[], notice: string | null, brevo: BrevoSettings | null): Safe {
  return html`<h1>Settings</h1>
${notice ? html`<p class="notice">${raw(notice)}</p>` : ""}
<h2>Newsletter (Brevo, non-secret settings)</h2>
<form method="post" action="/settings/brevo" class="stack"><p>The API key is a Worker secret (<code>BREVO_API_KEY</code>); nothing here reads or writes contacts. The hosted form URL sets <code>signup.mode = brevo_hosted</code> on the public site.</p>
<label class="field">List id<input type="number" name="list_id" min="1" value="${brevo?.list_id ?? ""}"></label>
<label class="field">Hosted signup form URL (https://*.sibforms.com/...)<input type="url" name="form_url" value="${brevo?.form_url ?? ""}"></label>
<label class="field">Sender name<input type="text" name="sender_name" value="${brevo?.sender_name ?? ""}"></label>
<label class="field">Sender address (must be a verified Brevo sender)<input type="email" name="sender_email" value="${brevo?.sender_email ?? ""}"></label>
<label class="field">Consent footer (your reviewed wording: why the reader receives this, who sends it and from where; must keep the sentence "Unsubscribe at any time." — drafts without a campaign footer carry a template marker and cannot be sent)<textarea name="consent_footer" rows="3" maxlength="600">${brevo?.consent_footer ?? ""}</textarea></label>
<button class="secondary" type="submit">Save Brevo settings</button>${brevo?.updated_at ? html`<span class="meta"> saved ${brevo.updated_at} by ${brevo.updated_by ?? ""}</span>` : ""}</form>
<h3>Test signup flow (docs/CLOUDFLARE.md step 9)</h3>
<div class="checklist">${SIGNUP_TEST_CHECKLIST.map((item, i) => html`<label><input type="checkbox" name="signup_check_${i}"> ${item}</label>`)}</div>
<h2>Backup</h2>
<form method="post" action="/api/backup" class="stack"><p>Enqueues a <code>backup</code> job: the runner pulls <code>/api/runner/export.json</code> and every original by hash into one verified tar (docs/BACKUP.md). Organizers can also download <a href="/api/export.json">the D1 export</a> directly.</p><button class="secondary" type="submit">Queue backup job</button></form>
<h2>Secrets (presence only; values are never displayed)</h2>
<table class="data"><tr><th>Secret</th><th>Set</th><th>Purpose</th></tr>${secrets.map((s) => html`<tr><td><code>${s.name}</code></td><td>${s.present ? html`<span class="pill ok">present</span>` : html`<span class="pill bad">missing</span>`}</td><td>${s.note}</td></tr>`)}</table>
<h2>Runner token</h2>
<form method="post" action="/settings/rotate-runner-token" class="stack"><p>Rotating generates a new token, stores its SHA-256 fingerprint here and shows the value once. You must then set it as the Worker secret <code>RUNNER_TOKEN</code> and on the runner; the old token stops working when the secret is updated.</p><button class="danger" type="submit">Rotate runner token</button></form>
<h2>Campaign</h2>${campaign ? jsonBlock(campaign) : html`<p>No campaign row.</p>`}
<h2>Settings rows</h2><table class="data"><tr><th>Key</th><th>Value</th><th>Updated</th></tr>${rows.map((s) => html`<tr><td>${s.key}</td><td>${s.value_json}</td><td>${s.updated_at}</td></tr>`)}</table>`;
}
