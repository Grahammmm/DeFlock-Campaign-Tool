// Provisioning plan: every Cloudflare resource with the exact API call that creates it,
// its rollback call, and the placeholders resolved from earlier outputs.
import type { WizardState } from "./state.ts";
import { campaignSlug } from "./state.ts";

export type StepKind = "api" | "worker_upload" | "secret" | "d1_query";

export interface PlanStep {
  id: string;
  title: string;
  kind: StepKind;
  resource: string;
  method: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  path: string; // may contain {{output.key}} placeholders
  body: unknown; // JSON body (placeholders allowed in string values); secrets are marked with {{secret.NAME}}
  /** True when the body embeds organizer-typed text: it is sent verbatim, never placeholder-resolved. */
  literal_body?: boolean;
  /** Names of outputs captured from the response: { key: "result.id" } */
  captures: Record<string, string>;
  rollback: { method: "DELETE" | "POST" | "PUT"; path: string; body?: unknown } | null;
  redact: string[]; // body keys never shown or stored
}

export interface Receipt {
  step_id: string;
  resource: string;
  method: string;
  path: string;
  status: number | null;
  ok: boolean;
  dry_run: boolean;
  outputs: Record<string, string>;
  error: string | null;
  at: string;
}

export interface RollbackStep {
  step_id: string;
  resource: string;
  method: string;
  path: string;
  body?: unknown;
}

export interface PlanNames {
  slug: string;
  workspaceScript: string;
  publicScript: string;
  d1: string;
  originalsBucket: string;
  publicBucket: string;
  kv: string;
  queue: string;
  publicHostname: string;
  workspaceHostname: string;
  mailAddress: string;
  accessAppName: string;
}

export function planNames(state: WizardState): PlanNames {
  if (!state.campaign) throw new Error("campaign settings required");
  const slug = campaignSlug(state.campaign);
  const domain = state.campaign.domain;
  return {
    slug,
    workspaceScript: `${slug}-workspace`,
    publicScript: `${slug}-public-site`,
    d1: `${slug}-campaign`,
    originalsBucket: `${slug}-originals`,
    publicBucket: `${slug}-public`,
    kv: `${slug}-cache`,
    queue: `${slug}-jobs`,
    publicHostname: domain,
    workspaceHostname: `workspace.${domain}`,
    mailAddress: `requests@${domain}`,
    accessAppName: `${state.campaign.name} workspace`,
  };
}

