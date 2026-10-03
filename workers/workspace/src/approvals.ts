// Approval cards (docs/CONTRACTS.md "Approval card"). Nothing executes without a row in
// external_action with state = approved and an approved_by identity from an Access JWT.
//
// Execution state machine: approved -> executing -> executed | failed. The claim
// (approved -> executing) is a single conditional UPDATE, so two concurrent executes cannot
// both run the executor. Executing an already `executed` card is a no-op that returns the
// existing provider receipt; conclusive failures may be re-proposed, while unresolved
// provider effects require an authenticated reconciliation before any new approval.
import { nowIso } from "@deflock/shared/ids";
import type { AccessIdentity } from "./auth.ts";
import { executionClaimId, requiresDeliveryReconciliation, requiresExecutionQuiescence, type ActionKind, type ExternalActionRow, type Repo } from "./db.ts";
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
  let proposal = action.proposal_json;
  if ((action.kind === "send_request" || action.kind === "send_followup") && action.subject_id) {
    const request = await repo.request(action.subject_id);
    if (request) proposal = JSON.stringify({ ...JSON.parse(proposal), outbox_binding: {
      agency_id: request.agency_id, scope_version: request.scope_version, fee_cap_cents: request.fee_cap_cents,
    } });
  }
  const changed = await repo.transitionAction(action, { state: "approved", approved_by: identity.email,
    approved_at: nowIso(), proposal_json: proposal });
  if (!changed) throw new ApprovalError("action changed during approval; reload and inspect it again");
  return changed;
}

export async function rejectAction(repo: Repo, actionId: string, identity: AccessIdentity, reason: string): Promise<ExternalActionRow> {
  if (!identity?.email) throw new ApprovalError("rejection requires an authenticated identity", 401);
  const action = await repo.action(actionId);
  if (!action) throw new ApprovalError("unknown action", 404);
  if (action.state !== "proposed" && action.state !== "approved") throw new ApprovalError(`action is ${action.state}`);
  const changed = await repo.transitionAction(action, { state: "rejected", error: `rejected by ${identity.email}: ${reason || "no reason given"}` });
  if (!changed) throw new ApprovalError("action changed during rejection; inspect its execution receipt");
  return changed;
}

/** Edit keeps the card in `proposed`; an edited proposal must be approved again from scratch. */
export async function editAction(repo: Repo, actionId: string, identity: AccessIdentity, proposal: unknown): Promise<ExternalActionRow> {
  if (!identity?.email) throw new ApprovalError("editing requires an authenticated identity", 401);
  const action = await repo.action(actionId);
  if (!action) throw new ApprovalError("unknown action", 404);
  if (action.state !== "proposed") throw new ApprovalError(`action is ${action.state}, not proposed`);
  const merged = { ...(JSON.parse(action.proposal_json) as Record<string, unknown>), ...(proposal as Record<string, unknown>), edited_by: identity.email, edited_at: nowIso() };
  const changed = await repo.transitionAction(action, { proposal_json: JSON.stringify(merged) });
  if (!changed) throw new ApprovalError("action changed during editing; reload before making another edit");
  return changed;
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
  // Only the newsletter can be held while its executor is running. Its provider
  // checkpoints and events therefore use claim-bound writes as well as completion.
  const executorRepo = claimed.kind === "send_newsletter" ? new Proxy(repo, {
    get(target, key) {
      if (key === "updateAction") return async (id: string, patch: Partial<ExternalActionRow>) => {
        if (id !== claimed.action_id) throw new ExecutorFailure("execution_claim_lost", "executor action mismatch");
        if (!await target.updateExecutingAction(claimed, patch)) {
          if (patch.provider_receipt) await target.createSubscriberEvent({ provider: "brevo", kind: "late_execution_receipt",
            payload_json: JSON.stringify({ action_id: id, execution_id: executionClaimId(claimed), provider_receipt: patch.provider_receipt }), occurred_at: nowIso() });
          throw new ExecutorFailure("execution_claim_lost", "the action is held; late provider evidence was retained without changing its state");
        }
      };
      if (key === "createSubscriberEvent") return async (event: Parameters<Repo["createSubscriberEvent"]>[0]) => {
        const saved = await target.createExecutionEvent(claimed, event);
        if (!saved) throw new ExecutorFailure("execution_claim_lost", "the action is held; no send event was stamped");
        return saved;
      };
      const value = Reflect.get(target, key);
      return typeof value === "function" ? value.bind(target) : value;
    },
  }) : repo;
  try {
    const result = await executor.execute(claimed, JSON.parse(claimed.proposal_json) as Record<string, unknown>, { env, repo: executorRepo, identity });
    const completed = await repo.updateExecutingAction(claimed, { state: "executed", executed_at: nowIso(), provider_receipt: result.provider_receipt, error: null });
    if (!completed && claimed.kind === "send_newsletter") await repo.createSubscriberEvent({ provider: "brevo", kind: "late_execution_receipt",
      payload_json: JSON.stringify({ action_id: actionId, execution_id: executionClaimId(claimed), provider_receipt: result.provider_receipt, phase: "completion" }), occurred_at: nowIso() });
  } catch (e) {
    const text = failureText(e);
    if (await repo.updateExecutingAction(claimed, { state: "failed", error: text })) {
      await repo.raiseIncident("action:" + actionId, "warning", `execution failed for ${action.kind}: ${text}`);
    }
  }
  return (await repo.action(actionId))!;
}

