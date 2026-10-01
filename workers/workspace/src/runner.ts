// Runner API exactly per docs/CONTRACTS.md. Bearer RUNNER_TOKEN, bound to one campaign.
// The runner never receives mail/MuckRock/Brevo credentials; it proposes, organizers approve.
import { Hono } from "hono";
import { HEX64, nowIso, sha256Hex } from "@deflock/shared/ids";
import { requireRunner } from "./auth.ts";
import { ACTION_KINDS, JOB_KINDS, Repo, type ActionKind, type JobKind } from "./db.ts";
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
  const outputs = { ...((body.outputs as object) ?? {}), receipt: body.receipt ?? null } as Record<string, unknown>;
  const updated = await c.var.repo.finishJob(job.job_id, body.status as "done" | "failed" | "blocked", outputs, body.error ?? null);
  if (body.status === "failed" && updated?.state === "failed") {
    await c.var.repo.raiseIncident("job:" + job.kind + ":" + job.idempotency_key, "warning", `job ${job.job_id} (${job.kind}) failed after ${job.attempt} attempts: ${body.error ?? ""}`);
  }
  const followups: string[] = [];
  if (body.status === "done") {
    // outputs.followups: [{kind, idempotency_key?, inputs?}] -> queued jobs (idempotent); other keys are ignored.
    for (const item of parseFollowups(outputs.followups)) {
      const key = item.idempotency_key ?? (await sha256Hex(item.kind + JSON.stringify(item.inputs)));
      const { row } = await c.var.repo.enqueueJob(item.kind, key, item.inputs);
      followups.push(row.job_id);
    }
    // classify_mail: outputs.correspondence_update patches the classified row (never the raw MIME).
    const patch = outputs.correspondence_update as Record<string, unknown> | undefined;
    const target = (JSON.parse(job.inputs_json) as { correspondence_id?: string }).correspondence_id;
    if (job.kind === "classify_mail" && patch && typeof patch === "object" && target && (await c.var.repo.correspondence(target))) {
      const classification = String(patch.classification ?? "unclassified");
      await c.var.repo.updateCorrespondence(target, {
        classification: CLASSIFICATIONS.includes(classification) ? classification : "unclassified",
        classification_confidence: typeof patch.classification_confidence === "string" ? patch.classification_confidence.slice(0, 32) : null,
        summary: typeof patch.summary === "string" ? patch.summary.slice(0, 300) : null,
      });
    }
  }
  return c.json({ job_id: job.job_id, state: updated?.state, followups });
});

const CLASSIFICATIONS = ["acknowledgement", "extension", "fee_estimate", "partial_production", "production", "denial", "clarification", "unrelated", "unclassified"];
const MAX_FOLLOWUPS = 200;

function parseFollowups(value: unknown): { kind: JobKind; idempotency_key: string | null; inputs: Record<string, unknown> }[] {
  if (!Array.isArray(value)) return [];
  const out: { kind: JobKind; idempotency_key: string | null; inputs: Record<string, unknown> }[] = [];
  for (const item of value.slice(0, MAX_FOLLOWUPS)) {
    if (!item || typeof item !== "object") continue;
    const kind = (item as { kind?: unknown }).kind;
    if (!JOB_KINDS.includes(kind as JobKind) || kind === "send_request") continue; // sends are never queued from runner output
    const key = (item as { idempotency_key?: unknown }).idempotency_key;
    const inputs = (item as { inputs?: unknown }).inputs;
    out.push({
      kind: kind as JobKind,
      idempotency_key: typeof key === "string" && HEX64.test(key) ? key : null,
      inputs: inputs && typeof inputs === "object" && !Array.isArray(inputs) ? (inputs as Record<string, unknown>) : {},
    });
  }
  return out;
}

// Public site staging (build_site job): PUT /api/runner/site/:version/<path> -> public bucket
// sites/<version>/<path>. Nothing is served until a deploy_site card flips site_version.
// Path safety: relative, plain segments (no "", ".", "..", leading dot or slash), an
// allowlisted extension (or one of the Pages-style control files), <= 16 MiB, and the
// caller's x-object-sha256 must match the bytes before anything is written.
export const SITE_VERSION = /^[a-z0-9._-]{1,64}$/;
const SITE_SEGMENT = /^[A-Za-z0-9_][A-Za-z0-9._-]{0,120}$/;
export const SITE_FILE_MAX_BYTES = 16 * 1024 * 1024;
export const SITE_EXTENSIONS: ReadonlySet<string> = new Set([
  "html", "css", "js", "mjs", "map", "json", "geojson", "xml", "txt", "webmanifest",
  "svg", "png", "jpg", "jpeg", "webp", "gif", "ico", "woff", "woff2", "pdf",
]);
export const SITE_CONTROL_FILES: ReadonlySet<string> = new Set(["_headers", "_redirects"]);

export function safeSitePath(path: string): string | null {
  if (!path || path.length > 1024 || path.includes("\\") || path.includes("\0")) return null;
  const parts = path.split("/");
  if (parts.length > 16 || parts.some((p) => !SITE_SEGMENT.test(p) || p === "." || p === "..")) return null;
  const name = parts[parts.length - 1]!;
  if (!SITE_CONTROL_FILES.has(name)) {
    const dot = name.lastIndexOf(".");
    if (dot <= 0 || !SITE_EXTENSIONS.has(name.slice(dot + 1).toLowerCase())) return null;
  }
  return parts.join("/");
}

runnerApi.put("/site/:version/*", async (c) => {
  const version = c.req.param("version");
  if (!SITE_VERSION.test(version)) return c.json({ error: "version must match [a-z0-9._-]{1,64}" }, 400);
  const raw = c.req.path.replace(/^\/api\/runner\/site\/[^/]+\/?/, "");
  let decoded: string;
  try {
    decoded = decodeURIComponent(raw);
  } catch {
    return c.json({ error: "path is not valid percent-encoding" }, 400);
  }
  const path = safeSitePath(decoded);
  if (!path) return c.json({ error: "path must be relative with plain segments and an allowlisted extension" }, 400);
  const expected = (c.req.header("x-object-sha256") ?? "").toLowerCase();
  if (!HEX64.test(expected)) return c.json({ error: "x-object-sha256 header (64 lowercase hex) required" }, 400);
  const declared = Number(c.req.header("content-length") ?? "0");
  if (declared > SITE_FILE_MAX_BYTES) return c.json({ error: "file exceeds 16 MiB" }, 413);
  const bytes = new Uint8Array(await c.req.arrayBuffer());
  if (bytes.byteLength > SITE_FILE_MAX_BYTES) return c.json({ error: "file exceeds 16 MiB" }, 413);
  const actual = await sha256Hex(bytes);
  if (actual !== expected) return c.json({ error: "hash mismatch", expected, actual }, 400);
  const key = `sites/${version}/${path}`;
  const mediaType = c.req.header("content-type") ?? "application/octet-stream";
  await c.env.PUBLIC_BUCKET.put(key, bytes, { httpMetadata: { contentType: mediaType }, customMetadata: { sha256: actual } });
  return c.json({ key, sha256: actual, bytes: bytes.byteLength }, 201);
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
