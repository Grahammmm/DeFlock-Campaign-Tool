import { env } from "cloudflare:test";
import { describe, expect, it } from "vitest";
import { verifyAccessJwt, requireRunner, AuthError } from "../src/auth.ts";
import type { Env } from "../src/env.ts";
import { call, claimsFor, makeSigner, seedCampaign } from "./helpers.ts";

describe("Access JWT validation", () => {
  it("accepts a valid RS256 token and extracts the email", async () => {
    const signer = await makeSigner();
    const token = await signer.sign(claimsFor("Organizer@Example.invalid"));
    const id = await verifyAccessJwt(token, { ...env, ACCESS_CERTS_JSON: signer.certsJson } as Env, { resetCache: true });
    expect(id.email).toBe("organizer@example.invalid");
    expect(id.sub).toBe("sub-Organizer@Example.invalid");
  });

  it("rejects a token signed by another key, even with the same kid", async () => {
    const a = await makeSigner("k1");
    const b = await makeSigner("k1");
    const token = await b.sign(claimsFor("x@example.invalid"));
    await expect(verifyAccessJwt(token, { ...env, ACCESS_CERTS_JSON: a.certsJson } as Env, { resetCache: true })).rejects.toThrow(/invalid signature/);
  });

  it("rejects expired, wrong-audience, wrong-issuer and alg=none tokens", async () => {
    const s = await makeSigner();
    const e = { ...env, ACCESS_CERTS_JSON: s.certsJson } as Env;
    const now = Math.floor(Date.now() / 1000);
    await expect(verifyAccessJwt(await s.sign(claimsFor("x@example.invalid", { exp: now - 1 })), e, { resetCache: true })).rejects.toThrow(/expired/);
    await expect(verifyAccessJwt(await s.sign(claimsFor("x@example.invalid", { aud: ["other"] })), e)).rejects.toThrow(/audience/);
    await expect(verifyAccessJwt(await s.sign(claimsFor("x@example.invalid", { iss: "https://evil.example.invalid" })), e)).rejects.toThrow(/issuer/);
    await expect(verifyAccessJwt(await s.sign(claimsFor("x@example.invalid"), { alg: "none" }), e)).rejects.toThrow(/unsupported/);
    await expect(verifyAccessJwt("not.a.jwt", e)).rejects.toThrow(AuthError);
  });

  it("uses the certificate fetcher and caches keys; unknown kid triggers one refresh", async () => {
    const s = await makeSigner("rotated");
    let fetches = 0;
    const fetcher = (async () => {
      fetches += 1;
      return new Response(s.certsJson, { headers: { "content-type": "application/json" } });
    }) as unknown as typeof fetch;
    const e = { ...env, ACCESS_CERTS_JSON: undefined } as Env;
    const token = await s.sign(claimsFor("x@example.invalid"));
    await verifyAccessJwt(token, e, { fetcher, resetCache: true });
    await verifyAccessJwt(token, e, { fetcher });
    expect(fetches).toBe(1);
    const other = await makeSigner("unknown-kid");
    await expect(verifyAccessJwt(await other.sign(claimsFor("x@example.invalid")), e, { fetcher })).rejects.toThrow(/unknown signing key/);
    expect(fetches).toBe(2);
  });

  it("the workspace returns 401 without a token and never trusts the email header alone", async () => {
    await seedCampaign();
    const s = await makeSigner();
    const anon = await call(new Request("https://workspace.example.invalid/"), { ACCESS_CERTS_JSON: s.certsJson });
    expect(anon.status).toBe(401);
    const spoof = await call(new Request("https://workspace.example.invalid/api/me", { headers: { "cf-access-authenticated-user-email": "admin@example.invalid" } }), { ACCESS_CERTS_JSON: s.certsJson });
    expect(spoof.status).toBe(401);
    const good = await call(new Request("https://workspace.example.invalid/api/me", { headers: { "cf-access-jwt-assertion": await s.sign(claimsFor("org@example.invalid")) } }), { ACCESS_CERTS_JSON: s.certsJson });
    expect(good.status).toBe(200);
    expect(((await good.json()) as { email: string }).email).toBe("org@example.invalid");
    const cookie = await call(new Request("https://workspace.example.invalid/api/me", { headers: { cookie: "CF_Authorization=" + (await s.sign(claimsFor("cookie@example.invalid"))) } }), { ACCESS_CERTS_JSON: s.certsJson });
    expect(cookie.status).toBe(200);
  });

  it("runner bearer token: constant-time compare, wrong token rejected", () => {
    const ok = new Request("https://x.invalid/", { headers: { authorization: "Bearer " + env.RUNNER_TOKEN } });
    expect(() => requireRunner(ok, env as Env)).not.toThrow();
    const bad = new Request("https://x.invalid/", { headers: { authorization: "Bearer nope" } });
    expect(() => requireRunner(bad, env as Env)).toThrow(/invalid runner token/);
    expect(() => requireRunner(new Request("https://x.invalid/"), env as Env)).toThrow(AuthError);
    expect(() => requireRunner(ok, { ...env, RUNNER_TOKEN: "short" } as Env)).toThrow(/not configured/);
  });
});
