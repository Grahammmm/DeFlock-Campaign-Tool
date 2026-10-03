import { env } from "cloudflare:test";
import { describe, expect, it, vi } from "vitest";
import { BrevoError, FakeBrevo } from "../src/brevo.ts";
import { approveAction, executeAction, reconcileAction } from "../src/approvals.ts";
import { buildExecutors } from "../src/executors/index.ts";
import type { AccessIdentity } from "../src/auth.ts";
import type { Env } from "../src/env.ts";
import { call, makeSigner, organizerRequest, seedCampaign } from "./helpers.ts";

const ORG: AccessIdentity = { email: "organizer@example.invalid", sub: "synthetic", issued_at: 0, expires_at: 0 };
const draft = { subject: "Synthetic update", html: "<p>Update {{ unsubscribe }}</p>", text: "" };
async function failed(key: string) {
  const repo = await seedCampaign();
  await repo.putSetting("brevo", { list_id: 42, form_url: null, sender_name: "Example", sender_email: "news@example.invalid" });
  const brevo = new FakeBrevo(); brevo.failSend = new BrevoError("timeout", 0);
  const { row } = await repo.propose("send_newsletter", null, draft, key, ORG.email);
  await approveAction(repo, row.action_id, ORG);
  const result = await executeAction(repo, env as Env, row.action_id, ORG, buildExecutors({ brevo }));
  return { repo, brevo, row, result };
}

