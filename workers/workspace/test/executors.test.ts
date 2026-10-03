import { env } from "cloudflare:test";
import { describe, expect, it, vi } from "vitest";
import { contentHash, type JsonObject } from "@deflock/shared/review";
import { approveAction, executeAction, reconcileAction } from "../src/approvals.ts";
import type { AccessIdentity } from "../src/auth.ts";
import { BrevoError, FakeBrevo } from "../src/brevo.ts";
import type { Repo } from "../src/db.ts";
import type { Env } from "../src/env.ts";
import { buildExecutors, type MailSender, type OutboundMessage } from "../src/executors/index.ts";
import { findingSlug } from "../src/manifest.ts";
import { issueCorrection, proposePublication, submitReview, withdrawFinding, WorkflowError } from "../src/publication.ts";
import { seedCampaign } from "./helpers.ts";

const AUTHOR: AccessIdentity = { email: "author@example.invalid", sub: "a", issued_at: 0, expires_at: 0 };
const ORG: AccessIdentity = { email: "organizer@example.invalid", sub: "o", issued_at: 0, expires_at: 0 };
const R1: AccessIdentity = { email: "reviewer-one@example.invalid", sub: "1", issued_at: 0, expires_at: 0 };
const R2: AccessIdentity = { email: "reviewer-two@example.invalid", sub: "2", issued_at: 0, expires_at: 0 };
const SHA = "a".repeat(64);
const HTML = "<p>Update</p><p><a href=\"{{ unsubscribe }}\">Unsubscribe</a></p>";

let counter = 0;

async function createFinding(repo: Repo, overrides: Partial<JsonObject> = {}, confidence = "verified") {
  counter += 1;
  const id = "fnd_" + counter.toString(16).padStart(16, "0");
  const doc: JsonObject = {
    id,
    author: AUTHOR.email,
    title: "Synthetic policy lacks a retention clause " + counter,
    summary: "Synthetic policy lacks a retention clause " + counter,
    classification: "documented_fact",
    confidence,
    event_date: "2026-09-01",
    sources: [{ sha256: SHA, locator: "page 3", title: "Synthetic policy" }],
    limitations: ["synthetic"],
    counterevidence: [],
    ...overrides,
  };
  const hash = await contentHash(doc);
  return repo.createFinding({ finding_id: id, author: AUTHOR.email, classification: String(doc.classification), confidence, summary: String(doc.summary), finding_json: JSON.stringify(doc), content_sha256: hash, state: "draft" });
}

/** Two independent reviewers covering factual, legal and privacy. */
async function fullyReview(repo: Repo, findingId: string) {
  const f = () => repo.finding(findingId) as Promise<NonNullable<Awaited<ReturnType<Repo["finding"]>>>>;
  await submitReview(repo, await f(), R1, { role: "factual", decision: "approve", rationale: "checked page 3" });
  await submitReview(repo, await f(), R1, { role: "legal", decision: "approve", rationale: "rule in force" });
  return submitReview(repo, await f(), R2, { role: "privacy", decision: "approve", rationale: "no personal data" });
}

async function proposeApprove(repo: Repo, findingId: string) {
  const { row } = await proposePublication(repo, (await repo.finding(findingId))!, ORG);
  await approveAction(repo, row.action_id, ORG);
  return row;
}

describe("review state machine", () => {
  it("draft -> in_review -> ready; author cannot review; challenge blocks", async () => {
    const repo = await seedCampaign();
    const f = await createFinding(repo);
    await expect(submitReview(repo, f, AUTHOR, { role: "factual", decision: "approve", rationale: "mine" })).rejects.toThrow(WorkflowError);
    await expect(submitReview(repo, f, R1, { role: "owner", decision: "approve", rationale: "x" })).rejects.toThrow(/role/);
    let next = await submitReview(repo, f, R1, { role: "factual", decision: "approve", rationale: "ok" });
    expect(next.state).toBe("in_review");
    next = await submitReview(repo, (await repo.finding(f.finding_id))!, R2, { role: "legal", decision: "changes_requested", rationale: "cite the rule" });
    expect(next.state).toBe("blocked");
    expect(next.blockers).toContain("unresolved_challenge");
    // a challenge is resolved by editing the finding (new hash) and reviewing again
    const doc = { ...(JSON.parse(f.finding_json) as JsonObject), rule_version: "us-ca@2026-01-01" };
    await repo.updateFinding(f.finding_id, { finding_json: JSON.stringify(doc), content_sha256: await contentHash(doc), state: "draft" });
    const last = await fullyReview(repo, f.finding_id);
    expect(last.state).toBe("ready");
    expect(last.blockers).toEqual([]);
  });

  it("structural blockers after an approval mark the finding blocked", async () => {
    const repo = await seedCampaign();
    const f = await createFinding(repo, { sources: [] });
    const next = await submitReview(repo, f, R1, { role: "factual", decision: "approve", rationale: "ok" });
    expect(next.state).toBe("blocked");
    expect(next.blockers).toContain("missing_evidence");
  });
});

