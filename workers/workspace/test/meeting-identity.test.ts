import { applyD1Migrations, env } from "cloudflare:test";
import { describe, expect, it } from "vitest";
import { D1_SCHEMA_SQL } from "@deflock/shared/generated/schema";
import type { MeetingFiling } from "../src/db.ts";
import * as V from "../src/views/screens.ts";
import { seedCampaign } from "./helpers.ts";

const PRE = env.TEST_MIGRATIONS.slice(0, 3);
const M4 = env.TEST_MIGRATIONS[3]!;
const CLOCK = "2030-01-01T00:00:00.000Z";
const NEW_COLUMNS = ["date_state", "source_url", "item_id", "title_sha256", "fetched_at", "revision", "supersedes"];
const hash = (n: number) => n.toString(16).padStart(64, "0");

function filing(over: Partial<MeetingFiling> = {}): MeetingFiling {
  return {
    body_name: "Example City Council", agency_id: null, starts_at: "2030-02-01T02:00:00.000Z", date_state: "known",
    agenda_url: "https://agenda.example.invalid/a/1", agenda_item: "Item 7: ALPR contract", relevance: "alpr_item",
    source: "legistar", source_url: "https://webapi.example.invalid/v1/example/events", item_id: "item-7",
    title_sha256: hash(1), fetched_at: CLOCK, ...over,
  };
}

async function rows(db: D1Database, where = "1 = 1", ...args: unknown[]): Promise<Record<string, unknown>[]> {
  return (await db.prepare(`SELECT * FROM meeting WHERE ${where} ORDER BY meeting_id`).bind(...args).all<Record<string, unknown>>()).results;
}

async function preMigrationDb(db: D1Database = env.DB_UPGRADE): Promise<D1Database> {
  expect(M4.name).toBe("0004_meeting_identity.sql");
  await applyD1Migrations(db, PRE);
  await db.prepare("INSERT INTO campaign (campaign_id, name, jurisdiction, county_fips, county_name, created_at, updated_at) VALUES ('c-up', 'Synthetic', 'us-ca', '00000', 'Example', ?, ?)")
    .bind(CLOCK, CLOCK).run();
  return db;
}

const insertRaw = (db: D1Database, id: string, cols: Record<string, unknown>) => {
  const all: Record<string, unknown> = { meeting_id: id, campaign_id: env.CAMPAIGN_ID, body_name: "Raw body", created_at: CLOCK, updated_at: CLOCK, ...cols };
  const keys = Object.keys(all);
  return db.prepare(`INSERT INTO meeting (${keys.join(",")}) VALUES (${keys.map(() => "?").join(",")})`).bind(...keys.map(k => all[k])).run();
};

describe("0004_meeting_identity migration", () => {
  it("keeps every pre-migration row and value, defaults date_state to known, and refuses a hand re-run", async () => {
    const db = await preMigrationDb();
    const ins = "INSERT INTO meeting (meeting_id, campaign_id, body_name, agency_id, starts_at, agenda_url, agenda_item, relevance, comment_kit_md, rsvp_count, source, created_at, updated_at) VALUES (?, 'c-up', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)";
    await db.prepare(ins).bind("mtg_a", "Council", "ca-example", "2030-03-01T01:00:00.000Z", "https://agenda.example.invalid/7", "Item 7", "alpr_item",
      "## Public comment\nLine with ünïcode", 3, "manual", CLOCK, "2030-01-02T00:00:00.000Z").run();
    await db.prepare(ins).bind("mtg_b", "Board", null, "2030-04-01T01:00:00.000Z", null, null, null, null, 0, null, CLOCK, CLOCK).run();
    const before = await rows(db);
    expect(before).toHaveLength(2);

    await applyD1Migrations(db, env.TEST_MIGRATIONS);
    const after = await rows(db);
    expect(after).toHaveLength(2);
    for (const [i, row] of after.entries()) {
      expect(Object.fromEntries(Object.entries(row).filter(([k]) => !NEW_COLUMNS.includes(k)))).toEqual(before[i]);
      expect(row).toMatchObject({ date_state: "known", revision: 1, source_url: null, item_id: null, title_sha256: null, fetched_at: null, supersedes: null });
    }
    const startsAt = (await db.prepare("PRAGMA table_info(meeting)").all<{ name: string; notnull: number }>()).results.find(c => c.name === "starts_at")!;
    expect(startsAt.notnull).toBe(0);

    // The tracked helper is a no-op the second time.
    await applyD1Migrations(db, env.TEST_MIGRATIONS);
    expect(await rows(db)).toEqual(after);
    // Running the file again by hand is refused as one transaction and destroys nothing.
    await expect(db.batch(M4.queries.map(sql => db.prepare(sql)))).rejects.toThrow(/duplicate column/i);
    expect(await rows(db)).toEqual(after);
    const tables = (await db.prepare("SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'meeting%'").all<{ name: string }>()).results;
    expect(tables.map(t => t.name)).toEqual(["meeting"]);
  });

  it("migrates an empty table and leaves the identity index in place", async () => {
    // A second fresh D1 (storage is shared between the tests of one file).
    const db = await preMigrationDb(env.DB_SCHEMA);
    expect(await rows(db)).toEqual([]);
    await applyD1Migrations(db, env.TEST_MIGRATIONS);
    expect(await rows(db)).toEqual([]);
    const cols = (await db.prepare("PRAGMA table_info(meeting)").all<{ name: string }>()).results.map(c => c.name);
    expect(cols).toEqual(expect.arrayContaining(NEW_COLUMNS));
    const idx = (await db.prepare("PRAGMA index_list(meeting)").all<{ name: string; unique: number; partial: number }>()).results
      .find(i => i.name === "meeting_source_identity");
    expect(idx).toMatchObject({ unique: 1, partial: 1 });
  });
});

