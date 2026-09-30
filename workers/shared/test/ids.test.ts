import { describe, expect, it } from "vitest";
import { isValidUlid, newJobId, receiptId, sha256Hex, timingSafeEqual, ulid } from "../src/ids.ts";

describe("ids", () => {
  it("ulid is 26 lowercase chars and time-ordered", () => {
    const a = ulid(1000);
    const b = ulid(2000);
    expect(isValidUlid(a)).toBe(true);
    expect(a.slice(0, 10) < b.slice(0, 10)).toBe(true);
  });
  it("prefixed ids", () => {
    expect(newJobId()).toMatch(/^job_[0-9a-f]{16}$/);
  });
  it("receipt id matches cli.ingest sha256(json [source_id, sha256])", async () => {
    const sha = "a".repeat(64);
    expect(await receiptId("src-1", sha)).toBe(await sha256Hex(JSON.stringify(["src-1", sha])));
  });
  it("timing safe equal", () => {
    expect(timingSafeEqual("abc", "abc")).toBe(true);
    expect(timingSafeEqual("abc", "abd")).toBe(false);
    expect(timingSafeEqual("abc", "abcd")).toBe(false);
    expect(timingSafeEqual("", "")).toBe(true);
  });
});
