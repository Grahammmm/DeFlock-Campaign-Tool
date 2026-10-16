// Workspace Worker: Access-protected organizer UI + API over D1/R2, runner API, cron and
// inbound mail. See docs/WORKERS.md. No code path here sends mail, submits a request or
// publishes without an approved external_action row.
import { Hono } from "hono";
import { HEX64, newFindingId, newMeetingId, nowIso, randomHex, sha256Hex } from "@deflock/shared/ids";
import { html } from "@deflock/shared/html";
import { contentHash, type JsonObject } from "@deflock/shared/review";
import { ApprovalError, approveAction, editAction, executeAction, rejectAction, reconcileAction, holdInterruptedAction } from "./approvals.ts";
import { AuthError, requireIdentity, requireRunner, type AccessIdentity } from "./auth.ts";
import { commentKitMarkdown, kitFinding } from "./comment_kit.ts";
import { runScheduled } from "./cron.ts";
import { Repo } from "./db.ts";
import type { Env } from "./env.ts";
import { exportResponse } from "./export.ts";
import { UNSUBSCRIBE_PLACEHOLDER } from "./executors/send_newsletter.ts";
import { emailHandler } from "./mail.ts";
import { assembleManifest, type BrevoSettings } from "./manifest.ts";
import { issueCorrection, proposePublication, submitReview, withdrawFinding, WorkflowError } from "./publication.ts";
import { recomputeState } from "./review_state.ts";
import { runnerApi } from "./runner.ts";
import { signDownload, verifyDownload } from "./signing.ts";
import { page } from "./views/layout.ts";
import * as V from "./views/screens.ts";

type Vars = { identity: AccessIdentity; repo: Repo };

function wantsJson(c: { req: { header(name: string): string | undefined } }): boolean {
  return Boolean(c.req.header("accept")?.includes("application/json"));
}

const app = new Hono<{ Bindings: Env; Variables: Vars }>();

app.onError((err, c) => {
  if (err instanceof AuthError) return c.json({ error: err.message }, err.status as 401);
  if (err instanceof ApprovalError) return c.json({ error: err.message }, err.status as 409);
  if (err instanceof WorkflowError) return c.json({ error: err.message }, err.status as 409);
  console.error(err);
  return c.json({ error: "internal error" }, 500);
});

// Runner API: bearer token, no Access identity.
// The D1 export is also served to the runner so a `backup` job can pull it without Access.
// That makes RUNNER_TOKEN a whole-database credential (review receipts with reviewer
// emails, correspondence, proposals, settings), not only a job-queue one: SECURITY.md and
// docs/WORKERS.md say so, and rotating it from Settings is the response to any leak.
app.get("/api/runner/export.json", (c) => {
  requireRunner(c.req.raw, c.env);
  return exportResponse(new Repo(c.env.DB, c.env.CAMPAIGN_ID), c.env.CAMPAIGN_ID);
});
app.route("/api/runner", runnerApi);

// Signed downloads: HMAC link minted by an authenticated organizer; no Access on this path.
app.get("/dl/:sha256", async (c) => {
  const sha = c.req.param("sha256");
  const exp = Number(c.req.query("exp"));
  const sig = c.req.query("sig") ?? "";
  if (!HEX64.test(sha) || !(await verifyDownload(c.env.DOWNLOAD_SIGNING_KEY, sha, exp, sig))) return c.text("link invalid or expired", 403);
  const repo = new Repo(c.env.DB, c.env.CAMPAIGN_ID);
  const row = await repo.original(sha);
  const obj = row ? await c.env.ORIGINALS.get(row.r2_key) : null;
  if (!row || !obj) return c.text("not found", 404);
  return new Response(obj.body, {
    headers: {
      "content-type": row.media_type ?? "application/octet-stream",
      "content-disposition": `attachment; filename="${sha}"`,
      "cache-control": "private, no-store",
      "x-object-sha256": sha,
    },
  });
});

app.get("/healthz", (c) => c.json({ ok: true, campaign: Boolean(c.env.CAMPAIGN_ID) }));

