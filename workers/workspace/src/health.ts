// /healthz body. Anonymous at the app level (registered before the Access middleware), so
// it carries booleans, one count and one fixed word only: never a secret name or value, an
// email, a host or an id.
import { Repo } from "./db.ts";
import type { Env } from "./env.ts";
import { mailSenderConfigured } from "./executors/index.ts";

export interface Health {
  ok: boolean;
  campaign: boolean;
  d1_ok: boolean;
  migrations_applied: number;
  access_configured: boolean;
  outbound: "off" | "on";
}

/** "on" only when mail or posts could really leave: the campaign switch is open AND a sender secret exists. */
function senderConfigured(env: Env): boolean {
  return mailSenderConfigured(env) || Boolean(env.BREVO_API_KEY) || Boolean(env.MASTODON_URL && env.MASTODON_TOKEN);
}

export async function healthReport(env: Env): Promise<{ status: 200 | 503; body: Health }> {
  const access_configured = Boolean(env.ACCESS_TEAM_DOMAIN?.trim() && env.ACCESS_AUD?.trim());
  let d1_ok = false;
  let migrations_applied = 0;
  let campaignRow = null;
  try {
    await env.DB.prepare("SELECT 1").first();
    // wrangler records applied migrations in d1_migrations; a database never migrated has no such table.
    const t = await env.DB.prepare("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'd1_migrations'").first();
    if (t) {
      const r = await env.DB.prepare("SELECT COUNT(*) AS n FROM d1_migrations").first<{ n: number }>();
      migrations_applied = Number(r?.n ?? 0);
    }
    campaignRow = env.CAMPAIGN_ID ? await new Repo(env.DB, env.CAMPAIGN_ID).campaign() : null;
    d1_ok = true;
  } catch {
    d1_ok = false;
    migrations_applied = 0;
  }
  const outbound = campaignRow?.external_sends === "approval_required" && senderConfigured(env) ? "on" : "off";
  return {
    status: d1_ok ? 200 : 503,
    body: { ok: d1_ok, campaign: Boolean(env.CAMPAIGN_ID), d1_ok, migrations_applied, access_configured, outbound },
  };
}
