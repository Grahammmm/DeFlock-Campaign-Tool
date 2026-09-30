// Runner API exactly per docs/CONTRACTS.md. Bearer RUNNER_TOKEN, bound to one campaign.
// The runner never receives mail/MuckRock/Brevo credentials; it proposes, organizers approve.
import { Hono } from "hono";
import { HEX64, nowIso, sha256Hex } from "@deflock/shared/ids";
import { requireRunner } from "./auth.ts";
import { ACTION_KINDS, Repo, type ActionKind } from "./db.ts";
import type { Env } from "./env.ts";

type Vars = { repo: Repo };

export const runnerApi = new Hono<{ Bindings: Env; Variables: Vars }>();

runnerApi.use("*", async (c, next) => {
  requireRunner(c.req.raw, c.env);
  c.set("repo", new Repo(c.env.DB, c.env.CAMPAIGN_ID));
  await next();
});

function jobPayload(row: import("./db.ts").JobRow, campaign: { privacy_tier: string } | null) {
  return {
    job_id: row.job_id,
    campaign_id: row.campaign_id,
    kind: row.kind,
    idempotency_key: row.idempotency_key,
    inputs: JSON.parse(row.inputs_json),
    attempt: row.attempt,
    max_attempts: row.max_attempts,
    enqueued_at: row.enqueued_at,
    leased_until: row.leased_until,
    privacy_tier: campaign?.privacy_tier ?? "redacted_cloud",
  };
}

// GET /api/runner/jobs?lease=300 -> next job (atomic lease) or 204
runnerApi.get("/jobs", async (c) => {
  const lease = Math.min(Math.max(parseInt(c.req.query("lease") ?? "300", 10) || 300, 30), 3600);
  const row = await c.var.repo.leaseJob(lease);
  if (!row) return c.body(null, 204);
  return c.json(jobPayload(row, await c.var.repo.campaign()));
});

// POST /api/runner/jobs/:id/result {status, outputs, receipt}
runnerApi.post("/jobs/:id/result", async (c) => {
  const body = (await c.req.json().catch(() => null)) as { status?: string; outputs?: unknown; receipt?: unknown; error?: string } | null;
  if (!body || !["done", "failed", "blocked"].includes(String(body.status))) return c.json({ error: "status must be done|failed|blocked" }, 400);
  const job = await c.var.repo.job(c.req.param("id"));
  if (!job) return c.json({ error: "unknown job" }, 404);
  if (job.state !== "leased") return c.json({ error: `job is ${job.state}, not leased` }, 409);
  const outputs = { ...((body.outputs as object) ?? {}), receipt: body.receipt ?? null };
  const updated = await c.var.repo.finishJob(job.job_id, body.status as "done" | "failed" | "blocked", outputs, body.error ?? null);
  if (body.status === "failed" && updated?.state === "failed") {
    await c.var.repo.raiseIncident("job:" + job.kind + ":" + job.idempotency_key, "warning", `job ${job.job_id} (${job.kind}) failed after ${job.attempt} attempts: ${body.error ?? ""}`);
  }
  return c.json({ job_id: job.job_id, state: updated?.state });
});

// GET /api/runner/originals/:sha256 -> bytes
runnerApi.get("/originals/:sha256", async (c) => {
  const sha = c.req.param("sha256");
  if (!HEX64.test(sha)) return c.json({ error: "sha256 must be 64 lowercase hex" }, 400);
  const row = await c.var.repo.original(sha);
  if (!row) return c.json({ error: "unknown original" }, 404);
  const obj = await c.env.ORIGINALS.get(row.r2_key);
  if (!obj) return c.json({ error: "object missing from bucket" }, 404);
  return new Response(obj.body, {
    headers: { "content-type": row.media_type ?? "application/octet-stream", "x-object-sha256": sha, "content-length": String(row.byte_count) },
  });
});