// Cross-site request forgery: Access authenticates with an ambient cookie, so a
// state-changing request must come from this origin. Browsers always send Origin (and
// Sec-Fetch-Site) on cross-site POSTs; a request that carries neither header is not a
// browser cross-site request and passes to the identity check.
app.use("*", async (c, next) => {
  if (!["GET", "HEAD", "OPTIONS"].includes(c.req.method)) {
    const site = c.req.header("sec-fetch-site");
    if (site && site !== "same-origin" && site !== "none") throw new AuthError("cross-site request refused", 403);
    const origin = c.req.header("origin");
    if (origin && origin !== new URL(c.req.url).origin) throw new AuthError("cross-site request refused", 403);
  }
  await next();
});

// Everything else requires a verified Access JWT.
app.use("*", async (c, next) => {
  const identity = await requireIdentity(c.req.raw, c.env);
  c.set("identity", identity);
  c.set("repo", new Repo(c.env.DB, c.env.CAMPAIGN_ID));
  await next();
});

async function render(c: { env: Env; var: Vars; req: { path: string } }, title: string, body: import("@deflock/shared/html").Safe): Promise<Response> {
  const campaign = await c.var.repo.campaign();
  return new Response(page(title, campaign?.name ?? "Workspace", c.req.path, c.var.identity, body), {
    headers: { "content-type": "text/html; charset=utf-8", "cache-control": "private, no-store", "x-frame-options": "DENY", "content-security-policy": "default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:; form-action 'self'; base-uri 'none'; frame-ancestors 'none'" },
  });
}

app.get("/", async (c) => {
  const repo = c.var.repo;
  return render(c, "Dashboard", V.dashboard(await repo.campaign(), await repo.counts(), await repo.incidents(), await repo.runs(), await repo.jobs(30)));
});

app.get("/api/me", (c) => c.json({ email: c.var.identity.email, campaign_id: c.env.CAMPAIGN_ID }));

// Requests --------------------------------------------------------------------------
app.get("/requests", async (c) => {
  const agencies = new Map((await c.var.repo.agencies()).map((a) => [a.agency_id, a]));
  return render(c, "Requests", V.requestsList(await c.var.repo.requests(), agencies));
});

app.get("/requests/:id", async (c) => {
  const r = await c.var.repo.request(c.req.param("id"));
  if (!r) return c.text("not found", 404);
  const jobs = await c.var.repo.jobs(200);
  const followup = jobs.find((j) => j.kind === "draft_followup" && (JSON.parse(j.inputs_json) as { request_id?: string }).request_id === r.request_id) ?? null;
  return render(c, "Request", V.requestDetail(r, await c.var.repo.agency(r.agency_id), await c.var.repo.correspondenceFor(r.request_id), followup));
});

app.post("/requests/:id/draft-followup", async (c) => {
  const r = await c.var.repo.request(c.req.param("id"));
  if (!r) return c.text("not found", 404);
  const key = await sha256Hex("draft_followup:" + r.request_id + ":" + nowIso().slice(0, 10) + ":" + c.var.identity.email);
  const job = await c.var.repo.enqueueJob("draft_followup", key, { request_id: r.request_id, requested_by: c.var.identity.email });
  if (c.req.header("accept")?.includes("application/json")) return c.json({ job_id: job.row.job_id, created: job.inserted });
  return c.redirect("/requests/" + r.request_id);
});

// Inbox -----------------------------------------------------------------------------
app.get("/inbox", async (c) => {
  const requests = new Map((await c.var.repo.requests()).map((r) => [r.request_id, r]));
  const agencies = new Map((await c.var.repo.agencies()).map((a) => [a.agency_id, a]));
  return render(c, "Inbox", V.inbox(await c.var.repo.inbox(), requests, agencies));
});

// Records ---------------------------------------------------------------------------
app.get("/records", async (c) => render(c, "Records", V.records(await c.var.repo.originals())));

async function signedLink(c: { env: Env; req: { url: string } }, sha: string): Promise<string> {
  const exp = Math.floor(Date.now() / 1000) + 600;
  const sig = await signDownload(c.env.DOWNLOAD_SIGNING_KEY, sha, exp);
  return new URL(`/dl/${sha}?exp=${exp}&sig=${sig}`, c.req.url).toString();
}

app.get("/records/:sha256", async (c) => {
  const sha = c.req.param("sha256");
  const o = await c.var.repo.original(sha);
  if (!o) return c.text("not found", 404);
  return render(c, "Original", V.recordDetail(o, await c.var.repo.receiptsFor(sha), null));
});

