import { createExecutionContext, env, waitOnExecutionContext } from "cloudflare:test";
import worker from "../src/index.ts";
import { Repo } from "../src/db.ts";
import type { Env } from "../src/env.ts";

export const CAMPAIGN_ID = env.CAMPAIGN_ID;

export function repo(): Repo {
  return new Repo(env.DB, CAMPAIGN_ID);
}

export async function seedCampaign(): Promise<Repo> {
  const r = repo();
  if (!(await r.campaign())) {
    await r.createCampaign({
      name: "Example County ALPR Records",
      jurisdiction: "us-ca",
      county_fips: "06079",
      county_name: "San Luis Obispo",
      place_fips: null,
      place_name: null,
      public_hostname: "campaign.example.invalid",
      workspace_hostname: "workspace.example.invalid",
      privacy_tier: "redacted_cloud",
      law_package_status: "draft",
      external_sends: "approval_required",
      schedule_cron: "0 7,13 * * *;30 20 * * *",
      timezone: "America/Los_Angeles",
    });
    await r.createAgency({
      agency_id: "ca-example-police",
      name: "Example Police Department",
      kind: "police",
      jurisdiction_name: "City of Example",
      records_url: null,
      records_email: "records@example.invalid",
      portal_vendor: "unknown",
      portal_url: null,
      flock_transparency_slug: null,
      muckrock_agency_id: null,
      selected: 1,
      verified: 0,
      sources_json: "[]",
    });
  }
  return r;
}

export async function call(request: Request, overrides: Partial<Env> = {}): Promise<Response> {
  const ctx = createExecutionContext();
  const res = await worker.fetch(request, { ...env, ...overrides } as Env, ctx);
  await waitOnExecutionContext(ctx);
  return res;
}

export function runnerHeaders(extra: Record<string, string> = {}): Record<string, string> {
  return { authorization: "Bearer " + env.RUNNER_TOKEN, "content-type": "application/json", ...extra };
}

// --- Access JWT test material -------------------------------------------------------
function b64url(bytes: ArrayBuffer | Uint8Array | string): string {
  const arr = typeof bytes === "string" ? new TextEncoder().encode(bytes) : new Uint8Array(bytes);
  let bin = "";
  for (const b of arr) bin += String.fromCharCode(b);
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export interface TestSigner {
  kid: string;
  certsJson: string;
  sign(claims: Record<string, unknown>, headerOverride?: Record<string, unknown>): Promise<string>;
}

export async function makeSigner(kid = "kid-" + crypto.randomUUID()): Promise<TestSigner> {
  const pair = await crypto.subtle.generateKey({ name: "RSASSA-PKCS1-v1_5", modulusLength: 2048, publicExponent: new Uint8Array([1, 0, 1]), hash: "SHA-256" }, true, ["sign", "verify"]);
  const jwk = (await crypto.subtle.exportKey("jwk", pair.publicKey)) as JsonWebKey;
  const certsJson = JSON.stringify({ keys: [{ kid, kty: "RSA", alg: "RS256", use: "sig", n: jwk.n, e: jwk.e }] });
  return {
    kid,
    certsJson,
    async sign(claims, headerOverride = {}) {
      const header = b64url(JSON.stringify({ alg: "RS256", kid, typ: "JWT", ...headerOverride }));
      const payload = b64url(JSON.stringify(claims));
      const sig = await crypto.subtle.sign("RSASSA-PKCS1-v1_5", pair.privateKey, new TextEncoder().encode(header + "." + payload));
      return header + "." + payload + "." + b64url(sig);
    },
  };
}

export function claimsFor(email: string, extra: Record<string, unknown> = {}): Record<string, unknown> {
  const now = Math.floor(Date.now() / 1000);
  return { iss: "https://" + env.ACCESS_TEAM_DOMAIN + ".cloudflareaccess.com", aud: [env.ACCESS_AUD], email, sub: "sub-" + email, iat: now - 10, nbf: now - 10, exp: now + 600, ...extra };
}

/** Build an authenticated organizer request: verified JWT header plus certs in env. */
export async function organizerRequest(signer: TestSigner, email: string, path: string, init: RequestInit = {}): Promise<{ request: Request; overrides: Partial<Env> }> {
  const token = await signer.sign(claimsFor(email));
  const headers = new Headers(init.headers);
  headers.set("cf-access-jwt-assertion", token);
  return { request: new Request("https://workspace.example.invalid" + path, { ...init, headers }), overrides: { ACCESS_CERTS_JSON: signer.certsJson } };
}
