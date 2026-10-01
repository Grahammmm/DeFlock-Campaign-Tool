// Cloudflare Access JWT validation. Every workspace request must carry a JWT signed by the
// team's Access certificates (RS256). The Cf-Access-Authenticated-User-Email header alone is
// never trusted: the identity comes from the verified token's `email` claim.
import { timingSafeEqual } from "@deflock/shared/ids";
import type { Env } from "./env.ts";

export interface AccessIdentity {
  email: string;
  sub: string;
  issued_at: number;
  expires_at: number;
}

export class AuthError extends Error {
  constructor(
    message: string,
    public readonly status: number = 401,
  ) {
    super(message);
  }
}

interface Jwk {
  kid: string;
  kty: string;
  alg?: string;
  n: string;
  e: string;
}

interface CertCacheEntry {
  fetchedAt: number;
  keys: Map<string, CryptoKey>;
}

const CERT_TTL_MS = 60 * 60 * 1000;
const certCache = new Map<string, CertCacheEntry>();

export function accessIssuer(teamDomain: string): string {
  return "https://" + teamDomain + ".cloudflareaccess.com";
}

export function certsUrl(teamDomain: string): string {
  return accessIssuer(teamDomain) + "/cdn-cgi/access/certs";
}

function b64urlToBytes(s: string): Uint8Array {
  const pad = s.length % 4 === 0 ? "" : "=".repeat(4 - (s.length % 4));
  const bin = atob(s.replace(/-/g, "+").replace(/_/g, "/") + pad);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

function decodeJson(segment: string): Record<string, unknown> {
  try {
    return JSON.parse(new TextDecoder().decode(b64urlToBytes(segment))) as Record<string, unknown>;
  } catch {
    throw new AuthError("malformed token");
  }
}

async function importJwk(jwk: Jwk): Promise<CryptoKey> {
  return crypto.subtle.importKey(
    "jwk",
    { kty: "RSA", n: jwk.n, e: jwk.e, alg: "RS256", ext: true },
    { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" },
    false,
    ["verify"],
  );
}

export interface AuthOptions {
  fetcher?: typeof fetch;
  now?: () => number;
  /** Clear the process-level certificate cache (tests). */
  resetCache?: boolean;
}

async function loadKeys(env: Env, opts: AuthOptions, force: boolean): Promise<Map<string, CryptoKey>> {
  const team = env.ACCESS_TEAM_DOMAIN;
  const now = (opts.now ?? Date.now)();
  const cached = certCache.get(team);
  if (!force && cached && now - cached.fetchedAt < CERT_TTL_MS) return cached.keys;
  let payload: { keys?: Jwk[] };
  if (env.ACCESS_CERTS_JSON) {
    // Inline JWKS for tests. Whoever can set this var can equally set ACCESS_TEAM_DOMAIN
    // and ACCESS_AUD to a team they control, so it grants nothing that var access does not
    // already grant; keep all three out of reach (wrangler vars are deploy-time only).
    payload = JSON.parse(env.ACCESS_CERTS_JSON) as { keys?: Jwk[] };
  } else {
    const res = await (opts.fetcher ?? fetch)(certsUrl(team), { headers: { accept: "application/json" } });
    if (!res.ok) throw new AuthError("access certificates unavailable", 503);
    payload = (await res.json()) as { keys?: Jwk[] };
  }
  const keys = new Map<string, CryptoKey>();
  for (const jwk of payload.keys ?? []) {
    if (jwk.kty !== "RSA" || !jwk.kid) continue;
    keys.set(jwk.kid, await importJwk(jwk));
  }
  certCache.set(team, { fetchedAt: now, keys });
  return keys;
}

export function tokenFromRequest(request: Request): string | null {
  const header = request.headers.get("cf-access-jwt-assertion");
  if (header) return header.trim();
  const cookie = request.headers.get("cookie") ?? "";
  const m = /(?:^|;\s*)CF_Authorization=([^;]+)/.exec(cookie);
  return m ? m[1].trim() : null;
}

/** Verify an Access JWT: RS256 signature against the team certs, iss, aud, exp, nbf. */
export async function verifyAccessJwt(token: string, env: Env, opts: AuthOptions = {}): Promise<AccessIdentity> {
  if (opts.resetCache) certCache.clear();
  if (!env.ACCESS_TEAM_DOMAIN || !env.ACCESS_AUD) throw new AuthError("access not configured", 503);
  const parts = token.split(".");
  if (parts.length !== 3) throw new AuthError("malformed token");
  const header = decodeJson(parts[0]);
  const claims = decodeJson(parts[1]);
  if (header.alg !== "RS256" || typeof header.kid !== "string") throw new AuthError("unsupported token");
  let keys = await loadKeys(env, opts, false);
  let key = keys.get(header.kid);
  if (!key) {
    keys = await loadKeys(env, opts, true); // one bounded refresh on key rotation
    key = keys.get(header.kid);
  }
  if (!key) throw new AuthError("unknown signing key");
  const data = new TextEncoder().encode(parts[0] + "." + parts[1]);
  const ok = await crypto.subtle.verify("RSASSA-PKCS1-v1_5", key, b64urlToBytes(parts[2]) as BufferSource, data);
  if (!ok) throw new AuthError("invalid signature");
  const nowSec = Math.floor((opts.now ?? Date.now)() / 1000);
  if (claims.iss !== accessIssuer(env.ACCESS_TEAM_DOMAIN)) throw new AuthError("wrong issuer");
  const aud = claims.aud;
  const audOk = Array.isArray(aud) ? aud.some((a) => typeof a === "string" && timingSafeEqual(a, env.ACCESS_AUD)) : typeof aud === "string" && timingSafeEqual(aud, env.ACCESS_AUD);
  if (!audOk) throw new AuthError("wrong audience");
  if (typeof claims.exp !== "number" || claims.exp <= nowSec) throw new AuthError("token expired");
  if (typeof claims.nbf === "number" && claims.nbf > nowSec + 60) throw new AuthError("token not yet valid");
  if (typeof claims.email !== "string" || !claims.email) throw new AuthError("token has no email");
  return {
    email: claims.email.toLowerCase(),
    sub: typeof claims.sub === "string" ? claims.sub : "",
    issued_at: typeof claims.iat === "number" ? claims.iat : 0,
    expires_at: claims.exp,
  };
}

export async function requireIdentity(request: Request, env: Env, opts: AuthOptions = {}): Promise<AccessIdentity> {
  const token = tokenFromRequest(request);
  if (!token) throw new AuthError("missing access token");
  return verifyAccessJwt(token, env, opts);
}

/** Runner bearer auth: constant-time comparison against RUNNER_TOKEN. */
export function requireRunner(request: Request, env: Env): void {
  const auth = request.headers.get("authorization") ?? "";
  const m = /^Bearer\s+(.+)$/i.exec(auth);
  if (!env.RUNNER_TOKEN || env.RUNNER_TOKEN.length < 16) throw new AuthError("runner token not configured", 503);
  if (!m || !timingSafeEqual(m[1].trim(), env.RUNNER_TOKEN)) throw new AuthError("invalid runner token");
}
