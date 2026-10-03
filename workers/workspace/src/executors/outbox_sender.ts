import { bytesToHex } from "@deflock/shared/ids";
import { ExecutorFailure, type ExecutorContext, type MailSender, type OutboundMessage, type SendReceipt } from "./types.ts";

const DOMAIN = "deflock-outbox-v1\n";
const encoder = new TextEncoder();

export async function canonicalOutboxKey(kind: string, requestId: string, scopeVersion: number, intentId = ""): Promise<string> {
  if (intentId && (kind !== "send_followup" || !/^[A-Za-z0-9._:-]{1,100}$/.test(intentId))) throw new Error("invalid follow-up intent");
  const identity = `${kind}\0${requestId}\0${scopeVersion}` + (intentId ? `\0followup-intent-v1\0${intentId}` : "");
  return bytesToHex(new Uint8Array(await crypto.subtle.digest("SHA-256", encoder.encode(identity))));
}

export async function signOutboxFrame(secret: string, body: string): Promise<string> {
  const key = await crypto.subtle.importKey("raw", encoder.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  return bytesToHex(new Uint8Array(await crypto.subtle.sign("HMAC", key, encoder.encode(DOMAIN + body))));
}

function refusal(code: string): never {
  throw new ExecutorFailure(code, "outbox request refused; inspect the approved card and gateway configuration");
}

async function boundedJson(response: Response): Promise<Record<string, unknown>> {
  if (!response.body) throw new Error("missing response");
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    for (;;) {
      const result = await reader.read();
      if (result.done) break;
      size += result.value.length;
      if (size > 16384) throw new Error("response limit");
      chunks.push(result.value);
    }
  } finally { await reader.cancel(); }
  const bytes = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.length; }
  const value: unknown = JSON.parse(new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(bytes));
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("response shape");
  return value as Record<string, unknown>;
}

/** No provider configuration here: only a private TLS gateway that owns the journal. */
export class SignedOutboxSender implements MailSender {
  readonly name = "signed_outbox";
  private endpoint: string;
  constructor(url: string, private readonly secret: string, private readonly fetcher: typeof fetch = fetch,
    private readonly clock: () => string = () => new Date().toISOString()) {
    const endpoint = new URL(url);
    if (endpoint.protocol !== "https:" || endpoint.username || endpoint.password || endpoint.search || endpoint.hash || endpoint.pathname !== "/send" || secret.length < 32) {
      throw new Error("invalid private outbox configuration");
    }
    this.endpoint = endpoint.href;
  }

  async send(message: OutboundMessage, ctx: ExecutorContext): Promise<SendReceipt> {
    const action = await ctx.repo.action(message.action_id);
    if (!action || action.state !== "executing" || !action.approved_by || !action.approved_at ||
      action.campaign_id !== ctx.repo.campaignId || action.kind !== message.kind ||
      action.subject_id !== message.request_id || action.idempotency_key !== message.idempotency_key) refusal("outbox_approval_mismatch");
    await ctx.repo.assertExecuting(action);
    const campaign = await ctx.repo.campaign();
    if (campaign?.external_sends !== "approval_required") refusal("external_sends_disabled");
    const proposal = JSON.parse(action.proposal_json) as Record<string, unknown>;
    if (proposal.subject !== message.subject || proposal.body_md !== message.body_md || proposal.channel !== message.channel ||
      (typeof proposal.to === "string" ? proposal.to : null) !== message.to) refusal("outbox_draft_mismatch");
    if (!message.request_id || !message.to || message.channel === "portal_manual") refusal("outbox_request_required");
    const request = await ctx.repo.request(message.request_id);
    const binding = proposal.outbox_binding as Record<string, unknown> | undefined;
    if (!request || !binding || binding.agency_id !== request.agency_id || binding.scope_version !== request.scope_version ||
      binding.fee_cap_cents !== request.fee_cap_cents || !Number.isInteger(request.scope_version) || request.scope_version < 1 ||
      !Number.isInteger(request.fee_cap_cents) || request.fee_cap_cents < 0) refusal("outbox_request_changed");
    const intent = message.kind === "send_followup" ? binding.intent_id : "";
    if (typeof intent !== "string" || (message.kind === "send_followup" &&
      (intent !== action.idempotency_key || !/^[A-Za-z0-9._:-]{1,100}$/.test(intent)))) refusal("outbox_approval_mismatch");
    const canonical = await canonicalOutboxKey(message.kind, request.request_id, request.scope_version, intent);
    const frame = { v: 1, campaign_id: action.campaign_id, action_id: action.action_id,
      approved_by: action.approved_by, approved_at: action.approved_at, issued_at: this.clock(), canonical_key: canonical,
      draft: { request_id: request.request_id, agency_id: request.agency_id, scope_version: request.scope_version,
        channel: message.channel, subject: message.subject, body: message.body_md, to: message.to,
        fee_cap_cents: request.fee_cap_cents, kind: message.kind,
        ...(intent ? { intent_id: intent } : {}),
        ...(typeof proposal.in_reply_to === "string" ? { in_reply_to: proposal.in_reply_to } : {}) } };
    const body = JSON.stringify(frame);
    if (encoder.encode(body).length > 65536) refusal("outbox_frame_too_large");
    const signature = await signOutboxFrame(this.secret, body);
    // Recheck the claim after asynchronous signing, before any network effect.
    await ctx.repo.assertExecuting(action);
    try {
      const response = await this.fetcher(this.endpoint, { method: "POST", redirect: "manual",
        headers: { "Content-Type": "application/json", "X-Outbox-Signature": signature }, body,
        signal: AbortSignal.timeout(10000) });
      const receipt = await boundedJson(response);
      if (response.status !== 200) {
        const safeRefusals: Record<string, number> = { invalid_frame: 400, invalid_signature: 401,
          frame_too_large: 413, transport_not_configured: 503, draft_conflict: 409, approval_conflict: 409,
          fresh_outbox_approval_required: 409, outbox_blocked: 409, outbox_refused: 409 };
        if (Object.hasOwn(safeRefusals, String(receipt.code)) && safeRefusals[String(receipt.code)] === response.status) refusal("outbox_refused");
        throw new Error("unknown gateway outcome");
      }
      if (receipt.v !== 1 || receipt.action_id !== action.action_id || receipt.canonical_key !== canonical ||
        receipt.provider !== message.channel || typeof receipt.provider_message_id !== "string" ||
        !receipt.provider_message_id.trim() || receipt.provider_message_id.length > 1000 ||
        typeof receipt.sent_at !== "string" || receipt.sent_at.length > 40 || !receipt.sent_at.endsWith("Z") ||
        !Number.isFinite(Date.parse(receipt.sent_at))) throw new Error("receipt mismatch");
      return { provider_message_id: receipt.provider_message_id, provider: message.channel, sent_at: receipt.sent_at };
    } catch (error) {
      if (error instanceof ExecutorFailure) throw error;
      throw new ExecutorFailure("mail_send_ambiguous", "gateway delivery is uncertain; reconcile provider evidence before retrying");
    }
  }
}
