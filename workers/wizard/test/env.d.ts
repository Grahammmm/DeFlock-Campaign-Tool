import type { Env as WizardEnv } from "../src/env.ts";
declare global {
  namespace Cloudflare {
    interface Env extends WizardEnv {}
  }
}
export {};