describe("delivery reconciliation", () => {
  it("confirmed non-delivery retains key and draft but requires fresh approval", async () => {
    const { repo, brevo, row, result } = await failed("reconcile-notsent");
    const checked = await reconcileAction(repo, row.action_id, ORG, "not_delivered", "Synthetic provider never queued campaign");
    expect(checked.state).toBe("proposed"); expect(checked.approved_by).toBeNull(); expect(checked.approved_at).toBeNull();
    expect(checked.idempotency_key).toBe(row.idempotency_key); expect(checked.proposal_json).toBe(row.proposal_json);
    expect(JSON.parse(checked.provider_receipt!).previous_receipt).toBe(result.provider_receipt);
    await expect(executeAction(repo, env as Env, row.action_id, ORG, buildExecutors({ brevo }))).rejects.toThrow(/only approved/);
    brevo.failSend = null; await approveAction(repo, row.action_id, ORG);
    expect((await executeAction(repo, env as Env, row.action_id, ORG, buildExecutors({ brevo }))).state).toBe("executed");
    expect(brevo.created).toHaveLength(2);
    expect((await repo.latestSubscriberEvent("action_reconciliation_attempt"))?.payload_json).toContain('"outcome":"not_delivered"');
  });
  it("rejects missing identity, uncertain outcome and blank evidence", async () => {
    const { repo, row } = await failed("reconcile-invalid");
    await expect(reconcileAction(repo, row.action_id, { email: "" } as AccessIdentity, "delivered", "checked")).rejects.toThrow(/identity/);
    await expect(reconcileAction(repo, row.action_id, ORG, "unknown", "checked")).rejects.toThrow(/outcome/);
    await expect(reconcileAction(repo, row.action_id, ORG, "delivered", " ")).rejects.toThrow(/reference/);
    expect((await repo.action(row.action_id))!.state).toBe("failed");
  });
  it("HTTP recovery requires Access and retains cross-origin protection", async () => {
    const { repo, row } = await failed("reconcile-http"), signer = await makeSigner();
    const path = `/approvals/${row.action_id}/reconcile`;
    const init = { method: "POST", headers: { "content-type": "application/json", accept: "application/json" }, body: JSON.stringify({ outcome: "delivered", reference: "Synthetic provider confirms delivery", delivered_at: "2026-01-01T12:00:00Z" }) };
    expect((await call(new Request("https://workspace.example.invalid" + path, init))).status).toBe(401);
    const cross = await organizerRequest(signer, ORG.email, path, { ...init, headers: { ...init.headers, origin: "https://attacker.example" } });
    expect((await call(cross.request, cross.overrides)).status).toBe(403);
    const accepted = await organizerRequest(signer, ORG.email, path, init);
    expect((await call(accepted.request, accepted.overrides)).status).toBe(200);
    expect((await repo.action(row.action_id))!.state).toBe("executed");
    const event = await env.DB.prepare("SELECT occurred_at FROM subscriber_event WHERE campaign_id = ? AND kind = ? AND json_extract(payload_json, '$.action_id') = ?").bind(repo.campaignId, "campaign_sent", row.action_id).first<{ occurred_at: string }>();
    expect(event?.occurred_at).toBe("2026-01-01T12:00:00.000Z");
  });
  it("stale evidence cannot overwrite a concurrently changed card", async () => {
    const { repo, result } = await failed("reconcile-race");
    expect(await repo.reconcileDelivery(result, true, '"first"', "2026-01-01T12:00:00Z")).toBe(true);
    expect(await repo.reconcileDelivery(result, false, '"stale"')).toBe(false);
    expect((await repo.action(result.action_id))!.provider_receipt).toBe('"first"');
  });
  it("creation timeout is held even without a returned campaign id", async () => {
    const repo = await seedCampaign(), brevo = new FakeBrevo();
    await repo.putSetting("brevo", { list_id: 42, form_url: null, sender_name: "Example", sender_email: "news@example.invalid" });
    brevo.failCreate = new BrevoError("timeout", 0);
    const { row } = await repo.propose("send_newsletter", null, draft, "reconcile-create", ORG.email);
    await approveAction(repo, row.action_id, ORG);
    const failed = await executeAction(repo, env as Env, row.action_id, ORG, buildExecutors({ brevo }));
    expect(failed.error).toMatch(/^brevo_create_ambiguous:/);
    expect((await repo.propose("send_newsletter", null, {}, "reconcile-create", ORG.email)).row.state).toBe("failed");
    expect(brevo.created).toHaveLength(0);
  });
  it("successful provider send plus local bookkeeping failure retains the provider receipt", async () => {
    const repo = await seedCampaign(), brevo = new FakeBrevo();
    await repo.putSetting("brevo", { list_id: 42, form_url: null, sender_name: "Example", sender_email: "news@example.invalid" });
    const { row } = await repo.propose("send_newsletter", null, draft, "reconcile-bookkeeping", ORG.email);
    await approveAction(repo, row.action_id, ORG);
    const spy = vi.spyOn(repo, "createSubscriberEvent").mockRejectedValueOnce(new Error("synthetic local storage failure"));
    const failed = await executeAction(repo, env as Env, row.action_id, ORG, buildExecutors({ brevo }));
    spy.mockRestore();
    expect(failed.state).toBe("failed");
    expect(JSON.parse(failed.provider_receipt!)).toMatchObject({ brevo_campaign_id: 1001, provider: "brevo" });
    expect((await repo.propose("send_newsletter", null, {}, "reconcile-bookkeeping", ORG.email)).row.state).toBe("failed");
    expect(brevo.created).toHaveLength(1);
  });
  it("receipt transition rolls back if its audit event cannot be stored", async () => {
    const { repo, row, result } = await failed("reconcile-atomic");
    await env.DB.exec("CREATE TRIGGER reject_reconciliation BEFORE INSERT ON subscriber_event WHEN NEW.kind = 'action_reconciled' BEGIN SELECT RAISE(ABORT, 'synthetic journal unavailable'); END");
    try {
      await expect(reconcileAction(repo, row.action_id, ORG, "not_delivered", "Synthetic provider status")).rejects.toThrow(/journal unavailable/);
      expect((await repo.action(row.action_id))!.state).toBe("failed");
      expect((await repo.action(row.action_id))!.provider_receipt).toBe(result.provider_receipt);
    } finally { await env.DB.exec("DROP TRIGGER reject_reconciliation"); }
  });
  it("local campaign-receipt failure stops before send and still holds the known campaign", async () => {
    const repo = await seedCampaign(), brevo = new FakeBrevo();
    await repo.putSetting("brevo", { list_id: 42, form_url: null, sender_name: "Example", sender_email: "news@example.invalid" });
    const { row } = await repo.propose("send_newsletter", null, draft, "reconcile-receiptwrite", ORG.email);
    await approveAction(repo, row.action_id, ORG);
    const spy = vi.spyOn(repo, "updateAction").mockRejectedValueOnce(new Error("synthetic receipt write failed"));
    const failed = await executeAction(repo, env as Env, row.action_id, ORG, buildExecutors({ brevo }));
    spy.mockRestore();
    expect(failed.error).toMatch(/^brevo_send_ambiguous: Brevo campaign 1001/);
    expect(brevo.created).toHaveLength(1); expect(brevo.sent).toHaveLength(0);
    expect((await repo.propose("send_newsletter", null, draft, "reconcile-receiptwrite", ORG.email)).row.state).toBe("failed");
  });
  it("known configuration failures can still reopen", async () => {
    const repo = await seedCampaign();
    const { row } = await repo.propose("send_newsletter", null, draft, "reconcile-known", ORG.email);
    await approveAction(repo, row.action_id, ORG);
    expect((await executeAction(repo, env as Env, row.action_id, ORG, buildExecutors({ brevo: null }))).error).toMatch(/^brevo_not_configured:/);
    expect((await repo.propose("send_newsletter", null, draft, "reconcile-known", ORG.email)).row.state).toBe("proposed");
  });
});