describe("publish_finding executor", () => {
  it("refuses needs_attorney_review even with full receipts", async () => {
    const repo = await seedCampaign();
    const f = await createFinding(repo, {}, "needs_attorney_review");
    const last = await fullyReview(repo, f.finding_id);
    expect(last.state).toBe("ready");
    await expect(proposePublication(repo, (await repo.finding(f.finding_id))!, ORG)).rejects.toThrow(/needs_attorney_review/);
    // a card created anyway (e.g. by the runner) still fails at execution
    const { row } = await repo.propose("publish_finding", f.finding_id, { finding_id: f.finding_id }, "pf-nar-" + f.finding_id, "job_x");
    await approveAction(repo, row.action_id, ORG);
    const done = await executeAction(repo, env as Env, row.action_id, ORG, buildExecutors());
    expect(done.state).toBe("failed");
    expect(done.error).toMatch(/^needs_attorney_review: /);
    expect((await repo.finding(f.finding_id))!.state).toBe("ready");
    expect(await repo.publicationForFinding(f.finding_id)).toBeNull();
  });

  it("refuses non-empty blockers and stale content, re-checking at execution time", async () => {
    const repo = await seedCampaign();
    const f = await createFinding(repo);
    await expect(proposePublication(repo, f, ORG)).rejects.toThrow(/not ready/);
    const { row } = await repo.propose("publish_finding", f.finding_id, { finding_id: f.finding_id }, "pf-blk-" + f.finding_id, "job_y");
    await approveAction(repo, row.action_id, ORG);
    let done = await executeAction(repo, env as Env, row.action_id, ORG, buildExecutors());
    expect(done.error).toMatch(/^not_ready: /);
    // ready, proposed, then edited before execution -> content_changed
    const g = await createFinding(repo);
    await fullyReview(repo, g.finding_id);
    const card = await proposeApprove(repo, g.finding_id);
    const doc = { ...(JSON.parse(g.finding_json) as JsonObject), summary: "edited after approval" };
    await repo.updateFinding(g.finding_id, { finding_json: JSON.stringify(doc), content_sha256: await contentHash(doc) });
    done = await executeAction(repo, env as Env, card.action_id, ORG, buildExecutors());
    expect(done.state).toBe("failed");
    expect(done.error).toMatch(/^content_changed: /);
  });

  it("publishes a ready finding: publication row, build_site manifest, deploy card, newsletter draft; idempotent", async () => {
    const repo = await seedCampaign();
    const f = await createFinding(repo);
    await fullyReview(repo, f.finding_id);
    const card = await proposeApprove(repo, f.finding_id);
    expect(JSON.parse(card.proposal_json)).toMatchObject({ finding_id: f.finding_id, path: "/findings/" + findingSlug(f) + ".html", content_sha256: f.content_sha256 });
    const done = await executeAction(repo, env as Env, card.action_id, ORG, buildExecutors());
    expect(done.state).toBe("executed");
    const receipt = JSON.parse(done.provider_receipt!) as Record<string, string>;
    expect(receipt.path).toBe("/findings/" + findingSlug(f) + ".html");
    expect(receipt.site_version).toMatch(/^m[0-9a-f]{16}$/);
    const pub = await repo.publicationForFinding(f.finding_id);
    expect(pub?.content_sha256).toBe(f.content_sha256);
    expect(pub?.published_by).toBe(ORG.email);
    expect((await repo.finding(f.finding_id))!.state).toBe("published");
    const build = await repo.job(receipt.build_job_id);
    expect(build?.kind).toBe("build_site");
    const inputs = JSON.parse(build!.inputs_json) as { site_version: string; manifest: { site: JsonObject; findings: JsonObject[]; agencies: JsonObject[]; map: unknown; sources: unknown[]; meetings: unknown[] } };
    expect(inputs.site_version).toBe(receipt.site_version);
    expect(inputs.manifest.findings.map((x) => x.id)).toContain(f.finding_id);
    const published = inputs.manifest.findings.find((x) => x.id === f.finding_id)!;
    expect(published.state).toBe("published");
    expect(published.published_at).toBe(pub!.published_at);
    expect(published.corrections).toEqual([]);
    expect(inputs.manifest.site).toMatchObject({ schema_version: 1, signup: { mode: "none" }, base_url: "https://campaign.example.invalid" });
    expect(inputs.manifest.agencies.map((a) => a.agency_id)).toEqual(["ca-example-police"]);
    expect(inputs.manifest.map).toBeNull();
    const deploy = await repo.action(receipt.deploy_action_id);
    expect(deploy?.kind).toBe("deploy_site");
    expect(deploy?.state).toBe("proposed");
    expect(JSON.parse(deploy!.proposal_json)).toMatchObject({ site_version: receipt.site_version, build_job_id: receipt.build_job_id });
    const draft = await repo.job(receipt.newsletter_draft_job_id);
    expect(draft?.kind).toBe("newsletter_draft");
    expect((JSON.parse(draft!.inputs_json) as { manifest: { findings: { path: string }[] } }).manifest.findings[0].path).toBe(receipt.path);
    // executing the same card again is a no-op with the same receipt
    const again = await executeAction(repo, env as Env, card.action_id, ORG, buildExecutors());
    expect(again.provider_receipt).toBe(done.provider_receipt);
    expect((await repo.publications()).filter((p) => p.finding_id === f.finding_id).length).toBe(1);
    // deploy refuses while the build job is not done, then flips the version once it is
    await approveAction(repo, deploy!.action_id, ORG);
    let dep = await executeAction(repo, env as Env, deploy!.action_id, ORG, buildExecutors());
    expect(dep.error).toMatch(/^build_not_done: /);
    await repo.leaseJob(60);
    await repo.db.prepare("UPDATE job SET state = 'done', leased_until = NULL WHERE job_id = ?").bind(build!.job_id).run();
    const { row: deploy2 } = await repo.propose("deploy_site", null, { site_version: receipt.site_version, build_job_id: build!.job_id }, "deploy-retry-" + f.finding_id, ORG.email);
    await approveAction(repo, deploy2.action_id, ORG);
    dep = await executeAction(repo, env as Env, deploy2.action_id, ORG, buildExecutors());
    expect(dep.state).toBe("executed");
    expect(await repo.setting("site_version")).toBe(receipt.site_version);
    expect((await repo.publicationForFinding(f.finding_id))!.deploy_receipt).toContain(receipt.site_version);
  });
});

