/// <reference types="vite/client" />
import { env } from "cloudflare:test";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { approveAction, executeAction } from "../src/approvals.ts";
import type { AccessIdentity } from "../src/auth.ts";
import { EXPORT_TABLES } from "../src/db.ts";
import type { Env } from "../src/env.ts";
import { defaultExecutors } from "../src/executors/index.ts";
import { signatureHeaders, WINDOW_SECONDS } from "../src/routes/subscribe_webhook.ts";
import * as V from "../src/views/screens.ts";
import routeSource from "../src/routes/subscribe_webhook.ts?raw";
import areaSource from "../src/subscribers.ts?raw";
import { ensureSubscriberArea } from "../src/subscribers.ts";
import { call, runnerHeaders, seedCampaign } from "./helpers.ts";

const SECRET = "test-subscribe-webhook-secret-0123456789"; // pragma: allowlist secret -- synthetic test secret only
const URL_ = "https://workspace.example.invalid/api/webhooks/subscribe";
const ORG: AccessIdentity = { email: "organizer@example.invalid", sub: "o", issued_at: 0, expires_at: 0 };
const ADDR = "reader.one@example.invalid";
const ADDR2 = "reader.two@example.invalid";
let seq = 0;
let logged: string[] = [];

function event(kind: string, email: string, extra: Record<string, unknown> = {}): Record<string, unknown> {
  seq += 1;
  return { id: "evt-" + seq + "-" + crypto.randomUUID(), event: kind, email, ...extra };
}

async function deliver(payload: unknown, opts: { secret?: string; ts?: number; headers?: Record<string, string>; env?: Partial<Env> } = {}): Promise<Response> {
  const body = typeof payload === "string" ? payload : JSON.stringify(payload);
  const headers = opts.headers ?? (await signatureHeaders(opts.secret ?? SECRET, body, opts.ts));
  return call(new Request(URL_, { method: "POST", body, headers }), { SUBSCRIBE_WEBHOOK_SECRET: SECRET, ...opts.env });
}

async function latestCount(): Promise<number | null> {
  const row = await env.DB.prepare("SELECT payload_json FROM subscriber_event WHERE kind = 'list_count' ORDER BY occurred_at DESC LIMIT 1").first<{ payload_json: string }>();
  return row ? (JSON.parse(row.payload_json) as { count: number }).count : null;
}

async function incident(reason: string): Promise<{ count: number; message: string } | null> {
  return env.DB.prepare("SELECT count, message FROM incident WHERE fingerprint = ?").bind("subscribe_webhook:" + reason).first();
}

/** Every D1 table except the subscriber area, as text. */
async function everythingOutsideSubscriberArea(): Promise<string> {
  const tables = (await env.DB.prepare("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '_cf_%' AND name <> 'd1_migrations'").all<{ name: string }>()).results;
  let out = "";
  for (const t of tables) if (t.name !== "subscriber") out += JSON.stringify((await env.DB.prepare(`SELECT * FROM "${t.name}"`).all()).results);
  return out.toLowerCase();
}

beforeEach(async () => {
  await seedCampaign();
  await ensureSubscriberArea(env.DB);
  await env.DB.prepare("DELETE FROM subscriber").run();
  await env.DB.prepare("DELETE FROM subscriber_event").run();
  await env.DB.prepare("DELETE FROM incident WHERE fingerprint LIKE 'subscribe_webhook:%'").run();
  logged = [];
  for (const m of ["log", "info", "warn", "error", "debug"] as const) {
    vi.spyOn(console, m).mockImplementation((...args: unknown[]) => { logged.push(args.map(String).join(" ")); });
  }
});
afterEach(() => vi.restoreAllMocks());