// PUT /api/runner/originals/:sha256 -> store (verifies the hash before anything is kept)
runnerApi.put("/originals/:sha256", async (c) => {
  const sha = c.req.param("sha256");
  if (!HEX64.test(sha)) return c.json({ error: "sha256 must be 64 lowercase hex" }, 400);
  const bytes = new Uint8Array(await c.req.arrayBuffer());
  const actual = await sha256Hex(bytes);
  if (actual !== sha) return c.json({ error: "hash mismatch", expected: sha, actual }, 400);
  const mediaType = c.req.header("content-type") ?? "application/octet-stream";
  const key = "originals/" + sha;
  if (!(await c.env.ORIGINALS.head(key))) await c.env.ORIGINALS.put(key, bytes, { httpMetadata: { contentType: mediaType } });
  const { inserted } = await c.var.repo.putOriginal({ sha256: sha, byte_count: bytes.byteLength, media_type: mediaType, r2_key: key, stored_at: nowIso() });
  return c.json({ sha256: sha, byte_count: bytes.byteLength, created: inserted }, inserted ? 201 : 200);
});

// POST /api/runner/proposals (idempotent on idempotency_key)
runnerApi.post("/proposals", async (c) => {
  const body = (await c.req.json().catch(() => null)) as {
    kind?: string; subject_id?: string | null; proposal?: unknown; idempotency_key?: string; proposed_by?: string;
  } | null;
  if (!body || !ACTION_KINDS.includes(body.kind as ActionKind)) return c.json({ error: "kind must be one of " + ACTION_KINDS.join("|") }, 400);
  if (!body.idempotency_key || !body.proposed_by) return c.json({ error: "idempotency_key and proposed_by required" }, 400);
  if (body.proposal === undefined || body.proposal === null || typeof body.proposal !== "object") return c.json({ error: "proposal object required" }, 400);
  const { row, inserted } = await c.var.repo.propose(body.kind as ActionKind, body.subject_id ?? null, body.proposal, body.idempotency_key, body.proposed_by);
  return c.json({ action_id: row.action_id, state: row.state, created: inserted }, inserted ? 201 : 200);
});

// POST /api/runner/correspondence (portal/MuckRock imports; deduped on provider_message_id)
runnerApi.post("/correspondence", async (c) => {
  const b = (await c.req.json().catch(() => null)) as Record<string, unknown> | null;
  if (!b || (b.direction !== "inbound" && b.direction !== "outbound") || typeof b.channel !== "string") {
    return c.json({ error: "direction inbound|outbound and channel required" }, 400);
  }
  if (b.request_id && !(await c.var.repo.request(String(b.request_id)))) return c.json({ error: "unknown request_id" }, 404);
  if (b.raw_sha256 && !(await c.var.repo.original(String(b.raw_sha256)))) return c.json({ error: "raw_sha256 is not a stored original" }, 409);
  const { row, inserted } = await c.var.repo.createCorrespondence({
    request_id: (b.request_id as string) ?? null,
    direction: b.direction,
    channel: b.channel,
    provider_message_id: (b.provider_message_id as string) ?? null,
    from_addr: (b.from_addr as string) ?? null,
    to_addr: (b.to_addr as string) ?? null,
    subject: (b.subject as string) ?? null,
    received_at: typeof b.received_at === "string" ? b.received_at : nowIso(),
    raw_sha256: (b.raw_sha256 as string) ?? null,
    classification: (b.classification as string) ?? "unclassified",
    classification_confidence: (b.classification_confidence as string) ?? null,
    summary: (b.summary as string) ?? null,
  });
  if (inserted && row.request_id) await c.var.repo.updateRequest(row.request_id, { last_activity_at: row.received_at });
  return c.json({ correspondence_id: row.correspondence_id, created: inserted }, inserted ? 201 : 200);
});

// POST /api/runner/receipts: receipt_id = sha256([source_id, sha256]); idempotent
runnerApi.post("/receipts", async (c) => {
  const b = (await c.req.json().catch(() => null)) as Record<string, unknown> | null;
  if (!b || typeof b.sha256 !== "string" || !HEX64.test(b.sha256) || typeof b.source_id !== "string" || typeof b.original_name !== "string") {
    return c.json({ error: "sha256, source_id and original_name required" }, 400);
  }
  if (!(await c.var.repo.original(b.sha256))) return c.json({ error: "original not stored; PUT /api/runner/originals first" }, 409);
  const { row, inserted } = await c.var.repo.recordReceipt({
    sha256: b.sha256,
    source_id: b.source_id,
    correspondence_id: (b.correspondence_id as string) ?? null,
    agency_id: (b.agency_id as string) ?? null,
    request_id: (b.request_id as string) ?? null,
    original_name: b.original_name,
    received_at: typeof b.received_at === "string" ? b.received_at : nowIso(),
  });
  return c.json({ receipt_id: row.receipt_id, created: inserted }, inserted ? 201 : 200);
});
