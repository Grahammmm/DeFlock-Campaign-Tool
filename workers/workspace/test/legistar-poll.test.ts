import { env } from "cloudflare:test";
import { afterEach, describe, expect, it, vi } from "vitest";
import cases from "../../../tests/fixtures/meeting-keyword-parity.json";
import { matchesMeetingKeywords } from "@deflock/shared/generated/meeting-keywords";
import { runScheduled } from "../src/cron.ts";
import type { Env } from "../src/env.ts";
import { eventMatches, legistarReadiness, MAX_BYTES, pollLegistar } from "../src/legistar_poll.ts";
import { dashboard, legistarLine } from "../src/views/screens.ts";
import pollSource from "../src/legistar_poll.ts?raw";
import cronSource from "../src/cron.ts?raw";
import envSource from "../src/env.ts?raw";
import screensSource from "../src/views/screens.ts?raw";
import { seedCampaign } from "./helpers.ts";

// Synthetic feeds only. Planted private data lives in fields the poll must never store.
const PLANTED = ["Jane Q. Synthetic", "jane.synthetic@example.invalid", "805-555-0142", "7SYN123", "I object to this contract strongly"];
const PRIVATE = {
  EventComment: `Public comment from ${PLANTED[0]} <${PLANTED[1]}> ${PLANTED[2]} about plate ${PLANTED[3]}: ${PLANTED[4]}`,
  Attendees: [{ Name: PLANTED[0], Email: PLANTED[1], Phone: PLANTED[2] }],
};
const LEGAL = /violat|unlawful|illegal|non-?complian|breach/i;
type Ev = Record<string, unknown>;
const ev = (id: number, date: string | null, time: string | null, title: string, extra: Ev = {}): Ev => ({
  EventId: id, EventBodyName: "Example City Council", EventDate: date, EventTime: time,
  EventItems: [{ EventItemTitle: title, EventItemActionText: `Speaker ${PLANTED[0]} (${PLANTED[1]})` }], ...PRIVATE, ...extra,
});

type Reply = { status?: number; body?: string; headers?: Record<string, string> };
const calls: string[] = [];
function stubFetch(routes: Record<string, Reply | (() => Reply)>) {
  calls.length = 0;
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    calls.push(url);
    expect(init?.method ?? "GET").toBe("GET");
    expect(init?.redirect).toBe("manual");
    const route = routes[url];
    if (!route) return new Response("no route", { status: 404 });
    const r = typeof route === "function" ? route() : route;
    return new Response(r.body ?? "[]", { status: r.status ?? 200, headers: r.headers });
  });
}
afterEach(() => vi.restoreAllMocks());

const withFeeds = (feeds?: string) => ({ ...env, LEGISTAR_FEEDS: feeds }) as Env;
async function meetingRows(prefix: string) {
  return (await env.DB.prepare("SELECT * FROM meeting WHERE source_url LIKE ? ORDER BY item_id, revision").bind(prefix + "%").all<Record<string, unknown>>()).results;
}
const count = async (table: string) => (await env.DB.prepare(`SELECT COUNT(*) n FROM ${table}`).first<{ n: number }>())!.n;
async function incidentsFor(name: string) {
  return (await env.DB.prepare("SELECT * FROM incident WHERE fingerprint = ?").bind("legistar_poll:" + name).all<Record<string, unknown>>()).results;
}
function noLeak(blob: string) {
  for (const p of PLANTED) expect(blob.includes(p), "planted private text stored").toBe(false);
  expect(LEGAL.test(blob)).toBe(false);
}

const ALPHA = "https://webapi.example.invalid/v1/alpha/events";
const BROKEN = "https://webapi.example.invalid/v1/broken/events";
const ALPHA_EVENTS = [
  ev(101, "2031-01-14T00:00:00", "7:00 PM", "Approve Flock Safety contract for readers"),
  ev(102, "2031-01-14T00:00:00", "7:00 PM", "Park maintenance budget"),
  ev(103, null, null, "Surveillance technology use policy"),
  ev(104, "2020-01-07T00:00:00", "6:00 PM", "Annual license plate reader report"),
];

