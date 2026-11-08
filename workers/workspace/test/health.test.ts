import { env } from "cloudflare:test";
import { describe, expect, it } from "vitest";
import type { Env } from "../src/env.ts";
import { call, repo, seedCampaign } from "./helpers.ts";

const KEYS = ["access_configured", "campaign", "d1_ok", "migrations_applied", "ok", "outbound"];
const get = (o: Partial<Env> = {}) => call(new Request("https://w.example.invalid/healthz"), o);

describe("/healthz", () => {
  it("is anonymous and reports only booleans, one count and off/on", async () => {
    await seedCampaign();
    const res = await get();
    expect(res.status).toBe(200);
    const body = (await res.json()) as Record<string, unknown>;
    expect(Object.keys(body).sort()).toEqual(KEYS);
    expect(body).toMatchObject({ ok: true, campaign: true, d1_ok: true, access_configured: true, outbound: "off" });
    expect(Number.isInteger(body.migrations_applied)).toBe(true);
    expect(body.migrations_applied as number).toBeGreaterThanOrEqual(5);
    for (const [k, v] of Object.entries(body)) {
      if (k === "outbound") expect(["off", "on"]).toContain(v);
      else expect(["boolean", "number"]).toContain(typeof v);
    }
  });

  it("access_configured is false when the Access settings are empty", async () => {
    const body = (await (await get({ ACCESS_AUD: "" })).json()) as Record<string, unknown>;
    expect(body.access_configured).toBe(false);
    const b2 = (await (await get({ ACCESS_TEAM_DOMAIN: "" })).json()) as Record<string, unknown>;
    expect(b2.access_configured).toBe(false);
  });

  it("outbound stays off with only a sender secret, or only the campaign switch", async () => {
    await seedCampaign();
    await env.DB.prepare("UPDATE campaign SET external_sends = 'disabled' WHERE campaign_id = ?").bind(env.CAMPAIGN_ID).run();
    const withSender = { OUTBOX_GATEWAY_URL: "https://o.example.invalid", OUTBOX_SIGNING_KEY: "k" };
    expect(((await (await get(withSender)).json()) as { outbound: string }).outbound).toBe("off");
    await env.DB.prepare("UPDATE campaign SET external_sends = 'approval_required' WHERE campaign_id = ?").bind(env.CAMPAIGN_ID).run();
    expect(((await (await get()).json()) as { outbound: string }).outbound).toBe("off");
  });

  it("outbound is on only when the campaign switch is open and a sender secret exists, and leaks no secret", async () => {
    await seedCampaign();
    await env.DB.prepare("UPDATE campaign SET external_sends = 'approval_required' WHERE campaign_id = ?").bind(env.CAMPAIGN_ID).run();
    const res = await get({ BREVO_API_KEY: "brevo-secret-value-123" });
    const text = await res.text();
    expect(JSON.parse(text).outbound).toBe("on");
    expect(text).not.toContain("brevo-secret-value-123");
    expect(text).not.toContain("example-team");
    expect(text).not.toContain("@");
    await env.DB.prepare("UPDATE campaign SET external_sends = 'disabled' WHERE campaign_id = ?").bind(env.CAMPAIGN_ID).run();
  });

  it("answers 503 with d1_ok false when D1 fails, without the error text", async () => {
    const broken = {
      prepare() {
        throw new Error("D1_ERROR secret-detail");
      },
    } as unknown as D1Database;
    const res = await get({ DB: broken });
    expect(res.status).toBe(503);
    const text = await res.text();
    expect(text).not.toContain("secret-detail");
    expect(JSON.parse(text)).toEqual({ ok: false, campaign: true, d1_ok: false, migrations_applied: 0, access_configured: true, outbound: "off" });
  });
});
