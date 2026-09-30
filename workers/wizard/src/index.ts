// Setup wizard Worker: seven server-rendered screens, an encrypted one-hour session, and a
// plan/apply provisioning flow against the Cloudflare API (DRY_RUN=1 records only).
import { Hono } from "hono";
import { LocationError, agenciesFor, locate, placeChoices, type AgencySeed } from "@deflock/shared/agencies";
import { ulid } from "@deflock/shared/ids";
import type { LawPackage } from "@deflock/shared/requests";
import seedJson from "./generated/agencies-us-ca.json";
import lawJson from "./generated/law-us-ca.json";
import { PUBLIC_SITE_BUNDLE, WORKSPACE_BUNDLE, WORKSPACE_MIGRATIONS } from "./generated/bundles.ts";
import { applyPlan } from "./apply.ts";
import { CloudflareClient } from "./cloudflare.ts";
import { defaultChoices, reconcileDrafts } from "./drafts.ts";
import type { Env } from "./env.ts";
import { buildPlan, planNames, redactStep } from "./plan.ts";
import * as S from "./screens.ts";
import { SessionError, SessionStore, sessionIdFromCookie } from "./session.ts";
import { emptyState, isStrictLocalUrl, isValidCron, isValidDomain, isValidEmail, stripSecrets, type Accounts, type Channel, type WizardState } from "./state.ts";

const SEED = seedJson as AgencySeed;
const LAW = lawJson as LawPackage;

type Vars = { store: SessionStore<WizardState>; sid: string | null; state: WizardState | null; created: string | null };

export const app = new Hono<{ Bindings: Env; Variables: Vars }>();

app.use("*", async (c, next) => {
  const store = new SessionStore<WizardState>(c.env.SESSIONS, c.env.SESSION_KEY);
  c.set("store", store);
  const sid = sessionIdFromCookie(c.req.header("cookie") ?? null);
  c.set("sid", sid);
  c.set("state", null);
  c.set("created", null);
  if (sid) {
    try {
      const env = await store.load(sid);
      if (env) {
        c.set("state", env.state);
        c.set("created", env.created_at);
      }
    } catch (e) {
      if (!(e instanceof SessionError)) throw e;
    }
  }
  await next();
});

function page(c: { var: Vars }, step: number, body: import("@deflock/shared/html").Safe, error: string | null = null, extraHeaders: Record<string, string> = {}): Response {
  return new Response(S.frame(c.var.state, step, body, error), {
    headers: { "content-type": "text/html; charset=utf-8", "cache-control": "no-store", "x-frame-options": "DENY", "content-security-policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'", ...extraHeaders },
  });
}

async function save(c: { var: Vars; env: Env }, state: WizardState): Promise<Record<string, string>> {
  let sid = c.var.sid;
  const headers: Record<string, string> = {};
  if (!sid || !c.var.state) {
    sid = c.var.store.newId();
    headers["set-cookie"] = c.var.store.cookie(sid);
  }
  await c.var.store.save(sid, state, c.var.created ?? undefined);
  return headers;
}

function requireStep(c: { var: Vars }, step: number): WizardState | null {
  const s = c.var.state;
  if (!s || s.step < step) return null;
  return s;
}

function redirect(to: string, headers: Record<string, string> = {}): Response {
  return new Response(null, { status: 303, headers: { location: to, ...headers } });
}

app.get("/", (c) => redirect("/setup/1"));
app.get("/healthz", (c) => c.json({ ok: true, dry_run: c.env.DRY_RUN === "1" }));

// 1 Location --------------------------------------------------------------------------
app.get("/setup/1", (c) => page(c, 1, S.screenLocation(c.var.state, placeChoices(SEED))));

