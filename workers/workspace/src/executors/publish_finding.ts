// publish_finding: moves a `ready` finding to `published`. The review gate is re-checked at
// execution time against the current content hash (the proposal may be stale), and
// confidence needs_attorney_review is refused regardless of receipts. Publication writes a
// row with the exact content hash and public path, then enqueues a build_site job whose
// inputs carry the full content manifest; the matching deploy_site card is proposed, not
// approved. A newsletter draft job follows every publication.
import { newPublicationId, nowIso, sha256Hex } from "@deflock/shared/ids";
import { newsletterManifest, publicationPath, proposeRebuild } from "../manifest.ts";
import { blockersFor } from "../review_state.ts";
import { ExecutorFailure, type ActionExecutor } from "./types.ts";

export const publishFindingExecutor: ActionExecutor = {
  kind: "publish_finding",
  async execute(action, proposal, ctx) {
    const findingId = typeof proposal.finding_id === "string" ? proposal.finding_id : action.subject_id;
    if (!findingId) throw new ExecutorFailure("invalid_proposal", "proposal.finding_id required");
    const finding = await ctx.repo.finding(findingId);
    if (!finding) throw new ExecutorFailure("unknown_finding", `finding ${findingId} does not exist`);
    if (finding.confidence === "needs_attorney_review") throw new ExecutorFailure("needs_attorney_review", "confidence needs_attorney_review blocks publish_finding");
    if (typeof proposal.content_sha256 === "string" && proposal.content_sha256 !== finding.content_sha256) {
      throw new ExecutorFailure("content_changed", `finding content changed since the card was proposed (${proposal.content_sha256.slice(0, 12)} -> ${finding.content_sha256.slice(0, 12)}); propose again`);
    }
    if (finding.state === "published") {
      const existing = await ctx.repo.publicationForFinding(finding.finding_id);
      if (existing && existing.content_sha256 === finding.content_sha256) {
        return { provider_receipt: JSON.stringify({ publication_id: existing.publication_id, path: existing.path, content_sha256: existing.content_sha256, already_published: true }) };
      }
    }
    if (finding.state !== "ready") throw new ExecutorFailure("not_ready", `finding is ${finding.state}, not ready`);
    const receipts = await ctx.repo.reviewReceipts(finding.finding_id);
    const blockers = await blockersFor(finding, receipts);
    if (blockers.length) throw new ExecutorFailure("review_blockers", "review gate not satisfied: " + blockers.join(", "));
    const path = publicationPath(finding);
    const publishedAt = nowIso();
    const publication = await ctx.repo.createPublication({
      publication_id: newPublicationId(),
      finding_id: finding.finding_id,
      path,
      content_sha256: finding.content_sha256,
      published_at: publishedAt,
      published_by: ctx.identity.email,
      deploy_receipt: null,
    });
    await ctx.repo.updateFinding(finding.finding_id, { state: "published" });
    const rebuild = await proposeRebuild(ctx.repo, ctx.identity.email, "publish_finding " + finding.finding_id);
    const draft = await ctx.repo.enqueueJob("newsletter_draft", await sha256Hex("newsletter_draft:pub:" + publication.publication_id), {
      trigger: "publication",
      publication_id: publication.publication_id,
      finding_id: finding.finding_id,
      manifest: await newsletterManifest(ctx.repo),
    });
    return {
      provider_receipt: JSON.stringify({
        publication_id: publication.publication_id,
        path,
        content_sha256: finding.content_sha256,
        published_at: publishedAt,
        build_job_id: rebuild.job_id,
        site_version: rebuild.manifest.site_version,
        deploy_action_id: rebuild.action_id,
        newsletter_draft_job_id: draft.row.job_id,
      }),
    };
  },
};
