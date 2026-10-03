// send_newsletter: creates a Brevo email campaign from the approved draft and sends it to
// the configured list. The receipt is the Brevo campaign id. The workspace never imports,
// lists or edits contacts; suppression and unsubscribe are provider-side, and the draft
// must carry Brevo's `{{ unsubscribe }}` placeholder.
import { nowIso } from "@deflock/shared/ids";
import { BrevoError, type BrevoClient } from "../brevo.ts";
import type { BrevoSettings } from "../manifest.ts";
import { ExecutorFailure, type ActionExecutor } from "./types.ts";

export const UNSUBSCRIBE_PLACEHOLDER = "{{ unsubscribe }}";
/** campaign_tool.newsletter.draft appends this when the campaign has no reviewed consent footer. */
export const CONSENT_FOOTER_TEMPLATE_MARKER = "[TEMPLATE CONSENT FOOTER";

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
  if (html.includes(CONSENT_FOOTER_TEMPLATE_MARKER) || text.includes(CONSENT_FOOTER_TEMPLATE_MARKER)) throw new ExecutorFailure("template_consent_footer", "the draft carries the engine's template consent footer; set the campaign's reviewed consent footer in Settings and draft again");
  return { subject, html, text };
}

export function newsletterExecutor(client: BrevoClient | null): ActionExecutor {
  return {
    kind: "send_newsletter",
    async execute(action, proposal, ctx) {
      const settings = await ctx.repo.setting<BrevoSettings>("brevo");
      // The list comes from Settings at execute time, never from the card: an older draft
      // must not carry a test list into a send after Settings moved to the real one.
      const listId = settings?.list_id ?? null;
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
      } catch (e) {
        if (e instanceof BrevoError && [400, 401, 403, 404, 422].includes(e.status)) throw new ExecutorFailure("brevo_error", e.message);
        throw new ExecutorFailure("brevo_create_ambiguous", "campaign creation did not return a conclusive result; check the action id and idempotency tag at Brevo before retrying");
      }
      // Retain the provider identity before sending or doing local bookkeeping.
      try {
        await ctx.repo.updateAction(action.action_id, { provider_receipt: JSON.stringify({ provider: "brevo", brevo_campaign_id: created.id, list_id: listId, delivery: "unconfirmed" }) });
      } catch {
        throw new ExecutorFailure("brevo_send_ambiguous", `Brevo campaign ${created.id} exists but its local receipt could not be retained; check provider status before retrying`);
      }
      // From here the campaign exists at Brevo. A failed or timed-out send is ambiguous
      // (Brevo may have queued it), so the card fails with the campaign id in the error and
      // an event records it: an organizer checks that campaign at Brevo before proposing a
      // new draft, instead of the workspace creating a second campaign for the same key.
      try {
        await client.sendCampaignNow(created.id);
      } catch (e) {
        const detail = e instanceof Error ? e.message : String(e);
        await ctx.repo.createSubscriberEvent({
          provider: "brevo",
          kind: "campaign_send_ambiguous",
          payload_json: JSON.stringify({ brevo_campaign_id: created.id, subject: draft.subject, action_id: action.action_id, list_id: listId, error: detail }),
          occurred_at: nowIso(),
        });
        throw new ExecutorFailure("brevo_send_ambiguous", `Brevo campaign ${created.id} was created but the send call failed (${detail}); check that campaign at Brevo before proposing a new draft`);
      }
      const sentAt = nowIso();
      const receipt = JSON.stringify({ provider: "brevo", brevo_campaign_id: created.id, list_id: listId, sent_at: sentAt });
      await ctx.repo.updateAction(action.action_id, { provider_receipt: receipt });
      await ctx.repo.createSubscriberEvent({
        provider: "brevo",
        kind: "campaign_sent",
        payload_json: JSON.stringify({ brevo_campaign_id: created.id, subject: draft.subject, action_id: action.action_id, list_id: listId }),
        occurred_at: sentAt,
      });
      return { provider_receipt: receipt };
    },
  };
}
