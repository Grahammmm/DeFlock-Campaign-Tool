import { env } from "cloudflare:test";
import { describe, expect, it } from "vitest";
import { approveAction, executeAction, holdInterruptedAction, reconcileAction } from "../src/approvals.ts";
import { executionClaimId, requiresDeliveryReconciliation } from "../src/db.ts";
import { FakeBrevo, type CampaignInput } from "../src/brevo.ts";
import { buildExecutors } from "../src/executors/index.ts";
import type { AccessIdentity } from "../src/auth.ts";
import type { Env } from "../src/env.ts";
import { call, makeSigner, organizerRequest, seedCampaign } from "./helpers.ts";

const ORG: AccessIdentity = { email: "organizer@example.invalid", sub: "synthetic", issued_at: 0, expires_at: 0 };
const draft = { subject: "Synthetic interrupted update", html: "<p>Update {{ unsubscribe }}</p>", text: "" };
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(done => { resolve = done; });
  return { promise, resolve };
}
async function approved(key: string) {
  const repo = await seedCampaign();
  await repo.putSetting("brevo", { list_id: 42, form_url: null, sender_name: "Example", sender_email: "news@example.invalid" });
  const { row } = await repo.propose("send_newsletter", null, draft, key, ORG.email);
  await approveAction(repo, row.action_id, ORG);
  return { repo, row };
}
async function claimed(key: string) {
  const { repo, row } = await approved(key), claim = (await repo.claimForExecution(row.action_id))!;
  return { repo, row, claim, executionId: executionClaimId(claim)! };
}

