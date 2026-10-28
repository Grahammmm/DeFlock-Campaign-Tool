// Wizard state: everything the seven screens collect. Only screen 5 holds credentials, and
// `stripSecrets` removes them at handoff.
import type { AgencyKind, Location, SuggestedAgency } from "@deflock/shared/agencies";
import type { PlanStep, Receipt, RollbackStep } from "./plan.ts";

export type PrivacyTier = "redacted_cloud" | "strict_local";
export type Channel = "email" | "muckrock" | "portal_manual";

export interface CampaignSettings {
  name: string;
  tagline: string;
  domain: string;
  privacy_tier: PrivacyTier;
  schedule_cron: string; // ';'-separated cron expressions
  timezone: string;
  organizer_email: string;
}

export interface AgencyChoice {
  agency_id: string;
  kind: AgencyKind;
  name: string;
  selected: boolean;
}

export interface RequestDraft {
  agency_id: string;
  subject: string;
  body_md: string;
  channel: Channel;
  fee_cap_cents: number;
}

export interface CloudflareAccount {
  api_token: string;
  account_id: string;
  zone_id: string;
  access_team: string; // <team>.cloudflareaccess.com
}

export type MailboxMode = "email_routing" | "imap_smtp";

export interface Mailbox {
  mode: MailboxMode;
  address: string; // requests@<domain>
  imap_host?: string;
  imap_user?: string;
  imap_password?: string;
  smtp_host?: string;
  smtp_user?: string;
  smtp_password?: string;
}

export interface Brevo {
  api_key: string;
  list_id: string;
}

export interface ModelProvider {
  base_url: string;
  api_key: string;
  model_id: string;
}

export interface Accounts {
  cloudflare: CloudflareAccount;
  mailbox: Mailbox;
  brevo: Brevo | null;
  model: ModelProvider | null;
}

export interface DeployResult {
  dry_run: boolean;
  status: "applied" | "failed" | "rolled_back";
  receipts: Receipt[];
  rollback: RollbackStep[];
  failed_step: string | null;
  error: string | null;
  finished_at: string;
  outputs: Record<string, string>;
}

export interface WizardState {
  campaign_id: string;
  step: number;
  jurisdiction: string;
  location: Location | null;
  suggested: SuggestedAgency[];
  campaign: CampaignSettings | null;
  agencies: AgencyChoice[];
  requests: Record<string, RequestDraft>;
  accounts: Accounts | null;
  plan: PlanStep[] | null;
  deploy: DeployResult | null;
  handoff_at: string | null;
}

export function emptyState(campaignId: string): WizardState {
  return {
    campaign_id: campaignId,
    step: 1,
    jurisdiction: "us-ca",
    location: null,
    suggested: [],
    campaign: null,
    agencies: [],
    requests: {},
    accounts: null,
    plan: null,
    deploy: null,
    handoff_at: null,
  };
}

export function slugify(text: string): string {
  return text.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 40) || "campaign";
}

/** Resource-name prefix from the campaign domain (or name). */
export function campaignSlug(campaign: CampaignSettings): string {
  return slugify(campaign.domain.split(".")[0] || campaign.name);
}

export function isValidDomain(d: string): boolean {
  return /^(?=.{4,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$/.test(d);
}

export function isValidEmail(e: string): boolean {
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(e);
}

export function isValidCron(expr: string): boolean {
  return expr.split(";").every((c) => /^\s*(\S+\s+){4}\S+\s*$/.test(c));
}

/** strict_local: the model endpoint must be loopback or Tailscale (100.64.0.0/10 or *.ts.net). */
export function isStrictLocalUrl(raw: string): boolean {
  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    return false;
  }
  if (!/^https?:$/.test(url.protocol)) return false;
  const host = url.hostname.replace(/^\[|\]$/g, "").toLowerCase();
  if (host === "localhost" || host === "::1" || host.endsWith(".ts.net")) return true;
  const v4 = /^(\d+)\.(\d+)\.(\d+)\.(\d+)$/.exec(host);
  if (v4) {
    const [a, b] = [Number(v4[1]), Number(v4[2])];
    if (a === 127) return true;
    if (a === 100 && b >= 64 && b <= 127) return true; // CGNAT range used by Tailscale
  }
  return false;
}

/** Exact minimum Cloudflare API token permissions the wizard needs. */
export const CLOUDFLARE_TOKEN_SCOPES: { scope: string; why: string }[] = [
  { scope: "Account / Workers Scripts / Edit", why: "upload the workspace and public-site Workers, secrets and cron triggers" },
  { scope: "Account / Workers KV Storage / Edit", why: "create the cache namespace" },
  { scope: "Account / Workers R2 Storage / Edit", why: "create the originals and public buckets" },
  { scope: "Account / D1 / Edit", why: "create the database and run migrations" },
  { scope: "Account / Queues / Edit", why: "create the jobs queue" },
  { scope: "Account / Access: Apps and Policies / Edit", why: "protect the workspace hostname with an Access application and organizer policy" },
  { scope: "Zone / Zone / Read", why: "confirm the zone id matches the domain" },
  { scope: "Zone / DNS / Edit", why: "add the workspace and public hostnames" },
  { scope: "Zone / Workers Routes / Edit", why: "route the hostnames to the Workers" },
  { scope: "Zone / Email Routing Rules / Edit", why: "deliver requests@<domain> to the workspace Worker" },
];

export interface CostLine {
  item: string;
  basis: string;
  note: string;
}

/** Neutral cost table; no capacity or price guarantees. Verify against current provider pages. */
export const COST_TABLE: CostLine[] = [
  { item: "Domain registration", basis: "per year, registrar", note: "required; the organizer owns it" },
  { item: "Cloudflare Workers (wizard, workspace, public site)", basis: "requests / CPU time", note: "free tier may cover a small campaign; paid plan needed for cron-heavy or high-traffic use" },
  { item: "Cloudflare D1", basis: "rows read/written, storage", note: "campaign state; small" },
  { item: "Cloudflare R2", basis: "storage, class A/B operations", note: "originals and public site; egress from R2 to Workers is free" },
  { item: "Cloudflare KV and Queues", basis: "operations", note: "cache, jobs; Queues requires the Workers Paid plan" },
  { item: "Cloudflare Access", basis: "per user", note: "free for up to a small number of users at the time of writing; verify" },
  { item: "Mailbox (Email Routing inbound is free; IMAP/SMTP provider)", basis: "per mailbox", note: "outbound sends need SMTP or a provider; Email Routing alone cannot send" },
  { item: "Newsletter provider (Brevo)", basis: "contacts / sends", note: "optional; subscriber identities stay with the provider" },
  { item: "Model provider", basis: "tokens", note: "redacted_cloud only; strict_local uses your own hardware" },
  { item: "Records fees", basis: "per request, agency direct cost", note: "capped per request by the fee cap; pay_fee needs approval" },
];

export function stripSecrets(state: WizardState): WizardState {
  return { ...state, accounts: null };
}

export const STEP_TITLES = ["Location", "Campaign", "Agencies", "Requests", "Accounts", "Deploy", "Handoff"] as const;
