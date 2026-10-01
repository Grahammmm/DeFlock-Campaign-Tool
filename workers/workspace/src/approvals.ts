// Approval cards (docs/CONTRACTS.md "Approval card"). Nothing executes without a row in
// external_action with state = approved and an approved_by identity from an Access JWT.
import { nowIso } from "@deflock/shared/ids";
import type { AccessIdentity } from "./auth.ts";
import type { ActionKind, ExternalActionRow, Repo } from "./db.ts";
import type { Env } from "./env.ts";

export interface ExecutionResult {
  provider_receipt: string;
}

/** An executor performs one approved external effect and returns a provider receipt. */
export interface ActionExecutor {
  readonly kind: ActionKind;
  execute(action: ExternalActionRow, proposal: Record<string, unknown>, ctx: ExecutorContext): Promise<ExecutionResult>;
}

export interface ExecutorContext {
  env: Env;
  repo: Repo;
  identity: AccessIdentity;
}

export class ApprovalError extends Error {
  constructor(
    message: string,
    public readonly status: number = 409,
  ) {
    super(message);
  }
}

/**
 * deploy_site: flips the `site_version` setting that the public-site Worker serves. The
 * proposal carries `{ "site_version": "<version>" }`. Site files must already be in the
 * public R2 bucket under sites/<version>/ (written by an approved build_site job).
 */
export const deploySiteExecutor: ActionExecutor = {
  kind: "deploy_site",
  async execute(action, proposal, ctx) {
    const version = proposal.site_version;
    if (typeof version !== "string" || !/^[a-z0-9._-]{1,64}$/.test(version)) throw new ApprovalError("proposal.site_version invalid", 400);
    const previous = await ctx.repo.setting<string>("site_version");
    await ctx.repo.putSetting("site_version", version);
    await ctx.repo.putSetting("site_version_previous", previous ?? null);
    await ctx.env.CACHE.put("site_version", version);
    return { provider_receipt: JSON.stringify({ site_version: version, previous, action_id: action.action_id, at: nowIso() }) };
  },
};

/**
 * Registry. send_request / send_followup / send_newsletter / pay_fee / publish_finding /
 * post_social are interfaces only in this phase: approving them records the approval and
 * they stay `approved` until an executor (mailbox, MuckRock, Brevo) is registered.
 */
export function defaultExecutors(): Map<ActionKind, ActionExecutor> {
  return new Map<ActionKind, ActionExecutor>([[deploySiteExecutor.kind, deploySiteExecutor]]);
}

export async function approveAction(repo: Repo, actionId: string, identity: AccessIdentity): Promise<ExternalActionRow> {
  if (!identity?.email) throw new ApprovalError("approval requires an authenticated identity", 401);
  const action = await repo.action(actionId);
  if (!action) throw new ApprovalError("unknown action", 404);
  if (action.state !== "proposed") throw new ApprovalError(`action is ${action.state}, not proposed`);
  await repo.updateAction(actionId, { state: "approved", approved_by: identity.email, approved_at: nowIso() });
  return (await repo.action(actionId))!;
}

export async function rejectAction(repo: Repo, actionId: string, identity: AccessIdentity, reason: string): Promise<ExternalActionRow> {
  if (!identity?.email) throw new ApprovalError("rejection requires an authenticated identity", 401);
  const action = await repo.action(actionId);
  if (!action) throw new ApprovalError("unknown action", 404);
  if (action.state !== "proposed" && action.state !== "approved") throw new ApprovalError(`action is ${action.state}`);
  await repo.updateAction(actionId, { state: "rejected", error: `rejected by ${identity.email}: ${reason || "no reason given"}` });
  return (await repo.action(actionId))!;
}

/** Edit keeps the card in `proposed`; an edited proposal must be approved again from scratch. */
export async function editAction(repo: Repo, actionId: string, identity: AccessIdentity, proposal: unknown): Promise<ExternalActionRow> {
  if (!identity?.email) throw new ApprovalError("editing requires an authenticated identity", 401);
  const action = await repo.action(actionId);
  if (!action) throw new ApprovalError("unknown action", 404);
  if (action.state !== "proposed") throw new ApprovalError(`action is ${action.state}, not proposed`);
  const merged = { ...(JSON.parse(action.proposal_json) as Record<string, unknown>), ...(proposal as Record<string, unknown>), edited_by: identity.email, edited_at: nowIso() };
  await repo.updateAction(actionId, { proposal_json: JSON.stringify(merged) });
  return (await repo.action(actionId))!;
}

export async function executeAction(
  repo: Repo,
  env: Env,
  actionId: string,
  identity: AccessIdentity,
  executors: Map<ActionKind, ActionExecutor> = defaultExecutors(),
): Promise<ExternalActionRow> {
  const action = await repo.action(actionId);
  if (!action) throw new ApprovalError("unknown action", 404);
  if (action.state !== "approved" || !action.approved_by) throw new ApprovalError("only approved actions execute");
  const executor = executors.get(action.kind);
  if (!executor) throw new ApprovalError(`no executor registered for ${action.kind}; the action stays approved`, 501);
  const claimed = await repo.claimForExecution(actionId);
  if (!claimed) throw new ApprovalError("action was claimed by another execution");
  try {
    const result = await executor.execute(claimed, JSON.parse(claimed.proposal_json) as Record<string, unknown>, { env, repo, identity });
    await repo.updateAction(actionId, { state: "executed", executed_at: nowIso(), provider_receipt: result.provider_receipt, error: null });
  } catch (e) {
    await repo.updateAction(actionId, { state: "failed", error: String((e as Error).message ?? e) });
    await repo.raiseIncident("action:" + actionId, "warning", `execution failed for ${action.kind}: ${(e as Error).message}`);
  }
  return (await repo.action(actionId))!;
}