describe("corrections and withdrawal", () => {
  it("a correction records the reason, marks the finding corrected and re-proposes build + deploy with the note", async () => {
    const repo = await seedCampaign();
    const f = await createFinding(repo);
    await fullyReview(repo, f.finding_id);
    const card = await proposeApprove(repo, f.finding_id);
    await executeAction(repo, env as Env, card.action_id, ORG, buildExecutors());
    const buildsBefore = (await repo.jobsByKind("build_site")).length;
    await expect(issueCorrection(repo, (await repo.finding(f.finding_id))!, ORG, { reason: "", replacement_finding_id: null })).rejects.toThrow(/reason/);
    await expect(issueCorrection(repo, (await repo.finding(f.finding_id))!, ORG, { reason: "x", replacement_finding_id: "fnd_0000000000000000" })).rejects.toThrow(/does not exist/);
    const out = await issueCorrection(repo, (await repo.finding(f.finding_id))!, ORG, { reason: "Page 3 cites the 2024 policy, not 2025.", replacement_finding_id: null });
    expect((await repo.finding(f.finding_id))!.state).toBe("corrected");
    expect(out.correction.corrected_by).toBe(ORG.email);
    const build = await repo.job(out.build_job_id);
    const manifest = (JSON.parse(build!.inputs_json) as { manifest: { findings: JsonObject[] } }).manifest;
    const doc = manifest.findings.find((x) => x.id === f.finding_id)!;
    expect(doc.state).toBe("published");
    // The organizer's reason stays in the workspace (it never passed the two-reviewer gate);
    // the public note records only that a correction happened.
    expect(doc.corrections).toEqual([{ date: out.correction.corrected_at.slice(0, 10), note: "Corrected." }]);
    expect(JSON.stringify(manifest)).not.toContain("2024 policy");
    expect((await repo.correctionsFor(out.correction.publication_id))[0].reason).toBe("Page 3 cites the 2024 policy, not 2025.");
    const deploy = await repo.action(out.deploy_action_id);
    expect(deploy?.state).toBe("proposed");
    expect((await repo.jobsByKind("build_site")).length).toBe(buildsBefore + 1); // the correction changes the manifest, so a new build
    // unpublished findings cannot be corrected or withdrawn
    const g = await createFinding(repo);
    await expect(issueCorrection(repo, g, ORG, { reason: "no", replacement_finding_id: null })).rejects.toThrow(/only published/);
    await expect(withdrawFinding(repo, g, ORG, "no")).rejects.toThrow(/only published/);
  });

  it("withdrawal removes the finding from the manifest and proposes a rebuild", async () => {
    const repo = await seedCampaign();
    const f = await createFinding(repo);
    await fullyReview(repo, f.finding_id);
    const card = await proposeApprove(repo, f.finding_id);
    await executeAction(repo, env as Env, card.action_id, ORG, buildExecutors());
    const out = await withdrawFinding(repo, (await repo.finding(f.finding_id))!, ORG, "Source document was superseded.");
    expect((await repo.finding(f.finding_id))!.state).toBe("withdrawn");
    const build = await repo.job(out.build_job_id);
    const manifest = (JSON.parse(build!.inputs_json) as { manifest: { findings: JsonObject[] } }).manifest;
    expect(manifest.findings.map((x) => x.id)).not.toContain(f.finding_id);
    expect((await repo.action(out.deploy_action_id))?.kind).toBe("deploy_site");
  });
});

