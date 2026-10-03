import { env } from "cloudflare:test";
import { describe, expect, it } from "vitest";
import { approveAction, executeAction } from "../src/approvals.ts";
import { buildExecutors, defaultExecutors } from "../src/executors/index.ts";
import { canonicalOutboxKey, SignedOutboxSender, signOutboxFrame } from "../src/executors/outbox_sender.ts";
import type { OutboundMessage } from "../src/executors/types.ts";
import type { Env } from "../src/env.ts";
import { seedCampaign } from "./helpers.ts";

const SECRET = "synthetic-signing-key-for-tests-only-0000"; // pragma: allowlist secret -- public synthetic test key
const ORG = { email: "organizer@example.invalid", sub: "o", issued_at: 0, expires_at: 0 };
const EXEC = { ...ORG, email: "executor@example.invalid" };
const ENDPOINT = "https://outbox.example.invalid/send";
let number = 0;

async function setup(kind: "send_request" | "send_followup" = "send_request") {
  const repo = await seedCampaign();
  const request = await repo.createRequest({ agency_id: "ca-example-police", scope_id: "synthetic", scope_version: 2,
    subject: "Synthetic gateway " + ++number, body_md: "Synthetic body", channel: "email", fee_cap_cents: 500,
    state: "draft", sent_at: null, determination_due: null, extension_claimed_until: null,
    last_activity_at: null, next_action: null, external_ref: null });
  const proposal = { subject: request.subject, body_md: request.body_md, channel: "email", to: "records@example.invalid",
    agency_id: "forged", scope_version: 99, fee_cap_cents: 9999, outbox_binding: { agency_id: "forged", scope_version: 99, fee_cap_cents: 9999 } };
  const { row: action } = await repo.propose(kind, request.request_id, proposal, "gateway-" + number, ORG.email);
  return { repo, request, action };
}

function reply(frame: Record<string, unknown>, patch: Record<string, unknown> = {}) {
  return Response.json({ v: 1, action_id: frame.action_id, canonical_key: frame.canonical_key,
    provider: "email", provider_message_id: "synthetic-gateway-message-" + frame.action_id,
    sent_at: "2026-10-03T10:10:00.000Z", ...patch });
}

