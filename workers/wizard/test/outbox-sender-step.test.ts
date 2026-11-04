import { createExecutionContext, env } from "cloudflare:test";
import { describe, expect, it } from "vitest";
import worker from "../src/index.ts";
import { applyPlan } from "../src/apply.ts";
import { CloudflareClient } from "../src/cloudflare.ts";
import { buildPlan, redactStep } from "../src/plan.ts";
import { decryptBlob } from "../src/session.ts";
import { HALF_CONFIGURED, outboxSecrets, parseOutboxSender } from "../src/steps/outbox-sender.ts";
import stepSource from "../src/steps/outbox-sender.ts?raw";
import { agenciesFor, locate, type AgencySeed } from "@deflock/shared/agencies";
import seed from "../src/generated/agencies-us-ca.json";
import { emptyState, type WizardState } from "../src/state.ts";

// Synthetic values only: 43 base64url characters (32 bytes) built here, never a real secret.
const KEY = "t".repeat(20) + "-_" + "T".repeat(21);
const URL_OK = "https://outbox.example.invalid/send";
const CF = { cf_token: "cf-test-token-not-a-real-credential", cf_account_id: "a".repeat(32), cf_zone_id: "b".repeat(32), cf_access_team: "example-team", mail_mode: "email_routing", model_base_url: "https://model.example.invalid/v1", model_api_key: "k", model_id: "m" };
const BUNDLES = { workspace: "export default {}", public_site: "export default {}", migrations: [{ name: "0001_init.sql", sql: "CREATE TABLE t (x);" }] };

function fullState(): WizardState {
  const s = emptyState("01k6example0000000000000ab");
  s.location = locate("City of Morro Bay", "CA", seed as AgencySeed);
  s.suggested = agenciesFor(s.location, seed as AgencySeed);
  s.campaign = { name: "Morro Bay ALPR Records", tagline: "t", domain: "campaign.example.invalid", privacy_tier: "redacted_cloud", schedule_cron: "0 7 * * *", timezone: "America/Los_Angeles", organizer_email: "organizer@example.invalid" };
  s.accounts = {
    cloudflare: { api_token: "cf-test-token-not-a-real-credential", account_id: "a".repeat(32), zone_id: "b".repeat(32), access_team: "example-team" },
    mailbox: { mode: "email_routing", address: "requests@campaign.example.invalid" },
    brevo: null,
    model: null,
  };
  s.step = 6;
  return s;
}

describe("parseOutboxSender", () => {
  it("both given -> registered with the pair, trimmed", () => {
    expect(parseOutboxSender(` ${URL_OK} `, ` ${KEY} `)).toEqual({ status: "registered", sender: { gateway_url: URL_OK, signing_key: KEY }, error: null });
  });

  it("neither given -> skipped, no pair", () => {
    expect(parseOutboxSender("", "")).toEqual({ status: "skipped", sender: null, error: null });
    expect(parseOutboxSender("  ", "\t")).toEqual({ status: "skipped", sender: null, error: null });
  });

  it("exactly one given (each way) -> named refusal with no sender", () => {
    expect(parseOutboxSender(URL_OK, "")).toEqual({ status: "refused", sender: null, error: HALF_CONFIGURED });
    expect(parseOutboxSender("", KEY)).toEqual({ status: "refused", sender: null, error: HALF_CONFIGURED });
    expect(parseOutboxSender(URL_OK, "   ")).toMatchObject({ status: "refused", error: HALF_CONFIGURED });
    expect(HALF_CONFIGURED).toBe("outbox_sender: half configured — both settings or neither");
  });

  it.each([
    ["http scheme", "http://outbox.example.invalid/send", "must use https"],
    ["userinfo", "https://user:pw@outbox.example.invalid/send", "credentials"],
    ["username only", "https://user@outbox.example.invalid/send", "credentials"],
    ["query", "https://outbox.example.invalid/send?x=1", "query"],
    ["empty query marker", "https://outbox.example.invalid/send?", "query"],
    ["fragment", "https://outbox.example.invalid/send#f", "fragment"],
    ["no host", "https://", "not a URL"],
    ["host lost in extra slashes", "https:///send", "exactly /send"],
    ["not a url", "outbox.example.invalid/send", "not a URL"],
    ["wrong path", "https://outbox.example.invalid/", "exactly /send"],
    ["nested path", "https://outbox.example.invalid/x/send", "exactly /send"],
  ])("refuses a bad URL (%s)", (_name, url, why) => {
    const r = parseOutboxSender(url, KEY);
    expect(r.status).toBe("refused");
    expect(r.error).toContain(why);
    expect(r.sender).toBeNull();
  });

  it.each([
    ["too short", "t".repeat(42), "at least 32 bytes"],
    ["not base64url", "t".repeat(43) + "+", "base64url"],
    ["padding", "t".repeat(43) + "=", "base64url"],
    ["space inside", "t".repeat(20) + " " + "t".repeat(30), "base64url"],
  ])("refuses a bad secret (%s) without echoing it", (_name, key, why) => {
    const r = parseOutboxSender(URL_OK, key);
    expect(r.status).toBe("refused");
    expect(r.error).toContain(why);
    expect(r.error).not.toContain(key);
    expect(r.sender).toBeNull();
  });
});

