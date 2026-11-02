import { applyD1Migrations, env } from "cloudflare:test";
import { expect, it } from "vitest";
import { Repo } from "../src/db.ts";

it("upgrades the immutable baseline without losing job evidence and matches the fresh schema", async () => {
  const db = env.DB_UPGRADE;
  await applyD1Migrations(db, env.TEST_MIGRATIONS.slice(0, 1));
  const campaign = "synthetic-upgrade";
  const clock = "2030-01-01T00:00:00.000Z";
  await db.prepare("INSERT INTO campaign (campaign_id, name, jurisdiction, county_fips, county_name, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)")
    .bind(campaign, "Synthetic upgrade", "us-ca", "00000", "Example", clock, clock).run();
  const fixtures = [
    { id: "queued", state: "queued", attempt: 0, max: 3, until: null, output: null, error: null, finished: null },
    { id: "leased", state: "leased", attempt: 1, max: 3, until: clock, output: '{"prior":"failed attempt"}', error: "prior failure", finished: null },
    { id: "exhausted", state: "leased", attempt: 1, max: 1, until: clock, output: null, error: null, finished: null },
    { id: "retry", state: "queued", attempt: 1, max: 3, until: null, output: '{"failure":"evidence"}', error: "failed before upgrade", finished: null },
    { id: "done", state: "done", attempt: 2, max: 3, until: null, output: '{"evidence":"preserved"}', error: null, finished: clock },
  ];
  for (const f of fixtures) {
    await db.prepare("INSERT INTO job (job_id, campaign_id, kind, idempotency_key, inputs_json, state, attempt, max_attempts, leased_until, outputs_json, error, enqueued_at, finished_at, created_at, updated_at) VALUES (?, ?, 'extract', ?, '{}', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)")
      .bind(f.id, campaign, f.id, f.state, f.attempt, f.max, f.until, f.output, f.error, clock, f.finished, clock, clock).run();
  }
  const before = (await db.prepare("SELECT * FROM job ORDER BY job_id").all<Record<string, unknown>>()).results;
  await applyD1Migrations(db, env.TEST_MIGRATIONS);
  const r = new Repo(db, campaign);
  const after = (await db.prepare("SELECT * FROM job ORDER BY job_id").all<Record<string, unknown>>()).results;
  expect(after.map(row => Object.fromEntries(Object.entries(row).filter(([key]) => key !== "claim_token")))).toEqual(before);
  expect(after.find(row => row.job_id === "queued")!.claim_token).toBeNull();
  expect(after.filter(row => row.attempt !== 0).every(row => String(row.claim_token).startsWith("legacy_"))).toBe(true);
  const claims = await r.exportTable("job_claim");
  expect(claims).toHaveLength(4);
  const done = claims.find(row => row.job_id === "done")!;
  expect(JSON.parse(String(done.result_json))).toEqual({ legacy_snapshot: {
    state: "done", outputs_json: '{"evidence":"preserved"}', error: null, finished_at: clock,
  } });
  // Running the migration helper again is a no-op, including the saved evidence.
  await applyD1Migrations(db, env.TEST_MIGRATIONS);
  expect(await r.exportTable("job_claim")).toEqual(claims);
  const time = new Date(clock);
  expect(await r.sweepExpiredJobs(time)).toBe(1);
  expect((await r.job("exhausted"))?.error).toBe("lease_attempts_exhausted");
  expect(await r.sweepExpiredJobs(time)).toBe(0);
  expect(await r.incidents()).toHaveLength(1);
  const leased: string[] = [];
  for (let i = 0; i < 3; i++) leased.push((await r.leaseJob(30, time))!.job_id);
  expect(leased.sort()).toEqual(["leased", "queued", "retry"]);
  expect((await r.job("leased"))?.attempt).toBe(2);
  expect((await r.job("retry"))?.attempt).toBe(2);
  expect((await r.job("done"))?.outputs_json).toBe('{"evidence":"preserved"}');
  expect((await r.exportTable("job_claim")).find(row => row.job_id === "leased" && row.attempt === 1)?.result_json)
    .toContain("failed attempt");

  // The wizard's current schema and the complete migration chain must stay equivalent.
  await env.DB_SCHEMA.batch(env.TEST_SCHEMA.map(sql => env.DB_SCHEMA.prepare(sql)));
  const tables = (await env.DB_SCHEMA.prepare("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '_cf_%' ORDER BY name").all<{ name: string }>()).results;
  // Batch introspection rather than spending hundreds of D1 round trips on schema parity.
  const queries = tables.flatMap(({ name }) => [`PRAGMA table_info(${name})`, `PRAGMA index_list(${name})`]);
  const migrated = await db.batch<{ name: string }>(queries.map(sql => db.prepare(sql)));
  const fresh = await env.DB_SCHEMA.batch<{ name: string }>(queries.map(sql => env.DB_SCHEMA.prepare(sql)));
  const indexQueries: string[] = [];
  for (let i = 0; i < queries.length; i += 2) {
    expect(migrated[i]!.results).toEqual(fresh[i]!.results);
    const indexes = fresh[i + 1]!.results;
    expect(migrated[i + 1]!.results.map(index => index.name).sort()).toEqual(indexes.map(index => index.name).sort());
    indexQueries.push(...indexes.map(index => `PRAGMA index_info(${index.name})`));
  }
  const migratedIndexes = await db.batch(indexQueries.map(sql => db.prepare(sql)));
  const freshIndexes = await env.DB_SCHEMA.batch(indexQueries.map(sql => env.DB_SCHEMA.prepare(sql)));
  expect(migratedIndexes.map(result => result.results)).toEqual(freshIndexes.map(result => result.results));
});
