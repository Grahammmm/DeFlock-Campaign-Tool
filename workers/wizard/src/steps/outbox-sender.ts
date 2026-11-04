// Outbox sender step (screen 5, default skip). Registers the private signed outbox gateway
// as one both-or-neither pair of Worker secrets. Registering a sender is approval to be
// able to send, never approval to send a particular thing: the workspace still executes
// send_request/send_followup only for cards carrying a recorded human approval, and this
// step adds no flag or setting that skips that gate.
import { html, type Safe } from "@deflock/shared/html";

export interface OutboxSender {
  gateway_url: string;
  signing_key: string;
}

export type OutboxSenderStatus = "registered" | "skipped";

export type OutboxSenderResult =
  | { status: "skipped"; sender: null; error: null }
  | { status: "registered"; sender: OutboxSender; error: null }
  | { status: "refused"; sender: null; error: string };

export const HALF_CONFIGURED = "outbox_sender: half configured — both settings or neither";

const refuse = (error: string): OutboxSenderResult => ({ status: "refused", sender: null, error });

/** Gateway endpoint: https, a host, path exactly /send, no credentials, query or fragment. */
export function outboxUrlProblem(raw: string): string | null {
  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    return "outbox_sender: gateway URL is not a URL";
  }
  if (url.protocol !== "https:") return "outbox_sender: gateway URL must use https";
  if (!url.hostname) return "outbox_sender: gateway URL needs a host";
  if (url.username || url.password) return "outbox_sender: gateway URL must not carry credentials";
  if (url.search || raw.includes("?")) return "outbox_sender: gateway URL must not carry a query";
  if (url.hash || raw.includes("#")) return "outbox_sender: gateway URL must not carry a fragment";
  if (url.pathname !== "/send") return "outbox_sender: gateway URL path must be exactly /send";
  return null;
}

/** Signing secret: base64url (no padding) decoding to at least 32 bytes, i.e. 43+ characters. */
export function signingKeyProblem(key: string): string | null {
  if (!/^[A-Za-z0-9_-]+$/.test(key)) return "outbox_sender: signing secret must be base64url (A-Z a-z 0-9 - _)";
  if (Math.floor((key.length * 6) / 8) < 32) return "outbox_sender: signing secret must be at least 32 bytes of base64url";
  return null;
}

/**
 * Both given -> registered; both omitted -> skipped; exactly one -> refused by name.
 * Error strings never contain the secret.
 */
export function parseOutboxSender(rawUrl: string, rawKey: string): OutboxSenderResult {
  const url = rawUrl.trim();
  const key = rawKey.trim();
  if (!url && !key) return { status: "skipped", sender: null, error: null };
  if (!url || !key) return refuse(HALF_CONFIGURED);
  const problem = outboxUrlProblem(url) ?? signingKeyProblem(key);
  if (problem) return refuse(problem);
  return { status: "registered", sender: { gateway_url: url, signing_key: key }, error: null };
}

/** The pair of Worker secrets for the plan, or nothing at all. */
export function outboxSecrets(sender: OutboxSender | null | undefined): [name: string, value: string][] {
  if (!sender) return [];
  return [["OUTBOX_GATEWAY_URL", sender.gateway_url], ["OUTBOX_SIGNING_KEY", sender.signing_key]];
}

/** Form section. The URL may be shown again; the secret is never echoed. */
export function outboxSenderFields(sender: OutboxSender | null | undefined): Safe {
  const registered = sender ? html`<p class="meta">A sender is registered in this session. The secret is not shown again; leaving both fields empty on a new submit removes the registration.</p>` : "";
  return html`<h2>Outbox sender (optional; default: skip)</h2>
<p>Leave both empty to skip: requests stay drafted and nothing can be sent. Fill both to register the private signed outbox gateway (docs/HOSTED-OUTBOX-GATEWAY.md). Every send still needs a recorded organizer approval.</p>
${registered}
<label class="field">Gateway URL (https://host/send) <input type="url" name="outbox_gateway_url" value="${sender?.gateway_url ?? ""}"></label>
<label class="field">Signing secret (32+ bytes of base64url) <input type="password" name="outbox_signing_key" autocomplete="off"></label>`;
}