describe("send_newsletter executor", () => {
  async function configure(repo: Repo, listId: number | null = 42) {
    await repo.putSetting("brevo", { list_id: listId, form_url: "https://abc123.sibforms.com/serve/example", sender_name: "Example Campaign", sender_email: "news@campaign.example.invalid" });
  }

  it("creates and sends one Brevo campaign; receipt is the campaign id; never touches contacts", async () => {
    const repo = await seedCampaign();
    await configure(repo);
    const brevo = new FakeBrevo();
    const { row } = await repo.propose("send_newsletter", "job_d1", { subject: "September update", html: HTML, text: "Update\nUnsubscribe: {{ unsubscribe }}" }, "nl-1", ORG.email);
    await approveAction(repo, row.action_id, ORG);
    const done = await executeAction(repo, env as Env, row.action_id, ORG, buildExecutors({ brevo }));
    expect(done.state).toBe("executed");
    expect(JSON.parse(done.provider_receipt!)).toMatchObject({ provider: "brevo", brevo_campaign_id: 1001, list_id: 42 });
    expect(brevo.created.length).toBe(1);
    expect(brevo.created[0]).toMatchObject({ subject: "September update", listIds: [42], sender: { name: "Example Campaign", email: "news@campaign.example.invalid" } });
    expect(brevo.sent).toEqual([1001]);
    expect((await repo.latestSubscriberEvent("campaign_sent"))?.payload_json).toContain('"brevo_campaign_id":1001');
    // re-execute: no second campaign
    await executeAction(repo, env as Env, row.action_id, ORG, buildExecutors({ brevo }));
    expect(brevo.created.length).toBe(1);
  });

  it("fails cleanly without configuration, without the unsubscribe placeholder, or on a provider error", async () => {
    const repo = await seedCampaign();
    const brevo = new FakeBrevo();
    await repo.putSetting("brevo", { list_id: null, form_url: null, sender_name: null, sender_email: null });
    const { row: a } = await repo.propose("send_newsletter", null, { subject: "x", html: HTML, text: "" }, "nl-2", ORG.email);
    await approveAction(repo, a.action_id, ORG);
    expect((await executeAction(repo, env as Env, a.action_id, ORG, buildExecutors({ brevo: null }))).error).toMatch(/^brevo_not_configured: BREVO_API_KEY/);
    const { row: b } = await repo.propose("send_newsletter", null, { subject: "x", html: HTML, text: "" }, "nl-3", ORG.email);
    await approveAction(repo, b.action_id, ORG);
    expect((await executeAction(repo, env as Env, b.action_id, ORG, buildExecutors({ brevo }))).error).toMatch(/^brevo_not_configured: Brevo sender/);
    await configure(repo);
    const { row: c } = await repo.propose("send_newsletter", null, { subject: "x", html: "<p>no link</p>", text: "" }, "nl-4", ORG.email);
    await approveAction(repo, c.action_id, ORG);
    expect((await executeAction(repo, env as Env, c.action_id, ORG, buildExecutors({ brevo }))).error).toMatch(/^missing_unsubscribe: /);
    const { row: d } = await repo.propose("send_newsletter", null, { subject: "x", html: HTML + "<script>alert(1)</script>", text: "" }, "nl-5", ORG.email);
    await approveAction(repo, d.action_id, ORG);
    expect((await executeAction(repo, env as Env, d.action_id, ORG, buildExecutors({ brevo }))).error).toMatch(/scripts/);
    brevo.failCreate = new BrevoError("Brevo POST /emailCampaigns failed with 401: Key not found", 401, "unauthorized");
    const { row: e } = await repo.propose("send_newsletter", null, { subject: "x", html: HTML, text: "" }, "nl-6", ORG.email);
    await approveAction(repo, e.action_id, ORG);
    expect((await executeAction(repo, env as Env, e.action_id, ORG, buildExecutors({ brevo }))).error).toMatch(/^brevo_error: .*401/);
    expect(brevo.sent).toEqual([]);
  });
});