app.post("/setup/1", async (c) => {
  const form = await c.req.parseBody();
  const query = String(form.query ?? "").trim();
  const state = c.var.state ?? emptyState(ulid());
  try {
    const location = locate(query, String(form.state ?? "CA"), SEED);
    const suggested = agenciesFor(location, SEED);
    const changed = !state.location || state.location.county_fips !== location.county_fips || state.location.place_name !== location.place_name;
    const next: WizardState = {
      ...state,
      location,
      suggested,
      agencies: changed ? defaultChoices(suggested) : state.agencies,
      requests: changed ? {} : state.requests,
      step: Math.max(state.step, 2),
    };
    if (changed) next.requests = reconcileDrafts(LAW, suggested, next.agencies, {});
    return redirect("/setup/2", await save(c, next));
  } catch (e) {
    if (e instanceof LocationError) return page(c, 1, S.screenLocation(c.var.state, placeChoices(SEED), e.candidates), e.message);
    throw e;
  }
});

// 2 Campaign --------------------------------------------------------------------------
app.get("/setup/2", (c) => {
  const s = requireStep(c, 2);
  return s ? page(c, 2, S.screenCampaign(s)) : redirect("/setup/1");
});

app.post("/setup/2", async (c) => {
  const s = requireStep(c, 2);
  if (!s) return redirect("/setup/1");
  const f = await c.req.parseBody();
  const str = (k: string) => String(f[k] ?? "").trim();
  const domain = str("domain").toLowerCase();
  const errors: string[] = [];
  if (!str("name")) errors.push("name is required");
  if (!isValidDomain(domain)) errors.push("domain must be a registrable hostname");
  if (!isValidEmail(str("organizer_email"))) errors.push("organizer email is invalid");
  if (!isValidCron(str("schedule_cron"))) errors.push("schedule must be one or more 5-field cron expressions separated by ';'");
  const tier = str("privacy_tier") === "strict_local" ? "strict_local" : "redacted_cloud";
  const next: WizardState = {
    ...s,
    campaign: { name: str("name"), tagline: str("tagline"), domain, privacy_tier: tier, schedule_cron: str("schedule_cron"), timezone: str("timezone") || "America/Los_Angeles", organizer_email: str("organizer_email").toLowerCase() },
    step: Math.max(s.step, 3),
  };
  if (errors.length) return page(c, 2, S.screenCampaign(next), errors.join("; "));
  return redirect("/setup/3", await save(c, next));
});

// 3 Agencies --------------------------------------------------------------------------
app.get("/setup/3", (c) => {
  const s = requireStep(c, 3);
  return s ? page(c, 3, S.screenAgencies(s)) : redirect("/setup/1");
});

app.post("/setup/3", async (c) => {
  const s = requireStep(c, 3);
  if (!s) return redirect("/setup/1");
  const f = await c.req.parseBody({ all: true });
  const raw = f.agency;
  const chosen = new Set(Array.isArray(raw) ? raw.map(String) : raw ? [String(raw)] : []);
  const agencies = s.agencies.map((a) => ({ ...a, selected: chosen.has(a.agency_id) }));
  const next: WizardState = { ...s, agencies, requests: reconcileDrafts(LAW, s.suggested, agencies, s.requests), step: Math.max(s.step, 4) };
  return redirect("/setup/4", await save(c, next));
});

// 4 Requests --------------------------------------------------------------------------
app.get("/setup/4", (c) => {
  const s = requireStep(c, 4);
  return s ? page(c, 4, S.screenRequests(s)) : redirect("/setup/1");
});

app.post("/setup/4", async (c) => {
  const s = requireStep(c, 4);
  if (!s) return redirect("/setup/1");
  const f = await c.req.parseBody();
  const requests = { ...s.requests };
  for (const [id, draft] of Object.entries(requests)) {
    const subject = String(f["subject:" + id] ?? draft.subject).trim();
    const body = String(f["body:" + id] ?? draft.body_md);
    const channel = String(f["channel:" + id] ?? draft.channel);
    const fee = Math.max(0, Math.round(Number(f["fee_cap:" + id] ?? draft.fee_cap_cents / 100) * 100));
    if (!["email", "muckrock", "portal_manual"].includes(channel)) return page(c, 4, S.screenRequests(s), "invalid channel");
    if (!subject || !body.trim()) return page(c, 4, S.screenRequests(s), "subject and body are required");
    requests[id] = { ...draft, subject, body_md: body, channel: channel as Channel, fee_cap_cents: Number.isFinite(fee) ? fee : draft.fee_cap_cents };
  }
  return redirect("/setup/5", await save(c, { ...s, requests, step: Math.max(s.step, 5) }));
});

