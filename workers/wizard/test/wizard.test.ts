import { createExecutionContext, env } from "cloudflare:test";
import { describe, expect, it } from "vitest";
import worker from "../src/index.ts";
import { decryptBlob } from "../src/session.ts";
import type { WizardState } from "../src/state.ts";

const ORIGIN = "https://setup.example.invalid";
let cookie = "";

async function req(path: string, init: RequestInit & { form?: Record<string, string | string[]> } = {}): Promise<Response> {
  const headers = new Headers(init.headers);
  if (cookie) headers.set("cookie", cookie);
  let body = init.body;
  if (init.form) {
    const p = new URLSearchParams();
    for (const [k, v] of Object.entries(init.form)) for (const x of Array.isArray(v) ? v : [v]) p.append(k, x);
    body = p.toString();
    headers.set("content-type", "application/x-www-form-urlencoded");
  }
  const res = await worker.fetch(new Request(ORIGIN + path, { ...init, headers, body }), env, createExecutionContext());
  const set = res.headers.get("set-cookie");
  if (set) cookie = set.split(";")[0];
  return res;
}

async function sessionState(): Promise<WizardState> {
  const id = cookie.split("=")[1];
  const blob = await env.SESSIONS.get("session:" + id);
  return (await decryptBlob<{ state: WizardState }>(env.SESSION_KEY, blob!)).state;
}

