// post_social: posts ONE approved draft for ONE published finding to the ONE configured platform.
// Nothing posts without the approved card naming that exact post: the card's subject and
// proposal.finding_id are the finding, proposal.platform is the configured platform and
// proposal.content_sha256 equals sha256 of the exact text about to be posted, so an edited
// draft invalidates its own approval. (finding_id, platform) carries one receipt slot taken
// atomically before the platform call: a re-run or a racing card is `already_posted`, and an
// unknown outcome is recorded `uncertain` and never retried. Legal-claim flags (DF-4,
// proposal.review_flags from the draft generator) are shown on the card and counted on the
// receipt; they never block. A post cannot be recalled, so every doubt is a named refusal.
import { nowIso, sha256Hex } from "@deflock/shared/ids";
import type { ExternalActionRow, Repo } from "../db.ts";
import type { Env } from "../env.ts";
import { privacyHits, socialAdapterFor, SocialRejected, SocialUncertain, type SocialAdapter } from "./social_adapter.ts";
import { ExecutorFailure, type ActionExecutor } from "./types.ts";

/** An approval older than this must be given again before anything is posted. */
export const SOCIAL_APPROVAL_TTL_MS = 7 * 24 * 3600 * 1000;

/** Ids, hashes, counts and enums only: no post text, handle or personal data. */
export interface SocialReceipt {
  finding_id: string;
  platform: string;
  post_id: string;
  posted_at: string;
  content_sha256: string;
  approval_ref: string;
  legal_flag_count: number;
}

type Slot = { status: "posting" | "posted" | "uncertain" | "failed"; approval_ref: string; content_sha256: string; receipt?: SocialReceipt };

export const socialReceiptKey = (findingId: string, platform: string): string => `social_post:${findingId}:${platform}`;

/** Atomically take the (finding, platform) slot; only a conclusively failed slot may be taken again. */
async function reserve(repo: Repo, key: string, slot: Slot): Promise<boolean> {
  const now = nowIso();
  const res = await repo.db
    .prepare(
      "INSERT INTO setting (campaign_id, key, value_json, created_at, updated_at) VALUES (?,?,?,?,?) " +
        "ON CONFLICT(campaign_id, key) DO UPDATE SET value_json = excluded.value_json, updated_at = excluded.updated_at " +
        "WHERE json_extract(setting.value_json, '$.status') = 'failed'",
    )
    .bind(repo.campaignId, key, JSON.stringify(slot), now, now)
    .run();
  return res.meta.changes === 1;
}

function requireApproval(action: ExternalActionRow): void {
  if (action.state === "executed") throw new ExecutorFailure("approval_consumed", "this approval was already used for one post; propose and approve a new card");
  if (action.state !== "executing" || !action.approved_by || !action.approved_at) {
    throw new ExecutorFailure("no_approval", "no recorded human approval for this post; nothing was posted");
  }
  const at = Date.parse(action.approved_at);
  if (!Number.isFinite(at) || Date.now() - at > SOCIAL_APPROVAL_TTL_MS) {
    throw new ExecutorFailure("approval_expired", "the approval is older than 7 days; approve the draft again");
  }
}

export function postSocialExecutorWith(adapterFor: (env: Env) => SocialAdapter | null = (env) => socialAdapterFor(env)): ActionExecutor {
  return {
    kind: "post_social",
    async execute(action, proposal, ctx) {
      const adapter = adapterFor(ctx.env);
      if (!adapter) throw new ExecutorFailure("social_not_configured", "social_posting: not configured (MASTODON_URL and the MASTODON_TOKEN secret); nothing was posted");
      requireApproval(action);
      const findingId = proposal.finding_id;
      const text = proposal.text;
      if (typeof findingId !== "string" || !findingId || typeof text !== "string" || !text.trim() || typeof proposal.content_sha256 !== "string") {
        throw new ExecutorFailure("invalid_proposal", "proposal.finding_id, proposal.text and proposal.content_sha256 are required");
      }
      if (action.subject_id !== findingId || proposal.platform !== adapter.platform) {
        throw new ExecutorFailure("approval_mismatch", `the approval names a different finding or platform (configured: ${adapter.platform}); nothing was posted`);
      }
      const contentSha = await sha256Hex(text);
      if (contentSha !== proposal.content_sha256) {
        throw new ExecutorFailure("content_hash_mismatch", `the draft is not the approved text (${contentSha.slice(0, 12)} != approved ${proposal.content_sha256.slice(0, 12)}); approve the edited draft again`);
      }
      const finding = await ctx.repo.finding(findingId);
      if (!finding) throw new ExecutorFailure("unknown_finding", `finding ${findingId} does not exist`);
      if (finding.state !== "published") throw new ExecutorFailure("finding_not_published", `finding is ${finding.state}, not published`);
      const publication = await ctx.repo.publicationForFinding(findingId);
      if (!publication || publication.content_sha256 !== finding.content_sha256 || !publication.published_by) {
        throw new ExecutorFailure("finding_not_approved", "no publication record approves the finding's current content");
      }
      if ([...text].length > adapter.maxChars) throw new ExecutorFailure("too_long", `the draft exceeds ${adapter.maxChars} characters on ${adapter.platform}`);
      const hits = privacyHits(text);
      if (hits.length) throw new ExecutorFailure("privacy_lint", `the draft matches privacy pattern(s): ${hits.join(", ")}; nothing was posted`);
      const legalFlags = Array.isArray(proposal.review_flags) ? proposal.review_flags.filter((f) => typeof f === "string").length : 0;

      const key = socialReceiptKey(findingId, adapter.platform);
      const base = { approval_ref: action.action_id, content_sha256: contentSha };
      if (!(await reserve(ctx.repo, key, { status: "posting", ...base }))) {
        const held = await ctx.repo.setting<Slot>(key);
        if (held?.status === "uncertain") throw new ExecutorFailure("post_uncertain", "an earlier post for this finding and platform has an unknown outcome; check the platform by hand, nothing was posted");
        throw new ExecutorFailure("already_posted", `this finding was already posted to ${adapter.platform} (or a post is in progress); nothing was posted`);
      }
      let posted;
      try {
        posted = await adapter.post(text, `deflock-${findingId}-${adapter.platform}-${contentSha.slice(0, 16)}`);
      } catch (e) {
        if (e instanceof SocialRejected) {
          await ctx.repo.putSetting(key, { status: "failed", ...base });
          throw new ExecutorFailure("social_post_rejected", e.message + "; nothing was posted");
        }
        await ctx.repo.putSetting(key, { status: "uncertain", ...base });
        throw new ExecutorFailure("post_uncertain", (e instanceof SocialUncertain ? e.message : "adapter error") + "; the post may exist, check the platform by hand; never retried automatically");
      }
      const receipt: SocialReceipt = { finding_id: findingId, platform: adapter.platform, post_id: posted.post_id, posted_at: nowIso(),
        content_sha256: contentSha, approval_ref: action.action_id, legal_flag_count: legalFlags };
      await ctx.repo.putSetting(key, { status: "posted", ...base, receipt });
      return { provider_receipt: JSON.stringify(receipt) };
    },
  };
}

export const postSocialExecutor: ActionExecutor = postSocialExecutorWith();