// 5 Accounts --------------------------------------------------------------------------
app.get("/setup/5", (c) => {
  const s = requireStep(c, 5);
  return s ? page(c, 5, S.screenAccounts(s)) : redirect("/setup/1");
});

app.post("/setup/5", async (c) => {
  const s = requireStep(c, 5);
  if (!s || !s.campaign) return redirect("/setup/1");
  const f = await c.req.parseBody();
  const str = (k: string) => String(f[k] ?? "").trim();
  const errors: string[] = [];
  const prev = s.accounts;
  const token = str("cf_token") || prev?.cloudflare.api_token || "";
  if (token.length < 20) errors.push("Cloudflare API token is required");
  if (!/^[0-9a-f]{32}$/.test(str("cf_account_id"))) errors.push("account id must be 32 hex characters");
  if (!/^[0-9a-f]{32}$/.test(str("cf_zone_id"))) errors.push("zone id must be 32 hex characters");
  if (!/^[a-z0-9-]+$/.test(str("cf_access_team"))) errors.push("Zero Trust team name must be lowercase letters, digits or hyphens");
  const mode = str("mail_mode") === "imap_smtp" ? "imap_smtp" : "email_routing";
  if (mode === "imap_smtp" && (!str("imap_host") || !str("imap_user") || !(str("imap_password") || prev?.mailbox.imap_password))) errors.push("IMAP host, user and password are required for imap_smtp");
  const modelUrl = str("model_base_url");
  if (s.campaign.privacy_tier === "strict_local") {
    if (!modelUrl) errors.push("strict_local requires a model base URL");
    else if (!isStrictLocalUrl(modelUrl)) errors.push("strict_local: model base URL must be loopback (127.0.0.0/8, localhost, ::1) or Tailscale (100.64.0.0/10, *.ts.net)");
  } else if (modelUrl && !/^https:\/\//.test(modelUrl) && !isStrictLocalUrl(modelUrl)) errors.push("model base URL must use https unless it is a local endpoint");
  const accounts: Accounts = {
    cloudflare: { api_token: token, account_id: str("cf_account_id"), zone_id: str("cf_zone_id"), access_team: str("cf_access_team") },
    mailbox: {
      mode,
      address: "requests@" + s.campaign.domain,
      imap_host: str("imap_host") || undefined,
      imap_user: str("imap_user") || undefined,
      imap_password: str("imap_password") || prev?.mailbox.imap_password,
      smtp_host: str("smtp_host") || undefined,
      smtp_user: str("smtp_user") || undefined,
      smtp_password: str("smtp_password") || prev?.mailbox.smtp_password,
    },
    brevo: str("brevo_list_id") || str("brevo_api_key") || prev?.brevo ? { api_key: str("brevo_api_key") || prev?.brevo?.api_key || "", list_id: str("brevo_list_id") || prev?.brevo?.list_id || "" } : null,
    model: modelUrl ? { base_url: modelUrl, api_key: str("model_api_key") || prev?.model?.api_key || "", model_id: str("model_id") } : null,
  };
  if (accounts.brevo && (!accounts.brevo.api_key || !accounts.brevo.list_id)) errors.push("Brevo needs both an API key and a list id, or leave both empty");
  const next: WizardState = { ...s, accounts, plan: null, step: Math.max(s.step, 6) };
  if (errors.length) return page(c, 5, S.screenAccounts({ ...next, accounts: { ...accounts, cloudflare: { ...accounts.cloudflare, api_token: "" } } }), errors.join("; "));
  return redirect("/setup/6", await save(c, next));
});

// 6 Deploy ----------------------------------------------------------------------------
app.get("/setup/6", async (c) => {
  const s = requireStep(c, 6);
  if (!s || !s.accounts) return redirect("/setup/5");
  const plan = buildPlan(s);
  const headers = await save(c, { ...s, plan: plan.map(redactStep) });
  return page(c, 6, S.screenDeploy(s, plan, c.env.DRY_RUN === "1"), null, headers);
});

app.get("/setup/6/plan.json", (c) => {
  const s = requireStep(c, 6);
  if (!s || !s.accounts) return redirect("/setup/5");
  return c.json(buildPlan(s).map(redactStep));
});

app.post("/setup/6", async (c) => {
  const s = requireStep(c, 6);
  if (!s || !s.accounts) return redirect("/setup/5");
  const f = await c.req.parseBody();
  if (String(f.confirm ?? "") !== "yes") return page(c, 6, S.screenDeploy(s, buildPlan(s), c.env.DRY_RUN === "1"), "confirm the authorization checkbox");
  const dryRun = c.env.DRY_RUN === "1";
  const client = new CloudflareClient(s.accounts.cloudflare.api_token, { dryRun, base: c.env.CLOUDFLARE_API_BASE });
  const plan = buildPlan(s);
  const result = await applyPlan(plan, { client, bundles: { workspace: WORKSPACE_BUNDLE, public_site: PUBLIC_SITE_BUNDLE, migrations: WORKSPACE_MIGRATIONS } });
  const { generated, ...deploy } = result;
  if (deploy.status !== "applied") {
    return redirect("/setup/6", await save(c, { ...s, plan: plan.map(redactStep), deploy }));
  }
  // Handoff: account tokens are dropped from the session now; the runner token is shown once.
  const next: WizardState = { ...stripSecrets(s), plan: plan.map(redactStep), deploy, step: 7, handoff_at: new Date().toISOString() };
  const headers = await save(c, next);
  await c.env.SESSIONS.put("handoff-token:" + (c.var.sid ?? "none"), generated.runner_token, { expirationTtl: 300 });
  return redirect("/setup/7", headers);
});

// 7 Handoff ---------------------------------------------------------------------------
function urls(s: WizardState): { workspace: string; public: string } {
  const n = planNames(s);
  return { workspace: "https://" + n.workspaceHostname, public: "https://" + n.publicHostname };
}

app.get("/setup/7", async (c) => {
  const s = requireStep(c, 7);
  if (!s || !s.deploy) return redirect("/setup/6");
  const key = "handoff-token:" + (c.var.sid ?? "none");
  const runnerToken = await c.env.SESSIONS.get(key);
  if (runnerToken) await c.env.SESSIONS.delete(key);
  const u = urls(s);
  return page(c, 7, S.screenHandoff(s, u.workspace, u.public, runnerToken));
});

export function launchReceipt(s: WizardState): Record<string, unknown> {
  const u = urls(s);
  const n = planNames(s);
  return {
    schema_version: 1,
    campaign_id: s.campaign_id,
    campaign: s.campaign ? { name: s.campaign.name, domain: s.campaign.domain, privacy_tier: s.campaign.privacy_tier, schedule_cron: s.campaign.schedule_cron, timezone: s.campaign.timezone } : null,
    location: s.location,
    agencies: s.agencies.filter((a) => a.selected).map((a) => a.agency_id),
    requests: Object.values(s.requests).map((r) => ({ agency_id: r.agency_id, channel: r.channel, fee_cap_cents: r.fee_cap_cents })),
    resources: { ...n, workspace_url: u.workspace, public_url: u.public },
    deploy: s.deploy,
    handoff_at: s.handoff_at,
  };
}

app.get("/setup/7/launch-receipt.json", (c) => {
  const s = requireStep(c, 7);
  if (!s || !s.deploy) return redirect("/setup/6");
  return new Response(JSON.stringify(launchReceipt(s), null, 2), { headers: { "content-type": "application/json", "content-disposition": 'attachment; filename="launch-receipt.json"', "cache-control": "no-store" } });
});

app.post("/setup/reset", async (c) => {
  if (c.var.sid) await c.var.store.destroy(c.var.sid);
  return redirect("/setup/1", { "set-cookie": c.var.store.clearCookie() });
});

export default { fetch: app.fetch } satisfies ExportedHandler<Env>;
