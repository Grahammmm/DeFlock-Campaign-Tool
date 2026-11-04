import { env } from "cloudflare:test";
import { beforeEach, describe, expect, it } from "vitest";
import { call, runnerHeaders, seedCampaign } from "./helpers.ts";
import { COUNT_KEYS, MAX_AGE_SECONDS, readReadiness, readinessError, type ReadinessSnapshot } from "../src/readiness.ts";

const URL = "https://workspace.example.invalid/api/runner/readiness";
function snapshot(at = new Date()): ReadinessSnapshot {
  return { schema_version: 1, campaign_id: env.CAMPAIGN_ID, generated_at: at.toISOString(),
    law_package_status: "missing", production_ready: false, safe_to_send_automatically: false,
    next_actions: ["Verify county/state and official agency contacts.", "Obtain independently reviewed event-date laws and local policies.",
      "Follow docs/CLOUDFLARE.md before publishing a live campaign.", "Configure and test signup and suppression with one approved address."],
    counts: Object.fromEntries(COUNT_KEYS.map(key => [key, 0])) };
}
function post(body: unknown, headers = runnerHeaders()) {
  return call(new Request(URL, { method: "POST", headers, body: JSON.stringify(body) }));
}
async function rawSetting(value: string) {
  const now = new Date().toISOString();
  await env.DB.prepare("INSERT OR REPLACE INTO setting (campaign_id, key, value_json, created_at, updated_at) VALUES (?, 'readiness_snapshot', ?, ?, ?)")
    .bind(env.CAMPAIGN_ID, value, now, now).run();
}
async function noRow() {
  expect(await env.DB.prepare("SELECT * FROM setting WHERE campaign_id = ? AND key = 'readiness_snapshot'").bind(env.CAMPAIGN_ID).first()).toBeNull();
}

