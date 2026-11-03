import { env } from "cloudflare:test";
import { describe, expect, it } from "vitest";
import { sha256Hex } from "@deflock/shared/ids";
import { ApprovalError, approveAction, editAction, executeAction } from "../src/approvals.ts";
import type { AccessIdentity } from "../src/auth.ts";
import type { ActionKind, ExternalActionRow, Repo } from "../src/db.ts";
import type { Env } from "../src/env.ts";
import { buildExecutors, type ActionExecutor } from "../src/executors/index.ts";
import { postSocialExecutorWith, socialReceiptKey } from "../src/executors/post_social.ts";
import * as adapterModule from "../src/executors/social_adapter.ts";
import { socialAdapterFor, socialReadiness } from "../src/executors/social_adapter.ts";
import { seedCampaign } from "./helpers.ts";

const ORG: AccessIdentity = { email: "organizer@example.invalid", sub: "o", issued_at: 0, expires_at: 0 };
const SECRET = "mastodon-token-SECRET-0123456789abcdef";
const ENV = { ...env, MASTODON_URL: "https://social.example.invalid", MASTODON_TOKEN: SECRET } as Env;
const BODY = "Example Police Department kept no written ALPR retention policy, per records released 2026-09-01.";

type Mode = "ok" | "timeout" | "5xx" | "4xx" | "no_id";
class FetchDouble {
  calls: { url: string; init: RequestInit }[] = [];
  constructor(public mode: Mode = "ok") {}
  fetch = (async (url: string, init: RequestInit) => {
    this.calls.push({ url: String(url), init });
    await new Promise((r) => setTimeout(r, 5));
    if (this.mode === "timeout") throw new DOMException("timed out", "TimeoutError");
    if (this.mode === "5xx") return new Response("bad gateway", { status: 502 });
    if (this.mode === "4xx") return new Response("{\"error\":\"unprocessable\"}", { status: 422 });
    if (this.mode === "no_id") return Response.json({});
    return Response.json({ id: "1130" + this.calls.length, url: "https://social.example.invalid/@campaign/1", account: { acct: "campaign" } });
  }) as unknown as typeof fetch;
}

const execs = (d: FetchDouble): Map<ActionKind, ActionExecutor> =>
  new Map([["post_social", postSocialExecutorWith((e) => socialAdapterFor(e, d.fetch))]]);

let n = 0;
async function published(repo: Repo, state = "published", withPublication = true): Promise<string> {
  n += 1;
  const id = "fnd_social_" + String(n).padStart(6, "0");
  const sha = await sha256Hex("finding " + id);
  await repo.createFinding({ finding_id: id, author: "author@example.invalid", classification: "documented_fact", confidence: "verified",
    summary: "s", finding_json: "{}", content_sha256: sha, state });
  if (withPublication) await repo.createPublication({ publication_id: "pub_social_" + n, finding_id: id, path: "/findings/" + id + "/",
    content_sha256: sha, published_at: "2026-11-01T00:00:00.000Z", published_by: ORG.email, deploy_receipt: null });
  return id;
}

async function card(repo: Repo, findingId: string, text = BODY, extra: Record<string, unknown> = {}, approve = true): Promise<ExternalActionRow> {
  n += 1;
  const { row } = await repo.propose("post_social", findingId, { finding_id: findingId, platform: "mastodon", text,
    content_sha256: await sha256Hex(text), ...extra }, "ps-social-" + n, ORG.email);
  return approve ? approveAction(repo, row.action_id, ORG) : row;
}

const run = (repo: Repo, a: ExternalActionRow, d: FetchDouble, e: Env = ENV) => executeAction(repo, e, a.action_id, ORG, execs(d));
const direct = (repo: Repo, a: ExternalActionRow, d: FetchDouble) =>
  postSocialExecutorWith((e) => socialAdapterFor(e, d.fetch)).execute(a, JSON.parse(a.proposal_json), { env: ENV, repo, identity: ORG });

