// Identity minting per docs/CONTRACTS.md. Every identifier is stable text.

const ULID_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"; // Crockford base32, lowercased

/** 26-char lowercase ULID-like string: 10 chars of time, 16 chars of randomness. */
export function ulid(now: number = Date.now()): string {
  let time = now;
  let out = "";
  for (let i = 0; i < 10; i++) {
    out = ULID_ALPHABET[time % 32] + out;
    time = Math.floor(time / 32);
  }
  const rand = new Uint8Array(16);
  crypto.getRandomValues(rand);
  for (let i = 0; i < 16; i++) out += ULID_ALPHABET[rand[i] % 32];
  return out;
}

export function randomHex(bytes: number): string {
  const buf = new Uint8Array(bytes);
  crypto.getRandomValues(buf);
  return bytesToHex(buf);
}

export function bytesToHex(bytes: ArrayLike<number>): string {
  let out = "";
  for (let i = 0; i < bytes.length; i++) out += bytes[i].toString(16).padStart(2, "0");
  return out;
}

export function hexToBytes(hex: string): Uint8Array {
  const out = new Uint8Array(hex.length / 2);
  for (let i = 0; i < out.length; i++) out[i] = parseInt(hex.slice(i * 2, i * 2 + 2), 16);
  return out;
}

/** `<prefix>_` + 16 hex, e.g. req_, cor_, fnd_, job_, act_. */
export function prefixedId(prefix: string): string {
  return prefix + "_" + randomHex(8);
}

export const newRequestId = () => prefixedId("req");
export const newCorrespondenceId = () => prefixedId("cor");
export const newFindingId = () => prefixedId("fnd");
export const newJobId = () => prefixedId("job");
export const newActionId = () => prefixedId("act");
export const newReviewId = () => prefixedId("rev");
export const newIncidentId = () => prefixedId("inc");
export const newRunId = () => prefixedId("run");
export const newExtractionId = () => prefixedId("ext");
export const newDigestId = () => prefixedId("dig");
export const newPublicationId = () => prefixedId("pub");
export const newCorrectionId = () => prefixedId("crx");
export const newEventId = () => prefixedId("evt");
export const newMeetingId = () => prefixedId("mtg");

export async function sha256Hex(data: string | ArrayBuffer | Uint8Array): Promise<string> {
  const bytes = typeof data === "string" ? new TextEncoder().encode(data) : data;
  const digest = await crypto.subtle.digest("SHA-256", bytes as BufferSource);
  return bytesToHex(new Uint8Array(digest));
}

/** receipt_id = sha256(JSON [source_id, object_sha256]) exactly as cli.ingest. */
export function receiptId(sourceId: string, objectSha256: string): Promise<string> {
  return sha256Hex(JSON.stringify([sourceId, objectSha256]));
}

export const HEX64 = /^[0-9a-f]{64}$/;

export function nowIso(): string {
  return new Date().toISOString();
}

export function isValidUlid(value: string): boolean {
  return /^[0-9a-z]{26}$/.test(value);
}

/** Constant-time string equality for bearer tokens. */
export function timingSafeEqual(a: string, b: string): boolean {
  const ea = new TextEncoder().encode(a);
  const eb = new TextEncoder().encode(b);
  let diff = ea.length ^ eb.length;
  const n = Math.max(ea.length, eb.length);
  for (let i = 0; i < n; i++) diff |= (ea[i % (ea.length || 1)] ?? 0) ^ (eb[i % (eb.length || 1)] ?? 0);
  return diff === 0;
}
