import { cloudflareTest } from "@cloudflare/vitest-plugin";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [
    cloudflareTest({
      wrangler: { configPath: "./wrangler.jsonc" },
      miniflare: { bindings: { SESSION_KEY: "11".repeat(32), DRY_RUN: "1" } },
    }),
  ],
  test: { include: ["test/**/*.test.ts"] },
});