/** Fence this exact claim. This is a hold, not cancellation of an admitted call. */
export async function holdInterruptedAction(repo: Repo, actionId: string, identity: AccessIdentity, executionId: unknown, reference: unknown): Promise<ExternalActionRow> {
  if (!identity?.email) throw new ApprovalError("recovery requires an authenticated identity", 401);
  if (typeof reference !== "string" || !reference.trim() || reference.length > 500) throw new ApprovalError("an interruption reference is required (max 500 characters)", 400);
  const action = await repo.action(actionId);
  if (!action) throw new ApprovalError("unknown action", 404);
  const claimId = executionClaimId(action);
  if (action.kind !== "send_newsletter" || action.state !== "executing" || !claimId || executionId !== claimId) throw new ApprovalError("this exact newsletter execution claim is required; legacy/unbound executions stay held");
  const evidence = JSON.stringify({ action_id: actionId, execution_id: claimId, checked_by: identity.email,
    checked_at: nowIso(), reference: reference.trim(), previous_receipt: action.provider_receipt, outcome: "held_not_cancelled" });
  if (!await repo.holdExecution(action, evidence)) throw new ApprovalError("execution changed during recovery; inspect its current receipt");
  return (await repo.action(actionId))!;
}

/** Evidence comes from an authenticated organizer's provider check, not a resend. */
export async function reconcileAction(repo: Repo, actionId: string, identity: AccessIdentity, outcome: unknown, reference: unknown, deliveredAt?: unknown, quiescenceReference?: unknown, quiescenceConfirmed?: unknown, providerMessageId?: unknown): Promise<ExternalActionRow> {
  if (!identity?.email) throw new ApprovalError("reconciliation requires an authenticated identity", 401);
  if (outcome !== "delivered" && outcome !== "not_delivered") throw new ApprovalError("outcome must be delivered or not_delivered", 400);
  if (typeof reference !== "string" || !reference.trim() || reference.length > 500) throw new ApprovalError("a provider-check reference is required (max 500 characters)", 400);
  let deliveryTime: string | null = null;
  if (outcome === "delivered") {
    if (typeof deliveredAt !== "string" || !/^\d{4}-\d{2}-\d{2}T.*Z$/.test(deliveredAt) || !Number.isFinite(Date.parse(deliveredAt)) || Date.parse(deliveredAt) > Date.now()) throw new ApprovalError("provider delivery time is required as a past UTC ISO timestamp", 400);
    deliveryTime = new Date(deliveredAt).toISOString();
  }
  const action = await repo.action(actionId);
  if (!action) throw new ApprovalError("unknown action", 404);
  if (!requiresDeliveryReconciliation(action)) throw new ApprovalError("action is not awaiting delivery reconciliation");
  let mailMessageId: string | null = null;
  if (outcome === "delivered" && (action.kind === "send_request" || action.kind === "send_followup")) {
    let stored: unknown;
    try { stored = action.provider_receipt ? JSON.parse(action.provider_receipt).provider_message_id : null; } catch { stored = null; }
    const supplied = typeof providerMessageId === "string" ? providerMessageId.trim() : null;
    if (typeof stored === "string" && stored.trim()) {
      if (supplied && supplied !== stored) throw new ApprovalError("provider message id conflicts with the stored receipt", 400);
      mailMessageId = stored;
    } else { mailMessageId = supplied; }
    if (!mailMessageId || mailMessageId.length > 1000 || /[\r\n\x00]/.test(mailMessageId)) {
      throw new ApprovalError("provider-confirmed message id is required for delivered mail (max 1000 characters)", 400);
    }
  }
  if (requiresExecutionQuiescence(action) && ((quiescenceConfirmed !== true && quiescenceConfirmed !== "confirmed") ||
      typeof quiescenceReference !== "string" || !quiescenceReference.trim() || quiescenceReference.length > 500)) {
    throw new ApprovalError("confirm the old invocation has stopped and supply its quiescence evidence reference; elapsed time or a timeout is insufficient", 400);
  }
  const evidence = { action_id: actionId, outcome, reference: reference.trim(), checked_by: identity.email, checked_at: nowIso(), previous_error: action.error, previous_receipt: action.provider_receipt, delivered_at: deliveryTime,
    ...(mailMessageId ? { provider_message_id: mailMessageId } : {}),
    ...(requiresExecutionQuiescence(action) ? { quiescence_reference: (quiescenceReference as string).trim(), quiescence_confirmed: true } : {}) };
  // Preserve an immutable attempted-check record even if a concurrent transition wins.
  await repo.createSubscriberEvent({ provider: action.kind === "send_newsletter" ? "brevo" : "mail", kind: "action_reconciliation_attempt", payload_json: JSON.stringify(evidence), occurred_at: evidence.checked_at });
  if (!await repo.reconcileDelivery(action, outcome === "delivered", JSON.stringify(evidence), deliveryTime, mailMessageId)) throw new ApprovalError("action changed during reconciliation; inspect its current receipt");
  return (await repo.action(actionId))!;
}