describe("readiness-snapshot-v1 transport and age", () => {
  beforeEach(async () => {
    await seedCampaign();
    await env.DB.prepare("DELETE FROM setting WHERE campaign_id = ? AND key = 'readiness_snapshot'").bind(env.CAMPAIGN_ID).run();
  });

  it("stores, reads the same actions with age, and replaces with newer data", async () => {
    const repo = await seedCampaign();
    const body = snapshot();
    expect((await post(body)).status).toBe(200);
    expect(await repo.setting("readiness_snapshot")).toEqual(body);
    const read = await readReadiness(repo, new Date());
    expect(read).toMatchObject({ present: true, current: true, reason: null, generated_at: body.generated_at,
      next_actions: body.next_actions, counts: body.counts, law_package_status: "missing" });
    expect(read.age_seconds).toBeGreaterThanOrEqual(0);
    expect(read.age_seconds).toBeLessThan(1);
    const newer = { ...body, generated_at: new Date(Date.parse(body.generated_at) + 1000).toISOString(), next_actions: ["Review the packet."] };
    expect((await post(newer)).status).toBe(200);
    expect(await repo.setting("readiness_snapshot")).toEqual(newer);
  });

  it.each([undefined, "Bearer wrong"])("refuses bearer %s without any partial row", async authorization => {
    const headers: Record<string, string> = authorization ? { authorization } : {};
    expect((await post(snapshot(), headers)).status).toBe(401);
    await noRow();
  });

  it.each(Object.keys(snapshot()))("rejects missing required key %s", async key => {
    const body = { ...snapshot() } as Record<string, unknown>;
    delete body[key];
    const response = await post(body);
    expect(response.status).toBe(400);
    expect(await response.json()).toEqual({ error: key });
    await noRow();
  });

  const invalid: [string, unknown][] = [
    ["schema_version", 2], ["generated_at", "not-a-time"], ["generated_at", "2026-02-30T00:00:00Z"],
    ["generated_at", "2026-01-01T00:00:00-08:00"], ["campaign_id", "other-campaign"], ["campaign_id", ""],
    ["law_package_status", "unknown"], ["production_ready", 1], ["safe_to_send_automatically", "false"],
    ["next_actions", ["x".repeat(241)]], ["next_actions", Array(41).fill("Review.")], ["next_actions", [null]],
    ["next_actions", "Review."], ["counts", []], ["extra", true],
  ];
  it.each(invalid)("rejects malformed %s", async (key, value) => {
    const response = await post({ ...snapshot(), [key]: value });
    expect(response.status).toBe(400);
    expect(await response.json()).toEqual({ error: key });
    await noRow();
  });

  it.each(COUNT_KEYS)("validates count %s including missing, fraction, negative and extra", async key => {
    for (const value of [undefined, 0.5, -1, "1", true]) {
      const body = snapshot();
      const counts = body.counts as Record<string, unknown>;
      if (value === undefined) delete counts[key]; else counts[key] = value;
      const response = await post(body);
      expect(response.status).toBe(400);
      expect(await response.json()).toEqual({ error: "counts." + key });
      await noRow();
    }
    const body = snapshot();
    body.counts.extra = 0;
    const response = await post(body);
    expect(response.status).toBe(400);
    expect(await response.json()).toEqual({ error: "counts.extra" });
    await noRow();
  });

  it("refuses malformed JSON and ten-minute future timestamp; permits the 300s boundary", async () => {
    const malformed = await call(new Request(URL, { method: "POST", headers: runnerHeaders(), body: "{" }));
    expect(malformed.status).toBe(400);
    await noRow();
    const future = snapshot(new Date(Date.now() + 600_000));
    const response = await post(future);
    expect(response.status).toBe(400);
    expect(await response.json()).toEqual({ error: "generated_at" });
    await noRow();
    expect((await post(snapshot(new Date(Date.now() + 300_000)))).status).toBe(200);
  });

  it("accepts equal-time replay idempotently and rejects older without changing storage", async () => {
    const repo = await seedCampaign();
    const body = snapshot();
    expect((await post(body)).status).toBe(200);
    expect((await post(body)).status).toBe(200);
    expect((await post({ ...body, generated_at: new Date(Date.parse(body.generated_at) - 1000).toISOString() })).status).toBe(409);
    expect(await repo.setting("readiness_snapshot")).toEqual(body);
    const rejected = await post({ ...body, extra: true });
    expect(rejected.status).toBe(400);
    expect(await repo.setting("readiness_snapshot")).toEqual(body);
  });

  it("never makes an absent snapshot current or invents a next action", async () => {
    const repo = await seedCampaign();
    expect(await readReadiness(repo, new Date())).toEqual({ present: false, current: false, reason: "absent",
      generated_at: null, age_seconds: null, next_actions: [], law_package_status: null, counts: {} });
  });

  it.each(['{"schema_version":2}', 'not-json', 'null', '[]'])("returns unparseable for a stored invalid row %s", async value => {
    await rawSetting(value);
    expect(await readReadiness(await seedCampaign(), new Date())).toMatchObject({ present: true, current: false, reason: "unparseable", next_actions: [] });
  });

  it("wrong campaign is not current and cannot supply next actions", async () => {
    const body = { ...snapshot(), campaign_id: "wrong-campaign" };
    await rawSetting(JSON.stringify(body));
    const result = await readReadiness(await seedCampaign(), new Date(Date.parse(body.generated_at)));
    expect(result).toMatchObject({ present: true, current: false, reason: "wrong_campaign", generated_at: body.generated_at, age_seconds: 0, next_actions: [] });
  });

  it("is current exactly at 36h; one second past is stale, retaining timestamp and age", async () => {
    const repo = await seedCampaign();
    const body = snapshot();
    await repo.putSetting("readiness_snapshot", body);
    for (const [age, current, reason] of [[MAX_AGE_SECONDS, true, null], [MAX_AGE_SECONDS + 1, false, "stale"]] as const) {
      expect(await readReadiness(repo, new Date(Date.parse(body.generated_at) + age * 1000))).toMatchObject({
        present: true, current, reason, generated_at: body.generated_at, age_seconds: age, next_actions: body.next_actions });
    }
  });

  it("invalid stored future cannot masquerade as current; a corrupt row can be replaced", async () => {
    const repo = await seedCampaign();
    await repo.putSetting("readiness_snapshot", snapshot(new Date(Date.now() + 600_000)));
    expect(await readReadiness(repo, new Date())).toMatchObject({ current: false, reason: "unparseable", next_actions: [] });
    await rawSetting("broken");
    expect((await post(snapshot())).status).toBe(200);
    expect((await readReadiness(repo, new Date())).current).toBe(true);
  });

  it("keeps exact 240 Unicode code-point/40-entry limits without truncation", async () => {
    const body = snapshot();
    body.next_actions = Array(40).fill("😀".repeat(240));
    expect(readinessError(body)).toBeNull();
    expect((await post(body)).status).toBe(200);
    expect((await readReadiness(await seedCampaign(), new Date())).next_actions).toEqual(body.next_actions);
    body.next_actions = ["😀".repeat(241)];
    expect(readinessError(body)).toBe("next_actions");
  });
});