describe("signed private outbox sender", () => {
  it("binds follow-up intent to stored card key; later same-scope cards differ and replay stays local", async () => {
    const { repo, request, action } = await setup("send_followup");
    const frames: any[] = [];
    const sender = new SignedOutboxSender(ENDPOINT, SECRET, async (_url, init) => {
      const frame = JSON.parse(String(init?.body)); frames.push(frame); return reply(frame);
    });
    const executors = buildExecutors({ mailSender: sender });
    for (const row of [action, (await repo.propose("send_followup", request.request_id,
      { subject: "Second follow-up", body_md: "Second synthetic reminder", channel: "email", to: "records@example.invalid",
        outbox_binding: { intent_id: "forged" } }, "second-due-intent", ORG.email)).row]) {
      const approved = await approveAction(repo, row.action_id, ORG);
      expect(JSON.parse(approved.proposal_json).outbox_binding.intent_id).toBe(row.idempotency_key);
      expect((await executeAction(repo, env as Env, row.action_id, EXEC, executors)).state).toBe("executed");
      await executeAction(repo, env as Env, row.action_id, EXEC, executors);
    }
    expect(frames).toHaveLength(2);
    expect(frames[0].canonical_key).not.toBe(frames[1].canonical_key);
    expect(frames.map(f => f.draft.scope_version)).toEqual([2, 2]);
    expect(frames[0].draft.intent_id).toBe(action.idempotency_key);
  });
  it("matches Python canonical identity and HMAC protocol bytes", async () => {
    expect(await canonicalOutboxKey("send_request", "request-1", 1)).toBe("66ccc109eaaa437e7a00f542b562c1b04bc1bb4c50811845cf73df7c301f48be"); // pragma: allowlist secret -- reproducible public digest
    expect(await signOutboxFrame(SECRET, '{"synthetic":"frame"}')).toBe("86f3b891797acde0d42e63b1e9108d567425ad2b75fb020b53c9c1f95ce4515d"); // pragma: allowlist secret -- HMAC of public synthetic frame/key
  });

  it("signs stored approval and authoritative request; persists receipt and replays without network", async () => {
    const { repo, request, action } = await setup();
    const approved = await approveAction(repo, action.action_id, ORG);
    expect(JSON.parse(approved.proposal_json).outbox_binding).toEqual({ agency_id: request.agency_id, scope_version: 2, fee_cap_cents: 500 });
    const frames: Record<string, unknown>[] = [];
    const sender = new SignedOutboxSender(ENDPOINT, SECRET, async (url, init) => {
      expect(url).toBe(ENDPOINT); expect(init?.redirect).toBe("manual");
      const body = String(init?.body);
      expect(new Headers(init?.headers).get("X-Outbox-Signature")).toBe(await signOutboxFrame(SECRET, body));
      const frame = JSON.parse(body); frames.push(frame);
      expect(frame.approved_by).toBe(ORG.email); expect(frame.approved_at).toBe(approved.approved_at);
      expect(frame.draft).toMatchObject({ agency_id: request.agency_id, scope_version: 2, fee_cap_cents: 500,
        request_id: request.request_id, subject: request.subject, body: request.body_md });
      return reply(frame);
    });
    const executors = buildExecutors({ mailSender: sender });
    const completed = await executeAction(repo, env as Env, action.action_id, EXEC, executors);
    expect(completed.state).toBe("executed"); expect(frames).toHaveLength(1);
    expect((await repo.request(request.request_id))?.state).toBe("sent");
    expect(await repo.correspondenceFor(request.request_id)).toHaveLength(1);
    expect((await executeAction(repo, env as Env, action.action_id, EXEC, executors)).provider_receipt).toBe(completed.provider_receipt);
    expect(frames).toHaveLength(1);
  });

  it("refuses request changes after approval before fetching", async () => {
    const { repo, request, action } = await setup();
    await approveAction(repo, action.action_id, ORG);
    await repo.updateRequest(request.request_id, { scope_version: 3 });
    let calls = 0;
    const sender = new SignedOutboxSender(ENDPOINT, SECRET, async () => { calls++; throw new Error("must not fetch"); });
    const result = await executeAction(repo, env as Env, action.action_id, EXEC, buildExecutors({ mailSender: sender }));
    expect(result.error).toMatch(/^outbox_request_changed:/); expect(calls).toBe(0);
  });

  it("refuses an unclaimed card and forged outbound content", async () => {
    const { repo, request, action } = await setup();
    await approveAction(repo, action.action_id, ORG);
    let calls = 0;
    const sender = new SignedOutboxSender(ENDPOINT, SECRET, async () => { calls++; throw new Error("must not fetch"); });
    const message: OutboundMessage = { action_id: action.action_id, request_id: request.request_id, kind: "send_request",
      channel: "email", to: "records@example.invalid", subject: request.subject, body_md: request.body_md, idempotency_key: action.idempotency_key };
    const context = { repo, env: env as Env, identity: EXEC };
    await expect(sender.send(message, context)).rejects.toMatchObject({ code: "outbox_approval_mismatch" });
    await repo.claimForExecution(action.action_id);
    await expect(sender.send({ ...message, body_md: "unapproved body" }, context)).rejects.toMatchObject({ code: "outbox_draft_mismatch" });
    expect(calls).toBe(0);
  });

  it("holds network uncertainty and refuses another execution", async () => {
    const { repo, action } = await setup();
    await approveAction(repo, action.action_id, ORG);
    let calls = 0;
    const sender = new SignedOutboxSender(ENDPOINT, SECRET, async () => { calls++; throw new Error("private endpoint secret details"); });
    const executors = buildExecutors({ mailSender: sender });
    const result = await executeAction(repo, env as Env, action.action_id, EXEC, executors);
    expect(result.state).toBe("failed"); expect(result.error).toMatch(/^mail_send_ambiguous:/);
    expect(result.error).not.toContain("private endpoint");
    await expect(executeAction(repo, env as Env, action.action_id, EXEC, executors)).rejects.toThrow(/only approved/);
    expect(calls).toBe(1);
  });

  it("holds mismatched and oversized receipts rather than mark sent", async () => {
    for (const fault of ["action", "size", "redirect"]) {
      const { repo, request, action } = await setup();
      await approveAction(repo, action.action_id, ORG);
      const sender = new SignedOutboxSender(ENDPOINT, SECRET, async (_url, init) => {
        if (fault === "size") return new Response("x".repeat(16385));
        if (fault === "redirect") return Response.json({ code: "redirect" }, { status: 302 });
        return reply(JSON.parse(String(init?.body)), { action_id: "different-action" });
      });
      const result = await executeAction(repo, env as Env, action.action_id, EXEC, buildExecutors({ mailSender: sender }));
      expect(result.error).toMatch(/^mail_send_ambiguous:/);
      expect((await repo.request(request.request_id))?.state).toBe("draft");
      expect(await repo.correspondenceFor(request.request_id)).toHaveLength(0);
    }
  });

  it("preserves conclusive gateway refusal without exposing response details", async () => {
    const { repo, action } = await setup(); await approveAction(repo, action.action_id, ORG);
    const sender = new SignedOutboxSender(ENDPOINT, SECRET, async () => Response.json({ code: "outbox_blocked", reason: "private data" }, { status: 409 }));
    const result = await executeAction(repo, env as Env, action.action_id, EXEC, buildExecutors({ mailSender: sender }));
    expect(result.error).toMatch(/^outbox_refused:/); expect(result.error).not.toContain("private data");
  });

  it("keeps default sender disabled when either gateway setting is absent", async () => {
    const { repo, action } = await setup(); await approveAction(repo, action.action_id, ORG);
    const result = await executeAction(repo, env as Env, action.action_id, EXEC,
      defaultExecutors({ OUTBOX_GATEWAY_URL: ENDPOINT }));
    expect(result.error).toMatch(/^sender_not_configured:/);
    expect(() => new SignedOutboxSender("http://outbox.example.invalid/send", SECRET)).toThrow(/configuration/);
    expect(() => new SignedOutboxSender(ENDPOINT + "?token=synthetic", SECRET)).toThrow(/configuration/);
  });
});
