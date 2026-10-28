// Review -> ready -> propose -> (approve, execute publish_finding) -> build -> deploy, and
// the correction / withdrawal flows (docs/PUBLICATION.md). Every function takes the
// reviewer or organizer identity from the verified Access JWT, never from a form field.
import { newReviewId, nowIso, sha256Hex } from "@deflock/shared/ids";
import type { AccessIdentity } from "./auth.ts";
import type { CorrectionRow, ExternalActionRow, FindingRow, Repo } from "./db.ts";
import { publicationPath, proposeRebuild } from "./manifest.ts";
import { recomputeState } from "./review_state.ts";

export class WorkflowError extends Error {
  constructor(
    message: string,
    public readonly status: number = 409,
  ) {
    super(message);
  }
}

export const ROLES = ["factual", "legal", "privacy"] as const;
export const DECISIONS = ["approve", "reject", "changes_requested"] as const;

export interface ReviewInput {
  role: string;
  decision: string;
  rationale: string;
}

/** Write a review receipt bound to the current content hash and recompute the finding state. */
export async function submitReview(repo: Repo, finding: FindingRow, identity: AccessIdentity, input: ReviewInput): Promise<{ state: string; blockers: string[] }> {
  if (!identity?.email) throw new WorkflowError("review requires an authenticated identity", 401);
  if (!(ROLES as readonly string[]).includes(input.role)) throw new WorkflowError("role must be factual|legal|privacy", 400);
  if (!(DECISIONS as readonly string[]).includes(input.decision)) throw new WorkflowError("decision must be approve|reject|changes_requested", 400);
  const rationale = input.rationale.trim();
  if (!rationale) throw new WorkflowError("rationale required", 400);
  if (identity.email.toLowerCase() === finding.author.toLowerCase()) throw new WorkflowError("authors cannot review their own finding", 403);
  if (["published", "corrected", "withdrawn"].includes(finding.state)) throw new WorkflowError(`finding is ${finding.state}; reviews apply before publication`, 409);
  await repo.createReviewReceipt({
    review_id: newReviewId(),
    finding_id: finding.finding_id,
    content_sha256: finding.content_sha256,
    reviewer: identity.email,
    role: input.role,
    decision: input.decision,
    rationale,
    reviewed_at: nowIso(),
  });
  const receipts = await repo.reviewReceipts(finding.finding_id);
  const next = await recomputeState(finding, receipts);
  if (next.state !== finding.state) await repo.updateFinding(finding.finding_id, { state: next.state });
  return next;
}

/** Propose publication of a ready finding: a publish_finding card, idempotent per content hash. */
export async function proposePublication(repo: Repo, finding: FindingRow, identity: AccessIdentity): Promise<{ row: ExternalActionRow; inserted: boolean }> {
  if (!identity?.email) throw new WorkflowError("proposal requires an authenticated identity", 401);
  if (finding.state !== "ready") throw new WorkflowError(`finding is ${finding.state}, not ready`, 409);
  if (finding.confidence === "needs_attorney_review") throw new WorkflowError("confidence needs_attorney_review blocks publication", 409);
  const receipts = await repo.reviewReceipts(finding.finding_id);
  const { blockers } = await recomputeState(finding, receipts);
  if (blockers.length) throw new WorkflowError("review gate not satisfied: " + blockers.join(", "), 409);
  const path = publicationPath(finding);
  return repo.propose(
    "publish_finding",
    finding.finding_id,
    {
      finding_id: finding.finding_id,
      summary: finding.summary,
      classification: finding.classification,
      confidence: finding.confidence,
      content_sha256: finding.content_sha256,
      path,
      reviewers: receipts.filter((r) => r.content_sha256 === finding.content_sha256 && r.decision === "approve").map((r) => ({ reviewer: r.reviewer, role: r.role })),
    },
    await sha256Hex("publish_finding:" + finding.finding_id + ":" + finding.content_sha256),
    identity.email,
  );
}

export interface CorrectionInput {
  reason: string;
  replacement_finding_id: string | null;
}

/** Record a correction on the publication, mark the finding corrected and propose a rebuild + deploy. */
export async function issueCorrection(repo: Repo, finding: FindingRow, identity: AccessIdentity, input: CorrectionInput): Promise<{ correction: CorrectionRow; build_job_id: string; deploy_action_id: string }> {
  if (!identity?.email) throw new WorkflowError("correction requires an authenticated identity", 401);
  if (finding.state !== "published" && finding.state !== "corrected") throw new WorkflowError(`finding is ${finding.state}; only published findings are corrected`, 409);
  const reason = input.reason.trim();
  if (!reason) throw new WorkflowError("reason required", 400);
  const publication = await repo.publicationForFinding(finding.finding_id);
  if (!publication) throw new WorkflowError("no publication row for this finding", 409);
  if (input.replacement_finding_id) {
    const replacement = await repo.finding(input.replacement_finding_id);
    if (!replacement) throw new WorkflowError("replacement finding does not exist", 404);
    if (replacement.finding_id === finding.finding_id) throw new WorkflowError("a finding cannot replace itself", 400);
    if (replacement.state !== "published") throw new WorkflowError(`replacement finding is ${replacement.state}; publish it first`, 409);
  }
  const correction = await repo.createCorrection({
    publication_id: publication.publication_id,
    reason,
    replacement_finding_id: input.replacement_finding_id,
    corrected_at: nowIso(),
    corrected_by: identity.email,
  });
  await repo.updateFinding(finding.finding_id, { state: "corrected" });
  const rebuild = await proposeRebuild(repo, identity.email, "correction " + correction.correction_id);
  return { correction, build_job_id: rebuild.job_id, deploy_action_id: rebuild.action_id };
}

/** Withdraw a published finding: it leaves the manifest; the site is rebuilt without it. */
export async function withdrawFinding(repo: Repo, finding: FindingRow, identity: AccessIdentity, reason: string): Promise<{ build_job_id: string; deploy_action_id: string }> {
  if (!identity?.email) throw new WorkflowError("withdrawal requires an authenticated identity", 401);
  if (finding.state !== "published" && finding.state !== "corrected") throw new WorkflowError(`finding is ${finding.state}; only published findings are withdrawn`, 409);
  const text = reason.trim();
  if (!text) throw new WorkflowError("reason required", 400);
  const publication = await repo.publicationForFinding(finding.finding_id);
  if (publication) {
    await repo.createCorrection({ publication_id: publication.publication_id, reason: "withdrawn: " + text, replacement_finding_id: null, corrected_at: nowIso(), corrected_by: identity.email });
  }
  await repo.updateFinding(finding.finding_id, { state: "withdrawn" });
  const rebuild = await proposeRebuild(repo, identity.email, "withdraw " + finding.finding_id);
  return { build_job_id: rebuild.job_id, deploy_action_id: rebuild.action_id };
}
