// ADM-DLKEY-1 (gap B4): /dl and POST /records/:sha/link refuse with 503 download_not_configured
// unless DOWNLOAD_SIGNING_KEY is at least 32 bytes and ORIGINALS is bound. Synthetic data only.
import { describe, expect, it } from "vitest";
import { env } from "cloudflare:test";
import { signDownload } from "../src/signing.ts";
import type { Env } from "../src/env.ts";
import { call, makeSigner, organizerRequest, seedCampaign } from "./helpers.ts";

const SHA = "a".repeat(64);
const KEY32 = "k".repeat(32);

async function dl(overrides: Partial<Env>, key: string): Promise<Response> {
  const exp = Math.floor(Date.now() / 1000) + 600;
  const sig = await signDownload(key || "x", SHA, exp);
  return call(new Request(`https://workspace.example.invalid/dl/${SHA}?exp=${exp}&sig=${sig}`), overrides);
}

async function mint(overrides: Partial<Env>): Promise<Response> {
  await seedCampaign();
  const signer = await makeSigner();
  const r = await organizerRequest(signer, "organizer@example.invalid", `/records/${SHA}/link`, { method: "POST", headers: { accept: "application/json" } });
  return call(r.request, { ...r.overrides, ...overrides });
}

const BAD: Array<[string, Partial<Env>]> = [
  ["missing key", { DOWNLOAD_SIGNING_KEY: undefined } as unknown as Partial<Env>],
  ["empty key", { DOWNLOAD_SIGNING_KEY: "" }],
  ["31-byte key", { DOWNLOAD_SIGNING_KEY: "k".repeat(31) }],
  ["unbound ORIGINALS", { DOWNLOAD_SIGNING_KEY: KEY32, ORIGINALS: undefined } as unknown as Partial<Env>],
];

describe("download guard", () => {
  for (const [name, overrides] of BAD) {
    it(`/dl answers 503 download_not_configured: ${name}`, async () => {
      const res = await dl(overrides, KEY32);
      expect(res.status).toBe(503);
      expect(await res.json()).toEqual({ error: "download_not_configured" });
    });
    it(`POST /records/:sha/link answers 503 download_not_configured: ${name}`, async () => {
      const res = await mint(overrides);
      expect(res.status).toBe(503);
      expect(await res.json()).toEqual({ error: "download_not_configured" });
    });
  }

  it("a 32-byte key with ORIGINALS bound passes the guard (404 for an unknown record, 404 for a valid link to nothing)", async () => {
    const good: Partial<Env> = { DOWNLOAD_SIGNING_KEY: KEY32 };
    expect((await mint(good)).status).toBe(404);
    expect((await dl(good, KEY32)).status).toBe(404);
  });

  it("the test environment key is itself at least 32 bytes", () => {
    expect(new TextEncoder().encode(env.DOWNLOAD_SIGNING_KEY).length).toBeGreaterThanOrEqual(32);
  });
});
