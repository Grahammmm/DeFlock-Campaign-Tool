import { describe, expect, it } from "vitest";
import { call, makeSigner, organizerRequest, seedCampaign } from "./helpers.ts";

const ORG = "Organizer@example.invalid";

function form(data: Record<string, string>, extra: Record<string, string> = {}): RequestInit {
  return { method: "POST", headers: { "content-type": "application/x-www-form-urlencoded", accept: "application/json", ...extra }, body: new URLSearchParams(data).toString() };
}

async function getSettings(signer: Awaited<ReturnType<typeof makeSigner>>): Promise<string> {
  const r = await organizerRequest(signer, ORG, "/settings");
  return (await call(r.request, r.overrides)).text();
}

/** The checklist input tags of the page, in order. */
function boxes(page: string): string[] {
  return page.match(/<input type="checkbox" name="signup_check_\d"[^>]*>/g) ?? [];
}

async function post(signer: Awaited<ReturnType<typeof makeSigner>>, data: Record<string, string>, extra: Record<string, string> = {}): Promise<Response> {
  const r = await organizerRequest(signer, ORG, "/settings/signup-test", form(data, extra));
  return call(r.request, r.overrides);
}

describe("settings: signup test record", () => {
  it("renders the six boxes inside a form posting to /settings/signup-test, not recorded yet", async () => {
    await seedCampaign();
    const signer = await makeSigner();
    const page = await getSettings(signer);
    const formStart = page.indexOf('action="/settings/signup-test"');
    expect(formStart).toBeGreaterThan(-1);
    const formEnd = page.indexOf("</form>", formStart);
    const inside = page.slice(formStart, formEnd);
    expect((inside.match(/name="signup_check_\d"/g) ?? []).length).toBe(6);
    expect(page).toContain("Test signup flow");
    expect(page).toContain("Not recorded yet.");
    expect(boxes(page).every((b) => !b.includes("checked"))).toBe(true);
  });

  it("records boxes 0, 2 and 5 with the JWT identity and shows them back", async () => {
    const repo = await seedCampaign();
    const signer = await makeSigner();
    const res = await post(signer, { signup_check_0: "on", signup_check_2: "on", signup_check_5: "on" });
    expect(res.status).toBe(200);
    const stored = await repo.setting<{ checks: boolean[]; done: number; recorded_by: string; recorded_at: string }>("signup_test");
    expect(stored).toMatchObject({ checks: [true, false, true, false, false, true], done: 3, recorded_by: "organizer@example.invalid" });
    expect(Number.isNaN(Date.parse(stored?.recorded_at ?? ""))).toBe(false);
    const page = await getSettings(signer);
    expect(boxes(page).map((b) => b.includes("checked"))).toEqual([true, false, true, false, false, true]);
    expect(page).toContain("Recorded 3 of 6 by organizer@example.invalid at ");
    expect(page).not.toContain("Not recorded yet.");
  });

  it("ignores unknown keys, non-'on' values and any posted identity or address", async () => {
    const repo = await seedCampaign();
    const signer = await makeSigner();
    const res = await post(signer, { signup_check_9: "on", signup_check_0: "yes", recorded_by: "x@y", email: "test@example.invalid", done: "6" });
    expect(res.status).toBe(200);
    const stored = await repo.setting<{ checks: boolean[]; done: number; recorded_by: string }>("signup_test");
    expect(stored?.checks).toEqual([false, false, false, false, false, false]);
    expect(stored?.done).toBe(0);
    expect(stored?.recorded_by).toBe("organizer@example.invalid");
    const raw = JSON.stringify(stored);
    expect(raw).not.toContain("test@example.invalid");
    expect(raw).not.toContain("x@y");
    expect(Object.keys(stored ?? {}).sort()).toEqual(["checks", "done", "recorded_at", "recorded_by"]);
  });

  it("a post with no boxes after a full record clears every tick", async () => {
    const repo = await seedCampaign();
    const signer = await makeSigner();
    const all = Object.fromEntries([0, 1, 2, 3, 4, 5].map((i) => ["signup_check_" + i, "on"]));
    await post(signer, all);
    expect((await repo.setting<{ done: number }>("signup_test"))?.done).toBe(6);
    const res = await post(signer, {});
    expect(res.status).toBe(200);
    expect(await repo.setting("signup_test")).toMatchObject({ checks: [false, false, false, false, false, false], done: 0 });
    const page = await getSettings(signer);
    expect(boxes(page).every((b) => !b.includes("checked"))).toBe(true);
    expect(page).toContain("Recorded 0 of 6 by");
  });

  it("all six ticked says every step is confirmed", async () => {
    await seedCampaign();
    const signer = await makeSigner();
    await post(signer, Object.fromEntries([0, 1, 2, 3, 4, 5].map((i) => ["signup_check_" + i, "on"])));
    const page = await getSettings(signer);
    expect(page).toContain("All 6 steps confirmed");
    expect(boxes(page).every((b) => b.includes("checked"))).toBe(true);
  });

  it("a cross-site POST is refused with 403 and the record is unchanged", async () => {
    const repo = await seedCampaign();
    const signer = await makeSigner();
    await post(signer, { signup_check_1: "on" });
    const before = await repo.setting("signup_test");
    expect(before).toMatchObject({ done: 1 });
    for (const headers of [{ origin: "https://attacker.example" }, { "sec-fetch-site": "cross-site" }] as Record<string, string>[]) {
      const res = await post(signer, { signup_check_0: "on", signup_check_1: "on", signup_check_2: "on" }, headers);
      expect(res.status).toBe(403);
    }
    expect(await repo.setting("signup_test")).toEqual(before);
  });

  it("the form path redirects to /settings with the notice; JSON accepts true values", async () => {
    const repo = await seedCampaign();
    const signer = await makeSigner();
    const r = await organizerRequest(signer, ORG, "/settings/signup-test", { method: "POST", headers: { "content-type": "application/x-www-form-urlencoded" }, body: "signup_check_3=on&signup_check_4=on" });
    const res = await call(r.request, r.overrides);
    expect(res.status).toBe(302);
    expect(res.headers.get("location")).toBe("/settings?notice=" + encodeURIComponent("Signup test recorded: 2 of 6 steps confirmed"));
    const j = await organizerRequest(signer, ORG, "/settings/signup-test", { method: "POST", headers: { "content-type": "application/json", accept: "application/json" }, body: JSON.stringify({ signup_check_1: true, signup_check_2: "true", signup_check_7: true }) });
    const jr = await call(j.request, j.overrides);
    expect(jr.status).toBe(200);
    expect(await jr.json()).toMatchObject({ checks: [false, true, false, false, false, false], done: 1 });
    expect((await repo.setting<{ done: number }>("signup_test"))?.done).toBe(1);
  });

  it("the rotate-runner-token re-render still shows the recorded state", async () => {
    await seedCampaign();
    const signer = await makeSigner();
    await post(signer, { signup_check_0: "on", signup_check_4: "on" });
    const r = await organizerRequest(signer, ORG, "/settings/rotate-runner-token", { method: "POST", headers: { accept: "text/html" } });
    const res = await call(r.request, r.overrides);
    expect(res.status).toBe(200);
    const page = await res.text();
    expect(page).toContain("New runner token");
    expect(boxes(page).map((b) => b.includes("checked"))).toEqual([true, false, false, false, true, false]);
    expect(page).toContain("Recorded 2 of 6 by organizer@example.invalid");
  });
});
