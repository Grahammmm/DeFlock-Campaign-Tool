// deploy_site: flips the `site_version` setting that the public-site Worker serves. The
// proposal carries `{ "site_version": "<version>" }`. Site files must already be in the
// public R2 bucket under sites/<version>/ (written by a finished build_site job).
import { nowIso } from "@deflock/shared/ids";
import { ExecutorFailure, type ActionExecutor } from "./types.ts";

export const SITE_VERSION = /^[a-z0-9._-]{1,64}$/;

export const deploySiteExecutor: ActionExecutor = {
  kind: "deploy_site",
  async execute(action, proposal, ctx) {
    const version = proposal.site_version;
    if (typeof version !== "string" || !SITE_VERSION.test(version)) throw new ExecutorFailure("invalid_proposal", "proposal.site_version invalid");
    // The finding ids this version carries, from the build job's manifest; null when the
    // card names no build job (a manual version), in which case nothing is stamped.
    let deployed: Set<string> | null = null;
    if (typeof proposal.build_job_id === "string") {
      const job = await ctx.repo.job(proposal.build_job_id);
      if (job && job.state !== "done") throw new ExecutorFailure("build_not_done", `build_site job ${job.job_id} is ${job.state}; deploy after it is done`);
      if (job) {
        const inputs = JSON.parse(job.inputs_json) as { manifest?: { findings?: { id?: unknown }[] } };
        deployed = new Set((inputs.manifest?.findings ?? []).map((f) => (typeof f.id === "string" ? f.id : "")).filter(Boolean));
      }
    }
    const previous = await ctx.repo.setting<string>("site_version");
    await ctx.repo.putSetting("site_version", version);
    await ctx.repo.putSetting("site_version_previous", previous ?? null);
    await ctx.env.CACHE.put("site_version", version);
    const receipt = JSON.stringify({ site_version: version, previous, action_id: action.action_id, at: nowIso() });
    // Stamp only the publications this version actually carries: a stale deploy card
    // approved after later publications must not mark those as live.
    for (const pub of await ctx.repo.publications()) {
      if (pub.deploy_receipt) continue;
      if (!deployed || !pub.finding_id || !deployed.has(pub.finding_id)) continue;
      await ctx.repo.updatePublication(pub.publication_id, { deploy_receipt: receipt });
    }
    return { provider_receipt: receipt };
  },
};
