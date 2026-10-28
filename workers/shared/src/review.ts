// Port of campaign_tool/review.py. Content-binding checks only: NOT authenticated
// authorization or legal validation. Semantics follow Python truthiness and equality on
// JSON values so tests/fixtures/review-cases.json passes in both languages.

export const ROLES = ["factual", "legal", "privacy"] as const;
export const CLASSIFICATIONS = new Set([
  "documented_fact",
  "apparent_conflict",
  "confirmed_conflict",
  "information_gap",
  "redaction",
  "agency_assertion",
  "no_conflict",
]);

export type Json = null | boolean | number | string | Json[] | { [key: string]: Json };
export type JsonObject = { [key: string]: Json };

/** Python truthiness for JSON values: None, False, 0, "", [], {} are falsy. */
export function truthy(value: Json | undefined): boolean {
  if (value === null || value === undefined || value === false || value === 0 || value === "") return false;
  if (Array.isArray(value)) return value.length > 0;
  if (typeof value === "object") return Object.keys(value).length > 0;
  return true;
}

/** Python `==` for JSON values (1 == 1.0 == True; deep for lists and dicts). */
export function pyEqual(a: Json | undefined, b: Json | undefined): boolean {
  if (a === undefined || b === undefined) return a === b;
  const na = typeof a === "boolean" ? Number(a) : a;
  const nb = typeof b === "boolean" ? Number(b) : b;
  if (typeof na === "number" && typeof nb === "number") return na === nb;
  if (na === null || nb === null) return na === nb;
  if (typeof na !== typeof nb) return false;
  if (typeof na !== "object") return na === nb;
  if (Array.isArray(na) || Array.isArray(nb)) {
    if (!Array.isArray(na) || !Array.isArray(nb) || na.length !== nb.length) return false;
    return na.every((v, i) => pyEqual(v, nb[i]));
  }
  const ka = Object.keys(na);
  const kb = Object.keys(nb as JsonObject);
  if (ka.length !== kb.length) return false;
  return ka.every((k) => Object.hasOwn(nb as JsonObject, k) && pyEqual((na as JsonObject)[k], (nb as JsonObject)[k]));
}