describe("fileMeeting / fileMeetingRevision", () => {
  it("files once, re-files as inserted: false, and concurrent filings make exactly one row", async () => {
    const r = await seedCampaign();
    const url = "https://webapi.example.invalid/v1/idem/events";
    const first = await r.fileMeeting(filing({ source_url: url }));
    expect(first.inserted).toBe(true);
    expect(first.row).toMatchObject({ revision: 1, supersedes: null, date_state: "known", source_url: url, item_id: "item-7" });
    const again = await r.fileMeeting(filing({ source_url: url, fetched_at: "2030-01-05T00:00:00.000Z" }));
    expect(again.inserted).toBe(false);
    expect(again.row.meeting_id).toBe(first.row.meeting_id);
    expect(again.row.fetched_at).toBe(CLOCK);
    // A changed title is never silently folded into the existing row.
    await expect(r.fileMeeting(filing({ source_url: url, title_sha256: hash(2) }))).rejects.toThrow(/meeting_revision_required/);
    expect(await rows(env.DB, "source_url = ?", url)).toHaveLength(1);

    const race = "https://webapi.example.invalid/v1/race/events";
    const results = await Promise.all(Array.from({ length: 6 }, () => r.fileMeeting(filing({ source_url: race }))));
    expect(results.filter(x => x.inserted)).toHaveLength(1);
    expect(new Set(results.map(x => x.row.meeting_id)).size).toBe(1);
    expect(await rows(env.DB, "source_url = ?", race)).toHaveLength(1);
  });

  it("refuses filings without an identity or a valid title hash", async () => {
    const r = await seedCampaign();
    await expect(r.fileMeeting(filing({ source_url: "" }))).rejects.toThrow(/meeting_identity_required/);
    await expect(r.fileMeeting(filing({ item_id: "" }))).rejects.toThrow(/meeting_identity_required/);
    await expect(r.fileMeeting(filing({ source_url: "https://x.example.invalid/h", title_sha256: "ABC" }))).rejects.toThrow(/meeting_title_sha256_invalid/);
    expect(await rows(env.DB, "source_url = ?", "https://x.example.invalid/h")).toEqual([]);
  });

  it("builds an immutable chain of three; earlier rows stay byte-identical and stale or forked revisions are refused", async () => {
    const r = await seedCampaign();
    const url = "https://webapi.example.invalid/v1/chain/events";
    const one = (await r.fileMeeting(filing({ source_url: url }))).row;
    const snap1 = await rows(env.DB, "meeting_id = ?", one.meeting_id);
    const two = await r.fileMeetingRevision(one.meeting_id, filing({ source_url: url, title_sha256: hash(2) }));
    expect(two).toMatchObject({ revision: 2, supersedes: one.meeting_id, title_sha256: hash(2) });
    expect(await rows(env.DB, "meeting_id = ?", one.meeting_id)).toEqual(snap1);
    const snap2 = await rows(env.DB, "meeting_id = ?", two.meeting_id);
    const three = await r.fileMeetingRevision(two.meeting_id, filing({ source_url: url, title_sha256: hash(3), date_state: "unknown", starts_at: null }));
    expect(three).toMatchObject({ revision: 3, supersedes: two.meeting_id });
    expect(await rows(env.DB, "meeting_id = ?", one.meeting_id)).toEqual(snap1);
    expect(await rows(env.DB, "meeting_id = ?", two.meeting_id)).toEqual(snap2);

    // Stale (not the head), unchanged, wrong identity, unknown id and manual rows are refused.
    await expect(r.fileMeetingRevision(one.meeting_id, filing({ source_url: url, title_sha256: hash(9) }))).rejects.toThrow(/meeting_not_head/);
    await expect(r.fileMeetingRevision(three.meeting_id, filing({ source_url: url, title_sha256: hash(3) }))).rejects.toThrow(/meeting_unchanged/);
    await expect(r.fileMeetingRevision(three.meeting_id, filing({ source_url: url, item_id: "item-8", title_sha256: hash(9) }))).rejects.toThrow(/meeting_identity_mismatch/);
    await expect(r.fileMeetingRevision("mtg_missing", filing({ source_url: url, title_sha256: hash(9) }))).rejects.toThrow(/meeting_not_filed/);
    const manual = await r.createMeeting({ meeting_id: "mtg_manual_chain", body_name: "Manual", agency_id: null, starts_at: CLOCK, agenda_url: null,
      agenda_item: null, relevance: null, comment_kit_md: null, rsvp_count: 0, source: "manual" });
    await expect(r.fileMeetingRevision(manual.meeting_id, filing({ source_url: url, title_sha256: hash(9) }))).rejects.toThrow(/meeting_not_filed/);

    // Two concurrent revisions of the same head: the identity index lets exactly one in.
    const settled = await Promise.allSettled([4, 5].map(n => r.fileMeetingRevision(three.meeting_id, filing({ source_url: url, title_sha256: hash(n) }))));
    expect(settled.filter(s => s.status === "fulfilled")).toHaveLength(1);
    const chain = await rows(env.DB, "source_url = ?", url);
    expect(chain.map(c => c.revision).sort()).toEqual([1, 2, 3, 4]);

    // No method changes an identity column on an existing row.
    for (const patch of [{ title_sha256: hash(7) }, { source_url: "https://other.example.invalid" }, { item_id: "x" }, { supersedes: null }, { revision: 9 }]) {
      await expect(r.updateMeeting(one.meeting_id, patch as never)).rejects.toThrow(/meeting_identity_immutable/);
    }
    expect(await rows(env.DB, "meeting_id = ?", one.meeting_id)).toEqual(snap1);
    expect(await rows(env.DB, "meeting_id = ?", two.meeting_id)).toEqual(snap2);
  });
});