describe("double opt-in and list_count", () => {
  it("subscribed then confirmed raises list_count from 0 to 1", async () => {
    expect((await deliver(event("subscribed", ADDR))).status).toBe(200);
    expect(await latestCount()).toBe(0);
    const res = await deliver(event("confirmed", ADDR, { scope: "newsletter" }));
    expect(res.status).toBe(200);
    expect(await latestCount()).toBe(1);
    const row = await env.DB.prepare("SELECT state, scope, confirmed_at FROM subscriber WHERE address = ?").bind(ADDR).first<{ state: string; scope: string; confirmed_at: string }>();
    expect(row?.state).toBe("confirmed");
    expect(row?.scope).toBe("newsletter");
    expect(row?.confirmed_at).toBeTruthy();
  });

  it("unsubscribed returns it to 0 immediately", async () => {
    await deliver(event("subscribed", ADDR));
    await deliver(event("confirmed", ADDR));
    expect(await latestCount()).toBe(1);
    expect((await deliver(event("unsubscribed", ADDR))).status).toBe(200);
    expect(await latestCount()).toBe(0);
  });

  it("subscribed alone never counts", async () => {
    await deliver(event("subscribed", ADDR));
    await deliver(event("subscribed", ADDR2));
    expect(await latestCount()).toBe(0);
  });

  it("confirmed without a pending subscribe is refused and does not count", async () => {
    const res = await deliver(event("confirmed", ADDR));
    expect(res.status).toBe(409);
    expect(await res.json()).toEqual({ error: "not_pending" });
    expect(await latestCount()).toBeNull();
  });

  for (const removal of ["bounced", "complained", "hard_bounce", "spam"]) {
    it(`${removal} removes the address; a later subscribed without a fresh confirmed does not restore it`, async () => {
      await deliver(event("subscribed", ADDR));
      await deliver(event("confirmed", ADDR));
      expect(await latestCount()).toBe(1);
      expect((await deliver(event(removal, ADDR))).status).toBe(200);
      expect(await latestCount()).toBe(0);
      await deliver(event("subscribed", ADDR));
      expect(await latestCount()).toBe(0);
      await deliver(event("confirmed", ADDR));
      expect(await latestCount()).toBe(1);
    });
  }

  it("removed address cannot be restored by a confirmed without a new subscribe", async () => {
    await deliver(event("subscribed", ADDR));
    await deliver(event("confirmed", ADDR));
    await deliver(event("unsubscribed", ADDR));
    expect((await deliver(event("confirmed", ADDR))).status).toBe(409);
    expect(await latestCount()).toBe(0);
  });

  it("duplicate confirmed counts once; two addresses count 2", async () => {
    await deliver(event("subscribed", ADDR));
    await deliver(event("confirmed", ADDR));
    await deliver(event("confirmed", ADDR));
    expect(await latestCount()).toBe(1);
    await deliver(event("subscribed", ADDR2));
    await deliver(event("confirmed", ADDR2));
    expect(await latestCount()).toBe(2);
  });

  it("list_count is computed from our confirmed rows, not a provider figure", async () => {
    await deliver(event("subscribed", ADDR, { list_count: 999, count: 999 }));
    await deliver(event("confirmed", ADDR, { list_count: 999, count: 999, totalSubscribers: 999 }));
    expect(await latestCount()).toBe(1);
    const payload = JSON.parse((await env.DB.prepare("SELECT payload_json FROM subscriber_event WHERE kind = 'list_count' ORDER BY occurred_at DESC LIMIT 1").first<{ payload_json: string }>())!.payload_json);
    expect(payload.source).toBe("confirmed_rows");
    expect(payload.as_of).toMatch(/^\d{4}-\d\d-\d\dT/);
  });

  it("the Subscribers screen's existing reader renders the count unchanged", async () => {
    await deliver(event("subscribed", ADDR));
    await deliver(event("confirmed", ADDR));
    const repo = await seedCampaign();
    const page = V.subscribers(await repo.subscriberEvents(), [], [], null, null).toString();
    expect(page).toContain("<strong>1</strong>");
    expect(page.toLowerCase()).not.toContain(ADDR);
  });
});

