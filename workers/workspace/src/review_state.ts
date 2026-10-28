// Finding state machine over review receipts (docs/PUBLICATION.md).
//
//   draft      no receipt bound to the current content hash
//   in_review  receipts exist; only more reviews are missing
//   blocked    a challenge (reject / changes_requested) on the current content, or a
//              structural blocker (missing evidence, invalid hash, ...) after at least
//              one approval
//   ready      review_blockers() is empty
//
// published / corrected / withdrawn are set by publication and correction flows, never
// recomputed here. The blockers themselves come from the shared review.ts port, which
// mirrors campaign_tool/review.py.
import { reviewBlockers, type JsonObject } from "@deflock/shared/review";
import type { FindingRow, ReviewReceiptRow } from "./db.ts";

export const TERMINAL_STATES = new Set(["published", "corrected", "withdrawn"]);

/** Blockers that only mean "more reviews needed" rather than "something is wrong". */
export const PENDING_REVIEW_BLOCKERS = new Set(["missing_factual_review", "missing_legal_review", "missing_privacy_review", "need_two_independent_reviewers"]);

export function receiptsAsJson(receipts: ReviewReceiptRow[]): JsonObject[] {
  return receipts.map((r) => ({ content_sha256: r.content_sha256, decision: r.decision, reviewer: r.reviewer, role: r.role, rationale: r.rationale, reviewed_at: r.reviewed_at }));
}

export function blockersFor(finding: FindingRow, receipts: ReviewReceiptRow[]): Promise<string[]> {
  return reviewBlockers(JSON.parse(finding.finding_json) as JsonObject, receiptsAsJson(receipts));
}

export function nextState(current: string, contentSha256: string, blockers: string[], receipts: ReviewReceiptRow[]): string {
  if (TERMINAL_STATES.has(current)) return current;
  const bound = receipts.filter((r) => r.content_sha256 === contentSha256);
  if (!blockers.length) return "ready";
  if (!bound.length) return "draft";
  const approvals = bound.filter((r) => r.decision === "approve").length;
  const hard = blockers.filter((b) => !PENDING_REVIEW_BLOCKERS.has(b));
  if (blockers.includes("unresolved_challenge")) return "blocked";
  if (approvals > 0 && hard.length) return "blocked";
  return "in_review";
}

export async function recomputeState(finding: FindingRow, receipts: ReviewReceiptRow[]): Promise<{ state: string; blockers: string[] }> {
  const blockers = await blockersFor(finding, receipts);
  return { state: nextState(finding.state, finding.content_sha256, blockers, receipts), blockers };
}
