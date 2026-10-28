// Executor interfaces. An executor performs exactly one approved external effect
// (docs/CONTRACTS.md "Approval card") and returns a provider receipt. It never reads a
// card that is not `executing`, and it signals a refusal with `ExecutorFailure`, whose
// `code` becomes the first token of external_action.error (`<code>: <message>`).
import type { AccessIdentity } from "../auth.ts";
import type { ActionKind, ExternalActionRow, Repo } from "../db.ts";
import type { Env } from "../env.ts";

export interface ExecutionResult {
  provider_receipt: string;
}

export interface ExecutorContext {
  env: Env;
  repo: Repo;
  identity: AccessIdentity;
}

export interface ActionExecutor {
  readonly kind: ActionKind;
  execute(action: ExternalActionRow, proposal: Record<string, unknown>, ctx: ExecutorContext): Promise<ExecutionResult>;
}

/** A refusal with a stable machine-readable code (`failed: <code>` on the card). */
export class ExecutorFailure extends Error {
  constructor(
    public readonly code: string,
    message: string,
  ) {
    super(message);
  }
  toString(): string {
    return this.code + ": " + this.message;
  }
}

// --- Sender port -------------------------------------------------------------------
// send_request / send_followup hand an approved draft to a `MailSender`. The workspace
// outbox (transactional send with provider receipts, MuckRock adapter) plugs in here;
// until it is registered the default sender refuses with `sender_not_configured` and
// the card is marked failed, never silently executed.

export interface OutboundMessage {
  action_id: string;
  kind: "send_request" | "send_followup";
  channel: "email" | "muckrock" | "portal_manual";
  request_id: string | null;
  to: string | null;
  subject: string;
  body_md: string;
  idempotency_key: string;
}

export interface SendReceipt {
  /** Provider message id, MuckRock id, or portal reference. */
  provider_message_id: string;
  sent_at: string;
  provider: string;
}

/** Generic sender port for anything that leaves the system. */
export interface Sender<M, R> {
  readonly name: string;
  send(message: M, ctx: ExecutorContext): Promise<R>;
}

export type MailSender = Sender<OutboundMessage, SendReceipt>;

export class NotConfiguredSender implements MailSender {
  readonly name = "not_configured";
  async send(message: OutboundMessage): Promise<SendReceipt> {
    throw new ExecutorFailure(
      "sender_not_configured",
      `no outbound sender is registered for ${message.kind} via ${message.channel}; the approved card stays recorded and nothing was sent`,
    );
  }
}
