// Workspace Worker: Access-protected organizer UI + API over D1/R2, runner API, cron and
// inbound mail. See docs/WORKERS.md. No code path here sends mail, submits a request or
// publishes without an approved external_action row.
import { Hono } from "hono";
import { HEX64, newFindingId, newMeetingId, newReviewId, nowIso, randomHex, sha256Hex } from "@deflock/shared/ids";
import { html } from "@deflock/shared/html";
import { contentHash, reviewBlockers, type JsonObject } from "@deflock/shared/review";
import { ApprovalError, approveAction, editAction, executeAction, rejectAction } from "./approvals.ts";
import { AuthError, requireIdentity, type AccessIdentity } from "./auth.ts";
import { runScheduled } from "./cron.ts";
import { Repo } from "./db.ts";
import type { Env } from "./env.ts";
import { emailHandler } from "./mail.ts";
import { runnerApi } from "./runner.ts";
import { signDownload, verifyDownload } from "./signing.ts";
import { page } from "./views/layout.ts";
import * as V from "./views/screens.ts";

type Vars = { identity: AccessIdentity; repo: Repo };

const app = new Hono<{ Bindings: Env; Variables: Vars }>();

app.onError((err, c) => {
  if (err instanceof AuthError) return c.json({ error: err.message }, err.status as 401);
  if (err instanceof ApprovalError) return c.json({ error: err.message }, err.status as 409);
  console.error(err);
  return c.json({ error: "internal error" }, 500);
});

// Runner API: bearer token, no Access identity.
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
  const blockers = await reviewBlockers(JSON.parse(f.finding_json) as JsonObject, receipts.map((r) => ({ content_sha256: r.content_sha256, decision: r.decision, reviewer: r.reviewer, role: r.role, rationale: r.rationale, reviewed_at: r.reviewed_at })));
  return render(c, "Finding", V.findingDetail(f, receipts, blockers, c.var.identity.email));
});

app.post("/api/findings", async (c) => {
  const finding = (await c.req.json().catch(() => null)) as JsonObject | null;
  if (!finding || typeof finding !== "object") return c.json({ error: "finding object required" }, 400);
  const id = typeof finding.id === "string" ? finding.id : newFindingId();
  const full: JsonObject = { ...finding, id, author: typeof finding.author === "string" ? finding.author : c.var.identity.email };
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

app.post("/findings/:id/review", async (c) => {
  const f = await c.var.repo.finding(c.req.param("id"));
  if (!f) return c.text("not found", 404);
  const form = await c.req.parseBody();
  const role = String(form.role ?? "");
  const decision = String(form.decision ?? "");
  const rationale = String(form.rationale ?? "").trim();
  if (!["factual", "legal", "privacy"].includes(role) || !["approve", "reject", "changes_requested"].includes(decision) || !rationale) return c.text("role, decision and rationale required", 400);
  await c.var.repo.createReviewReceipt({
    review_id: newReviewId(),
    finding_id: f.finding_id,
    content_sha256: f.content_sha256,
    reviewer: c.var.identity.email, // from the verified Access JWT, never a form field
    role,
    decision,
    rationale,
    reviewed_at: nowIso(),
  });
  const receipts = await c.var.repo.reviewReceipts(f.finding_id);
  const blockers = await reviewBlockers(JSON.parse(f.finding_json) as JsonObject, receipts.map((r) => ({ content_sha256: r.content_sha256, decision: r.decision, reviewer: r.reviewer, role: r.role, rationale: r.rationale, reviewed_at: r.reviewed_at })));
  await c.var.repo.updateFinding(f.finding_id, { state: blockers.length ? (blockers.includes("unresolved_challenge") ? "blocked" : "in_review") : "ready" });
  return c.redirect("/findings/" + f.finding_id);
});

// Approvals -------------------------------------------------------------------------
app.get("/approvals", async (c) => render(c, "Approvals", V.approvals(await c.var.repo.actions(), c.req.query("notice") ?? null)));

function wantsJson(c: { req: { header(name: string): string | undefined } }): boolean {
  return Boolean(c.req.header("accept")?.includes("application/json"));
}

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

app.post("/approvals/:id/execute", async (c) => {
  const row = await executeAction(c.var.repo, c.env, c.req.param("id"), c.var.identity);
  return wantsJson(c) ? c.json(row) : c.redirect("/approvals?notice=" + encodeURIComponent(`${row.kind} ${row.action_id} is ${row.state}`));
});

// Publish ---------------------------------------------------------------------------
app.get("/publish", async (c) =>
  render(c, "Publish", V.publish(await c.var.repo.publications(), await c.var.repo.setting<string>("site_version"), await c.var.repo.setting<string>("site_version_previous"), await c.var.repo.campaign())),
);

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
app.get("/subscribers", async (c) => render(c, "Subscribers", V.subscribers(await c.var.repo.subscriberEvents())));

app.get("/meetings", async (c) => render(c, "Meetings", V.meetings(await c.var.repo.meetings())));

app.post("/meetings", async (c) => {
  const form = await c.req.parseBody();
  const starts = String(form.starts_at ?? "");
  if (!String(form.body_name ?? "").trim() || Number.isNaN(Date.parse(starts))) return c.text("body_name and a valid starts_at required", 400);
  await c.var.repo.createMeeting({
    meeting_id: newMeetingId(),
    body_name: String(form.body_name).trim(),
    agency_id: null,
    starts_at: new Date(starts).toISOString(),
    agenda_url: String(form.agenda_url ?? "") || null,
    agenda_item: String(form.agenda_item ?? "") || null,
    relevance: String(form.relevance ?? "") || null,
    comment_kit_md: null,
    rsvp_count: 0,
    source: "manual",
  });
  return c.redirect("/meetings");
});

// Settings --------------------------------------------------------------------------
function secretPresence(env: Env): V.SecretPresence[] {
  return [
    { name: "RUNNER_TOKEN", present: Boolean(env.RUNNER_TOKEN), note: "Bearer token the runner presents to /api/runner/*" },
    { name: "DOWNLOAD_SIGNING_KEY", present: Boolean(env.DOWNLOAD_SIGNING_KEY), note: "HMAC key for short-lived original download links" },
    { name: "ACCESS_AUD", present: Boolean(env.ACCESS_AUD), note: "Access application audience tag" },
    { name: "ACCESS_TEAM_DOMAIN", present: Boolean(env.ACCESS_TEAM_DOMAIN), note: "Access team domain used to fetch signing certificates" },
  ];
}

app.get("/settings", async (c) => render(c, "Settings", V.settings(await c.var.repo.campaign(), await c.var.repo.settings(), secretPresence(c.env), null)));

app.post("/settings/rotate-runner-token", async (c) => {
  const token = "rt_" + randomHex(32);
  const fingerprint = await sha256Hex(token);
  await c.var.repo.putSetting("runner_token_fingerprint", { sha256: fingerprint, rotated_by: c.var.identity.email, rotated_at: nowIso(), applied: false });
  if (wantsJson(c)) return c.json({ token, fingerprint });
  const notice = html`New runner token (shown once, not stored): <code>${token}</code><br>Set it as the Worker secret RUNNER_TOKEN and on the runner. Fingerprint ${fingerprint.slice(0, 16)}.`;
  return render(c, "Settings", V.settings(await c.var.repo.campaign(), await c.var.repo.settings(), secretPresence(c.env), notice.value));
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
