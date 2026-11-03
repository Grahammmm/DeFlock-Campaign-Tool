// The subscriber area: the one place a subscriber address is stored. A subscriber list is
// personal data we collect from members of the public (docs/DATA-HANDLING.md), so the
// `subscriber` table is not in EXPORT_TABLES, is never rendered, and nothing outside this
// file reads the address column. Everything else carries addressHash() only.
// Double opt-in: `subscribed` records a pending row; only `confirmed` makes it count;
// `unsubscribed`, `bounced` and `complained` remove it at once. The count is ours, computed
// from confirmed rows; a provider's own figure is never used.
import { nowIso, sha256Hex } from "@deflock/shared/ids";
import type { Repo } from "./db.ts";

export const SUBSCRIBER_EVENTS = ["subscribed", "confirmed", "unsubscribed", "bounced", "complained"] as const;
export type SubscriberEventKind = (typeof SUBSCRIBER_EVENTS)[number];
const ALIASES: Record<string, SubscriberEventKind> = { hard_bounce: "bounced", spam: "complained", unsubscribe: "unsubscribed" };

const DDL =
  "CREATE TABLE IF NOT EXISTS subscriber (campaign_id TEXT NOT NULL, address_hash TEXT NOT NULL, address TEXT NOT NULL, " +
  "state TEXT NOT NULL CHECK (state IN ('pending','confirmed','removed')), scope TEXT, confirmed_at TEXT, removed_at TEXT, " +
  "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY (campaign_id, address_hash))";

export type AddressRefusal = "malformed_address" | "address_too_long";

export function eventKind(value: unknown): SubscriberEventKind | null {
  if (typeof value !== "string") return null;
  const v = value.trim().toLowerCase();
  return (SUBSCRIBER_EVENTS as readonly string[]).includes(v) ? (v as SubscriberEventKind) : ALIASES[v] ?? null;
}

/** Lowercased address with an ASCII (punycode) domain, or the refusal reason by name. */
export function normalizeAddress(value: unknown): { address: string } | { refused: AddressRefusal } {
  if (typeof value !== "string") return { refused: "malformed_address" };
  const raw = value.trim();
  if (raw.length > 320) return { refused: "address_too_long" };
  const at = raw.lastIndexOf("@");
  if (at <= 0 || at === raw.length - 1) return { refused: "malformed_address" };
  const local = raw.slice(0, at);
  if (local.length > 64) return { refused: "address_too_long" };
  if (!/^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+$/.test(local) || local.startsWith(".") || local.endsWith(".") || local.includes("..")) {
    return { refused: "malformed_address" };
  }
  const rawDomain = raw.slice(at + 1);
  // Characters a URL parser would strip or treat as structure are refused, not "fixed".
  if (/[\s/?#:\\@[\]%]/.test(rawDomain)) return { refused: "malformed_address" };
  let domain: string;
  try {
    domain = new URL("http://" + rawDomain).hostname; // IDN -> punycode
  } catch {
    return { refused: "malformed_address" };
  }
  if (!/^[a-z0-9-]+(\.[a-z0-9-]+)+$/.test(domain)) return { refused: "malformed_address" };
  const address = local.toLowerCase() + "@" + domain;
  return address.length > 254 ? { refused: "address_too_long" } : { address };
}

/** The only identifier that leaves the subscriber area. */
export function addressHash(campaignId: string, address: string): Promise<string> {
  return sha256Hex("subscriber:" + campaignId + ":" + address);
}

export async function ensureSubscriberArea(db: D1Database): Promise<void> {
  await db.prepare(DDL).run();
}

export type ApplyOutcome = "pending" | "confirmed" | "already_confirmed" | "removed" | "not_pending";

/** Apply one verified event to the subscriber area. */
export async function applyEvent(db: D1Database, campaignId: string, kind: SubscriberEventKind, address: string, hash: string, scope: string): Promise<ApplyOutcome> {
  const now = nowIso();
  const row = await db.prepare("SELECT state FROM subscriber WHERE campaign_id = ? AND address_hash = ?").bind(campaignId, hash).first<{ state: string }>();
  if (kind === "subscribed") {
    if (row?.state === "confirmed") return "already_confirmed";
    // A removed address goes back to pending: it counts again only after a fresh `confirmed`.
    await db
      .prepare(
        "INSERT INTO subscriber (campaign_id, address_hash, address, state, scope, confirmed_at, removed_at, created_at, updated_at) " +
          "VALUES (?,?,?,'pending',NULL,NULL,NULL,?,?) ON CONFLICT(campaign_id, address_hash) DO UPDATE SET state = 'pending', " +
          "scope = NULL, confirmed_at = NULL, updated_at = excluded.updated_at",
      )
      .bind(campaignId, hash, address, now, now)
      .run();
    return "pending";
  }
  if (kind === "confirmed") {
    if (row?.state === "confirmed") return "already_confirmed";
    if (row?.state !== "pending") return "not_pending";
    await db
      .prepare("UPDATE subscriber SET state = 'confirmed', scope = ?, confirmed_at = ?, removed_at = NULL, updated_at = ? WHERE campaign_id = ? AND address_hash = ?")
      .bind(scope, now, now, campaignId, hash)
      .run();
    return "confirmed";
  }
  // unsubscribed / bounced / complained: honoured on receipt, no confirmation step. An
  // address we never held is not stored just to record its removal.
  if (row) {
    await db
      .prepare("UPDATE subscriber SET state = 'removed', removed_at = ?, updated_at = ? WHERE campaign_id = ? AND address_hash = ?")
      .bind(now, now, campaignId, hash)
      .run();
  }
  return "removed";
}

/** Confirmed rows only: pending and removed addresses never count. */
export async function confirmedCount(db: D1Database, campaignId: string): Promise<number> {
  const r = await db.prepare("SELECT COUNT(*) AS n FROM subscriber WHERE campaign_id = ? AND state = 'confirmed'").bind(campaignId).first<{ n: number }>();
  return r?.n ?? 0;
}

/** Write the `list_count` event the Subscribers screen reads (newest occurred_at wins). */
export async function writeListCount(repo: Repo, count: number): Promise<void> {
  const last = await repo.latestSubscriberEvent("list_count");
  let at = Date.now();
  if (last) at = Math.max(at, Date.parse(last.occurred_at) + 1);
  const asOf = new Date(at).toISOString();
  await repo.createSubscriberEvent({
    provider: "brevo",
    kind: "list_count",
    payload_json: JSON.stringify({ count, as_of: asOf, source: "confirmed_rows" }),
    occurred_at: asOf,
  });
}
