// Registry: external_action.kind -> executor. Every kind in docs/CONTRACTS.md has an entry,
// so an approved card always resolves to a deterministic outcome (executed, or failed with
// a stable code) instead of hanging in `approved`.
import { FetchBrevo, type BrevoClient } from "../brevo.ts";
import type { ActionKind } from "../db.ts";
import type { Env } from "../env.ts";
import { deploySiteExecutor } from "./deploy_site.ts";
import { payFeeExecutor } from "./pay_fee.ts";
import { postSocialExecutor } from "./post_social.ts";
import { publishFindingExecutor } from "./publish_finding.ts";
import { mailExecutor } from "./send_mail.ts";
import { SignedOutboxSender } from "./outbox_sender.ts";
import { newsletterExecutor } from "./send_newsletter.ts";
import { NotConfiguredSender, type ActionExecutor, type MailSender } from "./types.ts";

export interface RegistryOptions {
  brevo?: BrevoClient | null;
  mailSender?: MailSender;
}

export function buildExecutors(opts: RegistryOptions = {}): Map<ActionKind, ActionExecutor> {
  const sender = opts.mailSender ?? new NotConfiguredSender();
  const list: ActionExecutor[] = [
    mailExecutor("send_request", sender),
    mailExecutor("send_followup", sender),
    payFeeExecutor,
    publishFindingExecutor,
    newsletterExecutor(opts.brevo ?? null),
    postSocialExecutor,
    deploySiteExecutor,
  ];
  return new Map(list.map((e) => [e.kind, e]));
}

/** Production registry from the Worker environment: Brevo when the secret is present. */
export function defaultExecutors(env?: Pick<Env, "BREVO_API_KEY" | "OUTBOX_GATEWAY_URL" | "OUTBOX_SIGNING_KEY">): Map<ActionKind, ActionExecutor> {
  const brevo = env?.BREVO_API_KEY ? new FetchBrevo(env.BREVO_API_KEY) : null;
  const mailSender = env?.OUTBOX_GATEWAY_URL && env.OUTBOX_SIGNING_KEY
    ? new SignedOutboxSender(env.OUTBOX_GATEWAY_URL, env.OUTBOX_SIGNING_KEY) : undefined;
  return buildExecutors({ brevo, mailSender });
}

export { ExecutorFailure, NotConfiguredSender } from "./types.ts";
export type { ActionExecutor, ExecutionResult, ExecutorContext, MailSender, OutboundMessage, Sender, SendReceipt } from "./types.ts";
export { deploySiteExecutor } from "./deploy_site.ts";
export { publishFindingExecutor } from "./publish_finding.ts";
export { newsletterExecutor } from "./send_newsletter.ts";
export { mailExecutor } from "./send_mail.ts";
export { postSocialExecutor } from "./post_social.ts";
export { payFeeExecutor } from "./pay_fee.ts";
