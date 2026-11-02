import { env } from "cloudflare:test";
import { expect, it } from "vitest";
import { Repo } from "../src/db.ts";
import { seedCampaign } from "./helpers.ts";

it("idle polling reads a constant number of rows despite terminal job and claim history", async () => {
  const base = await seedCampaign();
  for (const count of [100, 1000, 10000]) {
    const campaignId = crypto.randomUUID();
    const r = new Repo(env.DB, campaignId);
    await r.createCampaign({ ...(await base.campaign())!, name: "Synthetic idle queue" });
    const clock = "2026-01-01T00:00:00.000Z";
    await env.DB.batch([
      env.DB.prepare("WITH RECURSIVE n(i) AS (VALUES(1) UNION ALL SELECT i + 1 FROM n WHERE i < ?) " +
        "INSERT INTO job (job_id, campaign_id, kind, idempotency_key, inputs_json, state, attempt, claim_token, enqueued_at, created_at, updated_at) " +
        "SELECT ? || ':' || i, ?, 'extract', 'history:' || i, '{}', " +
        "CASE i % 3 WHEN 0 THEN 'done' WHEN 1 THEN 'failed' ELSE 'blocked' END, 1, 'claim:' || i, ?, ?, ? FROM n")
        .bind(count, campaignId, campaignId, clock, clock, clock),
      env.DB.prepare("INSERT INTO job_claim (campaign_id, job_id, claim_token, attempt, leased_until, state, completion_id, created_at, updated_at) " +
        "SELECT campaign_id, job_id, claim_token, attempt, ?, state, 'old:' || job_id, ?, ? FROM job WHERE campaign_id = ?")
        .bind(clock, clock, clock, campaignId),
    ]);
    const reads: number[] = [];
    const db = new Proxy(env.DB, {
      get(target, property) {
        if (property === "batch") return async (statements: D1PreparedStatement[]) => {
          const results = await target.batch(statements);
          reads.push(...results.map(result => result.meta.rows_read));
          return results;
        };
        const value = Reflect.get(target, property);
        return typeof value === "function" ? value.bind(target) : value;
      },
    });
    expect(await new Repo(db, campaignId).leaseJob(300, new Date(clock))).toBeNull();
    expect(reads).toHaveLength(6);
    console.log(`idle poll with ${count} terminal jobs: rows_read=${JSON.stringify(reads)}`);
    expect(reads.reduce((sum, rows) => sum + rows, 0), `${count} terminal jobs`).toBeLessThanOrEqual(100);
  }
});