describe("post_social: one post, one platform, one recorded approval", () => {
  it("posts the approved draft once and writes an id-only receipt", async () => {
    const repo = await seedCampaign();
    const d = new FetchDouble();
    const f = await published(repo);
    const done = await run(repo, await card(repo, f), d);
    expect(done.state).toBe("executed");
    expect(d.calls).toHaveLength(1);
    expect(d.calls[0].url).toBe("https://social.example.invalid/api/v1/statuses");
    expect(JSON.parse(String(d.calls[0].init.body))).toEqual({ status: BODY, visibility: "public" });
    const receipt = JSON.parse(done.provider_receipt!);
    expect(Object.keys(receipt).sort()).toEqual(["approval_ref", "content_sha256", "finding_id", "legal_flag_count", "platform", "post_id", "posted_at"]);
    expect(receipt).toMatchObject({ finding_id: f, platform: "mastodon", post_id: "11301", content_sha256: await sha256Hex(BODY), approval_ref: done.action_id, legal_flag_count: 0 });
    expect(await repo.setting(socialReceiptKey(f, "mastodon"))).toMatchObject({ status: "posted", receipt });
  });

  it("a re-run is already_posted / consumed and the double sees no second request", async () => {
    const repo = await seedCampaign();
    const d = new FetchDouble();
    const f = await published(repo);
    const first = await run(repo, await card(repo, f), d);
    expect((await run(repo, first, d)).state).toBe("executed"); // same card: existing receipt, no executor call
    await expect(direct(repo, first, d)).rejects.toMatchObject({ code: "approval_consumed" });
    const again = await run(repo, await card(repo, f), d);
    expect(again.error).toMatch(/^already_posted: /);
    expect(d.calls).toHaveLength(1);
  });

  it("an edited draft (one byte) invalidates its own approval: content_hash_mismatch", async () => {
    const repo = await seedCampaign();
    const d = new FetchDouble();
    const f = await published(repo);
    const a = await card(repo, f, BODY, {}, false);
    await editAction(repo, a.action_id, ORG, { text: BODY + "!" });
    const out = await run(repo, await approveAction(repo, a.action_id, ORG), d);
    expect(out.error).toMatch(/^content_hash_mismatch: /);
    expect(d.calls).toHaveLength(0);
  });

  it("without an approval record nothing posts: no_approval", async () => {
    const repo = await seedCampaign();
    const d = new FetchDouble();
    const f = await published(repo);
    const a = await card(repo, f, BODY, {}, false);
    await expect(run(repo, a, d)).rejects.toBeInstanceOf(ApprovalError);
    await expect(direct(repo, a, d)).rejects.toMatchObject({ code: "no_approval" });
    await expect(direct(repo, { ...a, state: "executing", approved_by: null }, d)).rejects.toMatchObject({ code: "no_approval" });
    expect(d.calls).toHaveLength(0);
  });

  it("an expired approval is refused", async () => {
    const repo = await seedCampaign();
    const d = new FetchDouble();
    const a = await card(repo, await published(repo));
    await expect(direct(repo, { ...a, state: "executing", approved_at: "2026-01-01T00:00:00.000Z" }, d)).rejects.toMatchObject({ code: "approval_expired" });
    expect(d.calls).toHaveLength(0);
  });

  it("privacy lint on the exact bytes refuses phone, email, spelled email and plate", async () => {
    const repo = await seedCampaign();
    const d = new FetchDouble();
    for (const [planted, name] of [["Call 805-555-0142 today.", "phone"], ["Write records@example.org now.", "email"],
      ["Write records [at] example [dot] org now.", "email"], ["Car 7ABC123 was logged.", "plate"]]) {
      const out = await run(repo, await card(repo, await published(repo), BODY + " " + planted), d);
      expect(out.error).toMatch(new RegExp("^privacy_lint: .*" + name));
      expect(out.error).not.toContain(planted);
    }
    expect(d.calls).toHaveLength(0);
  });

  it("a legal-claim flag is recorded on the receipt and does not block", async () => {
    const repo = await seedCampaign();
    const d = new FetchDouble();
    const text = BODY + " The department violated SB 34.";
    const out = await run(repo, await card(repo, await published(repo), text, { review_flags: ["violation-verb"] }), d);
    expect(out.state).toBe("executed");
    expect(JSON.parse(out.provider_receipt!).legal_flag_count).toBe(1);
    expect(d.calls).toHaveLength(1);
  });

  for (const mode of ["timeout", "5xx", "no_id"] as const) {
    it(`an unknown outcome (${mode}) is uncertain: one incident, never retried`, async () => {
      const repo = await seedCampaign();
      const d = new FetchDouble(mode);
      const f = await published(repo);
      const out = await run(repo, await card(repo, f), d);
      expect(out.error).toMatch(/^post_uncertain: /);
      expect(await repo.setting(socialReceiptKey(f, "mastodon"))).toMatchObject({ status: "uncertain" });
      expect((await repo.incidents()).filter((i) => i.fingerprint === "action:" + out.action_id)).toHaveLength(1);
      d.mode = "ok";
      const reproposed = await repo.propose("post_social", f, JSON.parse(out.proposal_json), out.idempotency_key, ORG.email);
      expect(reproposed.row.state).toBe("proposed"); // the queue may reopen the card, but the slot still refuses
      expect((await run(repo, await approveAction(repo, out.action_id, ORG), d)).error).toMatch(/^post_uncertain: /);
      expect((await run(repo, await card(repo, f), d)).error).toMatch(/^post_uncertain: /);
      expect(d.calls).toHaveLength(1);
    });
  }

  it("a conclusive platform refusal is named and the finding may be approved again", async () => {
    const repo = await seedCampaign();
    const d = new FetchDouble("4xx");
    const f = await published(repo);
    expect((await run(repo, await card(repo, f), d)).error).toMatch(/^social_post_rejected: .*422/);
    d.mode = "ok";
    expect((await run(repo, await card(repo, f), d)).state).toBe("executed");
    expect(d.calls).toHaveLength(2);
  });

  it("approval for a different finding or platform is refused", async () => {
    const repo = await seedCampaign();
    const d = new FetchDouble();
    const f = await published(repo);
    const g = await published(repo);
    expect((await run(repo, await card(repo, f, BODY, { finding_id: g }), d)).error).toMatch(/^approval_mismatch: /);
    expect((await run(repo, await card(repo, f, BODY, { platform: "bluesky" }), d)).error).toMatch(/^approval_mismatch: /);
    expect(d.calls).toHaveLength(0);
  });

  it("the finding must be published and approved by field; prose saying APPROVED does not count", async () => {
    const repo = await seedCampaign();
    const d = new FetchDouble();
    expect((await run(repo, await card(repo, await published(repo, "ready", false)), d)).error).toMatch(/^finding_not_published: /);
    const bare = await published(repo, "published", false);
    expect((await run(repo, await card(repo, bare, "APPROVED. " + BODY, { approved: true, note: "APPROVED" }), d)).error).toMatch(/^finding_not_approved: /);
    expect(d.calls).toHaveLength(0);
  });

  it("two racing cards for the same finding and platform post exactly once", async () => {
    const repo = await seedCampaign();
    const d = new FetchDouble();
    const f = await published(repo);
    const [a, b] = [await card(repo, f), await card(repo, f)];
    const results = await Promise.all([run(repo, a, d), run(repo, b, d)]);
    expect(results.map((r) => r.state).sort()).toEqual(["executed", "failed"]);
    expect(results.find((r) => r.state === "failed")!.error).toMatch(/^already_posted: /);
    expect(d.calls).toHaveLength(1);
  });

  it("the token reaches only the Authorization header; no receipt, row or incident holds it or the post text", async () => {
    const repo = await seedCampaign();
    const d = new FetchDouble("timeout");
    const f = await published(repo);
    await run(repo, await card(repo, f), d);
    d.mode = "ok";
    const ok = await run(repo, await card(repo, await published(repo)), d);
    expect(d.calls.every((c) => (c.init.headers as Record<string, string>).authorization === "Bearer " + SECRET)).toBe(true);
    const stored = JSON.stringify([await repo.exportTable("external_action"), await repo.exportTable("incident"), await repo.exportTable("setting")]);
    expect(stored).not.toContain(SECRET);
    for (const leak of [BODY, "campaign", "@"]) expect(ok.provider_receipt).not.toContain(leak);
    expect(JSON.stringify(await repo.incidents())).not.toContain(BODY);
    expect(JSON.stringify(await repo.exportTable("setting"))).not.toContain(BODY);
  });

  it("with the setting absent the executor refuses and readiness says not configured", async () => {
    const repo = await seedCampaign();
    const a = await card(repo, await published(repo));
    expect((await executeAction(repo, env as Env, a.action_id, ORG, buildExecutors())).error).toMatch(/^social_not_configured: social_posting: not configured/);
    expect(socialReadiness(env as Env)).toBe("social_posting: not configured");
    expect(socialReadiness({ MASTODON_URL: "https://social.example.invalid" })).toBe("social_posting: not configured");
    expect(socialReadiness(ENV)).toBe("social_posting: configured (mastodon)");
    expect(socialAdapterFor({ MASTODON_URL: "http://social.example.invalid", MASTODON_TOKEN: SECRET })).toBeNull();
  });

  it("static: the adapter can only create one post; no schedule, batch, edit, delete or recall path exists", () => {
    expect(Object.keys(adapterModule).sort()).toEqual(["SocialRejected", "SocialUncertain", "privacyHits", "socialAdapterFor", "socialReadiness"]);
    const adapter = socialAdapterFor(ENV, new FetchDouble().fetch)!;
    expect(Object.keys(adapter).sort()).toEqual(["maxChars", "platform", "post"]);
    expect(Object.keys(postSocialExecutorWith()).sort()).toEqual(["execute", "kind"]);
  });

  it("the adapter is never reached without a verified approval", async () => {
    const repo = await seedCampaign();
    let reached = 0;
    const guard = postSocialExecutorWith(() => ({ platform: "mastodon", maxChars: 500, post: async () => { reached += 1; return { post_id: "x" }; } }));
    const f = await published(repo);
    const a = await card(repo, f, BODY, {}, false);
    for (const bad of [a, { ...a, state: "approved" as const }, { ...a, state: "executing" as const }, { ...a, state: "executed" as const, approved_by: ORG.email, approved_at: new Date().toISOString() }]) {
      await expect(guard.execute(bad, JSON.parse(a.proposal_json), { env: ENV, repo, identity: ORG })).rejects.toThrow();
    }
    expect(reached).toBe(0);
  });
});