app.post("/records/:sha256/link", async (c) => {
  const sha = c.req.param("sha256");
  const o = await c.var.repo.original(sha);
  if (!o) return c.text("not found", 404);
  const link = await signedLink(c, sha);
  if (c.req.header("accept")?.includes("application/json")) return c.json({ url: link, expires_in: 600 });
  return render(c, "Original", V.recordDetail(o, await c.var.repo.receiptsFor(sha), link));
});

// Findings --------------------------------------------------------------------------
app.get("/findings", async (c) => {
  const rows = [];
  for (const finding of await c.var.repo.findings()) rows.push({ finding, receipts: await c.var.repo.reviewReceipts(finding.finding_id) });
  return render(c, "Findings", await V.findings(rows));
});

app.get("/findings/:id", async (c) => {
  const f = await c.var.repo.finding(c.req.param("id"));
  if (!f) return c.text("not found", 404);
  const receipts = await c.var.repo.reviewReceipts(f.finding_id);
  const { blockers } = await recomputeState(f, receipts);
  const publication = await c.var.repo.publicationForFinding(f.finding_id);
  const corrections = publication ? await c.var.repo.correctionsFor(publication.publication_id) : [];
  const actions = (await c.var.repo.actions()).filter((a) => a.subject_id === f.finding_id);
  return render(c, "Finding", V.findingDetail(f, receipts, blockers, c.var.identity.email, publication, corrections, actions, c.req.query("notice") ?? null));
});

app.post("/api/findings", async (c) => {
  const finding = (await c.req.json().catch(() => null)) as JsonObject | null;
  if (!finding || typeof finding !== "object") return c.json({ error: "finding object required" }, 400);
  const id = typeof finding.id === "string" ? finding.id : newFindingId();
  // The author is always the verified Access identity: reviewBlockers counts reviews from
  // anyone but the author as independent, so a client-supplied author could review its own work.
  const full: JsonObject = { ...finding, id, author: c.var.identity.email };
  const hash = await contentHash(full);
  const row = await c.var.repo.createFinding({
    finding_id: id,
    author: full.author as string,
    classification: String(full.classification ?? ""),
    confidence: String(full.confidence ?? "needs_attorney_review"),
    summary: String(full.summary ?? ""),
    finding_json: JSON.stringify(full),
    content_sha256: hash,
    state: "draft",
  });
  return c.json({ finding_id: row.finding_id, content_sha256: hash }, 201);
});

async function formOrJson(c: { req: { header(name: string): string | undefined; json(): Promise<unknown>; parseBody(): Promise<Record<string, unknown>> } }): Promise<Record<string, unknown>> {
  if (c.req.header("content-type")?.includes("json")) return ((await c.req.json().catch(() => ({}))) as Record<string, unknown>) ?? {};
  return c.req.parseBody();
}

app.post("/findings/:id/review", async (c) => {
  const f = await c.var.repo.finding(c.req.param("id"));
  if (!f) return c.text("not found", 404);
  const form = await formOrJson(c);
  // reviewer comes from the verified Access JWT, never from a form field
  const next = await submitReview(c.var.repo, f, c.var.identity, { role: String(form.role ?? ""), decision: String(form.decision ?? ""), rationale: String(form.rationale ?? "") });
  if (wantsJson(c)) return c.json({ finding_id: f.finding_id, ...next });
  return c.redirect("/findings/" + f.finding_id);
});

app.post("/findings/:id/propose", async (c) => {
  const f = await c.var.repo.finding(c.req.param("id"));
  if (!f) return c.text("not found", 404);
  const { row, inserted } = await proposePublication(c.var.repo, f, c.var.identity);
  if (wantsJson(c)) return c.json({ action_id: row.action_id, state: row.state, created: inserted }, inserted ? 201 : 200);
  return c.redirect("/approvals?notice=" + encodeURIComponent(`publish_finding ${row.action_id} ${inserted ? "proposed" : "already proposed"} for ${f.finding_id}`));
});

