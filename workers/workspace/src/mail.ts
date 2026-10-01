// Inbound mail: raw MIME is preserved in R2 as mail/<sha256>, a correspondence row is
// created (deduplicated on Message-ID) and a classify_mail job is enqueued. Nothing here
// replies, forwards or classifies; the runner proposes classifications for review.
import { nowIso, sha256Hex } from "@deflock/shared/ids";
import { Repo } from "./db.ts";
import type { Env } from "./env.ts";

export interface InboundMail {
  from: string;
  to: string;
  headers: Headers;
  raw: Uint8Array;
}

export interface InboundResult {
  correspondence_id: string;
  raw_sha256: string;
  deduplicated: boolean;
  job_id: string | null;
}

function messageId(headers: Headers): string | null {
  const raw = headers.get("message-id");
  if (!raw) return null;
  const m = /<([^>]+)>/.exec(raw);
  return (m ? m[1] : raw).trim() || null;
}

export async function handleInboundMail(mail: InboundMail, env: Env): Promise<InboundResult> {
  const repo = new Repo(env.DB, env.CAMPAIGN_ID);
  const sha = await sha256Hex(mail.raw);
  const key = "mail/" + sha;
  if (!(await env.ORIGINALS.head(key))) {
    await env.ORIGINALS.put(key, mail.raw, { httpMetadata: { contentType: "message/rfc822" } });
  }
  await repo.putOriginal({ sha256: sha, byte_count: mail.raw.byteLength, media_type: "message/rfc822", r2_key: key, stored_at: nowIso() });
  const mid = messageId(mail.headers) ?? "sha256:" + sha;
  const dateHeader = mail.headers.get("date");
  const received = dateHeader && !Number.isNaN(Date.parse(dateHeader)) ? new Date(dateHeader).toISOString() : nowIso();
  const { row, inserted } = await repo.createCorrespondence({
    request_id: null,
    direction: "inbound",
    channel: "email",
    provider_message_id: mid,
    from_addr: mail.from,
    to_addr: mail.to,
    subject: mail.headers.get("subject"),
    received_at: received,
    raw_sha256: sha,
    classification: "unclassified",
    classification_confidence: null,
    summary: null,
  });
  let jobId: string | null = null;
  if (inserted) {
    const key = await sha256Hex("classify_mail" + sha);
    const job = await repo.enqueueJob("classify_mail", key, { correspondence_id: row.correspondence_id, raw_sha256: sha });
    jobId = job.row.job_id;
  }
  return { correspondence_id: row.correspondence_id, raw_sha256: sha, deduplicated: !inserted, job_id: jobId };
}

async function readAll(stream: ReadableStream<Uint8Array>, size?: number): Promise<Uint8Array> {
  const chunks: Uint8Array[] = [];
  const reader = stream.getReader();
  let total = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    chunks.push(value);
    total += value.byteLength;
  }
  const out = new Uint8Array(size ?? total);
  let off = 0;
  for (const c of chunks) {
    out.set(c, off);
    off += c.byteLength;
  }
  return out;
}

/** Adapter for the Worker `email()` handler. */
export async function emailHandler(message: ForwardableEmailMessage, env: Env): Promise<void> {
  const raw = await readAll(message.raw, message.rawSize);
  await handleInboundMail({ from: message.from, to: message.to, headers: message.headers, raw }, env);
}