describe("schema constraints", () => {
  it("refuses a date the state does not admit, and keeps half-identities and manual rows outside the unique index", async () => {
    await seedCampaign();
    await expect(insertRaw(env.DB, "mtg_ck1", { date_state: "unknown", starts_at: CLOCK })).rejects.toThrow(/CHECK constraint/);
    await expect(insertRaw(env.DB, "mtg_ck2", { date_state: "known", starts_at: null })).rejects.toThrow(/CHECK constraint/);
    await expect(insertRaw(env.DB, "mtg_ck3", { starts_at: null })).rejects.toThrow(/CHECK constraint/);
    await expect(insertRaw(env.DB, "mtg_ck4", { date_state: "maybe", starts_at: CLOCK })).rejects.toThrow(/CHECK constraint/);
    const r = await seedCampaign();
    await expect(r.fileMeeting(filing({ source_url: "https://ck.example.invalid/1", date_state: "unknown" }))).rejects.toThrow(/CHECK constraint/);
    await expect(r.fileMeeting(filing({ source_url: "https://ck.example.invalid/2", starts_at: null }))).rejects.toThrow(/CHECK constraint/);
    expect(await rows(env.DB, "meeting_id LIKE 'mtg_ck%' OR source_url LIKE 'https://ck.%'")).toEqual([]);

    // item_id without source_url and the reverse: not covered by the index, so duplicates coexist.
    for (const [id, cols] of [["mtg_half1", { item_id: "i-1" }], ["mtg_half2", { item_id: "i-1" }],
      ["mtg_half3", { source_url: "https://half.example.invalid" }], ["mtg_half4", { source_url: "https://half.example.invalid" }]] as const) {
      await insertRaw(env.DB, id, { starts_at: CLOCK, ...cols });
    }
    for (const id of ["mtg_two1", "mtg_two2"]) {
      await r.createMeeting({ meeting_id: id, body_name: "Same body", agency_id: null, starts_at: CLOCK, agenda_url: null, agenda_item: "Same item",
        relevance: null, comment_kit_md: null, rsvp_count: 0, source: "manual" });
    }
    const made = await rows(env.DB, "meeting_id LIKE 'mtg_half%' OR meeting_id LIKE 'mtg_two%'");
    expect(made).toHaveLength(6);
    expect(made.every(m => m.date_state === "known" && m.revision === 1)).toBe(true);
  });
});

