import { env } from "cloudflare:test";
import { describe, expect, it } from "vitest";
import { sha256Hex } from "@deflock/shared/ids";
import { call, runnerHeaders, seedCampaign } from "./helpers.ts";

const BASE = "https://workspace.example.invalid/api/runner";

describe("runner API", () => {
  it("rejects requests without the bearer token", async () => {
    await seedCampaign();
    expect((await call(new Request(BASE + "/jobs"))).status).toBe(401);
    expect((await call(new Request(BASE + "/jobs", { headers: { authorization: "Bearer wrong" } }))).status).toBe(401);
  });

  it("204 when no job; lease is atomic and expired leases requeue", async () => {
    const repo = await seedCampaign();
    expect((await call(new Request(BASE + "/jobs?lease=300", { headers: runnerHeaders() }))).status).toBe(204);
    await repo.enqueueJob("intake", "k1", { a: 1 });
    await repo.enqueueJob("digest", "k2", { b: 2 });
    // two concurrent leases must hand out two different jobs, a third gets nothing
    const [r1, r2, r3] = await Promise.all([
      call(new Request(BASE + "/jobs?lease=60", { headers: runnerHeaders() })),
      call(new Request(BASE + "/jobs?lease=60", { headers: runnerHeaders() })),
      call(new Request(BASE + "/jobs?lease=60", { headers: runnerHeaders() })),
    ]);
    const statuses = [r1.status, r2.status, r3.status].sort();
    expect(statuses).toEqual([200, 200, 204]);
    const leased = (await Promise.all([r1, r2, r3].filter((r) => r.status === 200).map((r) => r.json()))) as { job_id: string; kind: string; attempt: number; privacy_tier: string }[];
    expect(new Set(leased.map((j) => j.job_id)).size).toBe(2);
    expect(leased.every((j) => j.attempt === 1 && j.privacy_tier === "redacted_cloud")).toBe(true);
    // the same idempotency key does not enqueue again
    const dup = await repo.enqueueJob("intake", "k1", { a: 1 });
    expect(dup.inserted).toBe(false);
    // expired lease: lease again with a clock past leased_until
    const future = new Date(Date.now() + 120_000);
    const requeued = await repo.leaseJob(60, future);
    expect(requeued?.attempt).toBe(2);
    // exhausting attempts stops the requeue loop
    const again = await repo.leaseJob(60, new Date(future.getTime() + 120_000));
    expect(again?.attempt).toBe(3);
    const none = await repo.leaseJob(60, new Date(future.getTime() + 240_000));
    expect(none === null || none.job_id !== requeued!.job_id).toBe(true);
  });

  it("result: done finishes, failed with attempts left requeues, failed at max stays failed", async () => {
    const repo = await seedCampaign();
    const { row } = await repo.enqueueJob("extract", "kx", {}, 2);
    let leased = (await (await call(new Request(BASE + "/jobs", { headers: runnerHeaders() }))).json()) as { job_id: string };
    expect(leased.job_id).toBe(row.job_id);
    let res = await call(new Request(`${BASE}/jobs/${row.job_id}/result`, { method: "POST", headers: runnerHeaders(), body: JSON.stringify({ status: "failed", error: "boom" }) }));
    expect(((await res.json()) as { state: string }).state).toBe("queued");
    leased = (await (await call(new Request(BASE + "/jobs", { headers: runnerHeaders() }))).json()) as { job_id: string };
    res = await call(new Request(`${BASE}/jobs/${row.job_id}/result`, { method: "POST", headers: runnerHeaders(), body: JSON.stringify({ status: "failed", error: "boom again" }) }));
    expect(((await res.json()) as { state: string }).state).toBe("failed");
    expect((await repo.incidents()).length).toBe(1);
    // result on a job that is not leased is a conflict
    res = await call(new Request(`${BASE}/jobs/${row.job_id}/result`, { method: "POST", headers: runnerHeaders(), body: JSON.stringify({ status: "done" }) }));
    expect(res.status).toBe(409);
    res = await call(new Request(`${BASE}/jobs/${row.job_id}/result`, { method: "POST", headers: runnerHeaders(), body: JSON.stringify({ status: "weird" }) }));
    expect(res.status).toBe(400);
  });

  it("proposals are idempotent on idempotency_key", async () => {
    await seedCampaign();
    const body = JSON.stringify({ kind: "send_request", subject_id: "req_x", proposal: { to: "records@example.invalid" }, idempotency_key: "p1", proposed_by: "job_abc" });
    const a = await call(new Request(BASE + "/proposals", { method: "POST", headers: runnerHeaders(), body }));
    const b = await call(new Request(BASE + "/proposals", { method: "POST", headers: runnerHeaders(), body }));
    expect(a.status).toBe(201);
    expect(b.status).toBe(200);
    const ja = (await a.json()) as { action_id: string; created: boolean };
    const jb = (await b.json()) as { action_id: string; created: boolean };
    expect(ja.action_id).toBe(jb.action_id);
    expect([ja.created, jb.created]).toEqual([true, false]);
    const bad = await call(new Request(BASE + "/proposals", { method: "POST", headers: runnerHeaders(), body: JSON.stringify({ kind: "launch_missiles", proposal: {}, idempotency_key: "z", proposed_by: "x" }) }));
    expect(bad.status).toBe(400);
  });

  it("originals: hash mismatch rejected, matching hash stored, GET streams bytes back", async () => {
    await seedCampaign();
    const bytes = new TextEncoder().encode("synthetic policy text");
    const sha = await sha256Hex(bytes);
    const wrong = "0".repeat(64);
    let res = await call(new Request(`${BASE}/originals/${wrong}`, { method: "PUT", headers: runnerHeaders({ "content-type": "text/plain" }), body: bytes }));
    expect(res.status).toBe(400);
    expect(await env.ORIGINALS.head("originals/" + wrong)).toBeNull();
    res = await call(new Request(`${BASE}/originals/${sha}`, { method: "PUT", headers: runnerHeaders({ "content-type": "text/plain" }), body: bytes }));
    expect(res.status).toBe(201);
    res = await call(new Request(`${BASE}/originals/${sha}`, { method: "PUT", headers: runnerHeaders({ "content-type": "text/plain" }), body: bytes }));
    expect(res.status).toBe(200);
    res = await call(new Request(`${BASE}/originals/${sha}`, { headers: runnerHeaders() }));
    expect(res.status).toBe(200);
    expect(res.headers.get("x-object-sha256")).toBe(sha);
    expect(await res.text()).toBe("synthetic policy text");
    expect((await call(new Request(`${BASE}/originals/not-hex`, { headers: runnerHeaders() }))).status).toBe(400);
  });

  it("receipts are idempotent and require a stored original; correspondence dedupes", async () => {
    await seedCampaign();
    const bytes = new TextEncoder().encode("attachment bytes");
    const sha = await sha256Hex(bytes);
    let res = await call(new Request(`${BASE}/receipts`, { method: "POST", headers: runnerHeaders(), body: JSON.stringify({ sha256: sha, source_id: "msg-1#1", original_name: "policy.pdf" }) }));
    expect(res.status).toBe(409);
    await call(new Request(`${BASE}/originals/${sha}`, { method: "PUT", headers: runnerHeaders(), body: bytes }));
    res = await call(new Request(`${BASE}/receipts`, { method: "POST", headers: runnerHeaders(), body: JSON.stringify({ sha256: sha, source_id: "msg-1#1", original_name: "policy.pdf" }) }));
    expect(res.status).toBe(201);
    const first = (await res.json()) as { receipt_id: string };
    expect(first.receipt_id).toBe(await sha256Hex(JSON.stringify(["msg-1#1", sha])));
    res = await call(new Request(`${BASE}/receipts`, { method: "POST", headers: runnerHeaders(), body: JSON.stringify({ sha256: sha, source_id: "msg-1#1", original_name: "policy.pdf" }) }));
    expect(res.status).toBe(200);
    const corr = { direction: "inbound", channel: "portal", provider_message_id: "portal-77", subject: "Ack" };
    const c1 = await call(new Request(`${BASE}/correspondence`, { method: "POST", headers: runnerHeaders(), body: JSON.stringify(corr) }));
    const c2 = await call(new Request(`${BASE}/correspondence`, { method: "POST", headers: runnerHeaders(), body: JSON.stringify(corr) }));
    expect([c1.status, c2.status]).toEqual([201, 200]);
    expect(((await c1.json()) as { correspondence_id: string }).correspondence_id).toBe(((await c2.json()) as { correspondence_id: string }).correspondence_id);
  });
});
