// POST /api/webhooks/subscribe: records newsletter-provider subscription events and writes
// the `list_count` event the Subscribers screen reads. No Access identity: every delivery is
// verified first (HMAC-SHA256 with the SUBSCRIBE_WEBHOOK_SECRET Worker secret over
// "<timestamp>.<raw body>", a timestamp window and a replay guard on the provider event id).
// There is no unauthenticated write path, no send path and no export path here. A rejected
// delivery is answered with its reason enum only; its body, headers and any address are
// never logged, echoed or stored: only the reason, the event id hash and an incident count.
import { Hono } from "hono";
import { bytesToHex, nowIso, sha256Hex, timingSafeEqual } from "@deflock/shared/ids";
import { Repo } from "../db.ts";
import type { Env } from "../env.ts";
import { addressHash, applyEvent, confirmedCount, ensureSubscriberArea, eventKind, normalizeAddress, writeListCount } from "../subscribers.ts";

export const SIGNATURE_HEADER = "x-subscribe-signature";
export const TIMESTAMP_HEADER = "x-subscribe-timestamp";
export const WINDOW_SECONDS = 300;
export const MAX_BODY_BYTES = 16 * 1024;

export type RejectReason =
  | "not_configured" | "missing_signature" | "bad_signature" | "stale_timestamp" | "too_large"
  | "malformed_body" | "unknown_event" | "missing_event_id" | "malformed_address" | "address_too_long"
  | "replay" | "not_pending";

const STATUS: Record<RejectReason, 400 | 401 | 409 | 413 | 503> = {
  not_configured: 503, missing_signature: 401, bad_signature: 401, stale_timestamp: 401, too_large: 413,
  malformed_body: 400, unknown_event: 400, missing_event_id: 400, malformed_address: 400, address_too_long: 400,
  replay: 409, not_pending: 409,
};

async function sign(secret: string, timestamp: string, body: string): Promise<string> {
  const enc = new TextEncoder();
  const key = await crypto.subtle.importKey("raw", enc.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  return bytesToHex(new Uint8Array(await crypto.subtle.sign("HMAC", key, enc.encode(timestamp + "." + body))));
}

/** Signature headers for a body; used by a relay in front of the provider and by tests. */
export async function signatureHeaders(secret: string, body: string, timestamp = Math.floor(Date.now() / 1000)): Promise<Record<string, string>> {
  const ts = String(timestamp);
  return { [TIMESTAMP_HEADER]: ts, [SIGNATURE_HEADER]: await sign(secret, ts, body), "content-type": "application/json" };
}

async function reject(repo: Repo, reason: RejectReason, eventIdHash: string | null): Promise<Response> {
  // One deduplicated incident per reason; the message holds the enum and an id hash only.
  await repo.raiseIncident("subscribe_webhook:" + reason, "warning",
    "subscribe webhook delivery rejected: " + reason + (eventIdHash ? " (event " + eventIdHash.slice(0, 16) + ")" : ""));
  return new Response(JSON.stringify({ error: reason }), {
    status: STATUS[reason],
    headers: { "content-type": "application/json", "cache-control": "no-store" },
  });
}

export const subscribeWebhook = new Hono<{ Bindings: Env }>();

subscribeWebhook.post("/", async (c) => {
  const repo = new Repo(c.env.DB, c.env.CAMPAIGN_ID);
  const secret = c.env.SUBSCRIBE_WEBHOOK_SECRET;
  if (!secret) return reject(repo, "not_configured", null);
  const sig = c.req.header(SIGNATURE_HEADER) ?? "";
  const ts = c.req.header(TIMESTAMP_HEADER) ?? "";
  if (!/^[0-9a-f]{64}$/.test(sig) || !/^[0-9]{1,12}$/.test(ts)) return reject(repo, "missing_signature", null);
  const declared = Number(c.req.header("content-length") ?? "0");
  if (declared > MAX_BODY_BYTES) return reject(repo, "too_large", null);
  const body = await c.req.text();
  if (new TextEncoder().encode(body).length > MAX_BODY_BYTES) return reject(repo, "too_large", null);
  if (!timingSafeEqual(sig, await sign(secret, ts, body))) return reject(repo, "bad_signature", null);
  if (Math.abs(Date.now() / 1000 - Number(ts)) > WINDOW_SECONDS) return reject(repo, "stale_timestamp", null);

  // Verified from here on; still nothing from the body is logged or echoed.
  let payload: Record<string, unknown>;
  try {
    const parsed: unknown = JSON.parse(body);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return reject(repo, "malformed_body", null);
    payload = parsed as Record<string, unknown>;
  } catch {
    return reject(repo, "malformed_body", null);
  }
  const rawId = payload.id ?? payload.event_id;
  const eventId = typeof rawId === "number" && Number.isSafeInteger(rawId) ? String(rawId) : typeof rawId === "string" ? rawId.trim() : "";
  if (!eventId || eventId.length > 200) return reject(repo, "missing_event_id", null);
  const eventIdHash = await sha256Hex("subscribe-event:" + c.env.CAMPAIGN_ID + ":" + eventId);
  const kind = eventKind(payload.event);
  if (!kind) return reject(repo, "unknown_event", eventIdHash);
  const norm = normalizeAddress(payload.email);
  if ("refused" in norm) return reject(repo, norm.refused, eventIdHash);
  const scopeRaw = typeof payload.scope === "string" ? payload.scope : payload.list_id != null ? "list-" + String(payload.list_id) : "newsletter";
  const scope = /^[A-Za-z0-9_.-]{1,64}$/.test(scopeRaw) ? scopeRaw : "newsletter";

  await ensureSubscriberArea(c.env.DB);
  const hash = await addressHash(c.env.CAMPAIGN_ID, norm.address);
  // Replay guard: the event id hash is the row's primary key, so a second delivery of one
  // provider event inserts nothing.
  const now = nowIso();
  const guard = await c.env.DB.prepare(
    "INSERT OR IGNORE INTO subscriber_event (event_id, campaign_id, provider, kind, payload_json, occurred_at, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
  )
    .bind("whk_" + eventIdHash.slice(0, 32), c.env.CAMPAIGN_ID, "brevo", "webhook",
      JSON.stringify({ event: kind, address_hash: hash, event_id_hash: eventIdHash }), now, now, now)
    .run();
  if (!guard.meta.changes) return reject(repo, "replay", eventIdHash);

  const outcome = await applyEvent(c.env.DB, c.env.CAMPAIGN_ID, kind, norm.address, hash, scope);
  if (outcome === "not_pending") return reject(repo, "not_pending", eventIdHash);
  // Ours, from confirmed rows: any provider-supplied count in the payload is ignored.
  const count = await confirmedCount(c.env.DB, c.env.CAMPAIGN_ID);
  await writeListCount(repo, count);
  return c.json({ ok: true, event: kind, outcome, count });
});
