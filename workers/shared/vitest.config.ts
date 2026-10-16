import { cloudflareTest } from "@cloudflare/vitest-plugin";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [cloudflareTest({ miniflare: { compatibilityDate: "2026-08-01", compatibilityFlags: ["nodejs_compat"] } })],
  test: { include: ["test/**/*.test.ts"] },
});
