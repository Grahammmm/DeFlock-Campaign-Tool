import { env } from "cloudflare:test";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { Repo, type ActionKind, type RequestRow, type RequestState } from "../src/db.ts";
import { COUNT_KEYS } from "../src/readiness.ts";
import { home } from "../src/views/home.ts";
import { dashboard } from "../src/views/screens.ts";
import { call, makeSigner, organizerRequest, seedCampaign, type TestSigner } from "./helpers.ts";

const ACTION = "Review the packet.";
const KINDS: ActionKind[] = ["send_request", "send_followup", "pay_fee", "send_newsletter", "post_social", "publish_finding", "deploy_site"];
let signer: TestSigner;
let repo: Repo;
async function get(path = "/", extra = {}) {
  const auth = await organizerRequest(signer, "organizer@example.invalid", path);
  return call(auth.request, { ...auth.overrides, ...extra });
}
async function snapshot(patch = {}) {
  await repo.putSetting("readiness_snapshot", { schema_version: 1, campaign_id: env.CAMPAIGN_ID,
    generated_at: new Date().toISOString(), law_package_status: "draft", production_ready: false,
    safe_to_send_automatically: false, next_actions: [ACTION], counts: Object.fromEntries(COUNT_KEYS.map(k => [k, 0])), ...patch });
}
async function request(id: string, state: RequestState, due: string | null = null) {
  const at = "2026-01-01T00:00:00Z";
  await env.DB.prepare("INSERT INTO request (request_id,campaign_id,agency_id,scope_id,scope_version,subject,body_md,channel,fee_cap_cents,state,sent_at,determination_due,created_at,updated_at,next_action) VALUES (?,?,?,'scope',1,'APPROVED sent closed','PRIVATE-BODY','email',0,?,?,?,?,?,'closed APPROVED sent')")
    .bind(id, env.CAMPAIGN_ID, "ca-example-police", state, at, due, at, at).run();
}
function section(text: string, id: string) {
  return text.split(`<section id="${id}">`)[1]!.split("</section>")[0]!;
}
function fourBlocks(text: string) {
  for (const title of ["Waiting on you", "Where every request stands", "Next action", "Everything you can do"]) expect(text).toContain(`<h2>${title}</h2>`);
  expect(text).not.toContain("all clear");
}