app.post("/findings/:id/correct", async (c) => {
  const f = await c.var.repo.finding(c.req.param("id"));
  if (!f) return c.text("not found", 404);
  const form = await formOrJson(c);
  const replacement = String(form.replacement_finding_id ?? "").trim() || null;
  const out = await issueCorrection(c.var.repo, f, c.var.identity, { reason: String(form.reason ?? ""), replacement_finding_id: replacement });
  if (wantsJson(c)) return c.json({ correction_id: out.correction.correction_id, build_job_id: out.build_job_id, deploy_action_id: out.deploy_action_id }, 201);
  return c.redirect("/findings/" + f.finding_id + "?notice=" + encodeURIComponent(`correction recorded; build job ${out.build_job_id} queued and deploy card ${out.deploy_action_id} proposed`));
});

app.post("/findings/:id/withdraw", async (c) => {
  const f = await c.var.repo.finding(c.req.param("id"));
  if (!f) return c.text("not found", 404);
  const form = await formOrJson(c);
  const out = await withdrawFinding(c.var.repo, f, c.var.identity, String(form.reason ?? ""));
  if (wantsJson(c)) return c.json({ finding_id: f.finding_id, state: "withdrawn", ...out });
  return c.redirect("/findings/" + f.finding_id + "?notice=" + encodeURIComponent(`withdrawn; build job ${out.build_job_id} queued and deploy card ${out.deploy_action_id} proposed`));
});

// Approvals -------------------------------------------------------------------------
app.get("/approvals", async (c) => render(c, "Approvals", V.approvals(await c.var.repo.actions(), c.req.query("notice") ?? null)));

app.post("/approvals/:id/approve", async (c) => {
  const row = await approveAction(c.var.repo, c.req.param("id"), c.var.identity);
  return wantsJson(c) ? c.json(row) : c.redirect("/approvals?notice=" + encodeURIComponent(`${row.kind} ${row.action_id} approved by ${row.approved_by}`));
});

app.post("/approvals/:id/reject", async (c) => {
  const body = c.req.header("content-type")?.includes("json") ? ((await c.req.json()) as { reason?: string }) : (await c.req.parseBody());
  const row = await rejectAction(c.var.repo, c.req.param("id"), c.var.identity, String(body.reason ?? ""));
  return wantsJson(c) ? c.json(row) : c.redirect("/approvals?notice=" + encodeURIComponent(`${row.kind} ${row.action_id} rejected`));
});

app.post("/approvals/:id/edit", async (c) => {
  let proposal: unknown;
  if (c.req.header("content-type")?.includes("json")) proposal = await c.req.json();
  else {
    const form = await c.req.parseBody();
    try {
      proposal = JSON.parse(String(form.proposal ?? "{}"));
    } catch {
      return c.text("proposal must be JSON", 400);
    }
  }
  const row = await editAction(c.var.repo, c.req.param("id"), c.var.identity, proposal);
  return wantsJson(c) ? c.json(row) : c.redirect("/approvals?notice=" + encodeURIComponent(`${row.kind} ${row.action_id} edited; approve again to proceed`));
});

app.post("/approvals/:id/reconcile", async (c) => {
  const body = c.req.header("content-type")?.includes("json") ? await c.req.json() : await c.req.parseBody();
  const row = await reconcileAction(c.var.repo, c.req.param("id"), c.var.identity, body.outcome, body.reference, body.delivered_at, body.quiescence_reference, body.quiescence_confirmed, body.provider_message_id);
  return wantsJson(c) ? c.json(row) : c.redirect("/approvals?notice=" + encodeURIComponent(`${row.kind} ${row.action_id} reconciled as ${row.state}; no message sent`));
});

app.post("/approvals/:id/hold-execution", async (c) => {
  const body = c.req.header("content-type")?.includes("json") ? await c.req.json() : await c.req.parseBody();
  const row = await holdInterruptedAction(c.var.repo, c.req.param("id"), c.var.identity, body.execution_id, body.reference);
  return wantsJson(c) ? c.json(row) : c.redirect("/approvals?notice=" + encodeURIComponent("Execution held for provider and invocation checks; no message sent or cancellation claimed"));
});

app.get("/approvals/:id/execution-evidence", async (c) => {
  const action = await c.var.repo.action(c.req.param("id"));
  if (!action) return c.json({ error: "unknown action" }, 404);
  const evidence = await c.var.repo.actionExecutionEvidence(action.action_id);
  return c.json({ action_id: action.action_id, state: action.state, provider_receipt: action.provider_receipt,
    evidence: evidence.slice(0, 50), truncated: evidence.length > 50 });
});

