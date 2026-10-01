// Wizard session: an AES-GCM encrypted blob in KV with a one-hour TTL. The cookie holds only
// a random session id. Account tokens live in this blob until handoff and nowhere else.
import { bytesToHex, hexToBytes, randomHex } from "@deflock/shared/ids";

export const SESSION_TTL_SECONDS = 60 * 60;
export const COOKIE_NAME = "deflock_wizard";

export interface Envelope<T> {
  created_at: string;
  expires_at: string;
  state: T;
}

export class SessionError extends Error {}

async function aesKey(hexKey: string): Promise<CryptoKey> {
  if (!/^[0-9a-f]{64}$/i.test(hexKey)) throw new SessionError("SESSION_KEY must be 64 hex characters");
  return crypto.subtle.importKey("raw", hexToBytes(hexKey) as BufferSource, { name: "AES-GCM" }, false, ["encrypt", "decrypt"]);
}

export async function encryptBlob(hexKey: string, value: unknown): Promise<string> {
  const key = await aesKey(hexKey);
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const plaintext = new TextEncoder().encode(JSON.stringify(value));
  const ct = new Uint8Array(await crypto.subtle.encrypt({ name: "AES-GCM", iv }, key, plaintext));
  return bytesToHex(iv) + "." + bytesToHex(ct);
}

export async function decryptBlob<T>(hexKey: string, blob: string): Promise<T> {
  const key = await aesKey(hexKey);
  const [ivHex, ctHex] = blob.split(".");
  if (!ivHex || !ctHex) throw new SessionError("malformed session blob");
  try {
    const pt = await crypto.subtle.decrypt({ name: "AES-GCM", iv: hexToBytes(ivHex) as BufferSource }, key, hexToBytes(ctHex) as BufferSource);
    return JSON.parse(new TextDecoder().decode(pt)) as T;
  } catch {
    throw new SessionError("session blob cannot be decrypted");
  }
}

export function sessionIdFromCookie(cookie: string | null): string | null {
  const m = new RegExp("(?:^|;\\s*)" + COOKIE_NAME + "=([0-9a-f]{32})").exec(cookie ?? "");
  return m ? m[1] : null;
}

export class SessionStore<T> {
  constructor(
    private readonly kv: KVNamespace,
    private readonly hexKey: string,
    private readonly now: () => number = Date.now,
  ) {}

  newId(): string {
    return randomHex(16);
  }

  async load(id: string): Promise<Envelope<T> | null> {
    const blob = await this.kv.get("session:" + id);
    if (!blob) return null;
    const env = await decryptBlob<Envelope<T>>(this.hexKey, blob);
    if (Date.parse(env.expires_at) <= this.now()) {
      await this.kv.delete("session:" + id);
      return null;
    }
    return env;
  }

  /** Save; the TTL is measured from the session's creation, never extended. */
  async save(id: string, state: T, createdAt?: string): Promise<Envelope<T>> {
    const created = createdAt ?? new Date(this.now()).toISOString();
    const expires = new Date(Date.parse(created) + SESSION_TTL_SECONDS * 1000);
    const env: Envelope<T> = { created_at: created, expires_at: expires.toISOString(), state };
    const ttl = Math.max(60, Math.floor((expires.getTime() - this.now()) / 1000));
    await this.kv.put("session:" + id, await encryptBlob(this.hexKey, env), { expirationTtl: ttl });
    return env;
  }

  async destroy(id: string): Promise<void> {
    await this.kv.delete("session:" + id);
  }

  cookie(id: string): string {
    return `${COOKIE_NAME}=${id}; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=${SESSION_TTL_SECONDS}`;
  }

  clearCookie(): string {
    return `${COOKIE_NAME}=; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=0`;
  }
}