describe("meetings() and the meetings screen", () => {
  it("lists unknown dates first, then newest first, and a revision chain once by its head", async () => {
    const r = await seedCampaign();
    const mk = (id: string, starts: string) => r.createMeeting({ meeting_id: id, body_name: "Body " + id, agency_id: null, starts_at: starts,
      agenda_url: null, agenda_item: null, relevance: null, comment_kit_md: null, rsvp_count: 0, source: "manual" });
    await mk("mtg_old", "2030-01-01T00:00:00.000Z");
    await mk("mtg_new", "2030-06-01T00:00:00.000Z");
    const url = "https://webapi.example.invalid/v1/order/events";
    const head1 = (await r.fileMeeting(filing({ source_url: url, starts_at: "2030-03-01T00:00:00.000Z" }))).row;
    const head2 = await r.fileMeetingRevision(head1.meeting_id, filing({ source_url: url, starts_at: "2030-03-01T00:00:00.000Z", title_sha256: hash(2) }));
    const undated = (await r.fileMeeting(filing({ source_url: url, item_id: "item-u", date_state: "unknown", starts_at: null }))).row;

    const listed = (await r.meetings()).filter(m => ["mtg_old", "mtg_new", head1.meeting_id, head2.meeting_id, undated.meeting_id].includes(m.meeting_id));
    expect(listed.map(m => m.meeting_id)).toEqual([undated.meeting_id, "mtg_new", head2.meeting_id, "mtg_old"]);
    const all = await r.meetings();
    expect(all[0]!.date_state).toBe("unknown");
    expect(all.filter(m => m.meeting_id === head1.meeting_id)).toEqual([]);

    const page = String(V.meetings(listed, null));
    expect(page).toContain("date unknown");
    expect(page).toContain(`revision 2 of ${url} item item-7`);
    expect(page).not.toContain(head1.meeting_id);
    expect(page).not.toContain(`/meetings/${undated.meeting_id}/comment-kit`);
    expect(page).toContain(`/meetings/${head2.meeting_id}/comment-kit`);
  });
});

describe("privacy", () => {
  it("keeps planted personal data in the row only: no index, schema or error message carries it", async () => {
    const r = await seedCampaign();
    const planted = ["Jane Q. Synthetic", "jane.synthetic@example.invalid", "805-555-0142", "7SYN123"];
    const url = "https://webapi.example.invalid/v1/privacy/events";
    const first = (await r.fileMeeting(filing({ source_url: url, agenda_item: "Item 9 " + planted.join(" ") }))).row;
    expect((await r.meeting(first.meeting_id))!.agenda_item).toContain(planted[0]);

    const cols = (await env.DB.prepare("PRAGMA index_info(meeting_source_identity)").all<{ name: string }>()).results.map(c => c.name);
    expect(cols).toEqual(["campaign_id", "source_url", "item_id", "revision"]);
    const ddl = (await env.DB.prepare("SELECT group_concat(sql, ' ') AS s FROM sqlite_master WHERE sql IS NOT NULL").first<{ s: string }>())!.s;
    const errors: string[] = [];
    await r.fileMeeting(filing({ source_url: url, title_sha256: hash(5), agenda_item: planted.join(" ") })).catch((e: Error) => errors.push(e.message));
    await r.fileMeetingRevision(first.meeting_id, filing({ source_url: url, agenda_item: planted.join(" ") })).catch((e: Error) => errors.push(e.message));
    expect(errors).toHaveLength(2);
    for (const value of planted) {
      expect(ddl.includes(value)).toBe(false);
      expect(D1_SCHEMA_SQL.includes(value)).toBe(false);
      expect(M4.queries.join("\n").includes(value)).toBe(false);
      expect(errors.join("\n").includes(value)).toBe(false);
    }
  });
});