function sqlLiteral(v: unknown): string {
  if (v === null || v === undefined) return "NULL";
  if (typeof v === "number") return String(v);
  return "'" + String(v).replace(/'/g, "''") + "'";
}

/** SQL that seeds the campaign, agencies and request drafts into D1 after migrations. */
export function seedSql(state: WizardState): string {
  if (!state.campaign || !state.location) throw new Error("campaign and location required");
  const now = new Date().toISOString();
  const c = state.campaign;
  const names = planNames(state);
  const rows: string[] = [
    `INSERT INTO campaign (campaign_id,name,jurisdiction,county_fips,county_name,place_fips,place_name,public_hostname,workspace_hostname,privacy_tier,law_package_status,external_sends,schedule_cron,timezone,created_at,updated_at) VALUES (${[
      state.campaign_id, c.name, state.jurisdiction, state.location.county_fips, state.location.county_name, state.location.place_fips, state.location.place_name,
      names.publicHostname, names.workspaceHostname, c.privacy_tier, "draft", "approval_required", c.schedule_cron, c.timezone, now, now,
    ].map(sqlLiteral).join(",")});`,
    `INSERT INTO setting (campaign_id,key,value_json,created_at,updated_at) VALUES (${[state.campaign_id, "tagline", JSON.stringify(c.tagline), now, now].map(sqlLiteral).join(",")});`,
  ];
  for (const choice of state.agencies) {
    const seed = state.suggested.find((s) => s.agency_id === choice.agency_id);
    if (!seed) continue;
    rows.push(
      `INSERT INTO agency (agency_id,campaign_id,name,kind,jurisdiction_name,records_url,records_email,portal_vendor,portal_url,flock_transparency_slug,muckrock_agency_id,selected,verified,sources_json,created_at,updated_at) VALUES (${[
        seed.agency_id, state.campaign_id, seed.name, seed.kind, seed.jurisdiction_name, seed.records_url, seed.records_email, seed.portal.vendor, seed.portal.url,
        seed.flock_transparency_slug, seed.muckrock_agency_id, choice.selected ? 1 : 0, seed.verified ? 1 : 0, JSON.stringify(seed.sources), now, now,
      ].map(sqlLiteral).join(",")});`,
    );
  }
  let n = 0;
  for (const draft of Object.values(state.requests)) {
    if (!state.agencies.find((a) => a.agency_id === draft.agency_id && a.selected)) continue;
    n += 1;
    const requestId = "req_" + state.campaign_id.slice(-8) + n.toString(16).padStart(8, "0");
    rows.push(
      `INSERT INTO request (request_id,campaign_id,agency_id,scope_id,scope_version,subject,body_md,channel,fee_cap_cents,state,created_at,updated_at) VALUES (${[
        requestId, state.campaign_id, draft.agency_id, "combined", 1, draft.subject, draft.body_md, draft.channel, draft.fee_cap_cents, "draft", now, now,
      ].map(sqlLiteral).join(",")});`,
    );
  }
  return rows.join("\n");
}

/** The ordered plan. Later steps use {{key}} placeholders captured from earlier ones. */
export function buildPlan(state: WizardState): PlanStep[] {
  if (!state.campaign || !state.accounts) throw new Error("campaign and accounts required");
  const n = planNames(state);
  const a = state.accounts.cloudflare.account_id;
  const z = state.accounts.cloudflare.zone_id;
  const acct = `/accounts/${a}`;
  const zone = `/zones/${z}`;
  const crons = state.campaign.schedule_cron.split(";").map((c) => c.trim()).filter(Boolean);
  const steps: PlanStep[] = [];
  const api = (s: Omit<PlanStep, "kind" | "captures" | "redact"> & Partial<Pick<PlanStep, "kind" | "captures" | "redact">>): PlanStep =>
    ({ kind: "api", captures: {}, redact: [], ...s });

  steps.push(api({ id: "token.verify", title: "Verify the API token", resource: "token", method: "GET", path: "/user/tokens/verify", body: null, rollback: null }));
  steps.push(api({ id: "zone.verify", title: `Confirm zone ${z} serves ${state.campaign.domain}`, resource: "zone", method: "GET", path: zone, body: null, captures: { zone_name: "result.name" }, rollback: null }));
  steps.push(api({ id: "d1.create", title: `D1 database ${n.d1}`, resource: "d1", method: "POST", path: `${acct}/d1/database`, body: { name: n.d1 }, captures: { d1_id: "result.uuid" }, rollback: { method: "DELETE", path: `${acct}/d1/database/{{d1_id}}` } }));
  steps.push(api({ id: "r2.originals", title: `R2 bucket ${n.originalsBucket} (private originals)`, resource: "r2", method: "POST", path: `${acct}/r2/buckets`, body: { name: n.originalsBucket }, rollback: { method: "DELETE", path: `${acct}/r2/buckets/${n.originalsBucket}` } }));
  steps.push(api({ id: "r2.public", title: `R2 bucket ${n.publicBucket} (public site files)`, resource: "r2", method: "POST", path: `${acct}/r2/buckets`, body: { name: n.publicBucket }, rollback: { method: "DELETE", path: `${acct}/r2/buckets/${n.publicBucket}` } }));
  steps.push(api({ id: "kv.create", title: `KV namespace ${n.kv}`, resource: "kv", method: "POST", path: `${acct}/storage/kv/namespaces`, body: { title: n.kv }, captures: { kv_id: "result.id" }, rollback: { method: "DELETE", path: `${acct}/storage/kv/namespaces/{{kv_id}}` } }));
  steps.push(api({ id: "queue.create", title: `Queue ${n.queue}`, resource: "queue", method: "POST", path: `${acct}/queues`, body: { queue_name: n.queue }, captures: { queue_id: "result.queue_id" }, rollback: { method: "DELETE", path: `${acct}/queues/{{queue_id}}` } }));
  steps.push(api({ id: "worker.public", title: `Worker script ${n.publicScript}`, resource: "worker", kind: "worker_upload", method: "PUT", path: `${acct}/workers/scripts/${n.publicScript}`, body: {
    bundle: "public_site",
    metadata: {
      main_module: "index.mjs",
      compatibility_date: "2026-08-01",
      bindings: [
        { type: "r2_bucket", name: "PUBLIC_BUCKET", bucket_name: n.publicBucket },
        { type: "kv_namespace", name: "CACHE", namespace_id: "{{kv_id}}" },
      ],
    },
  }, rollback: { method: "DELETE", path: `${acct}/workers/scripts/${n.publicScript}` } }));
  steps.push(api({ id: "worker.workspace", title: `Worker script ${n.workspaceScript}`, resource: "worker", kind: "worker_upload", method: "PUT", path: `${acct}/workers/scripts/${n.workspaceScript}`, body: {
    bundle: "workspace",
    metadata: {
      main_module: "index.mjs",
      compatibility_date: "2026-08-01",
      compatibility_flags: ["nodejs_compat"],
      bindings: [
        { type: "d1", name: "DB", id: "{{d1_id}}" },
        { type: "r2_bucket", name: "ORIGINALS", bucket_name: n.originalsBucket },
        { type: "r2_bucket", name: "PUBLIC_BUCKET", bucket_name: n.publicBucket },
        { type: "kv_namespace", name: "CACHE", namespace_id: "{{kv_id}}" },
        { type: "queue", name: "JOBS", queue_name: n.queue },
      ],
    },
  }, rollback: { method: "DELETE", path: `${acct}/workers/scripts/${n.workspaceScript}` } }));
  steps.push(api({ id: "d1.migrate", title: "Run D1 migrations", resource: "d1", kind: "d1_query", method: "POST", path: `${acct}/d1/database/{{d1_id}}/query`, body: { sql: "{{migrations}}" }, rollback: null }));
  // The seed SQL carries organizer-typed text (names, request drafts), so it is literal: a
  // draft containing "{{generated.runner_token}}" must never be replaced with the real token.
  steps.push(api({ id: "d1.seed", title: "Seed campaign, agencies and request drafts", resource: "d1", kind: "d1_query", method: "POST", path: `${acct}/d1/database/{{d1_id}}/query`, body: { sql: seedSql(state) }, literal_body: true, rollback: null }));
  steps.push(api({ id: "dns.public", title: `DNS ${n.publicHostname} -> Workers (AAAA 100::, proxied)`, resource: "dns", method: "POST", path: `${zone}/dns_records`, body: { type: "AAAA", name: n.publicHostname, content: "100::", proxied: true, comment: "deflock public site" }, captures: { dns_public_id: "result.id" }, rollback: { method: "DELETE", path: `${zone}/dns_records/{{dns_public_id}}` } }));
  steps.push(api({ id: "dns.workspace", title: `DNS ${n.workspaceHostname} -> Workers (AAAA 100::, proxied)`, resource: "dns", method: "POST", path: `${zone}/dns_records`, body: { type: "AAAA", name: n.workspaceHostname, content: "100::", proxied: true, comment: "deflock workspace" }, captures: { dns_workspace_id: "result.id" }, rollback: { method: "DELETE", path: `${zone}/dns_records/{{dns_workspace_id}}` } }));
  steps.push(api({ id: "route.public", title: `Route ${n.publicHostname}/* -> ${n.publicScript}`, resource: "route", method: "POST", path: `${zone}/workers/routes`, body: { pattern: `${n.publicHostname}/*`, script: n.publicScript }, captures: { route_public_id: "result.id" }, rollback: { method: "DELETE", path: `${zone}/workers/routes/{{route_public_id}}` } }));
  steps.push(api({ id: "route.workspace", title: `Route ${n.workspaceHostname}/* -> ${n.workspaceScript}`, resource: "route", method: "POST", path: `${zone}/workers/routes`, body: { pattern: `${n.workspaceHostname}/*`, script: n.workspaceScript }, captures: { route_workspace_id: "result.id" }, rollback: { method: "DELETE", path: `${zone}/workers/routes/{{route_workspace_id}}` } }));
  steps.push(api({ id: "access.app", title: `Access application for ${n.workspaceHostname}`, resource: "access", method: "POST", path: `${acct}/access/apps`, body: { name: n.accessAppName, domain: n.workspaceHostname, type: "self_hosted", session_duration: "24h", auto_redirect_to_identity: false }, captures: { access_app_id: "result.id", access_aud: "result.aud" }, rollback: { method: "DELETE", path: `${acct}/access/apps/{{access_app_id}}` } }));
  steps.push(api({ id: "access.policy", title: `Access policy: allow ${state.campaign.organizer_email}`, resource: "access", method: "POST", path: `${acct}/access/apps/{{access_app_id}}/policies`, body: { name: "organizers", decision: "allow", include: [{ email: { email: state.campaign.organizer_email } }] }, captures: { access_policy_id: "result.id" }, rollback: null }));
  if (state.accounts.mailbox.mode === "email_routing") {
    steps.push(api({ id: "email.enable", title: "Enable Email Routing on the zone", resource: "email", method: "POST", path: `${zone}/email/routing/enable`, body: null, rollback: { method: "POST", path: `${zone}/email/routing/disable` } }));
    steps.push(api({ id: "email.rule", title: `Email Routing rule ${n.mailAddress} -> ${n.workspaceScript}`, resource: "email", method: "POST", path: `${zone}/email/routing/rules`, body: { name: "deflock requests inbox", enabled: true, matchers: [{ type: "literal", field: "to", value: n.mailAddress }], actions: [{ type: "worker", value: [n.workspaceScript] }] }, captures: { email_rule_id: "result.id" }, rollback: { method: "DELETE", path: `${zone}/email/routing/rules/{{email_rule_id}}` } }));
  }
  const secret = (name: string, value: string, title: string) =>
    api({ id: `secret.${name}`, title, resource: "secret", kind: "secret", method: "PUT", path: `${acct}/workers/scripts/${n.workspaceScript}/secrets`, body: { name, text: value, type: "secret_text" }, redact: ["text"], rollback: null });
  steps.push(secret("CAMPAIGN_ID", state.campaign_id, "Secret CAMPAIGN_ID"));
  steps.push(secret("ACCESS_TEAM_DOMAIN", state.accounts.cloudflare.access_team, "Secret ACCESS_TEAM_DOMAIN"));
  steps.push(secret("ACCESS_AUD", "{{access_aud}}", "Secret ACCESS_AUD (from the Access application)"));
  steps.push(secret("RUNNER_TOKEN", "{{generated.runner_token}}", "Secret RUNNER_TOKEN (generated)"));
  steps.push(secret("DOWNLOAD_SIGNING_KEY", "{{generated.download_key}}", "Secret DOWNLOAD_SIGNING_KEY (generated)"));
  if (state.accounts.mailbox.mode === "imap_smtp") {
    steps.push(secret("IMAP_HOST", state.accounts.mailbox.imap_host ?? "", "Secret IMAP_HOST"));
    steps.push(secret("IMAP_USER", state.accounts.mailbox.imap_user ?? "", "Secret IMAP_USER"));
    steps.push(secret("IMAP_PASSWORD", state.accounts.mailbox.imap_password ?? "", "Secret IMAP_PASSWORD"));
    steps.push(secret("SMTP_HOST", state.accounts.mailbox.smtp_host ?? "", "Secret SMTP_HOST"));
    steps.push(secret("SMTP_USER", state.accounts.mailbox.smtp_user ?? "", "Secret SMTP_USER"));
    steps.push(secret("SMTP_PASSWORD", state.accounts.mailbox.smtp_password ?? "", "Secret SMTP_PASSWORD"));
  }
  if (state.accounts.brevo) {
    steps.push(secret("BREVO_API_KEY", state.accounts.brevo.api_key, "Secret BREVO_API_KEY"));
    steps.push(secret("BREVO_LIST_ID", state.accounts.brevo.list_id, "Secret BREVO_LIST_ID"));
  }
  if (state.accounts.model) {
    steps.push(secret("MODEL_BASE_URL", state.accounts.model.base_url, "Secret MODEL_BASE_URL"));
    steps.push(secret("MODEL_API_KEY", state.accounts.model.api_key, "Secret MODEL_API_KEY"));
    steps.push(secret("MODEL_ID", state.accounts.model.model_id, "Secret MODEL_ID"));
  }
  steps.push(api({ id: "cron.workspace", title: `Cron triggers ${crons.join(" ; ")}`, resource: "cron", method: "PUT", path: `${acct}/workers/scripts/${n.workspaceScript}/schedules`, body: crons.map((cron) => ({ cron })), rollback: { method: "PUT", path: `${acct}/workers/scripts/${n.workspaceScript}/schedules`, body: [] } }));
  return steps;
}

/** Plan as shown to the organizer and stored in the receipt: secret values replaced. */
export function redactStep(step: PlanStep): PlanStep {
  if (!step.redact.length || !step.body || typeof step.body !== "object") return step;
  const body = { ...(step.body as Record<string, unknown>) };
  for (const k of step.redact) if (k in body) body[k] = "[redacted]";
  return { ...step, body };
}

export function resolvePlaceholders<T>(value: T, outputs: Record<string, string>): T {
  if (typeof value === "string") {
    return value.replace(/\{\{([a-z0-9_.]+)\}\}/g, (m, key: string) => (key in outputs ? outputs[key] : m)) as unknown as T;
  }
  if (Array.isArray(value)) return value.map((v) => resolvePlaceholders(v, outputs)) as unknown as T;
  if (value && typeof value === "object") {
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(value as Record<string, unknown>)) out[k] = resolvePlaceholders(v, outputs);
    return out as T;
  }
  return value;
}

export function unresolved(value: unknown): string[] {
  const found: string[] = [];
  const walk = (v: unknown) => {
    if (typeof v === "string") for (const m of v.matchAll(/\{\{([a-z0-9_.]+)\}\}/g)) found.push(m[1]);
    else if (Array.isArray(v)) v.forEach(walk);
    else if (v && typeof v === "object") Object.values(v).forEach(walk);
  };
  walk(value);
  return found;
}
