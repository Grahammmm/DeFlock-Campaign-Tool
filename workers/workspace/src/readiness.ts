import type { Repo } from "./db.ts";

export const MAX_AGE_SECONDS = 36 * 60 * 60;
export const COUNT_KEYS = ["stale_contacts", "stale_inputs", "stale_comment_kits", "contracts_ending", "correction_requests_open_over_14_days"] as const;
export interface ReadinessSnapshot {
  schema_version: 1;
  generated_at: string;
  campaign_id: string;
  law_package_status: string;
  production_ready: boolean;
  safe_to_send_automatically: boolean;
  next_actions: string[];
  counts: Record<string, number>;
}
const KEYS = ["schema_version", "generated_at", "campaign_id", "law_package_status", "production_ready", "safe_to_send_automatically", "next_actions", "counts"];
function object(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}
function exactKeys(value: Record<string, unknown>, keys: readonly string[]): string | null {
  return keys.find(key => !Object.hasOwn(value, key)) ?? Object.keys(value).find(key => !keys.includes(key)) ?? null;
}
// UTC RFC3339 only; reject date normalization (e.g. February 30), not just Date.parse failure.
function timestamp(value: unknown): value is string {
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|\+00:00)$/.test(value)) return false;
  const ms = Date.parse(value);
  return Number.isFinite(ms) && new Date(ms).toISOString().slice(0, 19) === value.slice(0, 19);
}
/** The Worker validates every field; it never trusts the runner's projection. */
export function readinessError(value: unknown): string | null {
  if (!object(value)) return "snapshot";
  const key = exactKeys(value, KEYS);
  if (key !== null) return key;
  if (value.schema_version !== 1) return "schema_version";
  if (!timestamp(value.generated_at)) return "generated_at";
  if (typeof value.campaign_id !== "string" || !value.campaign_id.trim()) return "campaign_id";
  if (!["draft", "reviewed", "missing"].includes(String(value.law_package_status)) || typeof value.law_package_status !== "string") return "law_package_status";
  for (const key of ["production_ready", "safe_to_send_automatically"]) if (typeof value[key] !== "boolean") return key;
  if (!Array.isArray(value.next_actions) || value.next_actions.length > 40 || value.next_actions.some(s => typeof s !== "string" || [...s].length > 240)) return "next_actions";
  if (!object(value.counts)) return "counts";
  const countKey = exactKeys(value.counts, COUNT_KEYS);
  if (countKey !== null) return "counts." + countKey;
  for (const key of COUNT_KEYS) if (!Number.isInteger(value.counts[key]) || Number(value.counts[key]) < 0) return "counts." + key;
  return null;
}

export interface ReadinessResult {
  present: boolean;
  current: boolean;
  reason: string | null;
  generated_at: string | null;
  age_seconds: number | null;
  next_actions: string[];
  law_package_status: string | null;
  counts: Record<string, number>;
}
export async function readReadiness(repo: Repo, now: Date): Promise<ReadinessResult> {
  const empty: ReadinessResult = { present: false, current: false, reason: "absent", generated_at: null, age_seconds: null, next_actions: [], law_package_status: null, counts: {} };
  let value: unknown;
  try {
    value = await repo.setting("readiness_snapshot");
  } catch (error) {
    if (!(error instanceof SyntaxError)) throw error;
    return { ...empty, present: true, reason: "unparseable" };
  }
  if (value === null) {
    // Distinguish JSON null from an absent row without changing Repo's setting API.
    const present = (await repo.settings()).some(row => row.key === "readiness_snapshot");
    return present ? { ...empty, present: true, reason: "unparseable" } : empty;
  }
  if (readinessError(value)) return { ...empty, present: true, reason: "unparseable" };
  const snapshot = value as ReadinessSnapshot;
  const age = (now.getTime() - Date.parse(snapshot.generated_at)) / 1000;
  const reason = snapshot.campaign_id !== repo.campaignId ? "wrong_campaign" :
    !Number.isFinite(age) || age < -300 ? "unparseable" : age > MAX_AGE_SECONDS ? "stale" : null;
  return { present: true, current: reason === null, reason, generated_at: snapshot.generated_at, age_seconds: age,
    next_actions: reason === "wrong_campaign" || reason === "unparseable" ? [] : snapshot.next_actions,
    law_package_status: snapshot.law_package_status, counts: snapshot.counts };
}
