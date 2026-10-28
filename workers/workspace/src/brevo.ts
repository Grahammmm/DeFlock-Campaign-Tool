// Brevo v3 API client for the send_newsletter executor. It creates an email campaign for
// one existing list and sends it. It never creates, imports or reads contacts: consent,
// suppression and unsubscribe stay inside the provider (docs/NEWSLETTER.md).
import type { ProviderFetch } from "./env.ts";

export const BREVO_API_BASE = "https://api.brevo.com/v3";

export interface BrevoSender {
  name: string;
  email: string;
}

export interface CampaignInput {
  name: string;
  subject: string;
  sender: BrevoSender;
  htmlContent: string;
  textContent?: string;
  listIds: number[];
  replyTo?: string;
  /** Idempotency tag stored in the campaign name/tag so a retry can be recognised. */
  tag?: string;
}

export interface CampaignReceipt {
  id: number;
}

export interface BrevoClient {
  readonly provider: "brevo";
  createEmailCampaign(input: CampaignInput): Promise<CampaignReceipt>;
  sendCampaignNow(id: number): Promise<void>;
}

export class BrevoError extends Error {
  constructor(
    message: string,
    public readonly status: number,
    public readonly code: string | null = null,
  ) {
    super(message);
  }
}

/** Real client. `fetcher` is injectable for tests; the API key never leaves this object. */
export class FetchBrevo implements BrevoClient {
  readonly provider = "brevo" as const;
  constructor(
    private readonly apiKey: string,
    private readonly fetcher: ProviderFetch = fetch,
    private readonly base: string = BREVO_API_BASE,
  ) {
    if (!apiKey) throw new Error("Brevo API key missing");
  }

  private async call<T>(method: "POST", path: string, body: unknown): Promise<T> {
    const res = await this.fetcher(this.base + path, {
      method,
      headers: { "api-key": this.apiKey, "content-type": "application/json", accept: "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      let code: string | null = null;
      let detail = "";
      try {
        const err = (await res.json()) as { code?: string; message?: string };
        code = err.code ?? null;
        detail = err.message ?? "";
      } catch {
        detail = await res.text().catch(() => "");
      }
      throw new BrevoError(`Brevo ${method} ${path} failed with ${res.status}${detail ? ": " + detail : ""}`, res.status, code);
    }
    if (res.status === 204) return undefined as T;
    return (await res.json()) as T;
  }

  async createEmailCampaign(input: CampaignInput): Promise<CampaignReceipt> {
    const payload: Record<string, unknown> = {
      name: input.name,
      subject: input.subject,
      sender: { name: input.sender.name, email: input.sender.email },
      htmlContent: input.htmlContent,
      recipients: { listIds: input.listIds },
      inlineImageActivation: false,
      mirrorActive: false,
    };
    if (input.textContent) payload.textContent = input.textContent;
    if (input.replyTo) payload.replyTo = input.replyTo;
    if (input.tag) payload.tag = input.tag;
    const out = await this.call<{ id: number }>("POST", "/emailCampaigns", payload);
    if (typeof out?.id !== "number") throw new BrevoError("Brevo returned no campaign id", 502);
    return { id: out.id };
  }

  async sendCampaignNow(id: number): Promise<void> {
    await this.call<void>("POST", `/emailCampaigns/${id}/sendNow`, {});
  }
}

/** Test double: records every call, hands out sequential ids, can be told to fail. */
export class FakeBrevo implements BrevoClient {
  readonly provider = "brevo" as const;
  readonly created: CampaignInput[] = [];
  readonly sent: number[] = [];
  failCreate: BrevoError | null = null;
  failSend: BrevoError | null = null;
  private nextId = 1001;

  async createEmailCampaign(input: CampaignInput): Promise<CampaignReceipt> {
    if (this.failCreate) throw this.failCreate;
    this.created.push(input);
    return { id: this.nextId++ };
  }

  async sendCampaignNow(id: number): Promise<void> {
    if (this.failSend) throw this.failSend;
    if (!this.created.length || id >= this.nextId) throw new BrevoError("unknown campaign " + id, 404);
    this.sent.push(id);
  }
}
