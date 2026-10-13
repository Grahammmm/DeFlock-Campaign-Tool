import { env } from "cloudflare:test";
import { describe, expect, it } from "vitest";
import { sha256Hex } from "@deflock/shared/ids";
import { Repo, type JobCompletion, type JobRow } from "../src/db.ts";
import { call, runnerHeaders, seedCampaign } from "./helpers.ts";

const BASE = "https://workspace.example.invalid/api/runner";

async function campaign(): Promise<Repo> {
  const base = await seedCampaign();
  const r = new Repo(env.DB, crypto.randomUUID());
  await r.createCampaign({ ...(await base.campaign())!, name: "Synthetic queue recovery" });
  return r;
}

async function post(r: Repo, job: JobRow, body: Record<string, unknown>, db = env.DB): Promise<Response> {
  return call(new Request(`${BASE}/jobs/${job.job_id}/result`, {
    method: "POST", headers: runnerHeaders(), body: JSON.stringify({ claim_token: job.claim_token, ...body }),
  }), { CAMPAIGN_ID: r.campaignId, DB: db });
}

async function renew(r: Repo, job: JobRow, lease_seconds = 300): Promise<Response> {
  return call(new Request(`${BASE}/jobs/${job.job_id}/renew`, {
    method: "POST", headers: runnerHeaders(), body: JSON.stringify({ claim_token: job.claim_token, lease_seconds }),
  }), { CAMPAIGN_ID: r.campaignId });
}

function withBatch(batch: (statements: D1PreparedStatement[]) => Promise<unknown>): D1Database {
  return new Proxy(env.DB, {
    get(target, property) {
      if (property === "batch") return batch;
      const value = Reflect.get(target, property);
      return typeof value === "function" ? value.bind(target) : value;
    },
  });
}

async function richJob(r: Repo) {
  const { row: correspondence } = await r.createCorrespondence({
    request_id: null, direction: "inbound", channel: "email", provider_message_id: crypto.randomUUID(),
    from_addr: null, to_addr: null, subject: "Synthetic evidence", received_at: new Date().toISOString(), raw_sha256: null,
    classification: "unclassified", classification_confidence: null, summary: null,
  });
  const keys = [await sha256Hex(crypto.randomUUID()), await sha256Hex(crypto.randomUUID())];
  await r.enqueueJob("classify_mail", crypto.randomUUID(), { correspondence_id: correspondence.correspondence_id });
  const job = (await r.leaseJob(300))!;
  const body = {
    status: "done", receipt: { engine: "synthetic" }, outputs: {
      followups: keys.map((key, i) => ({ kind: "digest", idempotency_key: key, inputs: { fixture: i } })),
      correspondence_update: { classification: "production", classification_confidence: "high", summary: "Synthetic production" },
    },
  };
  return { job, body, keys, correspondence };
}

async function assertRichComplete(r: Repo, fixture: Awaited<ReturnType<typeof richJob>>) {
  expect((await r.job(fixture.job.job_id))?.state).toBe("done");
  const jobs = await r.jobs();
  for (const key of fixture.keys) expect(jobs.filter(job => job.idempotency_key === key)).toHaveLength(1);
  expect((await r.correspondence(fixture.correspondence.correspondence_id))?.classification).toBe("production");
  const evidence = await env.DB.prepare("SELECT * FROM job_claim WHERE campaign_id = ? AND job_id = ?")
    .bind(r.campaignId, fixture.job.job_id).first<{ result_sha256: string; response_json: string }>();
  expect(evidence?.result_sha256).toMatch(/^[0-9a-f]{64}$/);
  expect(JSON.parse(evidence!.response_json).followups).toHaveLength(2);
}