const PY_WS = /^[\t\n\x0b\x0c\r\x1c-\x1f \x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+|[\t\n\x0b\x0c\r\x1c-\x1f \x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+$/g;

/** str.strip() with Python's whitespace set. */
export function pyStrip(s: string): string {
  return s.replace(PY_WS, "");
}

function codePointCompare(a: string, b: string): number {
  const ia = a[Symbol.iterator]();
  const ib = b[Symbol.iterator]();
  for (;;) {
    const x = ia.next();
    const y = ib.next();
    if (x.done && y.done) return 0;
    if (x.done) return -1;
    if (y.done) return 1;
    const cx = x.value.codePointAt(0)!;
    const cy = y.value.codePointAt(0)!;
    if (cx !== cy) return cx - cy;
  }
}

function pyRepr(n: number): string {
  if (!Number.isFinite(n)) throw new Error("allow_nan=False: non-finite number");
  if (Number.isInteger(n) && Math.abs(n) < 1e16) return String(n);
  // Floats: JS shortest round-trip repr matches Python's for ordinary magnitudes; the
  // exponent formats differ, so fixtures use integers only.
  return String(n);
}

function pyString(s: string): string {
  let out = '"';
  for (let i = 0; i < s.length; i++) {
    const code = s.charCodeAt(i);
    const ch = s[i];
    if (ch === '"') out += '\\"';
    else if (ch === "\\") out += "\\\\";
    else if (ch === "\n") out += "\\n";
    else if (ch === "\r") out += "\\r";
    else if (ch === "\t") out += "\\t";
    else if (ch === "\b") out += "\\b";
    else if (ch === "\f") out += "\\f";
    else if (code < 0x20 || code > 0x7e) out += "\\u" + code.toString(16).padStart(4, "0");
    else out += ch;
  }
  return out + '"';
}

/** json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False) */
export function canonicalJson(value: Json | undefined): string {
  if (value === undefined || value === null) return "null";
  if (value === true) return "true";
  if (value === false) return "false";
  if (typeof value === "number") return pyRepr(value);
  if (typeof value === "string") return pyString(value);
  if (Array.isArray(value)) return "[" + value.map(canonicalJson).join(",") + "]";
  const keys = Object.keys(value).sort(codePointCompare);
  return "{" + keys.map((k) => pyString(k) + ":" + canonicalJson(value[k])).join(",") + "}";
}

export async function contentHash(finding: Json): Promise<string> {
  const bytes = new TextEncoder().encode(canonicalJson(finding));
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function get(obj: JsonObject, key: string): Json | undefined {
  return Object.hasOwn(obj, key) ? obj[key] : undefined;
}

function setKey(value: Json | undefined): string {
  // Set identity for JSON values, mirroring Python hashing of str/int/None. Unhashable
  // values would raise in Python; here they are keyed by canonical JSON.
  if (typeof value === "boolean") return "n:" + Number(value);
  if (typeof value === "number") return "n:" + value;
  if (typeof value === "string") return "s:" + value;
  if (value === null || value === undefined) return "null";
  return "j:" + canonicalJson(value);
}

/** Caller must separately authenticate receipt issuers and validate artifacts. */
export async function reviewBlockers(finding: JsonObject, receipts: JsonObject[]): Promise<string[]> {
  const blockers: string[] = [];
  const classification = get(finding, "classification");
  if (typeof classification !== "string" || !CLASSIFICATIONS.has(classification)) blockers.push("unknown_classification");
  for (const field of ["id", "author", "summary", "sources", "limitations", "counterevidence"]) {
    if (!Object.hasOwn(finding, field)) blockers.push("missing_" + field);
  }
  let sources = get(finding, "sources");
  if (!Array.isArray(sources) || sources.length === 0) {
    blockers.push("missing_evidence");
    sources = [];
  }
  const summary = get(finding, "summary");
  if (typeof summary !== "string" || !pyStrip(summary)) blockers.push("missing_summary");
  for (const source of sources as Json[]) {
    const isDict = source !== null && typeof source === "object" && !Array.isArray(source);
    if (!isDict || !truthy(get(source as JsonObject, "locator")) || !truthy(get(source as JsonObject, "sha256"))) {
      blockers.push("source_missing_locator_or_hash");
    } else {
      const sha = (source as JsonObject).sha256;
      if (typeof sha !== "string" || !/^[0-9a-f]{64}$/.test(sha)) blockers.push("invalid_source_hash");
    }
  }
  if (pyEqual(classification, "apparent_conflict") || pyEqual(classification, "confirmed_conflict")) {
    for (const field of ["event_date", "rule_version", "duty", "exceptions"]) {
      if (!truthy(get(finding, field))) blockers.push("missing_" + field);
    }
  }
  const digest = await contentHash(finding);
  const author = get(finding, "author");
  const valid = receipts.filter(
    (r) =>
      pyEqual(get(r, "content_sha256"), digest) &&
      pyEqual(get(r, "decision"), "approve") &&
      truthy(get(r, "reviewer")) &&
      !pyEqual(r.reviewer, author) &&
      truthy(get(r, "rationale")) &&
      truthy(get(r, "reviewed_at")),
  );
  if (
    receipts.some(
      (r) =>
        pyEqual(get(r, "content_sha256"), digest) &&
        (pyEqual(get(r, "decision"), "reject") || pyEqual(get(r, "decision"), "changes_requested")),
    )
  ) {
    blockers.push("unresolved_challenge");
  }
  const roles = new Set(valid.map((r) => setKey(get(r, "role"))));
  for (const role of [...ROLES].sort()) if (!roles.has("s:" + role)) blockers.push("missing_" + role + "_review");
  const reviewers = new Set(
    valid.filter((r) => typeof get(r, "role") === "string" && (ROLES as readonly string[]).includes(r.role as string)).map((r) => setKey(r.reviewer)),
  );
  if (reviewers.size < 2) blockers.push("need_two_independent_reviewers");
  return [...new Set(blockers)].sort(codePointCompare);
}
