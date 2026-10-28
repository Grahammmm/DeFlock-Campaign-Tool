import { describe, expect, it } from "vitest";
import { agenciesFor, locate, type AgencySeed } from "@deflock/shared/agencies";
import seed from "../src/generated/agencies-us-ca.json";
import { applyPlan } from "../src/apply.ts";
import { CloudflareClient } from "../src/cloudflare.ts";
import { defaultChoices, reconcileDrafts } from "../src/drafts.ts";
import { buildPlan, planNames, redactStep, resolvePlaceholders, seedSql } from "../src/plan.ts";
import { emptyState, isStrictLocalUrl, type WizardState } from "../src/state.ts";
import law from "../src/generated/law-us-ca.json";
import type { LawPackage } from "@deflock/shared/requests";

const SEED = seed as AgencySeed;
const LAW = law as LawPackage;
const BUNDLES = { workspace: "export default {}", public_site: "export default {}", migrations: [{ name: "0001_init.sql", sql: "CREATE TABLE t (x);" }] };

export function fullState(): WizardState {
  const s = emptyState("01k6example0000000000000ab");
  s.location = locate("City of Morro Bay", "CA", SEED);
  s.suggested = agenciesFor(s.location, SEED);
  s.agencies = defaultChoices(s.suggested);
  s.campaign = { name: "Morro Bay ALPR Records", tagline: "t", domain: "campaign.example.invalid", privacy_tier: "redacted_cloud", schedule_cron: "0 7,13 * * *;30 20 * * *", timezone: "America/Los_Angeles", organizer_email: "organizer@example.invalid" };
  s.requests = reconcileDrafts(LAW, s.suggested, s.agencies, {});
  s.accounts = {
    cloudflare: { api_token: "cf-test-token-not-a-real-credential", account_id: "a".repeat(32), zone_id: "b".repeat(32), access_team: "example-team" },
    mailbox: { mode: "email_routing", address: "requests@campaign.example.invalid" },
    brevo: { api_key: "brevo-test-key", list_id: "7" }, // pragma: allowlist secret
    model: { base_url: "https://model.example.invalid/v1", api_key: "model-test-key", model_id: "example-model" }, // pragma: allowlist secret
  };
  s.step = 6;
  return s;
}

describe("plan", () => {
  it("lists every resource with its API call and rollback", () => {
    const s = fullState();
    const plan = buildPlan(s);
    const ids = plan.map((p) => p.id);
    for (const id of ["token.verify", "zone.verify", "d1.create", "r2.originals", "r2.public", "kv.create", "queue.create", "worker.public", "worker.workspace", "d1.migrate", "d1.seed", "dns.public", "dns.workspace", "route.public", "route.workspace", "access.app", "access.policy", "email.enable", "email.rule", "secret.CAMPAIGN_ID", "secret.ACCESS_AUD", "secret.RUNNER_TOKEN", "secret.DOWNLOAD_SIGNING_KEY", "secret.BREVO_API_KEY", "secret.MODEL_API_KEY", "cron.workspace"]) {
      expect(ids).toContain(id);
    }
    expect(ids.indexOf("d1.create")).toBeLessThan(ids.indexOf("worker.workspace"));
    expect(ids.indexOf("access.app")).toBeLessThan(ids.indexOf("secret.ACCESS_AUD"));
    const n = planNames(s);
    expect(n.originalsBucket).toBe("campaign-originals");
    expect(n.publicBucket).toBe("campaign-public");
    const created = plan.filter((p) => p.rollback);
    expect(created.length).toBeGreaterThanOrEqual(12);
    const cron = plan.find((p) => p.id === "cron.workspace")!;
    expect(cron.body).toEqual([{ cron: "0 7,13 * * *" }, { cron: "30 20 * * *" }]);
    // secrets are redacted in the displayed plan and the token never appears in it
    const shown = JSON.stringify(plan.map(redactStep));
    expect(shown).not.toContain("brevo-test-key");
    expect(shown).not.toContain("cf-test-token-not-a-real-credential");
    expect(shown).toContain("[redacted]");
  });

  it("seed SQL inserts the campaign, agencies with default selection and drafts", () => {
    const sql = seedSql(fullState());
    expect(sql).toContain("INSERT INTO campaign");
    expect(sql).toContain("'ca-morro-bay-police'");
    expect(sql.match(/INSERT INTO agency/g)?.length).toBe(6);
    expect(sql.match(/INSERT INTO request/g)?.length).toBe(2); // police + sheriff selected; CHP not
    expect(sql).toContain("'ca-san-luis-obispo-county-chp',");
    expect(sql).toMatch(/'ca-san-luis-obispo-county-chp',[^\n]*,0,0,/); // selected = 0
  });

  it("resolves placeholders and strict-local URL checks", () => {
    expect(resolvePlaceholders({ a: "{{x}}/y", b: ["{{y}}"] }, { x: "1", y: "2" })).toEqual({ a: "1/y", b: ["2"] });
    expect(isStrictLocalUrl("http://127.0.0.1:11434/v1")).toBe(true);
    expect(isStrictLocalUrl("http://100.101.1.2:8080")).toBe(true);
    expect(isStrictLocalUrl("http://box.tail1234.ts.net")).toBe(true);
    expect(isStrictLocalUrl("http://[::1]:8000")).toBe(true);
    expect(isStrictLocalUrl("https://api.example.invalid")).toBe(false);
    expect(isStrictLocalUrl("http://100.200.1.1")).toBe(false);
    expect(isStrictLocalUrl("not a url")).toBe(false);
  });
});

