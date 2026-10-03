import path from "node:path";
import { cloudflareTest, readD1Migrations } from "@cloudflare/vitest-plugin";
import { defineConfig } from "vitest/config";

export default defineConfig(async () => {
  const migrations = await readD1Migrations(path.join(import.meta.dirname, "migrations"));
  return {
    plugins: [
      cloudflareTest({
        wrangler: { configPath: "./wrangler.jsonc" },
        miniflare: {
          bindings: {
            TEST_MIGRATIONS: migrations,
            CAMPAIGN_ID: "01test0000000000000000000a",
            ACCESS_TEAM_DOMAIN: "example-team",
            ACCESS_AUD: "test-aud-0123456789abcdef",
            RUNNER_TOKEN: "test-runner-token-0123456789abcdef",
            DOWNLOAD_SIGNING_KEY: "test-download-signing-key",
          },
        },
      }),
    ],
    test: { include: ["test/**/*.test.ts"], setupFiles: ["./test/setup.ts"] },
  };
});