describe("interrupted execution recovery", () => {
  it("holds an interrupted claim, retains its key, and never auto-reopens or accepts timeout alone", async () => {
    const { repo, row, claim, executionId } = await claimed("interruption-held");
    const held = await holdInterruptedAction(repo, row.action_id, ORG, executionId, "Synthetic disconnected invocation");
    expect(held.state).toBe("failed"); expect(requiresDeliveryReconciliation(held)).toBe(true);
    expect(held.idempotency_key).toBe(row.idempotency_key);
    expect(held.approved_by).toBe(ORG.email);
    expect((await repo.propose("send_newsletter", null, {}, row.idempotency_key, ORG.email)).row.state).toBe("failed");
    await expect(reconcileAction(repo, row.action_id, ORG, "not_delivered", "Provider found no campaign")).rejects.toThrow(/old invocation/);
    await expect(reconcileAction(repo, row.action_id, ORG, "not_delivered", "Provider check", undefined, "Timeout", false)).rejects.toThrow(/old invocation/);
    expect(await repo.updateExecutingAction(claim, { state: "executed", error: null })).toBe(false);
    expect((await repo.action(row.action_id))!.state).toBe("failed");
  });
  it("requires fresh approval after recorded quiescence and provider non-delivery; old claims cannot win an ABA race", async () => {
    const { repo, row, claim, executionId } = await claimed("interruption-reapproval");
    await holdInterruptedAction(repo, row.action_id, ORG, executionId, "Synthetic interrupted invocation");
    const ready = await reconcileAction(repo, row.action_id, ORG, "not_delivered", "Provider confirmed absent", undefined, "Synthetic platform termination receipt", true);
    expect(ready.state).toBe("proposed"); expect(ready.approved_by).toBeNull(); expect(ready.approved_at).toBeNull();
    expect(ready.proposal_json).toBe(row.proposal_json); expect(ready.idempotency_key).toBe(row.idempotency_key);
    expect(JSON.parse(ready.provider_receipt!).quiescence_confirmed).toBe(true);
    await approveAction(repo, row.action_id, ORG);
    const next = (await repo.claimForExecution(row.action_id))!;
    expect(executionClaimId(next)).not.toBe(executionId);
    expect(await repo.updateExecutingAction(claim, { provider_receipt: '"late old receipt"' })).toBe(false);
    expect(await repo.createExecutionEvent(claim, { provider: "brevo", kind: "campaign_sent", payload_json: "{}", occurred_at: "2026-01-01T12:00:00Z" })).toBeNull();
    expect((await repo.action(row.action_id))!.error).toBe(next.error);
  });
  it("a delayed create cannot issue a send after the claim is held; late provider identity is journaled", async () => {
    const { repo, row } = await approved("interruption-late-create");
    const started = deferred<void>(), release = deferred<void>();
    class DelayedCreate extends FakeBrevo {
      override async createEmailCampaign(input: CampaignInput) {
        started.resolve(); await release.promise; return super.createEmailCampaign(input);
      }
    }
    const brevo = new DelayedCreate();
    const running = executeAction(repo, env as Env, row.action_id, ORG, buildExecutors({ brevo }));
    await started.promise;
    const claim = (await repo.action(row.action_id))!;
    await holdInterruptedAction(repo, row.action_id, ORG, executionClaimId(claim), "Synthetic interruption during create");
    release.resolve();
    expect((await running).error).toMatch(/^brevo_execution_interrupted:/);
    expect(brevo.created).toHaveLength(1); expect(brevo.sent).toHaveLength(0);
    const receipt = await env.DB.prepare("SELECT payload_json FROM subscriber_event WHERE kind = 'late_execution_receipt' AND json_extract(payload_json, '$.action_id') = ?").bind(row.action_id).first<{ payload_json: string }>();
    expect(JSON.parse(JSON.parse(receipt!.payload_json).provider_receipt).brevo_campaign_id).toBe(1001);
  });
  it("holding an admitted send is not cancellation; late completion cannot overwrite the hold or stamp a sent event", async () => {
    const { repo, row } = await approved("interruption-inflight-send");
    const started = deferred<void>(), release = deferred<void>();
    class DelayedSend extends FakeBrevo {
      override async sendCampaignNow(id: number) { started.resolve(); await release.promise; await super.sendCampaignNow(id); }
    }
    const brevo = new DelayedSend();
    const running = executeAction(repo, env as Env, row.action_id, ORG, buildExecutors({ brevo }));
    await started.promise;
    const claim = (await repo.action(row.action_id))!;
    const held = await holdInterruptedAction(repo, row.action_id, ORG, executionClaimId(claim), "Synthetic interruption during admitted send");
    expect(JSON.parse(held.provider_receipt!).brevo_campaign_id).toBe(1001);
    release.resolve(); const late = await running;
    expect(late.error).toMatch(/^brevo_execution_interrupted:/); expect(brevo.sent).toEqual([1001]);
    const event = await env.DB.prepare("SELECT event_id FROM subscriber_event WHERE kind = 'campaign_sent' AND json_extract(payload_json, '$.action_id') = ?").bind(row.action_id).first();
    expect(event).toBeNull();
    const checked = await reconcileAction(repo, row.action_id, ORG, "delivered", "Synthetic provider final delivery", "2026-01-01T12:00:00Z", "Synthetic completed invocation", true);
    expect(checked.state).toBe("executed");
    expect((await executeAction(repo, env as Env, row.action_id, ORG, buildExecutors({ brevo }))).state).toBe("executed");
    expect(brevo.created).toHaveLength(1); expect(brevo.sent).toEqual([1001]);
  });
  it("binds holds to the exact claim and journals only the winning transition", async () => {
    const { repo, row, claim, executionId } = await claimed("interruption-cas");
    await expect(holdInterruptedAction(repo, row.action_id, { email: "" } as AccessIdentity, executionId, "Synthetic")).rejects.toThrow(/identity/);
    await expect(holdInterruptedAction(repo, row.action_id, ORG, "wrong-claim", "Synthetic")).rejects.toThrow(/exact/);
    expect(await repo.holdExecution(claim, '{}')).toBe(true); expect(await repo.holdExecution(claim, '{}')).toBe(false);
    const count = await env.DB.prepare("SELECT count(*) AS n FROM subscriber_event WHERE kind = 'action_execution_held' AND campaign_id = ? AND payload_json = '{}'").bind(repo.campaignId).first<{ n: number }>();
    expect(count?.n).toBe(1);
  });
  it("rolls back the hold if its durable event fails", async () => {
    const { repo, row, executionId } = await claimed("interruption-journal-failure");
    await env.DB.exec("CREATE TRIGGER reject_execution_hold BEFORE INSERT ON subscriber_event WHEN NEW.kind = 'action_execution_held' BEGIN SELECT RAISE(ABORT, 'synthetic hold journal unavailable'); END");
    try {
      await expect(holdInterruptedAction(repo, row.action_id, ORG, executionId, "Synthetic interruption")).rejects.toThrow(/journal unavailable/);
      expect((await repo.action(row.action_id))!.state).toBe("executing");
    } finally { await env.DB.exec("DROP TRIGGER reject_execution_hold"); }
  });
  it("requires authenticated same-origin recovery and renders the claim/quiet-evidence workflow", async () => {
    const { repo, row } = await approved("interruption-http"), signer = await makeSigner();
    const claim = (await repo.claimForExecution(row.action_id))!;
    const executionId = /^execution_claim: (.+)$/.exec(claim.error ?? "")?.[1] ?? "legacy-unbound";
    const path = `/approvals/${row.action_id}/hold-execution`;
    const init = { method: "POST", headers: { "content-type": "application/json", accept: "application/json" }, body: JSON.stringify({ execution_id: executionId, reference: "Synthetic interrupted invocation" }) };
    expect((await call(new Request("https://workspace.example.invalid" + path, init))).status).toBe(401);
    const cross = await organizerRequest(signer, ORG.email, path, { ...init, headers: { ...init.headers, origin: "https://attacker.example" } });
    expect((await call(cross.request, cross.overrides)).status).toBe(403);
    const permitted = await organizerRequest(signer, ORG.email, path, init);
    expect((await call(permitted.request, permitted.overrides)).status).toBe(200);
    const missingProof = await organizerRequest(signer, ORG.email, `/approvals/${row.action_id}/reconcile`, { ...init, body: JSON.stringify({ outcome: "not_delivered", reference: "Provider check" }) });
    expect((await call(missingProof.request, missingProof.overrides)).status).toBe(400);
    const page = await organizerRequest(signer, ORG.email, "/approvals");
    expect(await (await call(page.request, page.overrides)).text()).toContain('name="quiescence_reference"');
    const evidencePath = `/approvals/${row.action_id}/execution-evidence`;
    expect((await call(new Request("https://workspace.example.invalid" + evidencePath))).status).toBe(401);
    const evidenceRequest = await organizerRequest(signer, ORG.email, evidencePath);
    const evidence = await (await call(evidenceRequest.request, evidenceRequest.overrides)).json() as { evidence: Array<{ payload_json: string }>; truncated: boolean };
    expect(evidence.truncated).toBe(false); expect(evidence.evidence).toHaveLength(1);
    expect(JSON.parse(evidence.evidence[0].payload_json).action_id).toBe(row.action_id);
  });
  it("bounds action evidence, excludes other actions, and identifies truncation", async () => {
    const { repo, row } = await approved("interruption-evidence-bounds"), signer = await makeSigner();
    for (let n = 0; n < 51; n++) await repo.createSubscriberEvent({ provider: "brevo", kind: "late_execution_receipt",
      payload_json: JSON.stringify({ action_id: row.action_id, synthetic_sequence: n }), occurred_at: "2026-01-01T12:00:00Z" });
    await repo.createSubscriberEvent({ provider: "brevo", kind: "late_execution_receipt", payload_json: '{"action_id":"unrelated"}', occurred_at: "2026-01-01T12:00:00Z" });
    const request = await organizerRequest(signer, ORG.email, `/approvals/${row.action_id}/execution-evidence`);
    const result = await (await call(request.request, request.overrides)).json() as { evidence: Array<{ payload_json: string }>; truncated: boolean };
    expect(result.truncated).toBe(true); expect(result.evidence).toHaveLength(50);
    expect(result.evidence.every(event => JSON.parse(event.payload_json).action_id === row.action_id)).toBe(true);
  });
  it("does not invent a claim for legacy or unsupported executions", async () => {
    const { repo, row } = await approved("interruption-legacy");
    await repo.updateAction(row.action_id, { state: "executing", error: null });
    await expect(holdInterruptedAction(repo, row.action_id, ORG, "legacy", "Synthetic interruption")).rejects.toThrow(/legacy/);
    expect((await repo.action(row.action_id))!.state).toBe("executing");
    const { row: other } = await repo.propose("deploy_site", null, { site_version: "synthetic" }, "interruption-other-kind", ORG.email);
    await approveAction(repo, other.action_id, ORG); const claim = (await repo.claimForExecution(other.action_id))!;
    await expect(holdInterruptedAction(repo, other.action_id, ORG, executionClaimId(claim), "Synthetic interruption")).rejects.toThrow(/newsletter/);
  });
});
