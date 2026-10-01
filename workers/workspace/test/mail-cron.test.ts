import { env } from "cloudflare:test";
import { describe, expect, it } from "vitest";
import { runScheduled } from "../src/cron.ts";
import type { Env } from "../src/env.ts";
import { handleInboundMail } from "../src/mail.ts";
import { seedCampaign } from "./helpers.ts";

function mail(id: string, subject: string) {
  const raw = `Message-ID: <${id}>\r\nFrom: records@example.invalid\r\nTo: requests@campaign.example.invalid\r\nSubject: ${subject}\r\nDate: Wed, 30 Sep 2026 10:00:00 +0000\r\n\r\nSynthetic body ${id}\r\n`;
  return { from: "records@example.invalid", to: "requests@campaign.example.invalid", headers: new Headers({ "message-id": `<${id}>`, subject, date: "Wed, 30 Sep 2026 10:00:00 +0000" }), raw: new TextEncoder().encode(raw) };
}

describe("inbound mail", () => {
  it("stores raw MIME by hash, creates one correspondence row per Message-ID and one classify job", async () => {
    const repo = await seedCampaign();
    const first = await handleInboundMail(mail("abc@example.invalid", "Acknowledgement"), env as Env);
    expect(first.deduplicated).toBe(false);
    expect(first.job_id).toMatch(/^job_/);
    expect(await env.ORIGINALS.head("mail/" + first.raw_sha256)).not.toBeNull();
    const again = await handleInboundMail(mail("abc@example.invalid", "Acknowledgement"), env as Env);
    expect(again.deduplicated).toBe(true);
    expect(again.correspondence_id).toBe(first.correspondence_id);
    expect(again.job_id).toBeNull();
    // same Message-ID but different bytes (re-delivery with altered headers) still dedupes
    const altered = mail("abc@example.invalid", "Acknowledgement (resent)");
    const third = await handleInboundMail(altered, env as Env);
    expect(third.deduplicated).toBe(true);
    expect((await repo.inbox()).length).toBe(1);
    expect((await repo.jobs()).filter((j) => j.kind === "classify_mail").length).toBe(1);
    const row = await repo.correspondence(first.correspondence_id);
    expect(row?.received_at).toBe("2026-09-30T10:00:00.000Z");
    expect(row?.provider_message_id).toBe("abc@example.invalid");
  });
});

describe("cron", () => {
  it("creates a run receipt with intake/digest jobs, idempotent per slot, draft_followup for overdue requests", async () => {
    const repo = await seedCampaign();
    const t = Date.parse("2026-09-30T13:00:00Z");
    const a = await runScheduled(env as Env, t);
    expect(a.jobs_created).toBe(3); // intake, digest and the month's newsletter_draft
    const kinds = (await repo.jobs()).filter((j) => a.job_ids.includes(j.job_id)).map((j) => j.kind).sort();
    expect(kinds).toEqual(["digest", "intake", "newsletter_draft"]);
    const draft = (await repo.jobsByKind("newsletter_draft"))[0];
    expect(JSON.parse(draft.inputs_json)).toMatchObject({ trigger: "monthly", month: "2026-09" });
    expect(JSON.parse(draft.inputs_json).manifest.campaign.name).toBe("Example County ALPR Records");
    const b = await runScheduled(env as Env, t);
    expect(b.jobs_created).toBe(0);
    expect((await repo.runs()).length).toBeGreaterThanOrEqual(2);
    await repo.createRequest({
      agency_id: "ca-example-police", scope_id: "combined", scope_version: 1, subject: "s", body_md: "b", channel: "email", fee_cap_cents: 5000,
      state: "sent", sent_at: "2026-09-01T00:00:00Z", determination_due: "2026-09-11", extension_claimed_until: null, last_activity_at: null, next_action: null, external_ref: null,
    });
    const c = await runScheduled(env as Env, t + 60_000);
    expect(c.jobs_created).toBe(3); // new slot: intake, digest, draft_followup (same month: no second newsletter draft)
    expect((await repo.jobs()).filter((j) => j.kind === "draft_followup").length).toBe(1);
    const d = await runScheduled(env as Env, t + 120_000);
    expect(d.jobs_created).toBe(2); // follow-up for the same request/day is not duplicated
    const nextMonth = await runScheduled(env as Env, Date.parse("2026-10-01T07:00:00Z"));
    expect((await repo.jobs()).filter((j) => j.kind === "newsletter_draft").length).toBe(2);
    expect(nextMonth.jobs_created).toBe(4); // intake, digest, newsletter_draft, draft_followup (new day)
  });
});