describe("stub executors", () => {
  it("post_social is not_implemented and pay_fee is manual_only with instructions", async () => {
    const repo = await seedCampaign();
    const { row: s } = await repo.propose("post_social", null, { text: "hello" }, "ps-1", ORG.email);
    await approveAction(repo, s.action_id, ORG);
    expect((await executeAction(repo, env as Env, s.action_id, ORG)).error).toMatch(/^not_implemented: /);
    const { row: p } = await repo.propose("pay_fee", null, { amount_cents: 12000, fee_cap_cents: 5000 }, "pf-1", ORG.email);
    await approveAction(repo, p.action_id, ORG);
    const done = await executeAction(repo, env as Env, p.action_id, ORG);
    expect(done.error).toMatch(/^manual_only: Pay the fee manually/);
    expect(done.error).toContain("exceeds the request fee cap");
    expect((await repo.incidents()).some((i) => i.fingerprint === "action:" + p.action_id)).toBe(true);
  });
});

describe("mail sender port", () => {
  it("send_request hands the approved draft to the registered sender, marks the request sent and logs correspondence", async () => {
    const repo = await seedCampaign();
    const request = await repo.createRequest({
      agency_id: "ca-example-police",
      scope_id: "agreements",
      scope_version: 1,
      subject: "Records request: ALPR agreements",
      body_md: "Please provide...",
      channel: "email",
      fee_cap_cents: 5000,
      state: "draft",
      sent_at: null,
      determination_due: null,
      extension_claimed_until: null,
      last_activity_at: null,
      next_action: null,
      external_ref: null,
    });
    const sent: OutboundMessage[] = [];
    const fake: MailSender = {
      name: "fake",
      async send(message) {
        sent.push(message);
        return { provider_message_id: "msg-" + sent.length, sent_at: "2026-09-30T12:00:00.000Z", provider: "fake" };
      },
    };
    const executors = buildExecutors({ mailSender: fake });
    const proposal = { channel: "email", to: "records@example.invalid", subject: request.subject, body_md: request.body_md };
    const { row } = await repo.propose("send_request", request.request_id, proposal, "sr-fake-1", ORG.email);
    await approveAction(repo, row.action_id, ORG);
    const done = await executeAction(repo, env as Env, row.action_id, ORG, executors);
    expect(done.state).toBe("executed");
    expect(JSON.parse(done.provider_receipt!)).toMatchObject({ provider_message_id: "msg-1", sender: "fake" });
    expect(sent).toHaveLength(1);
    expect(sent[0]).toMatchObject({ kind: "send_request", request_id: request.request_id, to: "records@example.invalid", idempotency_key: "sr-fake-1" });
    const after = (await repo.request(request.request_id))!;
    expect(after.state).toBe("sent");
    expect(after.external_ref).toBe("msg-1");
    const log = await repo.correspondenceFor(request.request_id);
    expect(log).toHaveLength(1);
    expect(log[0]).toMatchObject({ direction: "outbound", provider_message_id: "msg-1", to_addr: "records@example.invalid" });
    // re-execution returns the receipt without a second send
    const again = await executeAction(repo, env as Env, row.action_id, ORG, executors);
    expect(again.provider_receipt).toBe(done.provider_receipt);
    expect(sent).toHaveLength(1);
    // an invalid channel never reaches the sender
    const { row: bad } = await repo.propose("send_followup", request.request_id, { channel: "fax", subject: "x", body_md: "y" }, "sf-bad", ORG.email);
    await approveAction(repo, bad.action_id, ORG);
    expect((await executeAction(repo, env as Env, bad.action_id, ORG, executors)).error).toMatch(/^invalid_proposal: /);
    expect(sent).toHaveLength(1);
  });
});


