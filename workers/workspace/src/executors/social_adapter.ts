// One social platform (Mastodon) behind one adapter interface, for the post_social executor.
// The adapter can only CREATE one status: there is no edit, delete, boost, reply, schedule or
// batch call here, so nothing it does can be quietly undone or repeated. The access token is
// read from the Worker secret channel at use and never appears in a result, error or receipt.
import type { Env, ProviderFetch } from "../env.ts";

export type SocialPlatform = "mastodon";

/** Conclusive: the platform answered and did NOT create a post (safe to re-approve). */
export class SocialRejected extends Error {}
/** Unknown: the post may exist (timeout, network, 5xx, unreadable reply). Never retried. */
export class SocialUncertain extends Error {}

export interface SocialPostResult {
  post_id: string;
}

export interface SocialAdapter {
  readonly platform: SocialPlatform;
  readonly maxChars: number;
  post(text: string, idempotencyKey: string): Promise<SocialPostResult>;
}

/** Readiness text for the one optional setting; never echoes the URL or token. */
export function socialReadiness(env: Pick<Env, "MASTODON_URL" | "MASTODON_TOKEN"> | undefined): string {
  return env?.MASTODON_URL && env.MASTODON_TOKEN ? "social_posting: configured (mastodon)" : "social_posting: not configured";
}

/** The configured adapter, or null (the executor then refuses social_not_configured). */
export function socialAdapterFor(env: Pick<Env, "MASTODON_URL" | "MASTODON_TOKEN">, fetchImpl: ProviderFetch = fetch): SocialAdapter | null {
  if (!env.MASTODON_URL || !env.MASTODON_TOKEN) return null;
  let base: URL;
  try {
    base = new URL(env.MASTODON_URL);
  } catch {
    return null;
  }
  if (base.protocol !== "https:" || base.username || base.password) return null;
  const token = env.MASTODON_TOKEN;
  const endpoint = new URL("/api/v1/statuses", base.origin).toString();
  return {
    platform: "mastodon",
    maxChars: 500,
    async post(text, idempotencyKey) {
      let res: Response;
      try {
        res = await fetchImpl(endpoint, {
          method: "POST",
          headers: { authorization: "Bearer " + token, "content-type": "application/json", "idempotency-key": idempotencyKey },
          body: JSON.stringify({ status: text, visibility: "public" }),
          signal: AbortSignal.timeout(15_000),
        });
      } catch {
        throw new SocialUncertain("no reply from the platform (timeout or network error)");
      }
      if (res.status >= 500) throw new SocialUncertain(`platform answered ${res.status}`);
      if (res.status < 200 || res.status >= 300) throw new SocialRejected(`platform refused with ${res.status}`);
      let id: unknown;
      try {
        id = ((await res.json()) as { id?: unknown }).id;
      } catch {
        id = null;
      }
      if (typeof id !== "string" || !/^[A-Za-z0-9_-]{1,64}$/.test(id)) throw new SocialUncertain("platform accepted the request but returned no post id");
      return { post_id: id };
    },
  };
}

// Pre-post privacy lint. Same three pattern families (and names) as tools/flock_privacy.py
// PATTERNS; the Worker cannot import Python, so these mirror it. Any hit refuses the post.
const AT = String.raw`\s*(?:\[\s*at\s*\]|\(\s*at\s*\)|\{\s*at\s*\})\s*`;
const BDOT = String.raw`\s*(?:\[\s*dot\s*\]|\(\s*dot\s*\)|\{\s*dot\s*\})\s*`;
const PRIVACY: ReadonlyArray<[string, RegExp]> = [
  ["email", new RegExp(String.raw`(?<![^\s@])[^\s@]+@[^\s@]+\.[a-z]{2,}|[a-z0-9._%+-]+(?:${AT}|@)[a-z0-9-]+(?:(?:${BDOT}|\s*\.\s*)[a-z0-9-]+)*(?:${BDOT})[a-z]{2,}|[a-z0-9._%+-]+${AT}[a-z0-9-]+(?:\s*\.\s*[a-z0-9-]+)*\s*\.\s*[a-z]{2,}`, "i")],
  ["phone", /(?<!\d)(?:\+?1[ .-]?)?(?:\(\d{3}\)\s?|\d{3}[ .-])\d{3}[ .-]\d{4}(?!\d)/],
  ["plate", /\b(?:\d[A-Z]{3}[ -]?\d{3}|\d[A-Z]\d{5})\b/],
];

/** Pattern names (never the matched text) found in the exact bytes about to be posted. */
export function privacyHits(text: string): string[] {
  return PRIVACY.filter(([, rx]) => rx.test(text)).map(([name]) => name);
}