describe("delivery verification", () => {
  it("an unverified delivery is rejected with no body logged and one deduplicated incident", async () => {
    const payload = event("subscribed", ADDR);
    const res = await deliver(payload, { secret: "wrong-secret-0123456789" }); // pragma: allowlist secret -- synthetic wrong secret only
    expect(res.status).toBe(401);
    const text = await res.text();
    expect(JSON.parse(text)).toEqual({ error: "bad_signature" });
    expect(text).not.toContain(ADDR);
    expect((await incident("bad_signature"))?.count).toBe(1);
    await deliver(event("subscribed", ADDR), { secret: "wrong-secret-0123456789" }); // pragma: allowlist secret -- synthetic wrong secret only
    expect((await incident("bad_signature"))?.count).toBe(2);
    expect((await env.DB.prepare("SELECT COUNT(*) n FROM incident WHERE fingerprint = 'subscribe_webhook:bad_signature'").first<{ n: number }>())?.n).toBe(1);
    expect(await latestCount()).toBeNull();
    expect(logged.join("\n").toLowerCase()).not.toContain(ADDR);
    expect(await everythingOutsideSubscriberArea()).not.toContain(ADDR);
  });

  it("a delivery with no signature headers is rejected", async () => {
    const res = await deliver(event("subscribed", ADDR), { headers: { "content-type": "application/json" } });
    expect(res.status).toBe(401);
    expect(await res.json()).toEqual({ error: "missing_signature" });
    expect((await incident("missing_signature"))?.count).toBe(1);
  });

  it("a body changed after signing is rejected", async () => {
    const body = JSON.stringify(event("subscribed", ADDR));
    const headers = await signatureHeaders(SECRET, body);
    const res = await deliver(body.replace("subscribed", "confirmed"), { headers });
    expect(res.status).toBe(401);
    expect(await latestCount()).toBeNull();
  });

  it("a replayed verified event id is rejected and the count is unchanged", async () => {
    await deliver(event("subscribed", ADDR));
    const confirm = event("confirmed", ADDR);
    expect((await deliver(confirm)).status).toBe(200);
    await deliver(event("unsubscribed", ADDR));
    expect(await latestCount()).toBe(0);
    const res = await deliver(confirm);
    expect(res.status).toBe(409);
    expect(await res.json()).toEqual({ error: "replay" });
    expect(await latestCount()).toBe(0);
    const inc = await incident("replay");
    expect(inc?.count).toBe(1);
    expect(inc?.message).toMatch(/^subscribe webhook delivery rejected: replay \(event [0-9a-f]{16}\)$/);
  });

  it("an out-of-window delivery (stale or future) is rejected", async () => {
    const now = Math.floor(Date.now() / 1000);
    for (const ts of [now - WINDOW_SECONDS - 5, now + WINDOW_SECONDS + 5]) {
      const res = await deliver(event("subscribed", ADDR), { ts });
      expect(res.status).toBe(401);
      expect(await res.json()).toEqual({ error: "stale_timestamp" });
    }
    expect((await incident("stale_timestamp"))?.count).toBe(2);
    expect(await latestCount()).toBeNull();
  });

  it("without the webhook secret every delivery is refused not_configured", async () => {
    const res = await deliver(event("subscribed", ADDR), { env: { SUBSCRIBE_WEBHOOK_SECRET: undefined } });
    expect(res.status).toBe(503);
    expect(await res.json()).toEqual({ error: "not_configured" });
    expect(await latestCount()).toBeNull();
  });

  it("malformed JSON, unknown events, missing ids and oversize bodies are refused by name", async () => {
    const cases: [unknown, string][] = [
      ["{not json " + ADDR, "malformed_body"],
      [[event("subscribed", ADDR)], "malformed_body"],
      [event("opened", ADDR), "unknown_event"],
      [{ event: "subscribed", email: ADDR }, "missing_event_id"],
      [event("subscribed", ADDR, { pad: "x".repeat(20_000) }), "too_large"],
    ];
    for (const [payload, reason] of cases) {
      const res = await deliver(payload);
      const text = await res.text();
      expect(JSON.parse(text)).toEqual({ error: reason });
      expect(text).not.toContain(ADDR);
    }
    expect(logged.join("\n").toLowerCase()).not.toContain(ADDR);
    expect(await everythingOutsideSubscriberArea()).not.toContain(ADDR);
  });

  it("with BREVO_API_KEY absent the route still records and send_newsletter still fails brevo_not_configured", async () => {
    const noKey = { ...env, SUBSCRIBE_WEBHOOK_SECRET: SECRET, BREVO_API_KEY: undefined } as Env;
    await deliver(event("subscribed", ADDR), { env: { BREVO_API_KEY: undefined } });
    expect((await deliver(event("confirmed", ADDR), { env: { BREVO_API_KEY: undefined } })).status).toBe(200);
    expect(await latestCount()).toBe(1);
    const repo = await seedCampaign();
    const html = "<p>Update</p><p><a href=\"{{ unsubscribe }}\">Unsubscribe</a></p>";
    const { row } = await repo.propose("send_newsletter", null, { subject: "x", html, text: "" }, "subscribe-webhook-nl-" + crypto.randomUUID(), ORG.email);
    await approveAction(repo, row.action_id, ORG);
    const done = await executeAction(repo, noKey, row.action_id, ORG, defaultExecutors(noKey));
    expect(done.error).toMatch(/^brevo_not_configured: BREVO_API_KEY/);
  });
});