describe("mail receipt recovery", () => {
  it("holds delivery when request bookkeeping fails instead of admitting a duplicate send", async () => {
    const repo = await seedCampaign();
    const fake: MailSender = { name: "synthetic", async send() {
      return { provider_message_id: "receipt-one", sent_at: "2026-01-01T12:00:00Z", provider: "synthetic" };
    }};
    const proposal = { channel: "email", to: "records@example.invalid", subject: "Request", body_md: "Body" };
    const { row } = await repo.propose("send_request", "req_synthetic", proposal, "mail-bookkeeping", ORG.email);
    await approveAction(repo, row.action_id, ORG);
    const spy = vi.spyOn(repo, "updateRequest").mockRejectedValueOnce(new Error("synthetic database failure"));
    const result = await executeAction(repo, env as Env, row.action_id, ORG, buildExecutors({ mailSender: fake }));
    spy.mockRestore();
    expect(result.state).toBe("failed");
    expect(JSON.parse(result.provider_receipt!)).toMatchObject({ provider_message_id: "receipt-one" });
    expect((await repo.propose("send_request", "req_synthetic", proposal, "mail-bookkeeping", ORG.email)).row.state).toBe("failed");
  });
  it.each([null, {}, { provider_message_id: "", provider: "synthetic", sent_at: "2026-01-01T12:00:00Z" },
    { provider_message_id: "x", provider: "synthetic", sent_at: "not-a-date" }])("rejects malformed provider receipt without reopening the send", async (receipt) => {
    const repo = await seedCampaign();
    const fake: MailSender = { name: "synthetic", async send() { return receipt as any; }};
    const proposal = { channel: "email", subject: "Request", body_md: "Body" };
    const key = "mail-invalid-" + JSON.stringify(receipt);
    const { row } = await repo.propose("send_request", null, proposal, key, ORG.email);
    await approveAction(repo, row.action_id, ORG);
    const result = await executeAction(repo, env as Env, row.action_id, ORG, buildExecutors({ mailSender: fake }));
    expect(result.state).toBe("failed");
    expect(result.error).toMatch(/^mail_send_ambiguous:/);
    expect((await repo.propose("send_request", null, proposal, key, ORG.email)).row.state).toBe("failed");
  });
});


describe("mail uncertain outcome", () => {
  it.each(["transport", "receipt-write"])("keeps %s failure held until authenticated non-delivery reconciliation", async (mode) => {
    const repo = await seedCampaign(); let sends = 0;
    const fake: MailSender = { name: "synthetic", async send() {
      sends++;
      if (mode === "transport") throw new Error("synthetic connection lost");
      return { provider_message_id: "receipt-one", provider: "synthetic", sent_at: "2026-01-01T12:00:00Z" };
    }};
    const proposal = { channel: "email", subject: "Request", body_md: "Body" };
    const key = "mail-uncertain-" + mode;
    const { row } = await repo.propose("send_followup", null, proposal, key, ORG.email);
    await approveAction(repo, row.action_id, ORG);
    const spy = mode === "receipt-write" ? vi.spyOn(repo, "updateAction").mockRejectedValueOnce(new Error("synthetic storage lost")) : null;
    const result = await executeAction(repo, env as Env, row.action_id, ORG, buildExecutors({ mailSender: fake }));
    spy?.mockRestore();
    expect(result.error).toMatch(/^mail_send_ambiguous:/);
    expect((await repo.propose("send_followup", null, proposal, key, ORG.email)).row.state).toBe("failed");
    await expect(executeAction(repo, env as Env, row.action_id, ORG, buildExecutors({ mailSender: fake }))).rejects.toThrow(/only approved/);
    expect(sends).toBe(1);
    const recovered = await reconcileAction(repo, row.action_id, ORG, "not_delivered", "Synthetic provider confirms no delivery");
    expect(recovered.state).toBe("proposed"); expect(recovered.approved_by).toBeNull();
    expect(recovered.idempotency_key).toBe(key);
    const event = await repo.latestSubscriberEvent("action_reconciliation_attempt");
    expect(event?.provider).toBe("mail");
  });
});