app.post("/approvals/:id/execute", async (c) => {
  const row = await executeAction(c.var.repo, c.env, c.req.param("id"), c.var.identity);
  return wantsJson(c) ? c.json(row) : c.redirect("/approvals?notice=" + encodeURIComponent(`${row.kind} ${row.action_id} is ${row.state}`));
});

// Publish ---------------------------------------------------------------------------
app.get("/publish", async (c) =>
  render(c, "Publish", V.publish(await c.var.repo.publications(), await c.var.repo.setting<string>("site_version"), await c.var.repo.setting<string>("site_version_previous"), await c.var.repo.campaign())),
);

app.get("/api/manifest.json", async (c) => c.json(await assembleManifest(c.var.repo)));

/** Rebuild the site from the current manifest: enqueue build_site and propose deploy_site. */
app.post("/publish/rebuild", async (c) => {
  const { proposeRebuild } = await import("./manifest.ts");
  const out = await proposeRebuild(c.var.repo, c.var.identity.email, "manual rebuild");
  if (wantsJson(c)) return c.json({ job_id: out.job_id, job_created: out.job_created, action_id: out.action_id, action_created: out.action_created, site_version: out.manifest.site_version }, out.job_created ? 201 : 200);
  return c.redirect("/approvals?notice=" + encodeURIComponent(`build_site ${out.job_id} ${out.job_created ? "queued" : "already queued"}; deploy_site ${out.action_id} proposed for ${out.manifest.site_version}`));
});

app.post("/publish/propose", async (c) => {
  const form = c.req.header("content-type")?.includes("json") ? ((await c.req.json()) as Record<string, unknown>) : await c.req.parseBody();
  const version = String(form.site_version ?? "");
  if (!/^[a-z0-9._-]{1,64}$/.test(version)) return c.text("site_version invalid", 400);
  const { row } = await c.var.repo.propose("deploy_site", null, { site_version: version }, await sha256Hex("deploy_site:" + version), c.var.identity.email);
  return wantsJson(c) ? c.json(row, 201) : c.redirect("/approvals");
});

/** POST /api/deploy-site: executes an approved deploy_site card (flips site_version). */
app.post("/api/deploy-site", async (c) => {
  const body = (await c.req.json().catch(() => ({}))) as { action_id?: string };
  if (!body.action_id) return c.json({ error: "action_id of an approved deploy_site card required" }, 400);
  const action = await c.var.repo.action(body.action_id);
  if (!action || action.kind !== "deploy_site") return c.json({ error: "not a deploy_site action" }, 404);
  const row = await executeAction(c.var.repo, c.env, action.action_id, c.var.identity);
  return c.json({ action_id: row.action_id, state: row.state, site_version: await c.var.repo.setting("site_version"), provider_receipt: row.provider_receipt });
});

// Subscribers / meetings ------------------------------------------------------------
app.get("/subscribers", async (c) => {
  const drafts = await c.var.repo.jobsByKind("newsletter_draft", 20);
  const sends = (await c.var.repo.actions()).filter((a) => a.kind === "send_newsletter");
  return render(c, "Subscribers", V.subscribers(await c.var.repo.subscriberEvents(), drafts, sends, await c.var.repo.setting<BrevoSettings>("brevo"), c.req.query("notice") ?? null));
});

