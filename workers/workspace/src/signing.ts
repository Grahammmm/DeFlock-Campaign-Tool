// Short-lived HMAC-signed download links for originals, served by this Worker (never a
// bucket-level public URL). Links are never stored.
import { bytesToHex, timingSafeEqual } from "@deflock/shared/ids";

async function hmacKey(secret: string): Promise<CryptoKey> {
  return crypto.subtle.importKey("raw", new TextEncoder().encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
}

export async function signDownload(secret: string, sha256: string, expiresAtSec: number): Promise<string> {
  const key = await hmacKey(secret);
  const sig = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(`${sha256}:${expiresAtSec}`));
  return bytesToHex(new Uint8Array(sig));
}

export async function verifyDownload(secret: string, sha256: string, expiresAtSec: number, sig: string, nowSec = Math.floor(Date.now() / 1000)): Promise<boolean> {
  if (!Number.isFinite(expiresAtSec) || expiresAtSec < nowSec) return false;
  const expected = await signDownload(secret, sha256, expiresAtSec);
  return timingSafeEqual(expected, sig);
}
