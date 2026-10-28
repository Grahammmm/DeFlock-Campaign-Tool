export interface Env {
  SESSIONS: KVNamespace;
  /** 32 bytes as 64 hex chars; AES-GCM key for session blobs. */
  SESSION_KEY: string;
  /** "1": record every Cloudflare API call without performing it. */
  DRY_RUN?: string;
  /** Override the Cloudflare API base (tests). */
  CLOUDFLARE_API_BASE?: string;
}
