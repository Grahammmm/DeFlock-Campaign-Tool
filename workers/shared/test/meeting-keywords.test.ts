import { describe, expect, it } from "vitest";
import cases from "../../../tests/fixtures/meeting-keyword-parity.json";
import spec from "../../../data/meeting-keywords.json";
import { MEETING_KEYWORDS, MEETING_KEYWORDS_SOURCE, matchesMeetingKeywords } from "../src/generated/meeting-keywords.ts";

// Python (tests/test_meeting_keywords.py) asserts campaign_tool.meetings.KEYWORDS against the same verdicts.
describe("meeting keywords (generated from data/meeting-keywords.json)", () => {
  it("fixture holds the spec's coverage", () => {
    expect(cases.cases.length).toBeGreaterThanOrEqual(24);
    expect(cases.cases.some((c) => !c.match)).toBe(true);
  });

  it("matchesMeetingKeywords gives the recorded Python verdict for every fixture string", () => {
    // Compare index lists, never the strings, so fixture text (incl. planted synthetic contact data) is not echoed.
    const wrong = cases.cases.flatMap((c, i) => (matchesMeetingKeywords(c.text) === c.match ? [] : [i]));
    expect(wrong).toEqual([]);
  });

  it("is stateless across repeated calls (no global flag)", () => {
    expect(MEETING_KEYWORDS.global).toBe(false);
    expect(MEETING_KEYWORDS.sticky).toBe(false);
    const hit = cases.cases.find((c) => c.match)!.text;
    expect([matchesMeetingKeywords(hit), matchesMeetingKeywords(hit), matchesMeetingKeywords(hit)]).toEqual([true, true, true]);
  });

  it("source is the JSON alternatives joined exactly as Python joins them", () => {
    expect(spec.schema_version).toBe(1);
    expect(spec.flags).toBe("i");
    expect(MEETING_KEYWORDS_SOURCE).toBe(spec.alternatives.join("|"));
    expect(MEETING_KEYWORDS.ignoreCase).toBe(true);
  });
});
