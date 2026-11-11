import { env } from "cloudflare:test";
import { beforeAll, beforeEach, describe, expect, it } from "vitest";
import { dashboard, findings, meetings, publish, records, requestsList, settings, subscribers } from "../src/views/screens.ts";
import { call, makeSigner, organizerRequest, seedCampaign, type TestSigner } from "./helpers.ts";

let signer: TestSigner;
async function get(path: string) {
  const auth = await organizerRequest(signer, "organizer@example.invalid", path);
  return call(auth.request, auth.overrides);
}

/** Every <table> must sit directly inside a labelled, focusable .table-wrap region. */
function expectWrapped(markup: string, tables: number) {
  expect(markup.match(/<table\b/g)?.length ?? 0).toBe(tables);
  const wrapped = markup.match(/<div class="table-wrap" role="region" aria-label="[^"]+" tabindex="0"><table\b/g) ?? [];
  expect(wrapped.length).toBe(tables);
}

describe("console polish (S2-04/05/06/15/16)", () => {
  beforeAll(async () => { signer = await makeSigner(); });
  beforeEach(async () => { await seedCampaign(); });

  it("wraps each data table in a labelled overflow wrapper", () => {
    expectWrapped(String(dashboard(null, {}, [{ severity: "warn", message: "m", count: 1, last_seen: "t" } as never], [], [])), 3);
    expectWrapped(String(requestsList([], new Map())), 1);
    expectWrapped(String(publish([], null, null, null)), 1);
    expectWrapped(String(subscribers([], [], [], null, null)), 2);
    expectWrapped(String(meetings([], null)), 1);
    expectWrapped(String(settings(null, [], [], null, null, false)), 2);
  });

  it("gives the records table an Actions header and no empty th", () => {
    const out = String(records([]));
    expectWrapped(out, 1);
    expect(out).toContain('<th><span class="visually-hidden">Actions</span></th>');
    expect(out).not.toMatch(/<th>\s*<\/th>/);
  });

  it("makes every pre block focusable and labelled", async () => {
    const out = String(settings({ name: "n" } as never, [], [], null, null, false));
    const pres = out.match(/<pre\b[^>]*>/g) ?? [];
    expect(pres.length).toBeGreaterThan(0);
    for (const tag of pres) {
      expect(tag).toContain('tabindex="0"');
      expect(tag).toMatch(/aria-label="[^"]+"/);
    }
    const kit = String(meetings([{ meeting_id: "m", body_name: "b", starts_at: "s", date_state: "known", rsvp_count: 0, comment_kit_md: "kit" } as never], null));
    expect(kit).toMatch(/<pre class="json" tabindex="0" role="region" aria-label="Comment kit/);
    expect(String(await findings([]))).not.toContain("<pre");
  });

  it("keeps nav targets at least 24 px (44 px on phones)", async () => {
    const res = await get("/status");
    const text = await res.text();
    expect(text).toMatch(/\.app-nav a\{[^}]*min-height:24px/);
    expect(text).toMatch(/@media\(max-width:700px\)\{\.app-nav a\{min-height:44px\}\}/);
    expect(text).toContain(".table-wrap{overflow-x:auto");
  });

  it("serves unknown paths as a 404 in the shared layout", async () => {
    const res = await get("/no-such-page");
    expect(res.status).toBe(404);
    const text = await res.text();
    expect(text).toContain("<html lang=");
    expect(text).toContain("<h1>Page not found</h1>");
    expect(text).toContain('<a href="/">');
    expect(text).toContain('class="app-nav"');
  });
});
