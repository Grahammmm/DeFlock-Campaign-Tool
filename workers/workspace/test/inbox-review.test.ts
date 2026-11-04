// PANEL-INBOX-REVIEW: an organizer confirms or corrects one inbound message's classification
// from /inbox, the reviewer and time are recorded, and a later classify_mail result never
// overwrites a reviewed row. Synthetic campaign and addresses only.
import { env } from "cloudflare:test";
import { beforeAll, describe, expect, it } from "vitest";
import { Repo, type CorrespondenceRow } from "../src/db.ts";
import { call, leaseExactly, makeSigner, organizerRequest, runnerHeaders, seedCampaign, type TestSigner } from "./helpers.ts";

const ORG = "organizer@example.invalid";
const ORG2 = "second-organizer@example.invalid";
const RUNNER = "https://workspace.example.invalid/api/runner";
let signer: TestSigner;
let repo: Repo;

beforeAll(async () => {
  signer = await makeSigner();
  repo = await seedCampaign();
});

async function message(direction: "inbound" | "outbound" = "inbound", classification: string | null = "unclassified", target: Repo = repo): Promise<CorrespondenceRow> {
  const { row } = await target.createCorrespondence({
    request_id: null, direction, channel: "email", provider_message_id: "rev-" + crypto.randomUUID() + "@example.invalid", from_addr: "records@example.invalid", to_addr: "organizer@example.invalid",
    subject: "Synthetic message", received_at: "2030-01-01T10:00:00Z", raw_sha256: null, classification, classification_confidence: null, summary: null,
  });
  return row;
}

async function review(id: string, body: Record<string, string> | null, init: { json?: boolean; headers?: Record<string, string>; email?: string } = {}): Promise<Response> {
  const headers: Record<string, string> = { ...init.headers };
  let payload: BodyInit | undefined;
  if (body && init.json) { headers["content-type"] = "application/json"; headers.accept = "application/json"; payload = JSON.stringify(body); }
  else if (body) { headers["content-type"] = "application/x-www-form-urlencoded"; payload = new URLSearchParams(body).toString(); }
  const r = await organizerRequest(signer, init.email ?? ORG, `/inbox/${id}/classification`, { method: "POST", headers, body: payload });
  return call(r.request, r.overrides);
}

async function inboxHtml(): Promise<string> {
  const r = await organizerRequest(signer, ORG, "/inbox");
  const res = await call(r.request, r.overrides);
  expect(res.status).toBe(200);
  return res.text();
}

