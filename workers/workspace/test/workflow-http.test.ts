import { env } from "cloudflare:test";
import { describe, expect, it } from "vitest";
import { contentHash, type JsonObject } from "@deflock/shared/review";
import { EXPORT_TABLES } from "../src/db.ts";
import { call, makeSigner, organizerRequest, runnerHeaders, seedCampaign } from "./helpers.ts";

const SHA = "b".repeat(64);

async function seedFinding(author = "author@example.invalid") {
  const repo = await seedCampaign();
  const id = "fnd_" + crypto.randomUUID().replace(/-/g, "").slice(0, 16);
  const doc: JsonObject = { id, author, summary: "HTTP synthetic finding " + id, classification: "documented_fact", confidence: "likely", event_date: "2026-09-01", sources: [{ sha256: SHA, locator: "sheet 1 cell A2" }], limitations: [], counterevidence: [] };
  const row = await repo.createFinding({ finding_id: id, author, classification: "documented_fact", confidence: "likely", summary: String(doc.summary), finding_json: JSON.stringify(doc), content_sha256: await contentHash(doc), state: "draft" });
  return { repo, row };
}

function form(data: Record<string, string>): RequestInit {
  return { method: "POST", headers: { "content-type": "application/x-www-form-urlencoded", accept: "application/json" }, body: new URLSearchParams(data).toString() };
}

describe("review form over HTTP", () => {
  it("takes the reviewer from the Access JWT, refuses the author, and recomputes state", async () => {
    const { repo, row } = await seedFinding();
    const signer = await makeSigner();
    let r = await organizerRequest(signer, "author@example.invalid", `/findings/${row.finding_id}/review`, form({ role: "factual", decision: "approve", rationale: "mine", reviewer: "someone-else@example.invalid" }));
    let res = await call(r.request, r.overrides);
    expect(res.status).toBe(403);
    expect((await repo.reviewReceipts(row.finding_id)).length).toBe(0);
    r = await organizerRequest(signer, "Reviewer-One@example.invalid", `/findings/${row.finding_id}/review`, form({ role: "factual", decision: "approve", rationale: "checked", reviewer: "spoof@example.invalid" }));
    res = await call(r.request, r.overrides);
    expect(res.status).toBe(200);
    expect(await res.json()).toMatchObject({ state: "in_review" });
    const receipts = await repo.reviewReceipts(row.finding_id);
    expect(receipts[0].reviewer).toBe("reviewer-one@example.invalid"); // lowercased JWT email, never the form field
    expect(receipts[0].content_sha256).toBe(row.content_sha256);
    // detail page shows the form to a non-author and hides it from the author
    r = await organizerRequest(signer, "author@example.invalid", `/findings/${row.finding_id}`);
    expect(await (await call(r.request, r.overrides)).text()).toContain("authors cannot review their own work");
    r = await organizerRequest(signer, "reviewer-two@example.invalid", `/findings/${row.finding_id}`);
    expect(await (await call(r.request, r.overrides)).text()).toContain("Submit review receipt");
    // propose is refused until ready
    r = await organizerRequest(signer, "organizer@example.invalid", `/findings/${row.finding_id}/propose`, { method: "POST", headers: { accept: "application/json" } });
    res = await call(r.request, r.overrides);
    expect(res.status).toBe(409);
  });
});

describe("export.json", () => {
  it("streams every table for the campaign to organizers and to the runner, not anonymously", async () => {
    const repo = await seedCampaign();
    await repo.putSetting("brevo", { list_id: 7, form_url: null, sender_name: null, sender_email: null });
    const signer = await makeSigner();
    const r = await organizerRequest(signer, "organizer@example.invalid", "/api/export.json");
    const res = await call(r.request, r.overrides);
    expect(res.status).toBe(200);
    expect(res.headers.get("content-type")).toContain("application/json");
    const body = (await res.json()) as { schema_version: number; campaign_id: string; tables: string[]; rows: Record<string, unknown[]> };
    expect(body.schema_version).toBe(1);
    expect(body.campaign_id).toBe(env.CAMPAIGN_ID);
    expect(body.tables).toEqual([...EXPORT_TABLES]);
    expect(Object.keys(body.rows)).toEqual([...EXPORT_TABLES]);
    expect((body.rows.campaign[0] as { name: string }).name).toBe("Example County ALPR Records");
    expect((body.rows.setting as { key: string }[]).some((s) => s.key === "brevo")).toBe(true);
    expect(body.rows.original).toEqual([]);
    const runner = await call(new Request("https://workspace.example.invalid/api/runner/export.json", { headers: runnerHeaders() }));
    expect(runner.status).toBe(200);
    expect(((await runner.json()) as { tables: string[] }).tables.length).toBe(EXPORT_TABLES.length);
    expect((await call(new Request("https://workspace.example.invalid/api/export.json"))).status).toBe(401);
    expect((await call(new Request("https://workspace.example.invalid/api/runner/export.json"))).status).toBe(401);
  });
});

