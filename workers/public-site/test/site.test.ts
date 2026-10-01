import { createExecutionContext, env } from "cloudflare:test";
import { describe, expect, it } from "vitest";
import worker, { normalizePath, parseHeaders } from "../src/index.ts";

const HEADERS = "/*\n  Content-Security-Policy: default-src 'none'; style-src 'self'\n  X-Test: yes\n/feed.xml\n  Content-Type: application/atom+xml\n";

describe("public site", () => {
  it("503 before any version is approved", async () => {
    const res = await worker.fetch(new Request("https://campaign.example.invalid/"), env, createExecutionContext());
    expect(res.status).toBe(503);
  });

  it("serves the approved version with the _headers CSP and 404 fallback", async () => {
    await env.CACHE.put("site_version", "v1");
    await env.PUBLIC_BUCKET.put("sites/v1/index.html", "<h1>hi</h1>", { httpMetadata: { contentType: "text/html" } });
    await env.PUBLIC_BUCKET.put("sites/v1/404.html", "nope", { httpMetadata: { contentType: "text/html" } });
    await env.PUBLIC_BUCKET.put("sites/v1/_headers", HEADERS);
    const ok = await worker.fetch(new Request("https://campaign.example.invalid/"), env, createExecutionContext());
    expect(ok.status).toBe(200);
    expect(await ok.text()).toBe("<h1>hi</h1>");
    expect(ok.headers.get("content-security-policy")).toBe("default-src 'none'; style-src 'self'");
    expect(ok.headers.get("x-site-version")).toBe("v1");
    const missing = await worker.fetch(new Request("https://campaign.example.invalid/findings/none"), env, createExecutionContext());
    expect(missing.status).toBe(404);
    expect(await missing.text()).toBe("nope");
  });

  it("path normalization and header parsing", () => {
    expect(normalizePath("/")).toBe("/index.html");
    expect(normalizePath("/findings/")).toBe("/findings/index.html");
    expect(normalizePath("/about")).toBe("/about.html");
    expect(normalizePath("/../x")).toBe("/404.html");
    expect(parseHeaders(HEADERS, "/feed.xml")["content-type"]).toBe("application/atom+xml");
    expect(parseHeaders(HEADERS, "/index.html")["x-test"]).toBe("yes");
  });
});