/** Approve & send: turns a finished newsletter_draft job into a send_newsletter card. */
app.post("/subscribers/drafts/:job_id/propose", async (c) => {
  const job = await c.var.repo.job(c.req.param("job_id"));
  if (!job || job.kind !== "newsletter_draft") return c.text("not a newsletter draft", 404);
  if (job.state !== "done" || !job.outputs_json) return c.text("draft job is " + job.state + "; wait for the runner", 409);
  const outputs = JSON.parse(job.outputs_json) as { draft?: Record<string, unknown>; subject?: unknown; html?: unknown; text?: unknown };
  const draft = (outputs.draft ?? outputs) as Record<string, unknown>;
  if (typeof draft.subject !== "string" || typeof draft.html !== "string") return c.text("draft outputs need subject and html", 409);
  if (!draft.html.includes(UNSUBSCRIBE_PLACEHOLDER)) return c.text("draft html lacks the Brevo unsubscribe placeholder", 409);
  const brevo = await c.var.repo.setting<BrevoSettings>("brevo");
  const { row, inserted } = await c.var.repo.propose(
    "send_newsletter",
    job.job_id,
    { subject: draft.subject, html: draft.html, text: typeof draft.text === "string" ? draft.text : "", list_id: brevo?.list_id ?? null, draft_job_id: job.job_id },
    await sha256Hex("send_newsletter:" + job.job_id + ":" + (await sha256Hex(draft.html))),
    c.var.identity.email,
  );
  if (wantsJson(c)) return c.json({ action_id: row.action_id, state: row.state, created: inserted }, inserted ? 201 : 200);
  return c.redirect("/approvals?notice=" + encodeURIComponent(`send_newsletter ${row.action_id} ${inserted ? "proposed" : "already proposed"}; approve, then execute to send`));
});

app.get("/meetings", async (c) => render(c, "Meetings", V.meetings(await c.var.repo.meetings(), c.req.query("notice") ?? null)));

