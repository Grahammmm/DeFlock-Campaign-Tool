// Regression tests for the 2026-10-01 audit of the Phase 1-3 stack: CSRF, finding author,
// signed downloads, fail-closed Access, inbound size cap, failed-card re-proposal, the
// newsletter's ambiguous send, list id from Settings, template consent footer, deploy stamping.
import { describe, expect, it } from "vitest";
import { env } from "cloudflare:test";
import { BrevoError, FakeBrevo } from "../src/brevo.ts";
import { approveAction, executeAction } from "../src/approvals.ts";
import { buildExecutors } from "../src/executors/index.ts";
import { signDownload } from "../src/signing.ts";
import { MAX_INBOUND_BYTES, emailHandler } from "../src/mail.ts";
import type { Env } from "../src/env.ts";
import type { AccessIdentity } from "../src/auth.ts";
import { call, makeSigner, organizerRequest, seedCampaign } from "./helpers.ts";

const ORG: AccessIdentity = { email: "organizer@example.invalid", sub: "sub-organizer", issued_at: 0, expires_at: 0 };
const HTML = '<p>Update</p><p><a href="{{ unsubscribe }}">Unsubscribe</a></p>';

describe("cross-site requests", () => {
  it("a state-changing request from another origin is refused before the identity check", async () => {
    const signer = await makeSigner();
    const repo = await seedCampaign();
    const { row } = await repo.propose("deploy_site", null, { site_version: "csrf1" }, "csrf-1", "job_csrf");
    for (const headers of [{ origin: "https://attacker.example" }, { "sec-fetch-site": "cross-site" }] as Record<string, string>[]) {
      const r = await organizerRequest(signer, ORG.email, `/approvals/${row.action_id}/approve`, { method: "POST", headers: { accept: "application/json", ...headers } });
      const res = await call(r.request, r.overrides);
      expect(res.status).toBe(403);
    }
    expect((await repo.action(row.action_id))!.state).toBe("proposed");
    // same-origin and header-less (non-browser) requests pass to the identity check
    const same = await organizerRequest(signer, ORG.email, `/approvals/${row.action_id}/approve`, { method: "POST", headers: { accept: "application/json", origin: "https://workspace.example.invalid", "sec-fetch-site": "same-origin" } });
    expect((await call(same.request, same.overrides)).status).toBe(200);
    // reads are never blocked
    const get = await organizerRequest(signer, ORG.email, "/approvals", { headers: { origin: "https://attacker.example" } });
    expect((await call(get.request, get.overrides)).status).toBe(200);
  });
});

describe("finding author", () => {
  it("is always the verified identity, whatever the body says", async () => {
    const signer = await makeSigner();
    await seedCampaign();
    const r = await organizerRequest(signer, ORG.email, "/api/findings", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ summary: "Synthetic", author: "someone@else.invalid", classification: "documented_fact", confidence: "verified" }) });
    const res = await call(r.request, r.overrides);
    expect(res.status).toBe(201);
    const { finding_id } = (await res.json()) as { finding_id: string };
    const row = (await (await seedCampaign()).finding(finding_id))!;
    expect(row.author).toBe(ORG.email);
    expect((JSON.parse(row.finding_json) as { author: string }).author).toBe(ORG.email);
  });
});

describe("signed downloads and fail-closed Access", () => {
  it("rejects an expired link, a bad signature and a non-hex hash; 503 when Access is unconfigured", async () => {
    const sha = "a".repeat(64);
    const future = Math.floor(Date.now() / 1000) + 600;
    const good = await signDownload(env.DOWNLOAD_SIGNING_KEY, sha, future);
    const past = Math.floor(Date.now() / 1000) - 1;
    const expired = await signDownload(env.DOWNLOAD_SIGNING_KEY, sha, past);
    const base = "https://workspace.example.invalid/dl/";
    expect((await call(new Request(`${base}${sha}?exp=${past}&sig=${expired}`))).status).toBe(403);
    expect((await call(new Request(`${base}${sha}?exp=${future}&sig=${"0".repeat(64)}`))).status).toBe(403);
    expect((await call(new Request(`${base}nothex?exp=${future}&sig=${good}`))).status).toBe(403);
    // a valid link for an object that does not exist is a 404, not a 403: the signature path works
    expect((await call(new Request(`${base}${sha}?exp=${future}&sig=${good}`))).status).toBe(404);
    // fail closed: with Access unconfigured even a well-formed token is refused with 503
    const signer = await makeSigner();
    const r = await organizerRequest(signer, ORG.email, "/api/me");
    const unconfigured = await call(r.request, { ...r.overrides, ACCESS_TEAM_DOMAIN: "" } as Partial<Env>);
    expect(unconfigured.status).toBe(503);
  });
});

describe("inbound mail size", () => {
  it("refuses a message above the cap before any write and sizes the buffer from the bytes read", async () => {
    const seeded = await seedCampaign();
    const before = (await seeded.db.prepare("SELECT COUNT(*) AS n FROM correspondence").first<{ n: number }>())!.n;
    const bytes = new TextEncoder().encode("From: a@example.invalid\r\nTo: records@example.invalid\r\nSubject: s\r\n\r\nbody\r\n");
    const stream = () => new ReadableStream<Uint8Array>({ start(c) { c.enqueue(bytes); c.close(); } });
    const message = (rawSize: number) => ({ from: "a@example.invalid", to: "records@example.invalid", headers: new Headers(), raw: stream(), rawSize, setReject() {}, forward: async () => {}, reply: async () => {} });
    await expect(emailHandler(message(MAX_INBOUND_BYTES + 1) as unknown as ForwardableEmailMessage, env as Env)).rejects.toThrow(/exceeds/);
    const after = (await seeded.db.prepare("SELECT COUNT(*) AS n FROM correspondence").first<{ n: number }>())!.n;
    expect(after).toBe(before);
    // a wrong size hint neither pads nor truncates the stored bytes
    await emailHandler(message(bytes.byteLength * 3) as unknown as ForwardableEmailMessage, env as Env);
    const stored = (await seeded.db.prepare("SELECT raw_sha256 FROM correspondence ORDER BY rowid DESC LIMIT 1").first<{ raw_sha256: string }>())!;
    const digest = Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", bytes))).map((b) => b.toString(16).padStart(2, "0")).join("");
    expect(stored.raw_sha256).toBe(digest);
  });
});

