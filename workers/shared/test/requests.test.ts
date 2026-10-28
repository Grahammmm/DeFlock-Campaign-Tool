import { describe, expect, it } from "vitest";
import law from "../../wizard/src/generated/law-us-ca.json";
import { determinationDue, renderCombinedRequest, requestSubject, type LawPackage } from "../src/requests.ts";

const PKG = law as LawPackage;

describe("renderCombinedRequest (port of kit.render_combined_request)", () => {
  it("renders every scope, placeholders and the draft banner", () => {
    const md = renderCombinedRequest(PKG, { name: "Example Police Department", jurisdiction_name: "City of Example" });
    expect(md.startsWith("# Draft records request: Example Police Department\n")).toBe(true);
    expect(md).toContain("DRAFT ONLY. Nothing has been sent.");
    for (const scope of PKG.request_scopes) expect(md).toContain("## " + scope.title);
    expect(md).toContain("[explicit approved cap, e.g. $50]");
    expect(md).toContain("does not assert that this agency operates ALPR");
    expect(md.endsWith("\n")).toBe(true);
  });

  it("uses the records email as custodian and the supplied fee cap", () => {
    const md = renderCombinedRequest(PKG, { name: "X", records_email: "records@example.invalid" }, { feeCap: "$25" });
    expect(md).toContain("To: records@example.invalid");
    expect(md).toContain("fees beyond $25.");
  });

  it("rejects angle brackets in agency names", () => {
    expect(() => renderCombinedRequest(PKG, { name: "<script>" })).toThrow(/plain text/);
  });

  it("subject and determination due", () => {
    expect(requestSubject("A", "2024")).toBe("Records concerning any automated license plate reader (ALPR) use by A, 2024");
    expect(determinationDue(PKG, "2026-01-01T00:00:00Z")).toBe("2026-01-11");
    expect(determinationDue(PKG, "2026-01-01T00:00:00Z", true)).toBe("2026-01-25");
  });
});
