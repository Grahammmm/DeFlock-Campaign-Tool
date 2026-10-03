import { env } from "cloudflare:test";
import { describe, expect, it } from "vitest";
import { approveAction, reconcileAction } from "../src/approvals.ts";
import { call, makeSigner, organizerRequest, seedCampaign } from "./helpers.ts";
import type { AccessIdentity } from "../src/auth.ts";
const ORG: AccessIdentity = { email: "organizer@example.invalid", sub: "synthetic", issued_at: 0, expires_at: 0 };
const WHEN = "2026-01-01T12:00:00.000Z";
async function held(key: string, known = true, kind: "send_request" | "send_followup" = "send_request") {
  const repo = await seedCampaign();
  const request = await repo.createRequest({ agency_id: "ca-example-police", scope_id: "agreements", scope_version: 1,
    subject: "Synthetic request", body_md: "Body", channel: "email", fee_cap_cents: 0, state: "draft", sent_at: null,
    determination_due: null, extension_claimed_until: null, last_activity_at: null, next_action: null, external_ref: null });
  const row = (await repo.propose(kind, request.request_id, { channel: "email", to: "records@example.invalid", subject: "Synthetic request", body_md: "Body" }, key, ORG.email)).row;
  await approveAction(repo, row.action_id, ORG);
  const id = "receipt-" + key;
  await repo.updateAction(row.action_id, { state: "failed", error: "mail_send_ambiguous: synthetic failure",
    provider_receipt: known ? JSON.stringify({ provider_message_id: id, provider: "synthetic", sent_at: WHEN }) : null });
  return { repo, request, row, id };
}
describe("delivered mail reconciliation", () => {
  it("repairs request and correspondence without another send", async () => {
    const { repo, request, row, id } = await held("repair");
    const done = await reconcileAction(repo, row.action_id, ORG, "delivered", "Synthetic provider check", WHEN);
    expect(done.state).toBe("executed");
    expect(await repo.request(request.request_id)).toMatchObject({ state: "sent", sent_at: WHEN, external_ref: id, last_activity_at: WHEN });
    expect(await repo.correspondenceFor(request.request_id)).toEqual([expect.objectContaining({ provider_message_id: id, direction: "outbound", received_at: WHEN })]);
  });
  it("rolls the action and request back when correspondence repair cannot be stored", async () => {
    const { repo, request, row } = await held("rollback");
    await env.DB.exec("CREATE TRIGGER reject_mail_repair BEFORE INSERT ON correspondence BEGIN SELECT RAISE(ABORT, 'synthetic repair unavailable'); END");
    try {
      await expect(reconcileAction(repo, row.action_id, ORG, "delivered", "Synthetic check", WHEN)).rejects.toThrow(/repair unavailable/);
      expect((await repo.action(row.action_id))!.state).toBe("failed");
      expect((await repo.request(request.request_id))!.state).toBe("draft");
      expect(await repo.correspondenceFor(request.request_id)).toHaveLength(0);
    } finally { await env.DB.exec("DROP TRIGGER reject_mail_repair"); }
  });
  it("retains advanced request state and deduplicates an already stored correspondence", async () => {
    const { repo, request, row, id } = await held("advanced");
    await repo.updateRequest(request.request_id, { state: "acknowledged", last_activity_at: "2026-02-01T12:00:00Z" });
    await repo.createCorrespondence({ request_id: request.request_id, direction: "outbound", channel: "email", provider_message_id: id,
      from_addr: null, to_addr: "records@example.invalid", subject: "Synthetic request", received_at: WHEN, raw_sha256: null,
      classification: null, classification_confidence: null, summary: "records request sent" });
    await reconcileAction(repo, row.action_id, ORG, "delivered", "Synthetic check", WHEN);
    expect(await repo.correspondenceFor(request.request_id)).toHaveLength(1);
    expect(await repo.request(request.request_id)).toMatchObject({ state: "acknowledged", last_activity_at: "2026-02-01T12:00:00Z", sent_at: WHEN });
  });
});


