import { env } from "cloudflare:test";
import { afterEach, describe, expect, it, vi } from "vitest";
import { approveAction, executeAction } from "../src/approvals.ts";
import { defaultExecutors, mailSenderConfigured } from "../src/executors/index.ts";
import type { Env } from "../src/env.ts";
import { call, makeSigner, organizerRequest, seedCampaign } from "./helpers.ts";

// Synthetic values only: presence is all the screen may reveal.
const URL_SETTINGS = "https://outbox.example.invalid/x"; // pragma: allowlist secret -- public synthetic URL
const KEY_SETTINGS = "synthetic-settings-key-not-real-0000000001"; // pragma: allowlist secret -- public synthetic test key
const URL_SEND = "https://outbox.example.invalid/send"; // valid shape for SignedOutboxSender
const KEY_SEND = "synthetic-sender-key-for-tests-only-0000"; // pragma: allowlist secret -- public synthetic test key
const ORG = { email: "organizer@example.invalid", sub: "o", issued_at: 0, expires_at: 0 };
const EXEC = { ...ORG, email: "executor@example.invalid" };

async function settingsPage(overrides: Partial<Env>): Promise<string> {
  await seedCampaign();
  const signer = await makeSigner();
  const r = await organizerRequest(signer, "organizer@example.invalid", "/settings");
  const res = await call(r.request, { ...r.overrides, ...overrides });
  expect(res.status).toBe(200);
  return res.text();
}

function rows(page: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const m of page.matchAll(/<tr><td><code>([A-Z_]+)<\/code><\/td><td><span class="pill (?:ok|bad)">(present|missing)<\/span>/g)) out[m[1]!] = m[2]!;
  return out;
}

const OK_LINE = '<span class="pill ok">configured</span>';
const BAD_LINE = '<span class="pill bad">not configured</span>';

describe("settings secrets table: outbox coverage", () => {
  it("neither outbox secret: both rows missing, line says not configured, table has 9 rows", async () => {
    const page = await settingsPage({ OUTBOX_GATEWAY_URL: undefined, OUTBOX_SIGNING_KEY: undefined });
    const r = rows(page);
    expect(Object.keys(r)).toHaveLength(9);
    expect(r.OUTBOX_GATEWAY_URL).toBe("missing");
    expect(r.OUTBOX_SIGNING_KEY).toBe("missing");
    expect(page).toContain("Request mail delivery: " + BAD_LINE);
    expect(page).toContain("sender_not_configured");
    expect(page).not.toContain(OK_LINE);
  });

  it("only the gateway URL, and only the signing key: one row present, line says not configured", async () => {
    let page = await settingsPage({ OUTBOX_GATEWAY_URL: URL_SETTINGS, OUTBOX_SIGNING_KEY: undefined });
    expect(rows(page)).toMatchObject({ OUTBOX_GATEWAY_URL: "present", OUTBOX_SIGNING_KEY: "missing" });
    expect(page).toContain("Request mail delivery: " + BAD_LINE);
    page = await settingsPage({ OUTBOX_GATEWAY_URL: undefined, OUTBOX_SIGNING_KEY: KEY_SETTINGS });
    expect(rows(page)).toMatchObject({ OUTBOX_GATEWAY_URL: "missing", OUTBOX_SIGNING_KEY: "present" });
    expect(page).toContain("Request mail delivery: " + BAD_LINE);
  });

  it("both set: both present, line says configured, and neither value appears in the page", async () => {
    const page = await settingsPage({ OUTBOX_GATEWAY_URL: URL_SETTINGS, OUTBOX_SIGNING_KEY: KEY_SETTINGS });
    expect(rows(page)).toMatchObject({ OUTBOX_GATEWAY_URL: "present", OUTBOX_SIGNING_KEY: "present" });
    expect(page).toContain("Request mail delivery: " + OK_LINE);
    expect(page).not.toContain(BAD_LINE);
    expect(page).not.toContain(URL_SETTINGS);
    expect(page).not.toContain("outbox.example.invalid");
    expect(page).not.toContain(KEY_SETTINGS);
  });

  it("an empty OUTBOX_SIGNING_KEY or OUTBOX_GATEWAY_URL counts as missing", async () => {
    let page = await settingsPage({ OUTBOX_GATEWAY_URL: URL_SETTINGS, OUTBOX_SIGNING_KEY: "" });
    expect(rows(page)).toMatchObject({ OUTBOX_GATEWAY_URL: "present", OUTBOX_SIGNING_KEY: "missing" });
    expect(page).toContain("Request mail delivery: " + BAD_LINE);
    page = await settingsPage({ OUTBOX_GATEWAY_URL: "", OUTBOX_SIGNING_KEY: KEY_SETTINGS });
    expect(rows(page)).toMatchObject({ OUTBOX_GATEWAY_URL: "missing", OUTBOX_SIGNING_KEY: "present" });
    expect(page).toContain("Request mail delivery: " + BAD_LINE);
  });
});

