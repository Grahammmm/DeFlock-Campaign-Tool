import { env } from "cloudflare:test";
import { describe, expect, it } from "vitest";
import { call, makeSigner, organizerRequest, seedCampaign } from "./helpers.ts";

const ORG = "organizer@example.invalid";

async function get(path = "/publish"): Promise<Response> {
  const signer = await makeSigner();
  const r = await organizerRequest(signer, ORG, path);
  return call(r.request, r.overrides);
}

async function postForm(headers: Record<string, string> = {}): Promise<Response> {
  const signer = await makeSigner();
  const r = await organizerRequest(signer, ORG, "/publish/rebuild", { method: "POST", headers: { "content-type": "application/x-www-form-urlencoded", ...headers }, body: "" });
  return call(r.request, r.overrides);
}

/** Insert a build_site job with an explicit timestamp so ordering does not depend on the clock. */
async function seedBuild(key: string, inputs: string, enqueuedAt: string): Promise<string> {
  const repo = await seedCampaign();
  const { row } = await repo.enqueueJob("build_site", key, {});
  await env.DB.prepare("UPDATE job SET inputs_json = ?, enqueued_at = ? WHERE job_id = ?").bind(inputs, enqueuedAt, row.job_id).run();
  return row.job_id;
}

describe("publish rebuild control", () => {
  // The pool shares D1 state across the tests of this file, so the empty-state test must run first.
  it("renders the rebuild form exactly once and 'No build yet.' when there are no jobs", async () => {
    await seedCampaign();
    const res = await get();
    expect(res.status).toBe(200);
    const body = await res.text();
    expect(body.split('action="/publish/rebuild"').length - 1).toBe(1);
    expect(body).toContain("Rebuild site from manifest");
    expect(body).toContain("No build yet.");
  });

  it("a form POST redirects to /approvals naming the queued build_site, and a job and a deploy_site card exist", async () => {
    const repo = await seedCampaign();
    const res = await postForm();
    expect(res.status).toBe(302);
    const loc = res.headers.get("location") ?? "";
    expect(loc.startsWith("/approvals?notice=")).toBe(true);
    const notice = decodeURIComponent(loc.slice("/approvals?notice=".length));
    expect(notice).toMatch(/^build_site job_\S+ queued; deploy_site \S+ proposed for m[0-9a-f]{16}$/);
    const jobs = await repo.jobsByKind("build_site");
    expect(jobs.length).toBe(1);
    const cards = (await repo.actions()).filter((a) => a.kind === "deploy_site");
    expect(cards.length).toBe(1);
    expect(notice).toContain(jobs[0].job_id);
    expect(notice).toContain(cards[0].action_id);
  });

  it("a second identical form POST says 'already queued' and creates no second job or card", async () => {
    const repo = await seedCampaign();
    const res = await postForm();
    expect(res.status).toBe(302);
    expect(decodeURIComponent(res.headers.get("location") ?? "")).toContain("already queued");
    expect((await repo.jobsByKind("build_site")).length).toBe(1);
    expect((await repo.actions()).filter((a) => a.kind === "deploy_site").length).toBe(1);
  });

  it("shows a queued build with its site_version, then a failed one with the error escaped", async () => {
    const repo = await seedCampaign();
    const job = (await repo.jobsByKind("build_site"))[0];
    const version = (JSON.parse(job.inputs_json) as { site_version: string }).site_version;
    let body = await (await get()).text();
    expect(body).toContain(`Last build: ${job.job_id}`);
    expect(body).toContain(`for ${version}`);
    expect(body).not.toContain("No build yet.");
    await env.DB.prepare("UPDATE job SET state = 'failed', error = ? WHERE job_id = ?").bind("<script>x</script>", job.job_id).run();
    body = await (await get()).text();
    expect(body).toContain("failed");
    expect(body).toContain("&lt;script&gt;x&lt;/script&gt;");
    expect(body).not.toContain("<script>x</script>");
  });

  it("renders '?' for a job whose inputs lack site_version or are unparsable, and still returns 200", async () => {
    const lacking = await seedBuild("pr-lacking", JSON.stringify({ reason: "x" }), "2099-01-01T00:00:00.000Z");
    let res = await get();
    expect(res.status).toBe(200);
    let body = await res.text();
    expect(body).toContain(`Last build: ${lacking}`);
    expect(body).toMatch(new RegExp(`Last build: ${lacking} [\\s\\S]*? for \\? - enqueued`));
    const broken = await seedBuild("pr-broken", "{not json", "2099-01-02T00:00:00.000Z");
    res = await get();
    expect(res.status).toBe(200);
    body = await res.text();
    expect(body).toContain(`Last build: ${broken}`);
    expect(body).toMatch(new RegExp(`Last build: ${broken} [\\s\\S]*? for \\? - enqueued`));
    const nullInputs = await seedBuild("pr-null", "null", "2099-01-03T00:00:00.000Z");
    res = await get();
    expect(res.status).toBe(200);
    expect(await res.text()).toContain(`Last build: ${nullInputs}`);
  });

  it("shows the newer of two builds (newest first)", async () => {
    const older = await seedBuild("pr-older", JSON.stringify({ site_version: "vold" }), "2100-01-01T00:00:00.000Z");
    const newer = await seedBuild("pr-newer", JSON.stringify({ site_version: "vnew" }), "2100-01-02T00:00:00.000Z");
    const body = await (await get()).text();
    expect(body).toContain(`Last build: ${newer}`);
    expect(body).toContain("for vnew");
    expect(body).not.toContain(older);
    const repo = await seedCampaign();
    expect((await repo.jobsByKind("build_site"))[0].job_id).toBe(newer);
  });

  it("a cross-site POST is refused with 403 and creates no job", async () => {
    const repo = await seedCampaign();
    const before = (await repo.jobsByKind("build_site")).length;
    for (const headers of [{ origin: "https://attacker.example" }, { "sec-fetch-site": "cross-site" }] as Record<string, string>[]) {
      expect((await postForm(headers)).status).toBe(403);
    }
    expect((await repo.jobsByKind("build_site")).length).toBe(before);
  });

  it("the JSON reply of POST /publish/rebuild keeps its shape", async () => {
    const res = await postForm({ accept: "application/json" });
    expect([200, 201]).toContain(res.status);
    const body = (await res.json()) as Record<string, unknown>;
    expect(Object.keys(body).sort()).toEqual(["action_created", "action_id", "job_created", "job_id", "site_version"]);
  });
});
