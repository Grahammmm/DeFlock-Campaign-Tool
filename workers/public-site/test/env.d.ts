import type { Env as SiteEnv } from "../src/index.ts";
declare global {
  namespace Cloudflare {
    interface Env extends SiteEnv {}
  }
}
export {};
