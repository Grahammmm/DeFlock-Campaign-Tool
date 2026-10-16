import type { Env as WorkspaceEnv } from "../src/env.ts";

declare global {
  namespace Cloudflare {
    interface Env extends WorkspaceEnv {
      DB_UPGRADE: D1Database;
      DB_SCHEMA: D1Database;
      TEST_SCHEMA: string[];
      TEST_MIGRATIONS: import("@cloudflare/vitest-plugin").D1Migration[];
    }
  }
}
export {};