describe("mailSenderConfigured agrees with defaultExecutors", () => {
  afterEach(() => { vi.restoreAllMocks(); });

  let n = 0;
  async function sendRequestOutcome(shape: Pick<Env, "OUTBOX_GATEWAY_URL" | "OUTBOX_SIGNING_KEY">) {
    const repo = await seedCampaign();
    const request = await repo.createRequest({ agency_id: "ca-example-police", scope_id: "synthetic", scope_version: 2,
      subject: "Synthetic settings agreement " + ++n, body_md: "Synthetic body", channel: "email", fee_cap_cents: 500,
      state: "draft", sent_at: null, determination_due: null, extension_claimed_until: null,
      last_activity_at: null, next_action: null, external_ref: null });
    const { row } = await repo.propose("send_request", request.request_id, { subject: request.subject, body_md: request.body_md,
      channel: "email", to: "records@example.invalid" }, "settings-agree-" + n, ORG.email);
    await approveAction(repo, row.action_id, ORG);
    const fetched = vi.spyOn(globalThis, "fetch").mockRejectedValue(new Error("synthetic network stop"));
    const done = await executeAction(repo, env as Env, row.action_id, EXEC, defaultExecutors(shape as Env));
    return { error: done.error ?? "", fetches: fetched.mock.calls.length };
  }

  const shapes: Array<[string, Pick<Env, "OUTBOX_GATEWAY_URL" | "OUTBOX_SIGNING_KEY">]> = [
    ["neither", {}],
    ["url only", { OUTBOX_GATEWAY_URL: URL_SEND }],
    ["key only", { OUTBOX_SIGNING_KEY: KEY_SEND }],
    ["empty key", { OUTBOX_GATEWAY_URL: URL_SEND, OUTBOX_SIGNING_KEY: "" }],
    ["both", { OUTBOX_GATEWAY_URL: URL_SEND, OUTBOX_SIGNING_KEY: KEY_SEND }],
  ];

  for (const [name, shape] of shapes) {
    it(`${name}: a send_request executor uses the signed sender exactly when the helper is true`, async () => {
      const out = await sendRequestOutcome(shape);
      if (mailSenderConfigured(shape)) {
        expect(out.fetches).toBe(1);
        expect(out.error).not.toMatch(/^sender_not_configured:/);
      } else {
        expect(out.fetches).toBe(0);
        expect(out.error).toMatch(/^sender_not_configured:/);
      }
    });
  }

  it("the helper itself: undefined env and each partial shape are false, both set is true", () => {
    expect(mailSenderConfigured(undefined)).toBe(false);
    expect(mailSenderConfigured({})).toBe(false);
    expect(mailSenderConfigured({ OUTBOX_GATEWAY_URL: URL_SEND })).toBe(false);
    expect(mailSenderConfigured({ OUTBOX_GATEWAY_URL: URL_SEND, OUTBOX_SIGNING_KEY: "" })).toBe(false);
    expect(mailSenderConfigured({ OUTBOX_GATEWAY_URL: URL_SEND, OUTBOX_SIGNING_KEY: KEY_SEND })).toBe(true);
  });
});