describe("organizer entry point", () => {
  beforeAll(async () => { signer = await makeSigner(); });
  beforeEach(async () => {
    repo = await seedCampaign();
    await env.DB.exec("DELETE FROM external_action; DELETE FROM correspondence; DELETE FROM request; DELETE FROM setting;");
    await env.DB.prepare("UPDATE campaign SET law_package_status='draft' WHERE campaign_id=?").bind(env.CAMPAIGN_ID).run();
  });
  afterEach(() => vi.restoreAllMocks());

  it("draws four content blocks, exact gates, every named route and no private prose", async () => {
    for (const kind of KINDS) await repo.propose(kind, null, { text: "PRIVATE-PROPOSAL" }, kind, "test");
    await request("request-01", "sent", "2026-01-02");
    await snapshot();
    const res = await get();
    expect(res.status).toBe(200);
    expect(res.headers.get("cache-control")).toBe("private, no-store");
    expect(res.headers.get("content-security-policy")).toContain("form-action 'self'");
    const text = await res.text();
    fourBlocks(text);
    expect(section(text, "waiting")).toContain('Approve what goes out</a>: 5');
    expect(section(text, "waiting")).toContain('Approve what gets published</a>: 2');
    expect(section(text, "waiting")).toContain('Approve the law</a>: 1');
    expect(section(text, "waiting").match(/href="\/approvals"/g)).toHaveLength(3);
    expect(section(text, "next")).toContain(ACTION);
    expect(section(text, "next")).toMatch(/Generated at: \d{4}-.*age: \d+ seconds/);
    for (const [route, name] of [["requests", "Requests"], ["inbox", "Inbox"], ["records", "Records"], ["findings", "Findings"], ["approvals", "Approvals"], ["publish", "Publish"], ["subscribers", "Subscribers"], ["meetings", "Meetings"], ["settings", "Settings"], ["status", "Dashboard"]]) {
      expect(section(text, "everything")).toContain(`href="/${route}">${name}</a>`);
    }
    expect(text).toContain("flock_* store-backed scripts require the operator's private store");
    for (const secret of ["PRIVATE-BODY", "PRIVATE-PROPOSAL", "closed APPROVED sent", "APPROVED sent closed"]) expect(text).not.toContain(secret);
  });

  it("draws explicit empty states, not an all-clear readiness claim", async () => {
    await snapshot({ next_actions: [] });
    await env.DB.prepare("UPDATE campaign SET law_package_status='reviewed'").run();
    const text = await (await get()).text();
    fourBlocks(text);
    expect(section(text, "waiting")).toContain("Nothing waiting");
    expect(section(text, "requests")).toContain("Nothing waiting");
    expect(section(text, "next")).toContain("Nothing waiting");
    expect(section(text, "everything")).toContain("Start a campaign");
  });

  it("groups every request state from fields, exactly matches the overdue source and collapses terminal rows", async () => {
    const states: RequestState[] = ["draft", "approved", "sent", "acknowledged", "extended", "partial", "fulfilled", "appealed", "closed", "denied"];
    for (const [i, state] of states.entries()) await request(`req-${i}`, state, "2026-01-02");
    const expected = new Set((await repo.overdueRequests(new Date().toISOString().slice(0, 10))).map(r => r.request_id));
    const text = section(await (await get()).text(), "requests");
    expect(text).toContain("Open requests: 8");
    expect(text).toContain("not a legal due date");
    for (const [i, state] of states.entries()) {
      expect(text).toContain(`${state} (1)`);
      const row = text.split(`<td>req-${i}</td>`)[1]!.split("</tr>")[0]!;
      expect(row.includes("overdue")).toBe(expected.has(`req-${i}`));
    }
    for (const state of ["closed", "denied"]) expect(text).toContain(`<details><summary>${state} (1)</summary>`);
    expect(text).not.toContain("<details open");
    expect(text).not.toContain("APPROVED");
  });

  it.each(KINDS)("puts proposed %s in exactly one action gate", async kind => {
    await repo.propose(kind, null, {}, kind, "test");
    const text = section(await (await get()).text(), "waiting");
    const publication = kind === "publish_finding" || kind === "deploy_site";
    expect(text).toContain(`Approve what goes out</a>: ${publication ? 0 : 1}`);
    expect(text).toContain(`Approve what gets published</a>: ${publication ? 1 : 0}`);
  });

  it.each(["approved", "rejected", "executing", "executed", "failed"])("does not count %s actions as waiting", async state => {
    for (const kind of KINDS) await repo.propose(kind, null, {}, kind, "test");
    await env.DB.prepare("UPDATE external_action SET state=?").bind(state).run();
    const text = section(await (await get()).text(), "waiting");
    expect(text).toContain("Approve what goes out</a>: 0");
    expect(text).toContain("Approve what gets published</a>: 0");
  });

  it.each(["draft", "unreviewed", "reviewed"])("shows provisional law wording for %s only, never dropping reminder disclaimer", async law => {
    await env.DB.prepare("UPDATE campaign SET law_package_status=?").bind(law).run();
    const text = await (await get()).text();
    expect(section(text, "waiting")).toContain(`law package ${law}`);
    expect(text.includes("dates here are provisional reminders, not legal due dates")).toBe(law !== "reviewed");
    expect(text).toContain("not a legal due date");
  });

  it.each(["absent", "unparseable", "wrong_campaign", "stale"])("never renders next actions from %s readiness", async reason => {
    if (reason !== "absent") await snapshot(reason === "unparseable" ? { schema_version: 2 } : reason === "wrong_campaign" ? { campaign_id: "other" } : { generated_at: "2020-01-01T00:00:00Z" });
    const text = section(await (await get()).text(), "next");
    expect(text).toContain(`NOT CURRENT: readiness snapshot ${reason}`);
    expect(text).not.toContain(ACTION);
    expect(text).not.toContain("<li>");
    if (reason === "stale") expect(text).toMatch(/age: \d+ seconds/);
  });

  it.each([undefined, "https://setup.example.invalid/setup/1", "javascript:alert(1)", "data:text/html,x", "http://setup.example.invalid/", "ftp://setup.example.invalid/", "not-url", "https://user:password@setup.example.invalid/"])("only links an optional safe HTTPS SETUP_URL: %s", async url => {
    const text = await (await get("/", { SETUP_URL: url })).text();
    if (url === "https://setup.example.invalid/setup/1") expect(text).toContain(`href="${url}"`);
    else {
      expect(text).toContain("python3 -m campaign_tool setup");
      if (url) {
        expect(text).toContain("SETUP_URL refused:");
        expect(text).not.toContain(`href="${url}"`);
      }
    }
    expect(text).not.toContain("<form");
  });

  it.each(["campaign", "actions", "requests", "overdueRequests", "setting"] as const)("isolates throwing %s source at HTTP 200 without leaking error messages", async method => {
    vi.spyOn(Repo.prototype, method).mockRejectedValue(new TypeError("PRIVATE-ERROR"));
    const textRes = await get();
    expect(textRes.status).toBe(200);
    const text = await textRes.text();
    fourBlocks(text);
    const failed = method === "campaign" || method === "actions" ? "waiting on you" : method === "setting" ? "next action" : "requests";
    expect(text).toContain(`NOT CURRENT: ${failed} source failed (TypeError)`);
    expect(text.match(/source failed/g)).toHaveLength(1);
    expect(text).not.toContain("PRIVATE-ERROR");
  });

  it("isolates the everything block source failure", async () => {
    const settings = { get SETUP_URL(): string { throw new Error("PRIVATE-ERROR"); } };
    const text = String(await home(repo, settings));
    fourBlocks(text);
    expect(text).toContain("NOT CURRENT: everything you can do source failed (Error)");
    expect(text).not.toContain("PRIVATE-ERROR");
  });

  it("escapes readiness and request ids; suppresses campaign names and request prose", async () => {
    const attack = '<script>alert(1)</script>"onmouseover=</textarea>';
    await env.DB.prepare("UPDATE campaign SET name=?").bind(attack).run();
    await snapshot({ next_actions: [attack] });
    vi.spyOn(Repo.prototype, "requests").mockResolvedValue([{ request_id: attack, state: "draft", determination_due: null, extension_claimed_until: null } as RequestRow]);
    const text = await (await get()).text();
    expect(text).toContain("&lt;script&gt;alert(1)&lt;/script&gt;&quot;onmouseover=&lt;/textarea&gt;");
    expect(text).not.toContain("<script");
    expect(text).not.toContain("</textarea>");
    expect(text).not.toMatch(/<[^>]+\sonmouseover=/);
  });

  it("contains only navigation, no send/execute/publish/deploy control or third-party load", async () => {
    const text = await (await get()).text();
    expect(text).not.toMatch(/<(?:form|button|script|iframe|img|link)\b/i);
    for (const match of text.matchAll(/(?:href|src|action)="([^"]+)"/g)) {
      expect(match[1]).toMatch(/^(?:\/(?!\/)|#)/);
      expect(match[1]).not.toMatch(/\/(?:send|execute|deploy)(?:\/|$)/);
      // /publish is the shipped proposal/approval screen, not a direct publishing endpoint.
      expect(match[1]).not.toMatch(/\/publish\//);
    }
  });

  it("preserves the previous dashboard unchanged at /status", async () => {
    const expected = String(dashboard(await repo.campaign(), await repo.counts(), await repo.incidents(), await repo.runs(), await repo.jobs(30)));
    const response = await get("/status");
    expect(response.status).toBe(200);
    const text = await response.text();
    expect(text).toContain(expected);
    for (const label of ["Open incidents", "Recent runs", "Recent jobs", "Open requests"]) expect(text).toContain(label);
    expect(text).not.toContain('<section id="waiting">');
  });
});