describe("outbox sender in the plan", () => {
  it("registered -> exactly the pair of secrets, both redacted in the displayed plan", () => {
    const s = fullState();
    s.accounts!.outbox = { gateway_url: URL_OK, signing_key: KEY };
    const plan = buildPlan(s);
    const ids = plan.map((p) => p.id);
    expect(ids).toContain("secret.OUTBOX_GATEWAY_URL");
    expect(ids).toContain("secret.OUTBOX_SIGNING_KEY");
    expect(ids.filter((id) => id.includes("OUTBOX")).length).toBe(2);
    const shown = JSON.stringify(plan.map(redactStep));
    expect(shown).not.toContain(KEY);
    expect(shown).not.toContain(URL_OK);
    expect(outboxSecrets({ gateway_url: URL_OK, signing_key: KEY }).map(([n]) => n)).toEqual(["OUTBOX_GATEWAY_URL", "OUTBOX_SIGNING_KEY"]);
  });

  it("neither -> no outbox step at all (null and absent)", () => {
    const s = fullState();
    for (const outbox of [undefined, null]) {
      s.accounts!.outbox = outbox;
      expect(buildPlan(s).some((p) => p.id.includes("OUTBOX"))).toBe(false);
    }
    expect(outboxSecrets(null)).toEqual([]);
    expect(outboxSecrets(undefined)).toEqual([]);
  });

  it("apply sends the pair as Worker secrets and the receipts never carry the secret", async () => {
    const s = fullState();
    s.accounts!.outbox = { gateway_url: URL_OK, signing_key: KEY };
    const secrets: { name: string; text: string }[] = [];
    const fetcher = (async (url: string, init: RequestInit) => {
      const path = new URL(url).pathname;
      if (path.endsWith("/secrets")) secrets.push(JSON.parse(String(init.body)));
      const result = path.endsWith("/access/apps") ? { id: "app-1", aud: "aud" } : { id: "id", uuid: "uuid", name: "campaign.example.invalid" };
      return new Response(JSON.stringify({ success: true, errors: [], result }), { status: 200 });
    }) as unknown as typeof fetch;
    const r = await applyPlan(buildPlan(s), { client: new CloudflareClient("t", { dryRun: false, fetcher }), bundles: BUNDLES, generated: { runner_token: "rt_fixed", download_key: "dk_fixed" } });
    expect(r.status).toBe("applied");
    expect(secrets.find((x) => x.name === "OUTBOX_GATEWAY_URL")!.text).toBe(URL_OK);
    expect(secrets.find((x) => x.name === "OUTBOX_SIGNING_KEY")!.text).toBe(KEY);
    expect(JSON.stringify(r)).not.toContain(KEY);
  });
});

let cookie = "";
async function req(path: string, form?: Record<string, string | string[]>): Promise<Response> {
  const headers = new Headers();
  if (cookie) headers.set("cookie", cookie);
  let body: string | undefined;
  if (form) {
    const p = new URLSearchParams();
    for (const [k, v] of Object.entries(form)) for (const x of Array.isArray(v) ? v : [v]) p.append(k, x);
    body = p.toString();
    headers.set("content-type", "application/x-www-form-urlencoded");
  }
  const res = await worker.fetch(new Request("https://setup.example.invalid" + path, { method: form ? "POST" : "GET", headers, body }), env, createExecutionContext());
  const set = res.headers.get("set-cookie");
  if (set) cookie = set.split(";")[0];
  return res;
}
async function rawSession(): Promise<string> {
  const blob = await env.SESSIONS.get("session:" + cookie.split("=")[1]);
  return JSON.stringify(await decryptBlob(env.SESSION_KEY, blob!));
}
async function sessionState(): Promise<WizardState> {
  return (JSON.parse(await rawSession()) as { state: WizardState }).state;
}
async function toScreen5(): Promise<void> {
  cookie = "";
  await req("/setup/1", { query: "City of Morro Bay", state: "CA" });
  await req("/setup/2", { name: "Morro Bay ALPR Records", tagline: "t", domain: "campaign.example.invalid", privacy_tier: "redacted_cloud", schedule_cron: "0 7 * * *", timezone: "America/Los_Angeles", organizer_email: "organizer@example.invalid" });
  await req("/setup/3", { agency: ["ca-morro-bay-police"] });
  await req("/setup/4", { "subject:ca-morro-bay-police": "S", "body:ca-morro-bay-police": "B", "channel:ca-morro-bay-police": "email", "fee_cap:ca-morro-bay-police": "25" });
}