describe("mail reconciliation identity and callers", () => {
  it("provider-id collision with another request rolls back repair", async () => {
    const target = await held("collision-target");
    const other = await held("collision-other");
    await target.repo.createCorrespondence({ request_id: other.request.request_id, direction: "outbound", channel: "email", provider_message_id: target.id,
      from_addr: null, to_addr: "records@example.invalid", subject: "Synthetic request", received_at: WHEN, raw_sha256: null,
      classification: null, classification_confidence: null, summary: "records request sent" });
    await expect(reconcileAction(target.repo, target.row.action_id, ORG, "delivered", "Synthetic check", WHEN)).rejects.toThrow(/UNIQUE/);
    expect((await target.repo.action(target.row.action_id))!.state).toBe("failed");
    expect((await target.repo.request(target.request.request_id))!.state).toBe("draft");
    expect(await target.repo.correspondenceFor(target.request.request_id)).toHaveLength(0);
  });
  it("never overwrites a different initial request receipt", async () => {
    const { repo, request, row } = await held("request-conflict");
    await repo.updateRequest(request.request_id, { external_ref: "another-provider-message" });
    await expect(reconcileAction(repo, row.action_id, ORG, "delivered", "Synthetic check", WHEN)).rejects.toThrow(/different provider receipt/);
    expect((await repo.action(row.action_id))!.state).toBe("failed");
    expect(await repo.correspondenceFor(request.request_id)).toHaveLength(0);
  });
  it("requires a provider id for uncertain sends and the HTTP caller carries it into repair", async () => {
    const { repo, request, row } = await held("unknown-http", false);
    await expect(reconcileAction(repo, row.action_id, ORG, "delivered", "Synthetic check", WHEN)).rejects.toThrow(/message id/);
    expect((await repo.action(row.action_id))!.state).toBe("failed");
    const signer = await makeSigner();
    const signed = await organizerRequest(signer, ORG.email, `/approvals/${row.action_id}/reconcile`, {
      method: "POST", headers: { "content-type": "application/json", accept: "application/json" },
      body: JSON.stringify({ outcome: "delivered", reference: "Synthetic provider check", delivered_at: WHEN, provider_message_id: "confirmed-message" }),
    });
    const response = await call(signed.request, signed.overrides);
    expect(response.status).toBe(200);
    expect(await repo.request(request.request_id)).toMatchObject({ external_ref: "confirmed-message", state: "sent" });
    expect((await repo.correspondenceFor(request.request_id))[0].provider_message_id).toBe("confirmed-message");
  });
  it("rejects a conflicting provider id and leaves the action held", async () => {
    const { repo, row } = await held("conflict");
    await expect(reconcileAction(repo, row.action_id, ORG, "delivered", "Synthetic check", WHEN, undefined, undefined, "wrong-id")).rejects.toThrow(/conflicts/);
    expect((await repo.action(row.action_id))!.state).toBe("failed");
  });
  it("confirmed non-delivery changes no request or correspondence", async () => {
    const { repo, request, row } = await held("not-delivered", false);
    await reconcileAction(repo, row.action_id, ORG, "not_delivered", "Synthetic provider check");
    expect(await repo.request(request.request_id)).toMatchObject({ state: "draft", sent_at: null });
    expect(await repo.correspondenceFor(request.request_id)).toHaveLength(0);
  });
  it("follow-up repair retains original request delivery identity", async () => {
    const { repo, request, row, id } = await held("followup", true, "send_followup");
    await repo.updateRequest(request.request_id, { state: "acknowledged", sent_at: "2025-12-01T12:00:00Z", external_ref: "initial-message" });
    await reconcileAction(repo, row.action_id, ORG, "delivered", "Synthetic check", WHEN);
    expect(await repo.request(request.request_id)).toMatchObject({ state: "acknowledged", sent_at: "2025-12-01T12:00:00Z", external_ref: "initial-message", last_activity_at: WHEN });
    expect((await repo.correspondenceFor(request.request_id))[0]).toMatchObject({ provider_message_id: id, summary: "follow-up sent" });
  });
  it("stale action evidence cannot modify request records", async () => {
    const { repo, request, row, id } = await held("stale");
    const snapshot = (await repo.action(row.action_id))!;
    await repo.updateAction(row.action_id, { error: "mail_send_ambiguous: changed by another check" });
    expect(await repo.reconcileDelivery(snapshot, true, "{}", WHEN, id)).toBe(false);
    expect((await repo.request(request.request_id))!.state).toBe("draft");
    expect(await repo.correspondenceFor(request.request_id)).toHaveLength(0);
  });
});
