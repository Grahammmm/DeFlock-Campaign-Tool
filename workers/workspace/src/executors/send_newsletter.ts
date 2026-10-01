// send_newsletter: creates a Brevo email campaign from the approved draft and sends it to
// the configured list. The receipt is the Brevo campaign id. The workspace never imports,
// lists or edits contacts; suppression and unsubscribe are provider-side, and the draft
// must carry Brevo's `{{ unsubscribe }}` placeholder.
import { nowIso } from "@deflock/shared/ids";
import { BrevoError, type BrevoClient } from "../brevo.ts";
import type { BrevoSettings } from "../manifest.ts";
import { ExecutorFailure, type ActionExecutor } from "./types.ts";

export const UNSUBSCRIBE_PLACEHOLDER = "{{ unsubscribe }}";

export interface NewsletterDraft {
  subject: string;
  html: string;
  text: string;
}

export function draftFromProposal(proposal: Record<string, unknown>): NewsletterDraft {
  const subject = typeof proposal.subject === "string" ? proposal.subject.trim() : "";
  const html = typeof proposal.html === "string" ? proposal.html : "";
  const text = typeof proposal.text === "string" ? proposal.text : "";
  if (!subject || subject.length > 200) throw new ExecutorFailure("invalid_proposal", "proposal.subject required (max 200 chars)");
  if (!html.trim()) throw new ExecutorFailure("invalid_proposal", "proposal.html required");
  if (/<script\b/i.test(html)) throw new ExecutorFailure("invalid_proposal", "proposal.html must not contain scripts");
  if (!html.includes(UNSUBSCRIBE_PLACEHOLDER)) throw new ExecutorFailure("missing_unsubscribe", "proposal.html must include the Brevo " + UNSUBSCRIBE_PLACEHOLDER + " placeholder");
  return { subject, html, text };
}

export function newsletterExecutor(client: BrevoClient | null): ActionExecutor {
  return {
    kind: "send_newsletter",
    async execute(action, proposal, ctx) {
      const settings = await ctx.repo.setting<BrevoSettings>("brevo");
      const listId = typeof proposal.list_id === "number" ? proposal.list_id : settings?.list_id ?? null;
      if (!client) throw new ExecutorFailure("brevo_not_configured", "BREVO_API_KEY secret is not set on the Worker");
      if (!settings?.sender_email || !settings.sender_name) throw new ExecutorFailure("brevo_not_configured", "Brevo sender name and address are not set in Settings");
      if (!Number.isInteger(listId) || (listId as number) <= 0) throw new ExecutorFailure("brevo_not_configured", "Brevo list id is not set in Settings");
      const draft = draftFromProposal(proposal);
      const campaign = await ctx.repo.campaign();
      let created: { id: number };
      try {
        created = await client.createEmailCampaign({
          name: `${campaign?.name ?? "campaign"} ${nowIso().slice(0, 10)} ${action.action_id}`,
          subject: draft.subject,
          sender: { name: settings.sender_name, email: settings.sender_email },
          htmlContent: draft.html,
          textContent: draft.text || undefined,
          listIds: [listId as number],
          tag: action.idempotency_key.slice(0, 50),
        });
        await client.sendCampaignNow(created.id);
      } catch (e) {
        if (e instanceof BrevoError) throw new ExecutorFailure("brevo_error", e.message);
        throw e;
      }
      const sentAt = nowIso();
      await ctx.repo.createSubscriberEvent({
        provider: "brevo",
        kind: "campaign_sent",
        payload_json: JSON.stringify({ brevo_campaign_id: created.id, subject: draft.subject, action_id: action.action_id, list_id: listId }),
        occurred_at: sentAt,
      });
      return { provider_receipt: JSON.stringify({ provider: "brevo", brevo_campaign_id: created.id, list_id: listId, sent_at: sentAt }) };
    },
  };
}
