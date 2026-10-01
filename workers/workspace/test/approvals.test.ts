import { env } from "cloudflare:test";
import { describe, expect, it } from "vitest";
import { approveAction, executeAction, ApprovalError } from "../src/approvals.ts";
import type { AccessIdentity } from "../src/auth.ts";
import type { Env } from "../src/env.ts";
import { call, makeSigner, organizerRequest, seedCampaign } from "./helpers.ts";

const ORG: AccessIdentity = { email: "organizer@example.invalid", sub: "s", issued_at: 0, expires_at: 0 };

describe("approval cards", () => {
  it("approval requires an identity and a proposed card", async () => {
    const repo = await seedCampaign();
    const { row } = await repo.propose("deploy_site", null, { site_version: "v1" }, "d1", "job_1");
    await expect(approveAction(repo, row.action_id, { email: "" } as AccessIdentity)).rejects.toThrow(ApprovalError);
    await expect(approveAction(repo, row.action_id, undefined as unknown as AccessIdentity)).rejects.toThrow(/identity/);
    const approved = await approveAction(repo, row.action_id, ORG);
    expect(approved.state).toBe("approved");
    expect(approved.approved_by).toBe("organizer@example.invalid");
    await expect(approveAction(repo, row.action_id, ORG)).rejects.toThrow(/not proposed/);
  });

  it("execute refuses non-approved cards; send_request without a sender fails with sender_not_configured", async () => {
    const repo = await seedCampaign();
    const { row } = await repo.propose("send_request", null, { channel: "email", to: "records@example.invalid", subject: "Request", body_md: "Body" }, "s1", "job_2");
    await expect(executeAction(repo, env as Env, row.action_id, ORG)).rejects.toThrow(/only approved/);
    await approveAction(repo, row.action_id, ORG);
    const done = await executeAction(repo, env as Env, row.action_id, ORG);
    expect(done.state).toBe("failed");
    expect(done.error).toMatch(/^sender_not_configured: /);
    // a card with no executor at all stays approved
    await approveAction(repo, (await repo.propose("send_request", null, { channel: "email", subject: "x", body_md: "y" }, "s1b", "job_2b")).row.action_id, ORG);
    const empty = new Map();
    const stuck = (await repo.actions("approved"))[0];
    await expect(executeAction(repo, env as Env, stuck.action_id, ORG, empty)).rejects.toThrow(/no executor registered/);
    expect((await repo.action(stuck.action_id))!.state).toBe("approved");
  });

  it("deploy_site executes once, flips site_version and records a receipt", async () => {
    const repo = await seedCampaign();
    await repo.putSetting("site_version", "v0");
    const { row } = await repo.propose("deploy_site", null, { site_version: "v1" }, "d2", "job_3");
    await approveAction(repo, row.action_id, ORG);
    const done = await executeAction(repo, env as Env, row.action_id, ORG);
    expect(done.state).toBe("executed");
    expect(done.provider_receipt).toContain('"previous":"v0"');
    expect(await repo.setting("site_version")).toBe("v1");
    expect(await env.CACHE.get("site_version")).toBe("v1");
    // idempotent: a second execute is a no-op returning the same receipt
    const again = await executeAction(repo, env as Env, row.action_id, ORG);
    expect(again.state).toBe("executed");
    expect(again.provider_receipt).toBe(done.provider_receipt);
    expect(again.executed_at).toBe(done.executed_at);
  });

  it("HTTP: approve uses the Access identity, POST /api/deploy-site executes", async () => {
    const repo = await seedCampaign();
    const signer = await makeSigner();
    const { row } = await repo.propose("deploy_site", null, { site_version: "2026-09-30a" }, "d3", "job_4");
    let r = await organizerRequest(signer, "lead@example.invalid", `/approvals/${row.action_id}/approve`, { method: "POST", headers: { accept: "application/json" } });
    let res = await call(r.request, r.overrides);
    expect(res.status).toBe(200);
    expect(((await res.json()) as { approved_by: string }).approved_by).toBe("lead@example.invalid");
    r = await organizerRequest(signer, "lead@example.invalid", "/api/deploy-site", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ action_id: row.action_id }) });
    res = await call(r.request, r.overrides);
    expect(res.status).toBe(200);
    expect(((await res.json()) as { site_version: string }).site_version).toBe("2026-09-30a");
    // unauthenticated approve is refused before touching the card
    const { row: other } = await repo.propose("deploy_site", null, { site_version: "x" }, "d4", "job_5");
    res = await call(new Request(`https://workspace.example.invalid/approvals/${other.action_id}/approve`, { method: "POST" }), r.overrides);
    expect(res.status).toBe(401);
    expect((await repo.action(other.action_id))!.state).toBe("proposed");
  });

  it("the approvals screen renders cards", async () => {
    const repo = await seedCampaign();
    await repo.propose("send_newsletter", null, { subject: "Update" }, "n1", "job_6");
    const signer = await makeSigner();
    const r = await organizerRequest(signer, "lead@example.invalid", "/approvals");
    const res = await call(r.request, r.overrides);
    expect(res.status).toBe(200);
    const text = await res.text();
    expect(text).toContain("send_newsletter");
    expect(text).toContain("Signed in as lead@example.invalid");
  });
});
