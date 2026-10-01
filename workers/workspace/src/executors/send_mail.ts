// send_request / send_followup: hand the approved draft to the registered MailSender. The
// default sender is NotConfiguredSender, so without an outbox the card fails with
// `sender_not_configured` and nothing leaves the system.
import { nowIso } from "@deflock/shared/ids";
import { ExecutorFailure, NotConfiguredSender, type ActionExecutor, type MailSender, type OutboundMessage } from "./types.ts";

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
      const receipt = await sender.send(message, ctx);
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
      return { provider_receipt: JSON.stringify({ ...receipt, sender: sender.name, at: nowIso() }) };
    },
  };
}