describe("inbox classification review", () => {
  it("1. reviewing an unclassified inbound row records the classification, the JWT reviewer and the first proposal; /inbox shows the form and 'reviewed by'", async () => {
    const m = await message();
    const before = await inboxHtml();
    expect(before).toContain(`action="/inbox/${m.correspondence_id}/classification"`);
    expect(before).toContain("Save review");
    const res = await review(m.correspondence_id, { classification: "production" }, { json: true });
    expect(res.status).toBe(200);
    const body = (await res.json()) as CorrespondenceRow;
    expect(body.correspondence_id).toBe(m.correspondence_id);
    const row = (await repo.correspondence(m.correspondence_id))!;
    expect(row.classification).toBe("production");
    expect(row.classification_confidence).toBe("organizer");
    expect(row.reviewed_by).toBe(ORG);
    expect(row.reviewed_at).toMatch(/^\d{4}-\d\d-\d\dT/);
    expect(row.proposed_classification).toBe("unclassified");
    const after = await inboxHtml();
    expect(after).toContain(`reviewed by ${ORG}`);
    // the dashboard tile no longer counts it
    expect(row.classification).not.toBe("unclassified");
  });

  it("1b. a form post redirects back to /inbox with a notice and shows the current value selected plus 'proposed: X' when it differs", async () => {
    const m = await message("inbound", "extension");
    const res = await review(m.correspondence_id, { classification: "denial" });
    expect(res.status).toBe(302);
    expect(res.headers.get("location")).toContain("/inbox?notice=");
    const row = (await repo.correspondence(m.correspondence_id))!;
    expect(row.proposed_classification).toBe("extension");
    const page = await inboxHtml();
    expect(page).toContain("proposed: extension");
    expect(page).toContain('<option value="denial" selected>');
    const options = page.slice(page.indexOf(`/inbox/${m.correspondence_id}/classification`)).split("</select>")[0]!.match(/<option value="[a-z_]+"/g)!;
    expect(options).toHaveLength(8);
    expect(options.join("")).not.toContain('"unclassified"');
  });

  it("2. unclassified, an unknown value and a missing value are 400 'classification invalid' and leave the row unchanged", async () => {
    const m = await message("inbound", "acknowledgement");
    for (const body of [{ classification: "unclassified" }, { classification: "bogus" }, { classification: "" }, {}] as Record<string, string>[]) {
      for (const json of [true, false]) {
        const res = await review(m.correspondence_id, body, { json });
        expect(res.status, JSON.stringify(body)).toBe(400);
        expect(((await res.json()) as { error: string }).error).toBe("classification invalid");
      }
    }
    // a non-string value in JSON is refused too
    const r = await organizerRequest(signer, ORG, `/inbox/${m.correspondence_id}/classification`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ classification: ["production"] }) });
    expect((await call(r.request, r.overrides)).status).toBe(400);
    const row = (await repo.correspondence(m.correspondence_id))!;
    expect(row.classification).toBe("acknowledgement");
    expect(row.reviewed_by ?? null).toBeNull();
    expect(row.reviewed_at ?? null).toBeNull();
    expect(row.proposed_classification ?? null).toBeNull();
  });

  it("3. an unknown id, another campaign's id and an outbound row are 404; the outbound row has no form", async () => {
    expect((await review("cor_does_not_exist", { classification: "production" }, { json: true })).status).toBe(404);

    const other = new Repo(env.DB, "other-campaign");
    await other.createCampaign({
      name: "Other", jurisdiction: "us-ca", county_fips: "06079", county_name: "San Luis Obispo", place_fips: null, place_name: null,
      public_hostname: "other.example.invalid", workspace_hostname: "other-ws.example.invalid", privacy_tier: "redacted_cloud", law_package_status: "draft",
      external_sends: "approval_required", schedule_cron: "0 7 * * *", timezone: "America/Los_Angeles",
    });
    const foreign = await message("inbound", "unclassified", other);
    expect((await review(foreign.correspondence_id, { classification: "production" }, { json: true })).status).toBe(404);
    const foreignRow = (await other.correspondence(foreign.correspondence_id))!;
    expect(foreignRow.classification).toBe("unclassified");
    expect(foreignRow.reviewed_by ?? null).toBeNull();

    const outbound = await message("outbound", null);
    expect((await review(outbound.correspondence_id, { classification: "production" }, { json: true })).status).toBe(404);
    const outRow = (await repo.correspondence(outbound.correspondence_id))!;
    expect(outRow.classification).toBeNull();
    expect(outRow.reviewed_by ?? null).toBeNull();
    expect(await inboxHtml()).not.toContain(`/inbox/${outbound.correspondence_id}/classification`);
  });

  it("4. a reviewed_by form or JSON field is ignored: the stored reviewer is the Access identity", async () => {
    const a = await message();
    expect((await review(a.correspondence_id, { classification: "clarification", reviewed_by: "attacker@x" })).status).toBe(302);
    expect((await repo.correspondence(a.correspondence_id))!.reviewed_by).toBe(ORG);
    const b = await message();
    expect((await review(b.correspondence_id, { classification: "clarification", reviewed_by: "attacker@x" }, { json: true, email: ORG2 })).status).toBe(200);
    expect((await repo.correspondence(b.correspondence_id))!.reviewed_by).toBe(ORG2);
  });

  it("5. a cross-site POST is refused with 403 and the row is unchanged", async () => {
    const m = await message();
    for (const headers of [{ origin: "https://attacker.example" }, { "sec-fetch-site": "cross-site" }] as Record<string, string>[]) {
      const res = await review(m.correspondence_id, { classification: "production" }, { json: true, headers });
      expect(res.status).toBe(403);
    }
    const row = (await repo.correspondence(m.correspondence_id))!;
    expect(row.classification).toBe("unclassified");
    expect(row.reviewed_by ?? null).toBeNull();
  });

  it("5b. an unauthenticated POST is refused and the row is unchanged", async () => {
    const m = await message();
    const res = await call(new Request(`https://workspace.example.invalid/inbox/${m.correspondence_id}/classification`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ classification: "production" }) }));
    expect(res.status).toBeGreaterThanOrEqual(401);
    expect((await repo.correspondence(m.correspondence_id))!.classification).toBe("unclassified");
  });

  it("6. reviewing twice: the second value wins and proposed_classification stays the runner's first proposal", async () => {
    const m = await message("inbound", "denial");
    expect((await review(m.correspondence_id, { classification: "production" }, { json: true })).status).toBe(200);
    expect((await review(m.correspondence_id, { classification: "partial_production" }, { json: true, email: ORG2 })).status).toBe(200);
    const row = (await repo.correspondence(m.correspondence_id))!;
    expect(row.classification).toBe("partial_production");
    expect(row.proposed_classification).toBe("denial");
    expect(row.reviewed_by).toBe(ORG2);
  });

  it("7. a classify_mail job finishing after the review is accepted but keeps the human value; on an unreviewed row it still writes", async () => {
    const reviewed = await message();
    const fresh = await message();
    const finish = async (id: string, classification: string): Promise<Response> => {
      const { row } = await repo.enqueueJob("classify_mail", "rv-" + crypto.randomUUID(), { correspondence_id: id, raw_sha256: "a".repeat(64) });
      const claim_token = await leaseExactly(row.job_id);
      const body = JSON.stringify({ claim_token, status: "done", outputs: { correspondence_update: { classification, classification_confidence: "high", summary: "runner summary" } } });
      return call(new Request(`${RUNNER}/jobs/${row.job_id}/result`, { method: "POST", headers: runnerHeaders(), body }));
    };
    // out of order: the human reviews first, the runner finishes later
    expect((await review(reviewed.correspondence_id, { classification: "production" }, { json: true })).status).toBe(200);
    expect((await finish(reviewed.correspondence_id, "denial")).status).toBe(200);
    const kept = (await repo.correspondence(reviewed.correspondence_id))!;
    expect(kept.classification).toBe("production");
    expect(kept.classification_confidence).toBe("organizer");
    expect(kept.summary).toBeNull();
    expect(kept.reviewed_by).toBe(ORG);
    // positive control: an unreviewed row still takes the runner's value
    expect((await finish(fresh.correspondence_id, "denial")).status).toBe(200);
    const written = (await repo.correspondence(fresh.correspondence_id))!;
    expect(written.classification).toBe("denial");
    expect(written.classification_confidence).toBe("high");
    expect(written.reviewed_at ?? null).toBeNull();
  });
});