app.post("/meetings", async (c) => {
  const form = await c.req.parseBody();
  const starts = String(form.starts_at ?? "");
  if (!String(form.body_name ?? "").trim() || Number.isNaN(Date.parse(starts))) return c.text("body_name and a valid starts_at required", 400);
  const agendaUrl = String(form.agenda_url ?? "").trim();
  if (agendaUrl && !/^https:\/\/[^\s"'<>]+$/.test(agendaUrl)) return c.text("agenda_url must be https", 400);
  await c.var.repo.createMeeting({
    meeting_id: newMeetingId(),
    body_name: String(form.body_name).trim(),
    agency_id: String(form.agency_id ?? "").trim() || null,
    starts_at: new Date(starts).toISOString(),
    agenda_url: agendaUrl || null,
    agenda_item: String(form.agenda_item ?? "") || null,
    relevance: String(form.relevance ?? "") || null,
    comment_kit_md: null,
    rsvp_count: 0,
    source: "manual",
  });
  return c.redirect("/meetings");
});

/** Comment kit from published findings only; stored as Markdown text on the meeting row. */
app.post("/meetings/:id/comment-kit", async (c) => {
  const m = await c.var.repo.meeting(c.req.param("id"));
  if (!m) return c.text("not found", 404);
  const campaign = await c.var.repo.campaign();
  const findings = [];
  for (const f of await c.var.repo.findingsByState(["published"])) {
    const pub = await c.var.repo.publicationForFinding(f.finding_id);
    if (pub) findings.push(kitFinding(f, pub));
  }
  const md = commentKitMarkdown(m, findings, campaign?.name ?? "our campaign", campaign?.public_hostname ? "https://" + campaign.public_hostname : null);
  await c.var.repo.updateMeeting(m.meeting_id, { comment_kit_md: md });
  if (wantsJson(c)) return c.json({ meeting_id: m.meeting_id, comment_kit_md: md, findings: findings.length });
  return c.redirect("/meetings?notice=" + encodeURIComponent(`comment kit generated for ${m.body_name} from ${findings.length} published finding(s)`));
});

// Backup / export ---------------------------------------------------------------------
app.get("/api/export.json", (c) => exportResponse(c.var.repo, c.env.CAMPAIGN_ID));

app.post("/api/backup", async (c) => {
  const day = nowIso().slice(0, 10);
  const { row, inserted } = await c.var.repo.enqueueJob("backup", await sha256Hex("backup:" + day + ":" + c.var.identity.email), { requested_by: c.var.identity.email, day });
  if (wantsJson(c) || c.req.header("content-type")?.includes("json")) return c.json({ job_id: row.job_id, created: inserted, state: row.state }, inserted ? 201 : 200);
  return c.redirect("/settings?notice=" + encodeURIComponent(`backup job ${row.job_id} ${inserted ? "queued" : "already queued today"}`));
});

// Settings --------------------------------------------------------------------------
function secretPresence(env: Env): V.SecretPresence[] {
  return [
    { name: "RUNNER_TOKEN", present: Boolean(env.RUNNER_TOKEN), note: "Bearer token the runner presents to /api/runner/*" },
    { name: "DOWNLOAD_SIGNING_KEY", present: Boolean(env.DOWNLOAD_SIGNING_KEY), note: "HMAC key for short-lived original download links" },
    { name: "ACCESS_AUD", present: Boolean(env.ACCESS_AUD), note: "Access application audience tag" },
    { name: "ACCESS_TEAM_DOMAIN", present: Boolean(env.ACCESS_TEAM_DOMAIN), note: "Access team domain used to fetch signing certificates" },
    { name: "BREVO_API_KEY", present: Boolean(env.BREVO_API_KEY), note: "Brevo v3 key used only by the send_newsletter executor (campaign create + send)" },
  ];
}

app.get("/settings", async (c) => {
  const notice = c.req.query("notice");
  return render(c, "Settings", V.settings(await c.var.repo.campaign(), await c.var.repo.settings(), secretPresence(c.env), notice ? html`${notice}`.value : null, await c.var.repo.setting<BrevoSettings>("brevo")));
});

/** Non-secret Brevo settings: list id, hosted form URL, sender. The API key is a Worker secret. */
app.post("/settings/brevo", async (c) => {
  const form = await formOrJson(c);
  const listRaw = String(form.list_id ?? "").trim();
  const listId = listRaw ? Number(listRaw) : null;
  if (listId !== null && (!Number.isInteger(listId) || listId <= 0)) return c.text("list_id must be a positive integer", 400);
  const formUrl = String(form.form_url ?? "").trim() || null;
  if (formUrl) {
    let host = "";
    try {
      const u = new URL(formUrl);
      host = u.protocol === "https:" ? u.hostname.toLowerCase() : "";
    } catch {
      host = "";
    }
    if (!host || !(host.endsWith(".sibforms.com") && host !== "sibforms.com")) return c.text("form_url must be an https://*.sibforms.com hosted form", 400);
  }
  const senderEmail = String(form.sender_email ?? "").trim() || null;
  if (senderEmail && !/^[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,24}$/.test(senderEmail)) return c.text("sender_email invalid", 400);
  const consentFooter = String(form.consent_footer ?? "").replace(/[\u0000-\u0008\u000b-\u001f\u007f]/g, "").trim().slice(0, 600) || null;
  if (consentFooter && !consentFooter.includes("Unsubscribe at any time.")) return c.text("consent_footer must keep the sentence 'Unsubscribe at any time.' (the provider unsubscribe link is inserted there)", 400);
  const value: BrevoSettings = { list_id: listId, form_url: formUrl, sender_name: String(form.sender_name ?? "").trim() || null, sender_email: senderEmail, consent_footer: consentFooter, updated_by: c.var.identity.email, updated_at: nowIso() };
  await c.var.repo.putSetting("brevo", value);
  if (wantsJson(c)) return c.json(value);
  return c.redirect("/settings?notice=" + encodeURIComponent("Brevo settings saved (no contacts were read or written)"));
});

app.post("/settings/rotate-runner-token", async (c) => {
  const token = "rt_" + randomHex(32);
  const fingerprint = await sha256Hex(token);
  await c.var.repo.putSetting("runner_token_fingerprint", { sha256: fingerprint, rotated_by: c.var.identity.email, rotated_at: nowIso(), applied: false });
  if (wantsJson(c)) return c.json({ token, fingerprint });
  const notice = html`New runner token (shown once, not stored): <code>${token}</code><br>Set it as the Worker secret RUNNER_TOKEN and on the runner. Fingerprint ${fingerprint.slice(0, 16)}.`;
  return render(c, "Settings", V.settings(await c.var.repo.campaign(), await c.var.repo.settings(), secretPresence(c.env), notice.value, await c.var.repo.setting<BrevoSettings>("brevo")));
});

export default {
  fetch: app.fetch,
  async scheduled(event: ScheduledController, env: Env, ctx: ExecutionContext): Promise<void> {
    ctx.waitUntil(runScheduled(env, event.scheduledTime, "cron"));
  },
  async email(message: ForwardableEmailMessage, env: Env): Promise<void> {
    await emailHandler(message, env);
  },
} satisfies ExportedHandler<Env>;