describe("addresses", () => {
  it("handles +, long local parts, IDN domains and malformed addresses by name, never echoing them", async () => {
    expect((await deliver(event("subscribed", "reader+alpr@example.invalid"))).status).toBe(200);
    expect((await deliver(event("subscribed", "Reader@bücher.example.invalid"))).status).toBe(200);
    const stored = (await env.DB.prepare("SELECT address FROM subscriber ORDER BY address").all<{ address: string }>()).results.map((r) => r.address);
    expect(stored).toEqual(["reader+alpr@example.invalid", "reader@xn--bcher-kva.example.invalid"]);
    const long = "a".repeat(65) + "@example.invalid";
    const bad: [string, string][] = [[long, "address_too_long"], ["not-an-address", "malformed_address"], ["a@b@example.invalid", "malformed_address"],
      ["reader@example.invalid/x", "malformed_address"], [".dot@example.invalid", "malformed_address"], ["reader@localhost", "malformed_address"]];
    for (const [addr, reason] of bad) {
      const res = await deliver(event("subscribed", addr));
      expect(res.status).toBe(400);
      const text = await res.text();
      expect(JSON.parse(text)).toEqual({ error: reason });
      expect(text).not.toContain(addr);
    }
    expect(await latestCount()).toBe(0);
  });
});

describe("the address stays in the subscriber area", () => {
  it("after a full run the synthetic address appears only in the subscriber table", async () => {
    await deliver(event("subscribed", ADDR));
    await deliver(event("confirmed", ADDR));
    await deliver(event("subscribed", ADDR2));
    await deliver(event("bounced", ADDR2));
    await deliver(event("confirmed", ADDR)); // duplicate
    await deliver(event("subscribed", ADDR), { secret: "wrong-secret-0123456789" }); // pragma: allowlist secret -- synthetic wrong secret only
    const confirm = event("confirmed", ADDR2);
    await deliver(confirm);
    await deliver(confirm); // replay
    const area = (await env.DB.prepare("SELECT address FROM subscriber").all<{ address: string }>()).results.map((r) => r.address).sort();
    expect(area).toEqual([ADDR, ADDR2]);
    const outside = await everythingOutsideSubscriberArea();
    expect(outside).not.toContain(ADDR);
    expect(outside).not.toContain(ADDR2);
    expect(outside).toContain("list_count");
    expect(logged.join("\n").toLowerCase()).not.toContain("@example.invalid");
    // The D1 export (backups) never carries the subscriber area.
    expect(EXPORT_TABLES as readonly string[]).not.toContain("subscriber");
    const exported = await (await call(new Request("https://workspace.example.invalid/api/runner/export.json", { headers: runnerHeaders() }))).text();
    expect(exported).not.toContain(ADDR);
    expect(exported).not.toContain(ADDR2);
    const repo = await seedCampaign();
    const page = V.subscribers(await repo.subscriberEvents(), [], [], null, null).toString().toLowerCase();
    expect(page).not.toContain(ADDR);
    const dash = V.dashboard(await repo.campaign(), await repo.counts(), await repo.incidents(), await repo.runs(), []).toString().toLowerCase();
    expect(dash).not.toContain(ADDR);
  }, 30_000);

  it("static: no send, mail client, export, CSV writer or log call in the subscribe code", () => {
    for (const src of [routeSource, areaSource]) {
      const code = src.split("\n").filter((l) => !l.trim().startsWith("//")).join("\n");
      expect(code).not.toMatch(/\bfetch\s*\(|\.send\s*\(|sendEmail|FetchBrevo|Brevo\w*Client|executeAction|mailExecutor/);
      expect(code).not.toMatch(/console\.|text\/csv|\.csv\b|EXPORT_TABLES|PUBLIC_BUCKET|ORIGINALS/);
    }
  });
});