describe("wizard flow (DRY_RUN=1)", () => {
  it("screen 1 resolves a location, reports ambiguity, and applies the default selection", async () => {
    const amb = await req("/setup/1", { method: "POST", form: { query: "San Luis Obispo", state: "CA" } });
    expect(amb.status).toBe(200);
    expect(await amb.text()).toContain("ambiguous");
    const bad = await req("/setup/1", { method: "POST", form: { query: "Nowhere", state: "CA" } });
    expect(await bad.text()).toContain("No county or city");
    const ok = await req("/setup/1", { method: "POST", form: { query: "City of Morro Bay", state: "CA" } });
    expect(ok.status).toBe(303);
    expect(ok.headers.get("location")).toBe("/setup/2");
    expect(cookie).toMatch(/^deflock_wizard=[0-9a-f]{32}$/);
    const s = await sessionState();
    expect(s.location?.place_name).toBe("Morro Bay");
    expect(s.agencies.map((a) => [a.agency_id, a.selected])).toEqual([
      ["ca-morro-bay-police", true],
      ["ca-morro-bay-city-council", true],
      ["ca-san-luis-obispo-county-sheriff", true],
      ["ca-san-luis-obispo-county-board-of-supervisors", true],
      ["ca-san-luis-obispo-county-district-attorney", false],
      ["ca-san-luis-obispo-county-chp", false],
    ]);
    expect(Object.keys(s.requests).sort()).toEqual(["ca-morro-bay-police", "ca-san-luis-obispo-county-sheriff"]);
    expect((await req("/setup/3")).headers.get("location")).toBe("/setup/1"); // gated until screen 2 is done
    const page2 = await (await req("/setup/2")).text();
    expect(page2).toContain("Morro Bay (San Luis Obispo County, CA)");
  });

  it("screens 2-5 validate and store settings; later screens are gated", async () => {
    expect((await req("/setup/5")).headers.get("location")).toBe("/setup/1");
    const bad = await req("/setup/2", { method: "POST", form: { name: "X", domain: "not a domain", organizer_email: "nope", schedule_cron: "bad", timezone: "UTC" } });
    expect(await bad.text()).toContain("domain must be");
    const ok = await req("/setup/2", { method: "POST", form: { name: "Morro Bay ALPR Records", tagline: "t", domain: "Campaign.Example.Invalid", privacy_tier: "strict_local", schedule_cron: "0 7 * * *", timezone: "America/Los_Angeles", organizer_email: "Organizer@Example.invalid" } });
    expect(ok.headers.get("location")).toBe("/setup/3");
    expect((await sessionState()).campaign?.domain).toBe("campaign.example.invalid");
    const page3 = await (await req("/setup/3")).text();
    expect(page3).toContain('value="ca-morro-bay-police" checked');
    expect(page3).toContain('value="ca-san-luis-obispo-county-chp" >');
    // select CHP too; drop the sheriff
    const a3 = await req("/setup/3", { method: "POST", form: { agency: ["ca-morro-bay-police", "ca-san-luis-obispo-county-chp", "ca-morro-bay-city-council"] } });
    expect(a3.headers.get("location")).toBe("/setup/4");
    const s3 = await sessionState();
    expect(Object.keys(s3.requests).sort()).toEqual(["ca-morro-bay-police", "ca-san-luis-obispo-county-chp"]);
    expect(s3.requests["ca-morro-bay-police"].body_md).toContain("## ALPR vendor agreements and funding");
    const a4 = await req("/setup/4", { method: "POST", form: { "subject:ca-morro-bay-police": "Edited subject", "body:ca-morro-bay-police": "edited body", "channel:ca-morro-bay-police": "muckrock", "fee_cap:ca-morro-bay-police": "25" } });
    expect(a4.headers.get("location")).toBe("/setup/5");
    const s4 = await sessionState();
    expect(s4.requests["ca-morro-bay-police"]).toMatchObject({ subject: "Edited subject", body_md: "edited body", channel: "muckrock", fee_cap_cents: 2500 });
    expect(s4.requests["ca-san-luis-obispo-county-chp"].channel).toBe("email");
    // strict_local rejects a cloud model URL, accepts loopback
    const form = { cf_token: "cf-test-token-not-a-real-credential", cf_account_id: "a".repeat(32), cf_zone_id: "b".repeat(32), cf_access_team: "example-team", mail_mode: "email_routing", model_base_url: "https://model.example.invalid/v1", model_api_key: "k", model_id: "m" };
    const a5bad = await req("/setup/5", { method: "POST", form });
    expect(await a5bad.text()).toContain("strict_local: model base URL must be loopback");
    const a5 = await req("/setup/5", { method: "POST", form: { ...form, model_base_url: "http://127.0.0.1:11434/v1" } });
    expect(a5.headers.get("location")).toBe("/setup/6");
    expect((await sessionState()).accounts?.cloudflare.api_token).toBe("cf-test-token-not-a-real-credential");
  });

  it("screen 6 shows the plan with every resource; apply in DRY_RUN yields receipts, rollback and handoff without persisted tokens", async () => {
    const page6 = await (await req("/setup/6")).text();
    expect(page6).toContain("DRY_RUN is on");
    expect(page6).not.toContain("cf-test-token-not-a-real-credential");
    const plan = (await (await req("/setup/6/plan.json")).json()) as { id: string }[];
    for (const id of ["d1.create", "r2.originals", "r2.public", "kv.create", "queue.create", "worker.workspace", "worker.public", "d1.migrate", "d1.seed", "dns.public", "dns.workspace", "route.public", "route.workspace", "access.app", "access.policy", "email.rule", "cron.workspace", "secret.MODEL_BASE_URL"]) {
      expect(plan.map((p) => p.id)).toContain(id);
    }
    expect((await req("/setup/6", { method: "POST", form: {} })).status).toBe(200); // confirm required
    const applied = await req("/setup/6", { method: "POST", form: { confirm: "yes" } });
    expect(applied.headers.get("location")).toBe("/setup/7");
    const s = await sessionState();
    expect(s.accounts).toBeNull();
    expect(s.step).toBe(7);
    expect(s.deploy?.status).toBe("applied");
    expect(s.deploy?.dry_run).toBe(true);
    expect(s.deploy?.receipts.length).toBe(plan.length);
    expect(s.deploy?.rollback.length).toBeGreaterThan(10);
    const blob = await env.SESSIONS.get("session:" + cookie.split("=")[1]);
    const decrypted = JSON.stringify(await decryptBlob(env.SESSION_KEY, blob!));
    expect(decrypted).not.toContain("cf-test-token-not-a-real-credential");
    expect(decrypted).not.toContain("rt_");
    // the runner token waits for the handoff screen encrypted, never in clear in KV
    const sealed = await env.SESSIONS.get("handoff-token:" + cookie.split("=")[1]);
    expect(sealed).toBeTruthy();
    expect(sealed).not.toContain("rt_");
    expect(await decryptBlob(env.SESSION_KEY, sealed!)).toMatch(/^rt_[0-9a-f]{64}$/);
    const handoff = await (await req("/setup/7")).text();
    expect(handoff).toContain("https://workspace.campaign.example.invalid");
    expect(handoff).toContain("dry run: no resources were created");
    const token = /<code>(rt_[0-9a-f]{64})<\/code>/.exec(handoff)?.[1];
    expect(token).toBeTruthy();
    expect(await (await req("/setup/7")).text()).not.toContain(token!); // shown once
    const receipt = (await (await req("/setup/7/launch-receipt.json")).json()) as { campaign_id: string; deploy: { receipts: unknown[] }; resources: { workspace_url: string } };
    expect(receipt.campaign_id).toBe(s.campaign_id);
    expect(receipt.deploy.receipts.length).toBe(plan.length);
    expect(JSON.stringify(receipt)).not.toContain("cf-test-token-not-a-real-credential");
    expect(JSON.stringify(receipt)).not.toContain(token!);
    const reset = await req("/setup/reset", { method: "POST" });
    expect(reset.headers.get("set-cookie")).toContain("Max-Age=0");
    expect(await env.SESSIONS.get("session:" + cookie.split("=")[1])).toBeNull();
  });

  it("stores a sibling workspace hostname and switches an existing mailbox off without retaining credentials", async () => {
    cookie = "";
    await req("/setup/1", { method: "POST", form: { query: "City of Morro Bay", state: "CA" } });
    const campaign = { name: "Synthetic", domain: "tool.example.invalid", organizer_email: "organizer@example.invalid", schedule_cron: "0 7 * * *", workspace_hostname: "TOOL-Workspace.Example.Invalid" };
    expect((await req("/setup/2", { method: "POST", form: campaign })).status).toBe(303);
    await req("/setup/3", { method: "POST", form: {} });
    await req("/setup/4", { method: "POST", form: {} });
    const accounts = { cf_token: "cf-test-token-not-a-real-credential", cf_account_id: "a".repeat(32), cf_zone_id: "b".repeat(32), cf_access_team: "example-team" };
    const imap = await req("/setup/5", { method: "POST", form: { ...accounts, mail_mode: "imap_smtp", imap_host: "mail.example.invalid", imap_user: "synthetic", imap_password: "synthetic-password" } }); // pragma: allowlist secret -- synthetic test credential only
    expect(imap.status).toBe(303);
    const off = await req("/setup/5", { method: "POST", form: { ...accounts, mail_mode: "none", smtp_password: "synthetic-stale" } }); // pragma: allowlist secret -- synthetic stale credential only
    expect(off.status).toBe(303);
    expect((await sessionState()).accounts?.mailbox).toEqual({ mode: "none", address: "" });
    const html = await (await req("/setup/5")).text();
    expect(html).toContain('value="none" selected');
    const plan = await (await req("/setup/6/plan.json")).json() as { id: string; body: unknown }[];
    expect(plan.some((p) => p.id.startsWith("email.") || /^secret\.(IMAP|SMTP)_/.test(p.id))).toBe(false);
    expect(plan.find((p) => p.id === "access.app")!.body).toMatchObject({ domain: "tool-workspace.example.invalid" });
    expect((await sessionState()).campaign?.workspace_hostname).toBe("tool-workspace.example.invalid");
    for (const host of ["not/a/host", campaign.domain]) {
      const bad = await req("/setup/2", { method: "POST", form: { ...campaign, workspace_hostname: host } });
      expect(await bad.text()).toContain("workspace hostname must be");
    }
    const invalid = await req("/setup/5", { method: "POST", form: { ...accounts, mail_mode: "typo" } });
    expect(await invalid.text()).toContain("invalid mail mode");
    expect((await req("/setup/6", { method: "POST", form: { confirm: "yes" } })).status).toBe(303);
    expect((await sessionState()).accounts).toBeNull();
    const handoff = await (await req("/setup/7")).text();
    expect(handoff).toContain("Mail is off");
    expect(handoff).toContain("https://tool-workspace.example.invalid");
  });
});
