import { env } from "cloudflare:test";
import { describe, expect, it } from "vitest";
import { SESSION_TTL_SECONDS, SessionStore, decryptBlob, encryptBlob, sessionIdFromCookie } from "../src/session.ts";

describe("session blob", () => {
  it("encrypts and decrypts with AES-GCM; ciphertext is not plaintext", async () => {
    const blob = await encryptBlob(env.SESSION_KEY, { token: "cf-secret-value" });
    expect(blob).not.toContain("cf-secret-value");
    expect(await decryptBlob(env.SESSION_KEY, blob)).toEqual({ token: "cf-secret-value" });
  });

  it("rejects tampering and the wrong key", async () => {
    const blob = await encryptBlob(env.SESSION_KEY, { a: 1 });
    const [iv, ct] = blob.split(".");
    const flipped = iv + "." + (ct[0] === "0" ? "1" : "0") + ct.slice(1);
    await expect(decryptBlob(env.SESSION_KEY, flipped)).rejects.toThrow(/cannot be decrypted/);
    await expect(decryptBlob("22".repeat(32), blob)).rejects.toThrow(/cannot be decrypted/);
    await expect(encryptBlob("short", {})).rejects.toThrow(/64 hex/);
  });

  it("expires one hour after creation and the TTL is never extended by saves", async () => {
    let now = Date.parse("2026-09-30T12:00:00Z");
    const store = new SessionStore<{ n: number }>(env.SESSIONS, env.SESSION_KEY, () => now);
    const id = store.newId();
    const created = (await store.save(id, { n: 1 })).created_at;
    now += 30 * 60 * 1000;
    expect((await store.load(id))?.state.n).toBe(1);
    await store.save(id, { n: 2 }, created);
    now += 29 * 60 * 1000;
    expect((await store.load(id))?.state.n).toBe(2);
    now += 2 * 60 * 1000; // 61 minutes after creation
    expect(await store.load(id)).toBeNull();
    expect(await env.SESSIONS.get("session:" + id)).toBeNull();
    expect(SESSION_TTL_SECONDS).toBe(3600);
  });

  it("cookie parsing accepts only a 32-hex id", () => {
    expect(sessionIdFromCookie("deflock_wizard=" + "ab".repeat(16))).toBe("ab".repeat(16));
    expect(sessionIdFromCookie("other=1; deflock_wizard=zz")).toBeNull();
    expect(sessionIdFromCookie(null)).toBeNull();
  });
});
