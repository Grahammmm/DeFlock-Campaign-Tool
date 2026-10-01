// Approval cards (docs/CONTRACTS.md "Approval card"). Nothing executes without a row in
// external_action with state = approved and an approved_by identity from an Access JWT.
//
// Execution state machine: approved -> executing -> executed | failed. The claim
// (approved -> executing) is a single conditional UPDATE, so two concurrent executes cannot
// both run the executor. Executing an already `executed` card is a no-op that returns the
// existing provider receipt; a `failed` card must be proposed again.
import { nowIso } from "@deflock/shared/ids";
import type { AccessIdentity } from "./auth.ts";
import type { ActionKind, ExternalActionRow, Repo } from "./db.ts";
import type { Env } from "./env.ts";
import { defaultExecutors, ExecutorFailure, type ActionExecutor } from "./executors/index.ts";

export { defaultExecutors, deploySiteExecutor, ExecutorFailure } from "./executors/index.ts";
export type { ActionExecutor, ExecutionResult, ExecutorContext } from "./executors/index.ts";

export class ApprovalError extends Error {
  constructor(
    message: string,
    public readonly status: number = 409,
  ) {
    super(message);
  }
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

function failureText(e: unknown): string {
  if (e instanceof ExecutorFailure) return e.code + ": " + e.message;
  const msg = String((e as Error)?.message ?? e);
  return "error: " + msg;
}

export async function executeAction(
  repo: Repo,
  env: Env,
  actionId: string,
  identity: AccessIdentity,
  executors: Map<ActionKind, ActionExecutor> = defaultExecutors(env),
): Promise<ExternalActionRow> {
  if (!identity?.email) throw new ApprovalError("execution requires an authenticated identity", 401);
  const action = await repo.action(actionId);
  if (!action) throw new ApprovalError("unknown action", 404);
  if (action.state === "executed") return action; // idempotent: the receipt already exists
  if (action.state !== "approved" || !action.approved_by) throw new ApprovalError(`only approved actions execute (action is ${action.state})`);
  const executor = executors.get(action.kind);
  if (!executor) throw new ApprovalError(`no executor registered for ${action.kind}; the action stays approved`, 501);
  const claimed = await repo.claimForExecution(actionId);
  if (!claimed) {
    const now = await repo.action(actionId);
    if (now?.state === "executed") return now;
    throw new ApprovalError("action was claimed by another execution");
  }
  try {
    const result = await executor.execute(claimed, JSON.parse(claimed.proposal_json) as Record<string, unknown>, { env, repo, identity });
    await repo.updateAction(actionId, { state: "executed", executed_at: nowIso(), provider_receipt: result.provider_receipt, error: null });
  } catch (e) {
    const text = failureText(e);
    await repo.updateAction(actionId, { state: "failed", error: text });
    await repo.raiseIncident("action:" + actionId, "warning", `execution failed for ${action.kind}: ${text}`);
  }
  return (await repo.action(actionId))!;
}
