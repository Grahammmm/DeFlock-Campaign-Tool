export interface Env {
  /** Optional HTTPS navigation to guided setup; never provisions from the workspace. */
  SETUP_URL?: string;
  DB: D1Database;
  ORIGINALS: R2Bucket;
  PUBLIC_BUCKET: R2Bucket;
  CACHE: KVNamespace;
  JOBS?: Queue<unknown>;
  CAMPAIGN_ID: string;
  ACCESS_TEAM_DOMAIN: string; // <team> in <team>.cloudflareaccess.com
  ACCESS_AUD: string;
  RUNNER_TOKEN: string;
  DOWNLOAD_SIGNING_KEY: string;
  /** Brevo v3 API key (Worker secret). Absent: send_newsletter cards fail with brevo_not_configured. */
  BREVO_API_KEY?: string;
  /** HMAC secret for POST /api/webhooks/subscribe (Worker secret). Absent: every delivery is refused not_configured. */
  SUBSCRIBE_WEBHOOK_SECRET?: string;
  /** Opt-in private TLS outbox endpoint and independent signing secret. Both required. */
  OUTBOX_GATEWAY_URL?: string;
  OUTBOX_SIGNING_KEY?: string;
  /** Optional post_social platform (Mastodon instance https URL) plus its access-token Worker secret. Absent: social_not_configured. */
  MASTODON_URL?: string;
  MASTODON_TOKEN?: string;
  /** Optional Legistar events feed URLs (https, whitespace/comma separated) polled inline on each cron slot. Absent: legistar_poll: not configured. */
  LEGISTAR_FEEDS?: string;
  /** Test hook: replaces global fetch for the Access certificate download. */
  ACCESS_CERTS_JSON?: string;
}

export type ProviderFetch = typeof fetch;