describe("wizard screen 5 outbox sender step", () => {
  it("shows the step, default skip, with an empty password field", async () => {
    await toScreen5();
    const page = await (await req("/setup/5")).text();
    expect(page).toContain("Outbox sender");
    expect(page).toContain('name="outbox_gateway_url"');
    expect(page).toContain('name="outbox_signing_key"');
    expect(page).toContain("Every send still needs a recorded organizer approval");
  });

  it("skipped: neither setting is emitted and the step records skipped", async () => {
    await toScreen5();
    const res = await req("/setup/5", CF);
    expect(res.headers.get("location")).toBe("/setup/6");
    const s = await sessionState();
    expect(s.outbox_sender).toBe("skipped");
    expect(s.accounts?.outbox ?? null).toBeNull();
    const plan = (await (await req("/setup/6/plan.json")).json()) as { id: string }[];
    expect(plan.some((p) => p.id.includes("OUTBOX"))).toBe(false);
  });

  it.each([
    ["URL only", { outbox_gateway_url: URL_OK }],
    ["secret only", { outbox_signing_key: KEY }],
  ])("%s: refused by name, nothing saved or emitted, secret not echoed", async (_name, extra) => {
    await toScreen5();
    const res = await req("/setup/5", { ...CF, ...extra });
    expect(res.status).toBe(200);
    const text = await res.text();
    expect(text).toContain("outbox_sender: half configured");
    expect(text).not.toContain(KEY);
    const s = await sessionState();
    expect(s.accounts).toBeNull();
    expect(s.step).toBeLessThan(6);
    expect(s.outbox_sender).toBeUndefined();
    expect((await rawSession())).not.toContain(KEY);
  });

  it("a bad URL or short secret is refused and the secret is not echoed", async () => {
    await toScreen5();
    const bad = await (await req("/setup/5", { ...CF, outbox_gateway_url: "http://outbox.example.invalid/send", outbox_signing_key: KEY })).text();
    expect(bad).toContain("must use https");
    expect(bad).not.toContain(KEY);
    const short = "t".repeat(40);
    const bad2 = await (await req("/setup/5", { ...CF, outbox_gateway_url: URL_OK, outbox_signing_key: short })).text();
    expect(bad2).toContain("at least 32 bytes");
    expect(bad2).not.toContain(short);
    expect((await sessionState()).accounts).toBeNull();
  });

  it("registered: pair planned, secret never in pages, plan.json, session blob, receipt or handoff", async () => {
    await toScreen5();
    const res = await req("/setup/5", { ...CF, outbox_gateway_url: URL_OK, outbox_signing_key: KEY });
    expect(res.headers.get("location")).toBe("/setup/6");
    const s = await sessionState();
    expect(s.outbox_sender).toBe("registered");
    expect(s.accounts?.outbox).toEqual({ gateway_url: URL_OK, signing_key: KEY });
    const artefacts: string[] = [];
    artefacts.push(await (await req("/setup/5")).text());
    const page6 = await (await req("/setup/6")).text();
    artefacts.push(page6);
    const planText = await (await req("/setup/6/plan.json")).text();
    artefacts.push(planText);
    const plan = JSON.parse(planText) as { id: string; body?: { text?: string } }[];
    expect(plan.find((p) => p.id === "secret.OUTBOX_GATEWAY_URL")!.body!.text).toBe("[redacted]");
    expect(plan.find((p) => p.id === "secret.OUTBOX_SIGNING_KEY")!.body!.text).toBe("[redacted]");
    expect((await req("/setup/6", { confirm: "yes" })).headers.get("location")).toBe("/setup/7");
    const after = await sessionState();
    expect(after.accounts).toBeNull();
    expect(after.outbox_sender).toBe("registered");
    artefacts.push(await rawSession());
    artefacts.push(await (await req("/setup/7")).text());
    const receipt = await (await req("/setup/7/launch-receipt.json")).text();
    artefacts.push(receipt);
    expect((JSON.parse(receipt) as { outbox_sender: string }).outbox_sender).toBe("registered");
    for (const a of artefacts) expect(a.includes(KEY)).toBe(false);
  });

  it("the launch receipt names skipped when the step was skipped", async () => {
    await toScreen5();
    await req("/setup/5", CF);
    await req("/setup/6", { confirm: "yes" });
    const receipt = (await (await req("/setup/7/launch-receipt.json")).json()) as { outbox_sender: string };
    expect(receipt.outbox_sender).toBe("skipped");
  });
});

describe("outbox sender step source (static)", () => {
  it("adds no network call, deploy tool or approval-skipping setting", () => {
    expect(stepSource).not.toMatch(/\bfetch\s*\(/);
    expect(stepSource).not.toMatch(/wrangler/i);
    expect(stepSource).not.toMatch(/console\./);
    expect(stepSource).not.toMatch(/auto[_-]?send|skip[_-]?approval|bypass|AUTO_APPROVE/i);
  });

  it("the plan emits only the two outbox settings, so no extra switch exists", () => {
    const s = fullState();
    s.accounts!.outbox = { gateway_url: URL_OK, signing_key: KEY };
    expect(buildPlan(s).map((p) => p.id).filter((id) => /OUTBOX|SEND|APPROV/i.test(id))).toEqual(["secret.OUTBOX_GATEWAY_URL", "secret.OUTBOX_SIGNING_KEY"]);
  });
});