describe("hosted queue fencing and recovery on local D1", () => {
  it("refuses tokenless/malformed results before any state changes", async () => {
    const r = await campaign();
    await r.enqueueJob("extract", crypto.randomUUID(), {});
    const job = (await r.leaseJob(300))!;
    for (const body of [{ status: "done", claim_token: null }, { status: "done", outputs: [] }, { status: "done", error: 3 }]) {
      expect((await post(r, job, body)).status).toBe(400);
    }
    expect((await r.job(job.job_id))?.state).toBe("leased");
    expect((await renew(r, job, 29)).status).toBe(400);
    expect((await renew(r, job, 3601)).status).toBe(400);
  });

  it("rejects expired A before reclaim and stale A after B takes the lease", async () => {
    const r = await campaign();
    await r.enqueueJob("extract", crypto.randomUUID(), {});
    const a = (await r.leaseJob(30, new Date(Date.now() - 60_000)))!;
    expect((await renew(r, a)).status).toBe(409);
    expect((await post(r, a, { status: "done" })).status).toBe(409);
    const b = (await r.leaseJob(300))!;
    expect(b.claim_token).not.toBe(a.claim_token);
    expect(b.attempt).toBe(2);
    const badKey = await sha256Hex("stale-" + crypto.randomUUID());
    for (const status of ["done", "failed"]) {
      const response = await post(r, a, { status, outputs: { followups: [{ kind: "digest", idempotency_key: badKey }] } });
      expect(response.status).toBe(409);
      expect(await response.json()).toEqual({ error: "claim_conflict" });
    }
    expect((await renew(r, a)).status).toBe(409);
    expect((await r.job(a.job_id))?.claim_token).toBe(b.claim_token);
    expect((await r.jobs()).some(job => job.idempotency_key === badKey)).toBe(false);
    expect((await post(r, b, { status: "done", outputs: { worker: "B" } })).status).toBe(200);
    expect(JSON.parse((await r.job(b.job_id))!.outputs_json!).worker).toBe("B");
  });

  it("renewal extends only its exact claim and lets a long synthetic handler finish", async () => {
    const r = await campaign();
    await r.enqueueJob("extract", crypto.randomUUID(), {});
    const clock = new Date();
    const job = (await r.leaseJob(30, clock))!;
    const later = new Date(clock.getTime() + 25_000);
    const renewed = (await r.renewJob(job.job_id, job.claim_token!, 60, later))!;
    expect(renewed.attempt).toBe(job.attempt);
    expect(renewed.leased_until).toBe(new Date(clock.getTime() + 85_000).toISOString());
    expect(await r.leaseJob(30, new Date(clock.getTime() + 35_000))).toBeNull();
    expect(await r.renewJob(job.job_id, "wrong-claim", 60, later)).toBeNull();
    expect(await r.renewJob(job.job_id, job.claim_token!, 60, new Date(clock.getTime() + 85_000))).toBeNull();
    const response = await renew(r, job, 120);
    expect(response.status).toBe(200);
    expect(await response.json()).toMatchObject({ job_id: job.job_id, claim_token: job.claim_token, attempt: 1 });
    expect((await post(r, job, { status: "done" })).status).toBe(200);
    expect((await renew(r, job)).status).toBe(409);
  });

  it("a failed attempt replays its original queued response without changing a newer claim", async () => {
    const r = await campaign();
    await r.enqueueJob("extract", crypto.randomUUID(), {}, 2);
    const a = (await r.leaseJob(300))!;
    const body = { status: "failed", error: "synthetic retry", outputs: { n: 1 } };
    const original = await (await post(r, a, body)).json();
    expect(original).toMatchObject({ state: "queued" });
    const b = (await r.leaseJob(300))!;
    const before = await r.job(b.job_id);
    expect(await (await post(r, a, body)).json()).toEqual(original);
    expect(await r.job(b.job_id)).toEqual(before);
    expect((await post(r, a, { ...body, error: "different" })).status).toBe(409);
    expect((await post(r, b, { status: "failed", error: "final failure" })).status).toBe(200);
    expect(await (await post(r, a, body)).json()).toEqual(original);
    expect((await r.job(b.job_id))?.state).toBe("failed");
    const evidence = await r.exportTable("job_claim");
    expect(evidence).toHaveLength(2);
    expect(evidence.every(row => row.result_sha256 && row.result_json && row.response_json)).toBe(true);
    expect(await r.incidents()).toHaveLength(1);
  });

  it("competing completions permit exactly one result and exact concurrent replay is stable", async () => {
    const r = await campaign();
    const fixture = await richJob(r);
    const responses = await Promise.all([
      post(r, fixture.job, fixture.body),
      post(r, fixture.job, { status: "blocked", error: "competing result" }),
    ]);
    expect(responses.map(response => response.status).sort()).toEqual([200, 409]);
    const winner = responses[0]!.status === 200 ? fixture.body : { status: "blocked", error: "competing result" };
    const accepted = await responses.find(response => response.status === 200)!.json();
    const replays = await Promise.all([post(r, fixture.job, winner), post(r, fixture.job, winner)]);
    for (const response of replays) {
      expect(response.status).toBe(200);
      expect(await response.json()).toEqual(accepted);
    }
    expect((await r.exportTable("job_claim")).filter(row => row.result_sha256)).toHaveLength(1);
  });

  // Faults are actual failing statements inside workerd's D1 transaction, not a fake database.
  // The statement boundaries cover claim acceptance, parent, both followups, classification,
  // incident and acknowledgment persistence. No durable partial completion may escape.
  for (const boundary of [1, 2, 3, 4, 5, 6]) {
    it(`rolls back every result effect on an interruption after batch statement ${boundary}`, async () => {
      const r = await campaign();
      const fixture = await richJob(r);
      const before = await r.job(fixture.job.job_id);
      const db = withBatch(statements => env.DB.batch([
        ...statements.slice(0, boundary), env.DB.prepare("SELECT * FROM synthetic_missing_fault_table"), ...statements.slice(boundary),
      ]));
      expect((await post(r, fixture.job, fixture.body, db)).status).toBe(500);
      expect(await r.job(fixture.job.job_id)).toEqual(before);
      expect((await r.jobs()).filter(job => fixture.keys.includes(job.idempotency_key))).toHaveLength(0);
      expect((await r.correspondence(fixture.correspondence.correspondence_id))?.classification).toBe("unclassified");
      expect((await r.exportTable("job_claim"))[0]!.result_sha256).toBeNull();
      expect((await post(r, fixture.job, fixture.body)).status).toBe(200);
      await assertRichComplete(r, fixture);
    });
  }

  it("a failing second followup insert rolls back the first and the parent, then replay recovers both", async () => {
    const r = await campaign();
    const fixture = await richJob(r);
    const trigger = "synthetic_enqueue_failure";
    await env.DB.exec(`CREATE TRIGGER ${trigger} BEFORE INSERT ON job WHEN NEW.idempotency_key = '${fixture.keys[1]}' BEGIN SELECT RAISE(ABORT, 'synthetic enqueue failure'); END;`);
    try {
      expect((await post(r, fixture.job, fixture.body)).status).toBe(500);
      expect((await r.job(fixture.job.job_id))?.state).toBe("leased");
      expect((await r.jobs()).filter(job => fixture.keys.includes(job.idempotency_key))).toHaveLength(0);
    } finally {
      await env.DB.exec(`DROP TRIGGER ${trigger}`);
    }
    expect((await post(r, fixture.job, fixture.body)).status).toBe(200);
    await assertRichComplete(r, fixture);
  });

  it("lost acknowledgment after commit recovers the identical response and key-order replay", async () => {
    const r = await campaign();
    const fixture = await richJob(r);
    const db = withBatch(async statements => {
      await env.DB.batch(statements);
      throw new Error("synthetic acknowledgment lost after transaction commit");
    });
    expect((await post(r, fixture.job, fixture.body, db)).status).toBe(500);
    await assertRichComplete(r, fixture);
    const replay = await post(r, fixture.job, fixture.body);
    expect(replay.status).toBe(200);
    const accepted = await replay.json();
    const reordered = {
      receipt: fixture.body.receipt,
      outputs: {
        correspondence_update: { summary: "Synthetic production", classification_confidence: "high", classification: "production" },
        followups: fixture.body.outputs.followups.map(item => ({ inputs: item.inputs, idempotency_key: item.idempotency_key, kind: item.kind })),
      },
      status: fixture.body.status,
    };
    expect(await (await post(r, fixture.job, reordered)).json()).toEqual(accepted);
    expect((await post(r, fixture.job, { ...fixture.body, receipt: { engine: "conflict" } })).status).toBe(409);
    await assertRichComplete(r, fixture);
  });

  it("expiry at the attempt cap is terminal with one incident and preserved previous evidence", async () => {
    const r = await campaign();
    await r.enqueueJob("extract", crypto.randomUUID(), {}, 2);
    const first = (await r.leaseJob(300))!;
    expect((await post(r, first, { status: "failed", error: "first attempt evidence" })).status).toBe(200);
    const final = (await r.leaseJob(30, new Date(Date.now() - 60_000)))!;
    expect(final.attempt).toBe(2);
    expect(await r.leaseJob(30)).toBeNull();
    for (let i = 0; i < 3; i++) expect(await r.sweepExpiredJobs()).toBe(0);
    const failed = (await r.job(final.job_id))!;
    expect(failed).toMatchObject({ state: "failed", error: "lease_attempts_exhausted", leased_until: null });
    expect(failed.finished_at).toBeTruthy();
    expect(await r.incidents()).toHaveLength(1);
    expect((await r.incidents())[0]!.count).toBe(1);
    expect((await r.counts()).jobs_queued).toBe(0);
    expect((await r.counts()).jobs_failed).toBe(1);
    expect((await post(r, final, { status: "done" })).status).toBe(409);
    const evidence = await r.exportTable("job_claim");
    expect(evidence).toHaveLength(2);
    expect(evidence.find(row => row.attempt === 1)!.result_json).toContain("first attempt evidence");
    expect(evidence.find(row => row.attempt === 2)!.state).toBe("expired");
  });

  it("does not accept another campaign's claim or write any result effects", async () => {
    const owner = await campaign();
    const other = await campaign();
    const fixture = await richJob(owner);
    expect((await post(other, fixture.job, fixture.body)).status).toBe(404);
    expect((await renew(other, fixture.job)).status).toBe(404);
    expect((await owner.job(fixture.job.job_id))?.state).toBe("leased");
    expect(await other.jobs()).toHaveLength(0);
    expect(await other.exportTable("job_claim")).toHaveLength(0);
  });

  it("a sweep incident failure rolls back terminalization and can be retried", async () => {
    const r = await campaign();
    await r.enqueueJob("extract", crypto.randomUUID(), {}, 1);
    const job = (await r.leaseJob(30, new Date(Date.now() - 60_000)))!;
    const db = withBatch(statements => env.DB.batch([
      ...statements.slice(0, 2), env.DB.prepare("SELECT * FROM synthetic_missing_sweep_table"), ...statements.slice(2),
    ]));
    await expect(new Repo(db, r.campaignId).sweepExpiredJobs()).rejects.toThrow();
    expect((await r.job(job.job_id))?.state).toBe("leased");
    expect((await r.exportTable("job_claim"))[0]!.state).toBe("leased");
    expect(await r.incidents()).toHaveLength(0);
    expect(await r.sweepExpiredJobs()).toBe(1);
    expect(await r.incidents()).toHaveLength(1);
  });

  it("expiry sweeping is bounded and later polls finish the remaining terminal attempts", async () => {
    const r = await campaign();
    const clock = new Date(Date.now() - 60_000).toISOString();
    const fixtures = Array.from({ length: 103 }, (_, index) => ({ id: crypto.randomUUID(), token: crypto.randomUUID(), key: `bounded-${index}` }));
    await env.DB.batch([
      env.DB.prepare("INSERT INTO job (job_id, campaign_id, kind, idempotency_key, inputs_json, state, attempt, max_attempts, leased_until, claim_token, enqueued_at, created_at, updated_at) " +
        "SELECT json_extract(value, '$.id'), ?, 'extract', json_extract(value, '$.key'), '{}', 'leased', 1, 1, ?, json_extract(value, '$.token'), ?, ?, ? FROM json_each(?)")
        .bind(r.campaignId, clock, clock, clock, clock, JSON.stringify(fixtures)),
      env.DB.prepare("INSERT INTO job_claim (campaign_id, job_id, claim_token, attempt, leased_until, state, created_at, updated_at) " +
        "SELECT campaign_id, job_id, claim_token, attempt, leased_until, state, created_at, updated_at FROM job WHERE campaign_id = ?")
        .bind(r.campaignId),
    ]);
    expect(await r.sweepExpiredJobs(new Date(), 2)).toBe(2);
    expect(await r.sweepExpiredJobs()).toBe(100);
    expect(await r.sweepExpiredJobs()).toBe(1);
    expect(await r.sweepExpiredJobs()).toBe(0);
    expect((await r.jobs()).every(job => job.state === "failed")).toBe(true);
    const incidents = await r.exportTable("incident");
    expect(incidents).toHaveLength(103);
    expect(incidents.every(incident => incident.count === 1)).toBe(true);
  });

  it("a completion racing final expiry has one fenced winner", async () => {
    for (let i = 0; i < 3; i++) {
      const r = await campaign();
      await r.enqueueJob("extract", crypto.randomUUID(), {}, 1);
      const clock = new Date();
      const job = (await r.leaseJob(30, clock))!;
      const payload = JSON.stringify({ status: "done", outputs: {} });
      const completion: JobCompletion = {
        job_id: job.job_id, claim_token: job.claim_token!, status: "done", outputs: {}, error: null,
        result_sha256: await sha256Hex(payload), result_json: payload, followups: [],
      };
      const [response, swept] = await Promise.all([
        r.finishJob(completion, new Date(clock.getTime() + 29_000)),
        r.sweepExpiredJobs(new Date(clock.getTime() + 30_000)),
      ]);
      const state = (await r.job(job.job_id))!.state;
      expect(["done", "failed"]).toContain(state);
      if (state === "done") {
        expect(response).toMatchObject({ state: "done" });
        expect(swept).toBe(0);
        expect(await r.incidents()).toHaveLength(0);
      } else {
        expect(response).toBe("claim_conflict");
        expect(swept).toBe(1);
        expect(await r.incidents()).toHaveLength(1);
      }
    }
  });
});
