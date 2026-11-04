// Legistar poll: runScheduled calls pollLegistar once per cron slot, inline (no job row, the
// runner is not involved). Each configured feed is one bounded https GET; events whose agenda
// items match the one generated ALPR keyword filter are filed through repo.fileMeeting /
// fileMeetingRevision. An undated event is filed date_state 'unknown' with starts_at NULL, never
// guessed. A failing feed becomes one deduplicated incident naming the feed (no response body),
// files nothing, and makes the slot `partial`; the other feeds still run. Only public agenda
// identifiers, dates, URLs, hashes and counts are stored: no item text, comment or attendee data.
import { sha256Hex } from "@deflock/shared/ids";
import { matchesMeetingKeywords } from "@deflock/shared/generated/meeting-keywords";
import type { MeetingFiling, Repo } from "./db.ts";
import type { Env, ProviderFetch } from "./env.ts";

// Mirrors campaign_tool/meetings.py (MAX_EVENTS, MAX_BYTES, TIMEOUT); never loosen.
export const MAX_EVENTS = 60;
export const MAX_BYTES = 4 * 1024 * 1024;
export const TIMEOUT_MS = 20_000;
const DEFAULT_ZONE = "America/Los_Angeles";
const ITEM_KEYS = ["EventItemTitle", "EventItemMatterName", "EventItemMatterFile", "EventItemActionText"] as const;

export interface LegistarPollResult {
  outcome: "ok" | "partial" | "not_configured";
  filed: number;
  revised: number;
  discarded: number;
  past: number;
  failed_feeds: string[];
}

export interface PollOptions {
  fetch?: ProviderFetch;
  timeoutMs?: number;
  now?: () => Date;
}

class FeedFailure extends Error {}

/** Configured feed strings (whitespace or comma separated), de-duplicated, in order. */
export function legistarFeeds(env: Pick<Env, "LEGISTAR_FEEDS">): string[] {
  return [...new Set((env.LEGISTAR_FEEDS ?? "").split(/[\s,]+/).filter(Boolean))];
}

/** Readiness text for the one optional setting; never echoes a feed URL. */
export function legistarReadiness(env: Pick<Env, "LEGISTAR_FEEDS">): string {
  const n = legistarFeeds(env).length;
  return n ? `legistar_poll: configured (${n} feed${n === 1 ? "" : "s"})` : "legistar_poll: not configured";
}

/** host + path of a feed: names it in incidents and summaries without userinfo or query. */
function feedName(raw: string, index: number): string {
  try {
    const u = new URL(raw);
    return (u.host + u.pathname).slice(0, 200);
  } catch {
    return `#${index + 1}`;
  }
}

/** The feed URL, or a refusal by name: https only, no credentials. */
function checkFeed(raw: string): URL {
  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    throw new FeedFailure("invalid_url");
  }
  if (url.protocol !== "https:") throw new FeedFailure("not_https");
  if (url.username || url.password) throw new FeedFailure("credentials_in_url");
  return url;
}

/** One GET, no redirect followed, bounded by time and bytes; returns the parsed JSON list. */
async function fetchFeed(url: URL, fetchImpl: ProviderFetch, timeoutMs: number): Promise<unknown[]> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    let res: Response;
    try {
      res = await fetchImpl(url.toString(), {
        method: "GET", redirect: "manual", signal: controller.signal,
        headers: { accept: "application/json", "user-agent": "deflock-campaign-tool/0.1 (+legistar-poll)" },
      });
    } catch {
      throw new FeedFailure(controller.signal.aborted ? "timeout" : "fetch_failed");
    }
    if (res.status >= 300 && res.status < 400) {
      await res.body?.cancel();
      throw new FeedFailure("redirect_refused");
    }
    if (!res.ok) {
      await res.body?.cancel();
      throw new FeedFailure(`http_${res.status}`);
    }
    if (Number(res.headers.get("content-length") ?? 0) > MAX_BYTES) {
      await res.body?.cancel();
      throw new FeedFailure("response_too_large");
    }
    const chunks: Uint8Array[] = [];
    let size = 0;
    if (res.body) {
      const reader = res.body.getReader();
      try {
        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          size += value.byteLength;
          if (size > MAX_BYTES) {
            await reader.cancel();
            throw new FeedFailure("response_too_large");
          }
          chunks.push(value);
        }
      } catch (err) {
        if (err instanceof FeedFailure) throw err;
        throw new FeedFailure(controller.signal.aborted ? "timeout" : "fetch_failed");
      }
    }
    const bytes = new Uint8Array(size);
    let at = 0;
    for (const c of chunks) { bytes.set(c, at); at += c.byteLength; }
    let payload: unknown;
    try {
      payload = JSON.parse(new TextDecoder("utf-8", { fatal: true, ignoreBOM: false }).decode(bytes));
    } catch {
      throw new FeedFailure("not_json");
    }
    if (!Array.isArray(payload)) throw new FeedFailure("not_a_list");
    if (payload.length > MAX_EVENTS) throw new FeedFailure("too_many_events");
    return payload;
  } finally {
    clearTimeout(timer);
  }
}

/** Agenda-item text from an expanded event; unavailable items are not a non-match. */
export function eventItemTexts(event: Record<string, unknown>): string[] {
  if (!Array.isArray(event.EventItems)) throw new FeedFailure("agenda_items_unavailable");
  const items = event.EventItems;
  return items.flatMap((item) => {
    if (!item || typeof item !== "object") return [];
    const parts = ITEM_KEYS.map((k) => (item as Record<string, unknown>)[k]).filter((v): v is string => typeof v === "string");
    const text = parts.join(" "); // unnormalized: the decision is matchesMeetingKeywords' on the item's own text
    return text.trim() ? [text] : [];
  });
}