describe("failed cards and the newsletter", () => {
  it("a failed card is re-opened by the next proposal with the same key; send is ambiguous after create", async () => {
    const repo = await seedCampaign();
    await repo.putSetting("brevo", { list_id: 42, form_url: null, sender_name: "Example Campaign", sender_email: "news@campaign.example.invalid" });
    const brevo = new FakeBrevo();
    brevo.failSend = new BrevoError("timeout", 0);
    const { row } = await repo.propose("send_newsletter", "job_x1", { subject: "x", html: HTML, text: "", list_id: 7 }, "audit-nl-1", ORG.email);
    await approveAction(repo, row.action_id, ORG);
    const failed = await executeAction(repo, env as Env, row.action_id, ORG, buildExecutors({ brevo }));
    expect(failed.state).toBe("failed");
    expect(failed.error).toMatch(/^brevo_send_ambiguous: Brevo campaign 1001/);
    expect(brevo.created[0].listIds).toEqual([42]); // Settings, not the card's list_id
    expect((await repo.latestSubscriberEvent("campaign_send_ambiguous"))?.payload_json).toContain('"brevo_campaign_id":1001');
    // the same key proposes again: the failed card is re-opened, not returned as failed
    const again = await repo.propose("send_newsletter", "job_x1", { subject: "x2", html: HTML, text: "" }, "audit-nl-1", ORG.email);
    expect(again.inserted).toBe(false);
    expect(again.row.action_id).toBe(row.action_id);
    expect(again.row.state).toBe("proposed");
    expect(again.row.error).toMatch(/^re-proposed after: brevo_send_ambiguous/);
    expect(JSON.parse(again.row.proposal_json).subject).toBe("x2");
    // an executed card is never re-opened
    brevo.failSend = null;
    await approveAction(repo, row.action_id, ORG);
    expect((await executeAction(repo, env as Env, row.action_id, ORG, buildExecutors({ brevo }))).state).toBe("executed");
    const sent = await repo.propose("send_newsletter", "job_x1", { subject: "x3", html: HTML, text: "" }, "audit-nl-1", ORG.email);
    expect(sent.row.state).toBe("executed");
  });

  it("refuses a draft that still carries the engine's template consent footer", async () => {
    const repo = await seedCampaign();
    await repo.putSetting("brevo", { list_id: 42, form_url: null, sender_name: "Example Campaign", sender_email: "news@campaign.example.invalid" });
    const brevo = new FakeBrevo();
    const { row } = await repo.propose("send_newsletter", null, { subject: "x", html: HTML + "<p>[TEMPLATE CONSENT FOOTER: replace it]</p>", text: "" }, "audit-nl-2", ORG.email);
    await approveAction(repo, row.action_id, ORG);
    expect((await executeAction(repo, env as Env, row.action_id, ORG, buildExecutors({ brevo }))).error).toMatch(/^template_consent_footer/);
    expect(brevo.created).toEqual([]);
  });
});

describe("deploy_site stamping", () => {
  it("stamps only publications carried by the deployed build's manifest", async () => {
    const repo = await seedCampaign();
    const stale = await repo.enqueueJob("build_site", "audit-build-1", { site_version: "v-stale", manifest: { findings: [{ id: "fnd_in_version" }] } });
    await repo.db.prepare("UPDATE job SET state = 'done' WHERE job_id = ?").bind(stale.row.job_id).run();
    await repo.createFinding({ finding_id: "fnd_in_version", author: ORG.email, classification: "documented_fact", confidence: "verified", summary: "in", finding_json: "{}", content_sha256: "a".repeat(64), state: "published" });
    await repo.createFinding({ finding_id: "fnd_later", author: ORG.email, classification: "documented_fact", confidence: "verified", summary: "later", finding_json: "{}", content_sha256: "b".repeat(64), state: "published" });
    const inVersion = await repo.createPublication({ publication_id: "pub_audit_in_version", finding_id: "fnd_in_version", path: "/findings/in.html", content_sha256: "a".repeat(64), published_at: "2026-09-01T00:00:00Z", published_by: ORG.email, deploy_receipt: null });
    const later = await repo.createPublication({ publication_id: "pub_audit_later", finding_id: "fnd_later", path: "/findings/later.html", content_sha256: "b".repeat(64), published_at: "2026-09-02T00:00:00Z", published_by: ORG.email, deploy_receipt: null });
    const { row } = await repo.propose("deploy_site", null, { site_version: "v-stale", build_job_id: stale.row.job_id }, "audit-deploy-1", ORG.email);
    await approveAction(repo, row.action_id, ORG);
    expect((await executeAction(repo, env as Env, row.action_id, ORG)).state).toBe("executed");
    expect((await repo.publication(inVersion.publication_id))!.deploy_receipt).toContain("v-stale");
    expect((await repo.publication(later.publication_id))!.deploy_receipt).toBeNull();
  });
});
