import { describe, expect, it } from "vitest";
import cases from "../../../tests/fixtures/review-cases.json";
import { canonicalJson, contentHash, reviewBlockers, type JsonObject } from "../src/review.ts";

describe("review_blockers parity with campaign_tool/review.py", () => {
  for (const c of cases.cases) {
    it(c.name, async () => {
      const blockers = await reviewBlockers(c.finding as unknown as JsonObject, c.receipts as unknown as JsonObject[]);
      expect(blockers).toEqual(c.expected_blockers);
    });
  }

  it("canonical JSON matches json.dumps(sort_keys, compact, ensure_ascii)", () => {
    expect(canonicalJson({ b: [true, 1, "é\n"], a: null })).toBe('{"a":null,"b":[true,1,"\\u00e9\\n"]}');
  });

  it("content hash is stable across key order", async () => {
    const a = await contentHash({ x: 1, y: [2, 3] });
    const b = await contentHash({ y: [2, 3], x: 1 });
    expect(a).toBe(b);
    expect(a).toMatch(/^[0-9a-f]{64}$/);
  });
});