/** The poll's one matching decision: an agenda item matches the generated keyword filter. */
export function eventMatches(event: Record<string, unknown>): boolean {
  return eventItemTexts(event).some((text) => matchesMeetingKeywords(text));
}

function zoneOffsetMs(utcMs: number, zone: string): number {
  const parts = Object.fromEntries(new Intl.DateTimeFormat("en-US", {
    timeZone: zone, hourCycle: "h23", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit",
  }).formatToParts(utcMs).map((p) => [p.type, p.value]));
  const wall = Date.UTC(+parts.year!, +parts.month! - 1, +parts.day!, +parts.hour!, +parts.minute!, +parts.second!);
  return wall - Math.floor(utcMs / 1000) * 1000;
}

/** UTC ISO start from EventDate + EventTime, local to `zone`; null if either is unknown. */
export function eventStartsAt(event: Record<string, unknown>, zone: string): string | null {
  const day = typeof event.EventDate === "string" ? event.EventDate.slice(0, 10) : "";
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(day);
  if (!m) return null;
  const [y, mo, d] = [+m[1]!, +m[2]!, +m[3]!];
  const check = new Date(Date.UTC(y, mo - 1, d));
  if (check.getUTCFullYear() !== y || check.getUTCMonth() !== mo - 1 || check.getUTCDate() !== d) return null;
  const t = typeof event.EventTime === "string" ? /^(\d{1,2}):(\d{2})\s*([AP])M$/i.exec(event.EventTime.trim()) : null;
  if (!t || +t[1]! < 1 || +t[1]! > 12 || +t[2]! >= 60) return null;
  const hour = (+t[1]! % 12) + (t[3]!.toUpperCase() === "P" ? 12 : 0);
  const minute = +t[2]!;
  let utc = Date.UTC(y, mo - 1, d, hour, minute);
  try {
    for (let i = 0; i < 2; i++) utc = Date.UTC(y, mo - 1, d, hour, minute) - zoneOffsetMs(utc, zone);
  } catch {
    return eventStartsAt(event, DEFAULT_ZONE);
  }
  return new Date(utc).toISOString();
}

function plainBody(value: unknown): string {
  const text = String(typeof value === "string" && value.trim() ? value : "Unknown body").replace(/[<>]/g, "");
  return text.split(/\s+/).filter(Boolean).join(" ").slice(0, 200) || "Unknown body";
}

async function filingsFor(url: URL, events: unknown[], zone: string, fetchedAt: string): Promise<{ filings: MeetingFiling[]; discarded: number }> {
  const filings: MeetingFiling[] = [];
  let discarded = 0;
  const base = url.origin + url.pathname.replace(/\/+$/, "");
  for (const event of events) {
    if (!event || typeof event !== "object" || !Number.isSafeInteger((event as Record<string, unknown>).EventId)
      || !eventMatches(event as Record<string, unknown>)) {
      discarded += 1;
      continue;
    }
    const e = event as Record<string, unknown>;
    const startsAt = eventStartsAt(e, zone);
    const bodyName = plainBody(e.EventBodyName);
    filings.push({
      body_name: bodyName, agency_id: null, starts_at: startsAt, date_state: startsAt ? "known" : "unknown",
      agenda_url: null, agenda_item: null, relevance: "alpr_item", source: "legistar",
      source_url: `${base}/${e.EventId}`, item_id: String(e.EventId), fetched_at: fetchedAt,
      title_sha256: await sha256Hex(JSON.stringify([bodyName, e.EventDate ?? null, e.EventTime ?? null, eventItemTexts(e)])),
    });
  }
  return { filings, discarded };
}

/** Poll every configured feed once for this slot and file matching meetings. */
export async function pollLegistar(env: Env, repo: Repo, slot: string, runId: string, opts: PollOptions = {}): Promise<LegistarPollResult> {
  const result: LegistarPollResult = { outcome: "ok", filed: 0, revised: 0, discarded: 0, past: 0, failed_feeds: [] };
  const feeds = legistarFeeds(env);
  if (!feeds.length) return { ...result, outcome: "not_configured" };
  const zone = (await repo.campaign())?.timezone || DEFAULT_ZONE;
  const fetchImpl = opts.fetch ?? fetch;
  for (const [index, raw] of feeds.entries()) {
    const name = feedName(raw, index);
    try {
      const url = checkFeed(raw);
      const fetchedAt = (opts.now?.() ?? new Date()).toISOString();
      const events = await fetchFeed(url, fetchImpl, opts.timeoutMs ?? TIMEOUT_MS);
      const { filings, discarded } = await filingsFor(url, events, zone, fetchedAt);
      result.discarded += discarded;
      for (const filing of filings) {
        let filed: boolean;
        try {
          filed = (await repo.fileMeeting(filing)).inserted;
          if (filed) result.filed += 1;
        } catch (err) {
          const previous = /^meeting_revision_required: (\S+)$/.exec(err instanceof Error ? err.message : "");
          if (!previous) throw err;
          await repo.fileMeetingRevision(previous[1]!, filing);
          result.revised += 1;
          filed = true;
        }
        if (filed && filing.starts_at !== null && filing.starts_at < fetchedAt) result.past += 1;
      }
    } catch (err) {
      const reason = err instanceof FeedFailure ? err.message : "file_failed";
      result.failed_feeds.push(name);
      await repo.raiseIncident("legistar_poll:" + name, "warning",
        `legistar_poll: feed ${name} failed (${reason}) in slot ${slot}, run ${runId}`);
    }
  }
  if (result.failed_feeds.length) result.outcome = "partial";
  return result;
}
