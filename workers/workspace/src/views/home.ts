import { html, type Safe } from "@deflock/shared/html";
import type { Repo, RequestRow } from "../db.ts";
import type { Env } from "../env.ts";
import { readReadiness } from "../readiness.ts";
import { tableWrap } from "./layout.ts";

const ROUTES = [
  ["/requests", "Requests"], ["/inbox", "Inbox"], ["/records", "Records"],
  ["/findings", "Findings"], ["/approvals", "Approvals"], ["/publish", "Publish"],
  ["/subscribers", "Subscribers"], ["/meetings", "Meetings"], ["/settings", "Settings"],
  ["/status", "Dashboard"],
] as const;
const STATES = ["draft", "approved", "sent", "acknowledged", "extended", "partial", "fulfilled", "appealed", "closed", "denied"] as const;
const OUTGOING = new Set(["send_request", "send_followup", "pay_fee", "send_newsletter", "post_social"]);
const PUBLICATION = new Set(["publish_finding", "deploy_site"]);

// Report only a bounded error type, never a database error's potentially private message.
async function block(name: string, load: () => Promise<Safe>): Promise<Safe> {
  try { return await load(); } catch (error) {
    const type = error instanceof TypeError ? "TypeError" : error instanceof SyntaxError ? "SyntaxError" : "Error";
    return html`<p class="notice warn">NOT CURRENT: ${name} source failed (${type})</p>`;
  }
}

function requestGroups(rows: RequestRow[], overdue: Set<string>): Safe {
  const open = rows.filter(r => r.state !== "closed" && r.state !== "denied").length;
  const group = (state: typeof STATES[number]): Safe => {
    const members = rows.filter(r => r.state === state);
    if (!members.length) return html``;
    const table = html`${tableWrap(`${state} requests`, html`<table class="data"><tr><th>Request id</th><th>Determination reminder</th><th>Extension claimed until</th><th>Reminder status</th></tr>${members.map(r => html`<tr><td>${r.request_id}</td><td>${r.determination_due ?? "not recorded"}</td><td>${r.extension_claimed_until ?? "not recorded"}</td><td>${overdue.has(r.request_id) ? "overdue" : ""}</td></tr>`)}</table>`)}`;
    return state === "closed" || state === "denied"
      ? html`<details><summary>${state} (${members.length})</summary>${table}</details>`
      : html`<h3>${state} (${members.length})</h3>${table}`;
  };
  return html`<p>Open requests: ${open}</p><p>Each date is a reminder, not a legal due date.</p>${rows.length ? STATES.map(group) : html`<p>Nothing waiting: no requests recorded.</p>`}`;
}

function setupLink(value: string | undefined): Safe {
  const fallback = html`<p>Start a campaign: <code>python3 -m campaign_tool setup</code></p>`;
  if (!value) return fallback;
  try {
    const url = new URL(value);
    if (url.protocol !== "https:") return html`<p>SETUP_URL refused: ${url.protocol} requires HTTPS.</p>${fallback}`;
    // An optional navigation link only: destination Access/auth and provisioning approvals still apply.
    if (url.username || url.password) return html`<p>SETUP_URL refused: embedded credentials.</p>${fallback}`;
    return html`<p><a href="${url.href}" rel="noreferrer">Start a campaign</a> — guided setup; destination authentication and provisioning approvals still apply.</p>`;
  } catch { return html`<p>SETUP_URL refused: invalid URL.</p>${fallback}`; }
}

/** Private, read-only entry point. No prose is consulted for request or approval state. */
export async function home(repo: Repo, env: Pick<Env, "SETUP_URL">, now = new Date()): Promise<Safe> {
  const [waiting, requests, next, everything] = await Promise.all([
    block("waiting on you", async () => {
      const [campaign, actions] = await Promise.all([repo.campaign(), repo.actions()]);
      const proposed = actions.filter(a => a.state === "proposed");
      const outgoing = proposed.filter(a => OUTGOING.has(a.kind)).length;
      const publication = proposed.filter(a => PUBLICATION.has(a.kind)).length;
      const law = campaign?.law_package_status ?? "unreviewed";
      return html`<ul><li><a href="/approvals">Approve what goes out</a>: ${outgoing}</li><li><a href="/approvals">Approve what gets published</a>: ${publication}</li><li><a href="/approvals">Approve the law</a>: ${law === "reviewed" ? 0 : 1} — law package ${law}</li></ul>${law !== "reviewed" ? html`<p>dates here are provisional reminders, not legal due dates</p>` : ""}${!outgoing && !publication && law === "reviewed" ? html`<p>Nothing waiting in the recorded gates.</p>` : ""}`;
    }),
    block("requests", async () => {
      const [rows, overdue] = await Promise.all([repo.requests(), repo.overdueRequests(now.toISOString().slice(0, 10))]);
      return requestGroups(rows, new Set(overdue.map(r => r.request_id)));
    }),
    block("next action", async () => {
      const snapshot = await readReadiness(repo, now);
      const age = html`<p>Generated at: ${snapshot.generated_at ?? "unknown"}; age: ${snapshot.age_seconds === null ? "unknown" : `${Math.floor(snapshot.age_seconds)} seconds`}</p>`;
      return html`${age}${snapshot.current
        ? snapshot.next_actions.length ? html`<ul>${snapshot.next_actions.map(s => html`<li>${s}</li>`)}</ul>` : html`<p>Nothing waiting: no next actions recorded.</p>`
        : html`<p class="notice warn">NOT CURRENT: readiness snapshot ${snapshot.reason}</p>`}`;
    }),
    block("everything you can do", async () => html`${setupLink(env.SETUP_URL)}<ul>${ROUTES.map(([href, label]) => html`<li><a href="${href}">${label}</a></li>`)}</ul><p>Operator tools: the flock_* store-backed scripts require the operator's private store. They are not organizer workspace actions.</p>`),
  ]);
  return html`<h1>What is waiting on me?</h1><section id="waiting"><h2>Waiting on you</h2>${waiting}</section><section id="requests"><h2>Where every request stands</h2>${requests}</section><section id="next"><h2>Next action</h2>${next}</section><section id="everything"><h2>Everything you can do</h2>${everything}</section>`;
}