describe("settings, subscribers, meetings, backup", () => {
  it("Brevo settings are validated and non-secret; the signup checklist renders", async () => {
    const repo = await seedCampaign();
    const signer = await makeSigner();
    let r = await organizerRequest(signer, "organizer@example.invalid", "/settings/brevo", form({ list_id: "12", form_url: "https://evil.example.invalid/form", sender_name: "x", sender_email: "n@example.invalid" }));
    expect((await call(r.request, r.overrides)).status).toBe(400);
    r = await organizerRequest(signer, "organizer@example.invalid", "/settings/brevo", form({ list_id: "12", form_url: "https://abc.sibforms.com/serve/xyz", sender_name: "Example", sender_email: "news@example.invalid" }));
    const res = await call(r.request, r.overrides);
    expect(res.status).toBe(200);
    expect(await repo.setting("brevo")).toMatchObject({ list_id: 12, form_url: "https://abc.sibforms.com/serve/xyz", updated_by: "organizer@example.invalid" });
    r = await organizerRequest(signer, "organizer@example.invalid", "/settings");
    const page = await (await call(r.request, r.overrides)).text();
    expect(page).toContain("Test signup flow");
    expect(page).toContain("Do not reactivate");
    expect(page).toContain("BREVO_API_KEY");
    expect(page).not.toContain("api-key");
    // the manifest now carries brevo_hosted signup
    r = await organizerRequest(signer, "organizer@example.invalid", "/api/manifest.json");
    const manifest = (await (await call(r.request, r.overrides)).json()) as { site: { signup: { mode: string; form_html_allowlisted_url: string } } };
    expect(manifest.site.signup).toEqual({ mode: "brevo_hosted", form_html_allowlisted_url: "https://abc.sibforms.com/serve/xyz" });
  });

  it("a finished newsletter_draft job becomes a send_newsletter card via Approve & send", async () => {
    const repo = await seedCampaign();
    const signer = await makeSigner();
    const { row: job } = await repo.enqueueJob("newsletter_draft", "nd-http-1", { trigger: "monthly", month: "2026-09" });
    let r = await organizerRequest(signer, "organizer@example.invalid", `/subscribers/drafts/${job.job_id}/propose`, { method: "POST", headers: { accept: "application/json" } });
    expect((await call(r.request, r.overrides)).status).toBe(409); // not done yet
    await repo.db.prepare("UPDATE job SET state = 'done', outputs_json = ? WHERE job_id = ?").bind(JSON.stringify({ draft: { subject: "September update", html: "<p>hi</p><p>{{ unsubscribe }}</p>", text: "hi" } }), job.job_id).run();
    r = await organizerRequest(signer, "organizer@example.invalid", `/subscribers/drafts/${job.job_id}/propose`, { method: "POST", headers: { accept: "application/json" } });
    const res = await call(r.request, r.overrides);
    expect(res.status).toBe(201);
    const { action_id } = (await res.json()) as { action_id: string };
    const action = await repo.action(action_id);
    expect(action?.kind).toBe("send_newsletter");
    expect(action?.state).toBe("proposed");
    expect(JSON.parse(action!.proposal_json)).toMatchObject({ subject: "September update", draft_job_id: job.job_id });
    r = await organizerRequest(signer, "organizer@example.invalid", "/subscribers");
    const page = await (await call(r.request, r.overrides)).text();
    expect(page).toContain("September update");
    expect(page).toContain("send_newsletter");
  });

  it("meetings: manual add, comment kit from published findings only, backup job", async () => {
    const repo = await seedCampaign();
    const signer = await makeSigner();
    let r = await organizerRequest(signer, "organizer@example.invalid", "/meetings", form({ body_name: "Example City Council", starts_at: "2026-11-03T18:00:00-08:00", agenda_url: "https://legistar.example.invalid/agenda/1", agenda_item: "Item 7: ALPR contract renewal", relevance: "alpr_item" }));
    expect((await call(r.request, r.overrides)).status).toBe(302);
    const meeting = (await repo.meetings()).find((m) => m.body_name === "Example City Council")!;
    expect(meeting.starts_at).toBe("2026-11-04T02:00:00.000Z");
    r = await organizerRequest(signer, "organizer@example.invalid", `/meetings/${meeting.meeting_id}/comment-kit`, { method: "POST", headers: { accept: "application/json" } });
    const kit = (await (await call(r.request, r.overrides)).json()) as { comment_kit_md: string; findings: number };
    expect(kit.comment_kit_md).toContain("## Two-minute comment");
    expect(kit.comment_kit_md).toContain("## Three asks");
    expect(kit.comment_kit_md).toContain("Continue this item");
    expect(kit.comment_kit_md).not.toContain("<");
    expect((await repo.meeting(meeting.meeting_id))!.comment_kit_md).toBe(kit.comment_kit_md);
    r = await organizerRequest(signer, "organizer@example.invalid", "/api/backup", { method: "POST", headers: { accept: "application/json" } });
    let res = await call(r.request, r.overrides);
    expect(res.status).toBe(201);
    const { job_id } = (await res.json()) as { job_id: string };
    expect((await repo.job(job_id))?.kind).toBe("backup");
    res = await call((await organizerRequest(signer, "organizer@example.invalid", "/api/backup", { method: "POST", headers: { accept: "application/json" } })).request, r.overrides);
    expect(res.status).toBe(200); // same day, same organizer: deduped
  });
});