describe("legistar poll in runScheduled", () => {
  it("files matching, undated and past meetings once, discards the rest, raises one incident and creates no job", async () => {
    const repo = await seedCampaign();
    const t = Date.parse("2030-06-01T14:00:00Z");
    await runScheduled(withFeeds(undefined), t); // the slot's jobs exist before the polling tick
    const jobsBefore = await count("job");
    stubFetch({ [ALPHA + "?EventItems=1"]: { body: JSON.stringify(ALPHA_EVENTS) }, [BROKEN]: { status: 500, body: "UPSTREAM-BODY-TEXT" } });
    const tick = await runScheduled(withFeeds(`${ALPHA}?EventItems=1, ${BROKEN}`), t);
    expect(calls).toEqual([ALPHA + "?EventItems=1", BROKEN]);
    expect(tick.legistar).toEqual({ outcome: "partial", filed: 3, revised: 0, discarded: 1, past: 1, failed_feeds: ["webapi.example.invalid/v1/broken/events"] });
    expect(await count("job")).toBe(jobsBefore);
    expect(tick.jobs_created).toBe(0);

    const rows = await meetingRows(ALPHA);
    expect(rows.map((r) => [r.item_id, r.source_url, r.date_state, r.starts_at, r.revision])).toEqual([
      ["101", ALPHA + "/101", "known", "2031-01-15T03:00:00.000Z", 1],
      ["103", ALPHA + "/103", "unknown", null, 1],
      ["104", ALPHA + "/104", "known", "2020-01-08T02:00:00.000Z", 1],
    ]);
    for (const r of rows) expect(r).toMatchObject({ source: "legistar", body_name: "Example City Council", agenda_item: null, relevance: "alpr_item" });
    expect((await repo.meetings())[0]!.meeting_id).toBe(rows[1]!.meeting_id);

    const receipt = (await repo.runs()).find((r) => r.run_id === tick.run_id)!;
    expect(JSON.parse(receipt.summary_json!).legistar).toEqual(tick.legistar);
    const incidents = await incidentsFor("webapi.example.invalid/v1/broken/events");
    expect(incidents).toHaveLength(1);
    expect(incidents[0]!.message).toContain("webapi.example.invalid/v1/broken/events failed (http_500)");
    expect(String(incidents[0]!.message)).not.toContain("UPSTREAM-BODY-TEXT");
    const line = legistarLine(await repo.runs());
    expect(line).toMatch(/^legistar_poll: partial at .* - filed 3, revised 0, past 1, discarded 1, failed feeds 1$/);
    noLeak(JSON.stringify([rows, incidents, receipt.summary_json, line]));

    // A retried tick for the same slot files nothing new and keeps one incident.
    const again = await runScheduled(withFeeds(`${ALPHA}?EventItems=1, ${BROKEN}`), t);
    expect(again.legistar).toMatchObject({ outcome: "partial", filed: 0, revised: 0, past: 0 });
    expect(await meetingRows(ALPHA)).toEqual(rows);
    const after = await incidentsFor("webapi.example.invalid/v1/broken/events");
    expect(after.map((i) => i.count)).toEqual([2]);
    expect(await count("job")).toBe(jobsBefore);
  });

  it("files changed bytes as one revision, leaves the previous row identical, and files an item two feeds share once", async () => {
    await seedCampaign();
    const feed = "https://webapi.example.invalid/v1/beta/events";
    const first = [ev(201, "2031-02-02T00:00:00", "6:30 PM", "ALPR pilot extension")];
    stubFetch({ [feed]: { body: JSON.stringify(first) }, [feed + "?EventItems=1"]: { body: JSON.stringify(first) } });
    const a = await runScheduled(withFeeds(`${feed} ${feed}?EventItems=1`), Date.parse("2030-06-02T14:00:00Z"));
    expect(a.legistar).toMatchObject({ outcome: "ok", filed: 1, revised: 0 });
    const [original] = await meetingRows(feed);
    stubFetch({ [feed]: { body: JSON.stringify([ev(201, "2031-02-02T00:00:00", "6:30 PM", "ALPR pilot extension (amended)")]) } });
    const b = await runScheduled(withFeeds(feed), Date.parse("2030-06-02T20:30:00Z"));
    expect(b.legistar).toMatchObject({ outcome: "ok", filed: 0, revised: 1 });
    const rows = await meetingRows(feed);
    expect(rows).toHaveLength(2);
    expect(rows[0]).toEqual(original);
    expect(rows[1]).toMatchObject({ item_id: "201", revision: 2, supersedes: original!.meeting_id });
    expect(rows[1]!.title_sha256).not.toBe(original!.title_sha256);
  });

  it("with LEGISTAR_FEEDS unset fetches nothing, records not_configured and the dashboard says so", async () => {
    const repo = await seedCampaign();
    const spy = stubFetch({});
    const tick = await runScheduled(withFeeds(undefined), Date.parse("2030-06-03T14:00:00Z"));
    expect(spy).not.toHaveBeenCalled();
    expect(tick.legistar).toEqual({ outcome: "not_configured", filed: 0, revised: 0, discarded: 0, past: 0, failed_feeds: [] });
    expect(legistarReadiness(withFeeds(undefined))).toBe("legistar_poll: not configured");
    expect(legistarReadiness(withFeeds(" , "))).toBe("legistar_poll: not configured");
    expect(legistarReadiness(withFeeds(ALPHA))).toBe("legistar_poll: configured (1 feed)");
    expect(legistarLine(await repo.runs())).toBe("legistar_poll: not configured");
    expect(String(dashboard(await repo.campaign(), {}, [], await repo.runs(), []))).toContain("legistar_poll: not configured");
  });

  it("refuses a non-https feed, a feed with credentials and a redirect by name, once each, filing nothing", async () => {
    const repo = await seedCampaign();
    const plain = "http://webapi.example.invalid/v1/gamma/events";
    const creds = "https://organizer:synthetic-pw@webapi.example.invalid/v1/delta/events";
    const moved = "https://webapi.example.invalid/v1/epsilon/events";
    stubFetch({ [moved]: { status: 302, headers: { location: "https://elsewhere.example.invalid/v1/epsilon/events" } } });
    const r = await pollLegistar(withFeeds(`${plain} ${creds} ${moved}`), repo, "slot", "run_x");
    expect(calls).toEqual([moved]);
    expect(r).toMatchObject({ outcome: "partial", filed: 0, failed_feeds: ["webapi.example.invalid/v1/gamma/events", "webapi.example.invalid/v1/delta/events", "webapi.example.invalid/v1/epsilon/events"] });
    for (const [name, reason] of [["gamma", "not_https"], ["delta", "credentials_in_url"], ["epsilon", "redirect_refused"]]) {
      const inc = await incidentsFor(`webapi.example.invalid/v1/${name}/events`);
      expect(inc).toHaveLength(1);
      expect(inc[0]!.message).toContain(`(${reason})`);
      expect(JSON.stringify(inc)).not.toMatch(/organizer|synthetic-pw/);
    }
    expect(await count("meeting WHERE source_url LIKE '%/gamma/%' OR source_url LIKE '%/delta/%' OR source_url LIKE '%/epsilon/%'")).toBe(0);
  });

  it("an oversized, timed-out, malformed or over-60-event feed is partial with an incident and no row", async () => {
    const repo = await seedCampaign();
    const base = "https://webapi.example.invalid/v1/";
    const many = Array.from({ length: 61 }, (_, i) => ev(500 + i, "2031-03-03T00:00:00", "7:00 PM", "Flock camera renewal"));
    const big = JSON.stringify([ev(400, "2031-03-03T00:00:00", "7:00 PM", "Flock camera renewal")]).padEnd(MAX_BYTES + 1, " ");
    stubFetch({ [base + "big/events"]: { body: big }, [base + "bad/events"]: { body: "[{\"EventId\": 7," }, [base + "many/events"]: { body: JSON.stringify(many) } });
    const slow = "https://slow.example.invalid/v1/slow/events";
    const fetchImpl = (async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input) !== slow) return fetch(input, init);
      return new Promise<Response>((_, reject) => init!.signal!.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError"))));
    }) as typeof fetch;
    const feeds = [base + "big/events", slow, base + "bad/events", base + "many/events"];
    const r = await pollLegistar(withFeeds(feeds.join(" ")), repo, "slot", "run_y", { fetch: fetchImpl, timeoutMs: 20 });
    expect(r).toMatchObject({ outcome: "partial", filed: 0, revised: 0 });
    expect(r.failed_feeds).toHaveLength(4);
    for (const [name, reason] of [["webapi.example.invalid/v1/big/events", "response_too_large"], ["slow.example.invalid/v1/slow/events", "timeout"],
      ["webapi.example.invalid/v1/bad/events", "not_json"], ["webapi.example.invalid/v1/many/events", "too_many_events"]]) {
      const inc = await incidentsFor(name!);
      expect(inc).toHaveLength(1);
      expect(inc[0]!.message).toContain(`(${reason})`);
    }
    for (const f of feeds) expect(await meetingRows(f)).toEqual([]);
  });

  it("uses the one generated keyword filter: decisions equal matchesMeetingKeywords on the parity fixture", () => {
    const wrong = cases.cases.flatMap((c, i) => (eventMatches({ EventItems: [{ EventItemTitle: c.text }] }) === matchesMeetingKeywords(c.text) ? [] : [i]));
    expect(wrong).toEqual([]);
    expect(eventMatches({ EventComment: "Flock ALPR surveillance", EventItems: [] })).toBe(false); // comments are never matched or stored
    for (const src of [pollSource, cronSource, envSource, screensSource]) {
      // Regex literals in expression position, plus any RegExp constructor call.
      const regexLiterals = src.match(/(?<=(?:^|[=(,:!&|?;{}]|return)\s*)\/(?![/*])(?:\\.|[^/\n\\])+\/[dgimsuy]*/gm) ?? [];
      const words = ["plate", "alpr", "flock", "surveillance"];
      expect(regexLiterals.filter((r: string) => words.some((w) => r.toLowerCase().includes(w)))).toEqual([]);
      expect(src).not.toMatch(/RegExp\(/);
    }
  });

  it("is static-clean: no queue, no runner, no credential, no POST, no cookie, no second scheduler", () => {
    expect(pollSource).not.toMatch(/enqueueJob|JOB_KINDS|JobKind|runner\/|POST|cookie|credentials:|authorization|scheduled\(|setInterval/i);
    expect(pollSource).toMatch(/redirect: "manual"/);
    expect(cronSource.match(/pollLegistar\(/g)).toHaveLength(1);
    expect(cronSource).not.toMatch(/"legistar_poll"/);
  });
});
