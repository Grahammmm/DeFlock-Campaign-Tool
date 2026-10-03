import type { Env as WorkspaceEnv } from "../src/env.ts";

declare global {
  namespace Cloudflare {
    interface Env extends WorkspaceEnv {
      TEST_MIGRATIONS: import("@cloudflare/vitest-plugin").D1Migration[];
    }
  }
}
export {};
