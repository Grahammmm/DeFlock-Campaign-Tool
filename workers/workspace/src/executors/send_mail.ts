// send_request / send_followup: hand the approved draft to the registered MailSender. The
// default sender is NotConfiguredSender, so without an outbox the card fails with
// `sender_not_configured` and nothing leaves the system.
import { nowIso } from "@deflock/shared/ids";
import { ExecutorFailure, NotConfiguredSender, type ActionExecutor, type MailSender, type OutboundMessage, type SendReceipt } from "./types.ts";

function outbound(kind: "send_request" | "send_followup", actionId: string, key: string, proposal: Record<string, unknown>, requestId: string | null): OutboundMessage {
  const channel = proposal.channel;
  if (channel !== "email" && channel !== "muckrock" && channel !== "portal_manual") throw new ExecutorFailure("invalid_proposal", "proposal.channel must be email|muckrock|portal_manual");
  if (typeof proposal.subject !== "string" || !proposal.subject.trim()) throw new ExecutorFailure("invalid_proposal", "proposal.subject required");
  if (typeof proposal.body_md !== "string" || !proposal.body_md.trim()) throw new ExecutorFailure("invalid_proposal", "proposal.body_md required");
  return {
    action_id: actionId,
    kind,
    channel,
    request_id: requestId,
    to: typeof proposal.to === "string" ? proposal.to : null,
    subject: proposal.subject,
    body_md: proposal.body_md,
    idempotency_key: key,
  };
}

export function mailExecutor(kind: "send_request" | "send_followup", sender: MailSender = new NotConfiguredSender()): ActionExecutor {
  return {
    kind,
    async execute(action, proposal, ctx) {
      const campaign = await ctx.repo.campaign();
      if (campaign?.external_sends !== "approval_required") throw new ExecutorFailure("external_sends_disabled", "campaign.external_sends is disabled; enable approval_required in Settings first");
      const message = outbound(kind, action.action_id, action.idempotency_key, proposal, action.subject_id);
      let receipt: SendReceipt;
      try {
        receipt = await sender.send(message, ctx);
      } catch (error) {
        if (error instanceof ExecutorFailure) throw error;
        throw new ExecutorFailure("mail_send_ambiguous", "sender outcome is unknown; reconcile delivery before retrying");
      }
      if (!receipt || typeof receipt.provider_message_id !== "string" || !receipt.provider_message_id.trim() ||
          receipt.provider_message_id.length > 1000 || typeof receipt.provider !== "string" || !receipt.provider.trim() ||
          receipt.provider.length > 100 || typeof receipt.sent_at !== "string" ||
          !/^\d{4}-\d{2}-\d{2}T.*Z$/.test(receipt.sent_at) || !Number.isFinite(Date.parse(receipt.sent_at))) {
        throw new ExecutorFailure("mail_send_ambiguous", "sender returned an invalid delivery receipt; reconcile before retrying");
      }
      const providerReceipt = JSON.stringify({ provider_message_id: receipt.provider_message_id,
        provider: receipt.provider, sent_at: new Date(receipt.sent_at).toISOString(), sender: sender.name, at: nowIso() });
      try {
        // Preserve delivery evidence before request bookkeeping can fail.
        await ctx.repo.updateAction(action.action_id, { provider_receipt: providerReceipt });
      } catch {
        throw new ExecutorFailure("mail_send_ambiguous", "delivery receipt storage failed; reconcile before retrying");
      }
      if (message.request_id) {
        const patch = kind === "send_request" ? { state: "sent" as const, sent_at: receipt.sent_at, external_ref: receipt.provider_message_id } : {};
        await ctx.repo.updateRequest(message.request_id, { ...patch, last_activity_at: receipt.sent_at });
        await ctx.repo.createCorrespondence({
          request_id: message.request_id,
          direction: "outbound",
          channel: message.channel,
          provider_message_id: receipt.provider_message_id,
          from_addr: null,
          to_addr: message.to,
          subject: message.subject,
          received_at: receipt.sent_at,
          raw_sha256: null,
          classification: null,
          classification_confidence: null,
          summary: kind === "send_request" ? "records request sent" : "follow-up sent",
        });
      }
      return { provider_receipt: providerReceipt };
    },
  };
}
