import { describe, expect, it } from "vitest";
import { canonicalJson, type JsonObject } from "@deflock/shared/review";
import { sha256Hex } from "@deflock/shared/ids";
import { assembleManifest, proposeRebuild } from "../src/manifest.ts";
import { call, runnerHeaders, seedCampaign } from "./helpers.ts";

describe("build envelope v1 / content manifest v2", () => {
  it("leases the actual producer inputs unchanged and retains the job-bound approval", async () => {
    const repo = await seedCampaign();
    const build = await proposeRebuild(repo, "organizer@example.invalid", "synthetic contract check");
    const again = await proposeRebuild(repo, "organizer@example.invalid", "same content, another reason");
    expect(again).toMatchObject({ job_id: build.job_id, action_id: build.action_id, job_created: false, action_created: false });
    const expected = JSON.parse((await repo.job(build.job_id))!.inputs_json);
    expect(expected).toMatchObject({ schema_version: 1, site_version: build.manifest.site_version,
      manifest: { schema_version: 2, campaign: { schema_version: 1, name: "Example County ALPR Records",
        county: "San Luis Obispo", state: "CA", country: "US" } } });
    expect(Object.keys(expected.manifest.campaign).sort()).toEqual(["country", "county", "name", "schema_version", "state"]);
    const { site_version, manifest_sha256, ...body } = build.manifest;
    expect(manifest_sha256).toBe(await sha256Hex(canonicalJson(body as unknown as JsonObject)));
    expect(site_version).toBe("m" + manifest_sha256.slice(0, 16));
    const unauthenticated = await call(new Request("https://workspace.example.invalid/api/runner/jobs"));
    expect(unauthenticated.status).toBe(401);
    const response = await call(new Request("https://workspace.example.invalid/api/runner/jobs", { headers: runnerHeaders() }));
    expect(response.status).toBe(200);
    expect(await response.json()).toMatchObject({ job_id: build.job_id, inputs: expected });
    const card = (await repo.action(build.action_id))!;
    expect(card.state).toBe("proposed");
    expect(card.approved_by).toBeNull();
    expect(JSON.parse(card.proposal_json)).toMatchObject({ build_job_id: build.job_id, site_version });
    expect(await repo.setting("site_version")).toBeNull();
  });

  it("binds campaign rendering identity into the manifest hash", async () => {
    const repo = await seedCampaign();
    const first = await assembleManifest(repo);
    await repo.db.prepare("UPDATE campaign SET name = ? WHERE campaign_id = ?").bind("Renamed synthetic campaign", repo.campaignId).run();
    const renamed = await assembleManifest(repo);
    expect(renamed.manifest_sha256).not.toBe(first.manifest_sha256);
    expect(renamed.site_version).not.toBe(first.site_version);
  });

  it("keeps invalid identity and unsupported numeric input in tracked jobs for consumer rejection", async () => {
    const repo = await seedCampaign();
    const jobs = (await repo.jobsByKind("build_site")).length;
    await repo.db.prepare("UPDATE campaign SET jurisdiction = ? WHERE campaign_id = ?").bind("unsupported", repo.campaignId).run();
    for (const value of [0.5, 1e-7, Number.MAX_SAFE_INTEGER + 1]) {
      await repo.putSetting("site_json", { schema_version: value });
      const build = await proposeRebuild(repo, "organizer@example.invalid", "invalid numeric value");
      expect((await repo.job(build.job_id))!.state).toBe("queued");
      expect(build.manifest.campaign.state).toBe("");
      expect(build.manifest.site.schema_version).toBe(value);
    }
    await repo.putSetting("site_json", null);
    expect((await repo.jobsByKind("build_site")).length).toBe(jobs + 3);
  });
});
