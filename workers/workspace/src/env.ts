export interface Env {
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
  /** Test hook: replaces global fetch for the Access certificate download. */
  ACCESS_CERTS_JSON?: string;
}

export type ProviderFetch = typeof fetch;