describe("apply", () => {
  it("DRY_RUN records every call, performs none, and produces receipts plus a rollback list", async () => {
    const s = fullState();
    const plan = buildPlan(s);
    let network = 0;
    const client = new CloudflareClient("t", { dryRun: true, fetcher: (async () => { network += 1; return new Response("{}"); }) as unknown as typeof fetch });
    const r = await applyPlan(plan, { client, bundles: BUNDLES });
    expect(network).toBe(0);
    expect(r.status).toBe("applied");
    expect(r.dry_run).toBe(true);
    expect(r.receipts.length).toBe(plan.length);
    expect(r.receipts.every((x) => x.ok && x.dry_run)).toBe(true);
    expect(client.calls.length).toBe(plan.length);
    expect(r.rollback[0].step_id).toBe("cron.workspace"); // newest first
    expect(r.rollback.at(-1)!.step_id).toBe("d1.create");
    expect(r.rollback.every((x) => !x.path.includes("{{"))).toBe(true);
    expect(r.outputs.d1_id).toMatch(/^dry-/);
    expect(r.outputs.access_aud).toMatch(/^dry-aud-/);
    // recorded bodies never hold secret values
    const recorded = JSON.stringify(client.calls);
    expect(recorded).not.toContain(r.generated.runner_token);
    expect(recorded).not.toContain("brevo-test-key");
    expect(JSON.stringify(r.receipts)).not.toContain(r.generated.runner_token);
  });

  it("organizer-typed request text is sent verbatim: a {{placeholder}} in a draft is never resolved", async () => {
    const s = fullState();
    const [first] = Object.keys(s.requests);
    s.requests[first] = { ...s.requests[first], body_md: s.requests[first].body_md + "\nToken: {{generated.runner_token}} and {{d1_id}}" };
    const plan = buildPlan(s);
    const client = new CloudflareClient("t", { dryRun: true, fetcher: (async () => new Response("{}")) as unknown as typeof fetch });
    const r = await applyPlan(plan, { client, bundles: BUNDLES });
    expect(r.status).toBe("applied");
    const seed = client.calls.filter((c) => c.path.endsWith("/query")).at(-1)!; // d1.migrate then d1.seed
    const sql = JSON.stringify(seed.body);
    expect(sql).toContain("{{generated.runner_token}}");
    expect(sql).not.toContain(r.generated.runner_token);
    expect(sql).not.toContain(r.outputs.d1_id);
  });

  it("stops at the first failure and lists (or performs) the rollback of what was created", async () => {
    const s = fullState();
    const plan = buildPlan(s);
    const seen: { method: string; url: string }[] = [];
    const fetcher = (async (url: string, init: RequestInit) => {
      seen.push({ method: String(init.method), url });
      if (url.endsWith("/r2/buckets") && JSON.parse(String(init.body)).name === "campaign-public") {
        return new Response(JSON.stringify({ success: false, errors: [{ code: 10004, message: "bucket already exists" }], result: null }), { status: 409 });
      }
      return new Response(JSON.stringify({ success: true, errors: [], result: { id: "real-id", uuid: "real-uuid", name: "campaign.example.invalid" } }), { status: 200 });
    }) as unknown as typeof fetch;
    let client = new CloudflareClient("t", { dryRun: false, fetcher, base: "https://api.example.invalid/v4" });
    let r = await applyPlan(plan, { client, bundles: BUNDLES });
    expect(r.status).toBe("failed");
    expect(r.failed_step).toBe("r2.public");
    expect(r.error).toContain("bucket already exists");
    expect(r.receipts.map((x) => x.step_id)).toEqual(["token.verify", "zone.verify", "d1.create", "r2.originals", "r2.public"]);
    expect(r.rollback.map((x) => x.step_id)).toEqual(["r2.originals", "d1.create"]);
    expect(r.rollback[1].path).toBe(`/accounts/${"a".repeat(32)}/d1/database/real-uuid`);
    expect(seen.every((x) => x.url.startsWith("https://api.example.invalid/v4/"))).toBe(true);
    // rollbackOnFailure performs the DELETE calls in newest-first order
    seen.length = 0;
    client = new CloudflareClient("t", { dryRun: false, fetcher, base: "https://api.example.invalid/v4" });
    r = await applyPlan(plan, { client, bundles: BUNDLES, rollbackOnFailure: true });
    expect(r.status).toBe("rolled_back");
    const deletes = seen.filter((x) => x.method === "DELETE").map((x) => x.url);
    expect(deletes).toEqual([`https://api.example.invalid/v4/accounts/${"a".repeat(32)}/r2/buckets/campaign-originals`, `https://api.example.invalid/v4/accounts/${"a".repeat(32)}/d1/database/real-uuid`]);
  });

  it("worker upload sends multipart metadata + module and captures the Access aud for the secret step", async () => {
    const s = fullState();
    const plan = buildPlan(s);
    const uploads: { path: string; metadata: unknown; moduleName: string }[] = [];
    const secrets: { name: string; text: string }[] = [];
    const fetcher = (async (url: string, init: RequestInit) => {
      const path = new URL(url).pathname;
      if (init.body instanceof FormData) {
        const meta = JSON.parse(await (init.body.get("metadata") as File).text());
        uploads.push({ path, metadata: meta, moduleName: [...init.body.keys()].filter((k) => k !== "metadata")[0] });
      } else if (path.endsWith("/secrets")) {
        secrets.push(JSON.parse(String(init.body)));
      }
      const result = path.endsWith("/access/apps") ? { id: "app-1", aud: "aud-from-access" } : { id: "id", uuid: "uuid", name: "campaign.example.invalid" };
      return new Response(JSON.stringify({ success: true, errors: [], result }), { status: 200 });
    }) as unknown as typeof fetch;
    const client = new CloudflareClient("t", { dryRun: false, fetcher });
    const r = await applyPlan(plan, { client, bundles: BUNDLES, generated: { runner_token: "rt_fixed", download_key: "dk_fixed" } });
    expect(r.status).toBe("applied");
    expect(uploads.length).toBe(2);
    const ws = uploads.find((u) => u.path.endsWith("-workspace"))!;
    expect(ws.moduleName).toBe("index.mjs");
    expect((ws.metadata as { bindings: { type: string; id?: string }[] }).bindings.find((b) => b.type === "d1")!.id).toBe("uuid");
    expect(secrets.find((x) => x.name === "ACCESS_AUD")!.text).toBe("aud-from-access");
    expect(secrets.find((x) => x.name === "RUNNER_TOKEN")!.text).toBe("rt_fixed");
    expect(secrets.find((x) => x.name === "CAMPAIGN_ID")!.text).toBe(s.campaign_id);
    expect(JSON.stringify(r.receipts)).not.toContain("rt_fixed");
  });
});
