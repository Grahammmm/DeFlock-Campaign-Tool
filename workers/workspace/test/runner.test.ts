import { env } from "cloudflare:test";
import { describe, expect, it } from "vitest";
import { sha256Hex } from "@deflock/shared/ids";
import { call, leaseExactly, runnerHeaders, seedCampaign } from "./helpers.ts";
import { safeSitePath } from "../src/runner.ts";

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

describe("runner API: followups, classification patch, site staging", () => {
  async function siteHeaders(bytes: Uint8Array | string, contentType = "text/html; charset=utf-8", extra: Record<string, string> = {}) {
    const data = typeof bytes === "string" ? new TextEncoder().encode(bytes) : bytes;
    return runnerHeaders({ "content-type": contentType, "x-object-sha256": await sha256Hex(data), ...extra });
  }

  it("done results enqueue follow-up jobs idempotently and never a send_request", async () => {
    const repo = await seedCampaign();
    const { row } = await repo.enqueueJob("classify_mail", "kf-" + crypto.randomUUID(), { correspondence_id: "cor_missing", raw_sha256: "a".repeat(64) });
    await leaseExactly(row.job_id);
    const key = await sha256Hex("followup-" + crypto.randomUUID());
    const digestSha = await sha256Hex("digest-" + crypto.randomUUID());
    const body = JSON.stringify({
      status: "done",
      outputs: {
        followups: [
          { kind: "extract", idempotency_key: key, inputs: { sha256: "c".repeat(64) } },
          { kind: "extract", idempotency_key: key, inputs: { sha256: "c".repeat(64) } },
          { kind: "send_request", inputs: { request_id: "req_x" } },
          { kind: "launch_missiles" },
          { kind: "digest", inputs: { sha256: digestSha } },
          { kind: "digest", idempotency_key: "not-hex", inputs: { sha256: digestSha } },
          "garbage",
        ],
      },
    });
    const res = await call(new Request(`${BASE}/jobs/${row.job_id}/result`, { method: "POST", headers: runnerHeaders(), body }));
    expect(res.status).toBe(200);
    const json = (await res.json()) as { state: string; followups: string[] };
    expect(json.state).toBe("done");
    // duplicate key returns the same job id twice; the digest without a key derives one from kind+inputs,
    // so the second digest entry (bad key -> derived) collapses onto it too.
    expect(json.followups.length).toBe(4);
    expect(new Set(json.followups).size).toBe(2);
    const jobs = await repo.jobs();
    const extracts = jobs.filter((j) => j.idempotency_key === key);
    expect(extracts.length).toBe(1);
    expect(extracts[0]!.kind).toBe("extract");
    expect(extracts[0]!.state).toBe("queued");
    const derived = await sha256Hex("digest" + JSON.stringify({ sha256: digestSha }));
    const digests = jobs.filter((j) => j.idempotency_key === derived);
    expect(digests.length).toBe(1);
    expect(digests[0]!.kind).toBe("digest");
    expect(jobs.filter((j) => j.kind === "send_request").length).toBe(0);
    expect(jobs.some((j) => j.inputs_json.includes("req_x"))).toBe(false);
    // the finished job's outputs keep the followups list for audit, but a second result is a conflict
    expect((await repo.job(row.job_id))!.outputs_json).toContain('"followups"');
    expect((await call(new Request(`${BASE}/jobs/${row.job_id}/result`, { method: "POST", headers: runnerHeaders(), body }))).status).toBe(409);
  });

  it("failed and blocked results never enqueue followups", async () => {
    const repo = await seedCampaign();
    const marker = await sha256Hex("never-" + crypto.randomUUID());
    for (const status of ["failed", "blocked"]) {
      const { row } = await repo.enqueueJob("extract", "kn-" + crypto.randomUUID(), { sha256: marker }, 1);
      await leaseExactly(row.job_id);
      const res = await call(new Request(`${BASE}/jobs/${row.job_id}/result`, {
        method: "POST", headers: runnerHeaders(), body: JSON.stringify({ status, error: "x", outputs: { followups: [{ kind: "digest", idempotency_key: marker }] } }),
      }));
      expect(res.status).toBe(200);
      expect(((await res.json()) as { followups: string[] }).followups).toEqual([]);
    }
    expect((await repo.jobs()).filter((j) => j.idempotency_key === marker).length).toBe(0);
  });

  it("classify_mail results patch the correspondence row's classification only", async () => {
    const repo = await seedCampaign();
    const { row: corr } = await repo.createCorrespondence({
      request_id: null, direction: "inbound", channel: "email", provider_message_id: "cls-" + crypto.randomUUID() + "@example.invalid", from_addr: "a@example.invalid", to_addr: "b@example.invalid",
      subject: "Ack", received_at: "2026-09-30T10:00:00Z", raw_sha256: null, classification: "unclassified", classification_confidence: null, summary: null,
    });
    const { row } = await repo.enqueueJob("classify_mail", "kc-" + crypto.randomUUID(), { correspondence_id: corr.correspondence_id, raw_sha256: "a".repeat(64) });
    await leaseExactly(row.job_id);
    const body = JSON.stringify({ status: "done", outputs: { correspondence_update: { classification: "extension", classification_confidence: "high", summary: "Inbound email classified as extension", subject: "must not change", from_addr: "evil@example.invalid" } } });
    expect((await call(new Request(`${BASE}/jobs/${row.job_id}/result`, { method: "POST", headers: runnerHeaders(), body }))).status).toBe(200);
    const updated = await repo.correspondence(corr.correspondence_id);
    expect(updated?.classification).toBe("extension");
    expect(updated?.classification_confidence).toBe("high");
    expect(updated?.summary).toBe("Inbound email classified as extension");
    expect(updated?.subject).toBe("Ack");
    expect(updated?.from_addr).toBe("a@example.invalid");
    // an unknown label falls back to unclassified instead of violating the CHECK constraint
    const { row: row2 } = await repo.enqueueJob("classify_mail", "kc2-" + crypto.randomUUID(), { correspondence_id: corr.correspondence_id, raw_sha256: "e".repeat(64) });
    await leaseExactly(row2.job_id);
    await call(new Request(`${BASE}/jobs/${row2.job_id}/result`, { method: "POST", headers: runnerHeaders(), body: JSON.stringify({ status: "done", outputs: { correspondence_update: { classification: "weird" } } }) }));
    expect((await repo.correspondence(corr.correspondence_id))?.classification).toBe("unclassified");
    // a non-classify job cannot patch correspondence
    const { row: row3 } = await repo.enqueueJob("extract", "kc3-" + crypto.randomUUID(), { correspondence_id: corr.correspondence_id, sha256: "f".repeat(64) });
    await leaseExactly(row3.job_id);
    await call(new Request(`${BASE}/jobs/${row3.job_id}/result`, { method: "POST", headers: runnerHeaders(), body: JSON.stringify({ status: "done", outputs: { correspondence_update: { classification: "denial" } } }) }));
    expect((await repo.correspondence(corr.correspondence_id))?.classification).toBe("unclassified");
  });

  it("safeSitePath accepts plain relative files with allowlisted extensions only", () => {
    expect(safeSitePath("index.html")).toBe("index.html");
    expect(safeSitePath("findings/example-finding.html")).toBe("findings/example-finding.html");
    expect(safeSitePath("map/cameras.geojson")).toBe("map/cameras.geojson");
    expect(safeSitePath("vendor/maplibre-gl.js")).toBe("vendor/maplibre-gl.js");
    expect(safeSitePath("_headers")).toBe("_headers");
    expect(safeSitePath("assets/Logo.PNG")).toBe("assets/Logo.PNG");
    for (const bad of ["", "/index.html", "a//b.html", "../index.html", "findings/../index.html", "./index.html", ".hidden", ".env",
      "index.html/", "run.sh", "setup.exe", "notes", "dir/noext", "a\\b.html", "x.html\0", "x.php", "x.htm.bak", "x.sqlite", "secrets.key",
      "a".repeat(130) + ".html", Array(17).fill("d").join("/") + "/x.html"]) {
      expect(safeSitePath(bad), bad).toBeNull();
    }
  });

  it("site staging writes to the public bucket under sites/<version>/ after hash, path and size checks", async () => {
    await seedCampaign();
    const html = new TextEncoder().encode("<!doctype html><title>synthetic</title>");
    let res = await call(new Request(`${BASE}/site/v1.0/findings/index.html`, { method: "PUT", headers: await siteHeaders(html), body: html }));
    expect(res.status).toBe(201);
    const json = (await res.json()) as { key: string; sha256: string; bytes: number };
    expect(json.key).toBe("sites/v1.0/findings/index.html");
    expect(json.sha256).toBe(await sha256Hex(html));
    expect(json.bytes).toBe(html.byteLength);
    const stored = await env.PUBLIC_BUCKET.get("sites/v1.0/findings/index.html");
    expect(await stored?.text()).toBe("<!doctype html><title>synthetic</title>");
    expect(stored?.httpMetadata?.contentType).toBe("text/html; charset=utf-8");
    expect(stored?.customMetadata?.sha256).toBe(json.sha256);
    // re-upload overwrites the same key in place (idempotent staging)
    res = await call(new Request(`${BASE}/site/v1.0/findings/index.html`, { method: "PUT", headers: await siteHeaders(html), body: html }));
    expect(res.status).toBe(201);
    const headers = "/*\n  X-Test: 1\n";
    res = await call(new Request(`${BASE}/site/v1.0/_headers`, { method: "PUT", headers: await siteHeaders(headers, "text/plain"), body: headers }));
    expect(res.status).toBe(201);
    expect(await (await env.PUBLIC_BUCKET.get("sites/v1.0/_headers"))?.text()).toBe(headers);
    // rejections: version, traversal, hidden, extension, missing/mismatched hash, size, auth
    expect((await call(new Request(`${BASE}/site/Bad%20Version/index.html`, { method: "PUT", headers: await siteHeaders(html), body: html }))).status).toBe(400);
    expect((await call(new Request(`${BASE}/site/v1.0/..%2Fescape.html`, { method: "PUT", headers: await siteHeaders(html), body: html }))).status).toBe(400);
    expect((await call(new Request(`${BASE}/site/v1.0/findings/..%2F..%2Fescape.html`, { method: "PUT", headers: await siteHeaders(html), body: html }))).status).toBe(400);
    expect((await call(new Request(`${BASE}/site/v1.0/.hidden`, { method: "PUT", headers: await siteHeaders(html), body: html }))).status).toBe(400);
    expect((await call(new Request(`${BASE}/site/v1.0/run.sh`, { method: "PUT", headers: await siteHeaders(html), body: html }))).status).toBe(400);
    expect((await call(new Request(`${BASE}/site/v1.0/%ZZ.html`, { method: "PUT", headers: await siteHeaders(html), body: html }))).status).toBe(400);
    expect((await call(new Request(`${BASE}/site/v1.0/index.html`, { method: "PUT", headers: runnerHeaders({ "content-type": "text/html" }), body: html }))).status).toBe(400);
    expect((await call(new Request(`${BASE}/site/v1.0/index.html`, { method: "PUT", headers: runnerHeaders({ "content-type": "text/html", "x-object-sha256": "0".repeat(64) }), body: html }))).status).toBe(400);
    expect((await call(new Request(`${BASE}/site/v1.0/index.html`, { method: "PUT", headers: await siteHeaders(html, "text/html", { "content-length": String(17 * 1024 * 1024) }), body: html }))).status).toBe(413);
    expect((await call(new Request(`${BASE}/site/v1.0/index.html`, { method: "PUT", body: html }))).status).toBe(401);
    expect(await env.PUBLIC_BUCKET.head("sites/v1.0/index.html")).toBeNull();
    expect(await env.PUBLIC_BUCKET.head("sites/v1.0/run.sh")).toBeNull();
    expect(await env.PUBLIC_BUCKET.head("sites/escape.html")).toBeNull();
    expect(await env.PUBLIC_BUCKET.head("escape.html")).toBeNull();
    // oversized body with an honest content-length is also refused before any write
    const big = new Uint8Array(16 * 1024 * 1024 + 1);
    res = await call(new Request(`${BASE}/site/v1.0/big.js`, { method: "PUT", headers: await siteHeaders(big, "text/javascript"), body: big }));
    expect(res.status).toBe(413);
    expect(await env.PUBLIC_BUCKET.head("sites/v1.0/big.js")).toBeNull();
  });
});
