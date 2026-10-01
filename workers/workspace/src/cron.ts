// Scheduled runs: each cron tick records a run_receipt and enqueues the periodic jobs
// (intake, digest, draft_followup for overdue requests, and one newsletter_draft on the
// first run of each month). Idempotency keys include the schedule slot so a retried tick
// never duplicates work.
import { sha256Hex } from "@deflock/shared/ids";
import { Repo, type JobKind } from "./db.ts";
import type { Env } from "./env.ts";
import { newsletterManifest } from "./manifest.ts";

export interface RunSummary {
  run_id: string;
  slot: string;
  jobs_created: number;
  job_ids: string[];
}

export function scheduleSlot(scheduledTime: number): string {
  return new Date(scheduledTime).toISOString().slice(0, 16); // minute precision
}

export async function runScheduled(env: Env, scheduledTime: number, trigger = "cron"): Promise<RunSummary> {
  const repo = new Repo(env.DB, env.CAMPAIGN_ID);
  const run = await repo.startRun(trigger);
  const slot = scheduleSlot(scheduledTime);
  const jobIds: string[] = [];
  let created = 0;
  const enqueue = async (kind: JobKind, inputs: Record<string, unknown>, keyMaterial: string) => {
    const { row, inserted } = await repo.enqueueJob(kind, await sha256Hex(kind + ":" + keyMaterial), { ...inputs, run_id: run.run_id });
    if (inserted) {
      created += 1;
      jobIds.push(row.job_id);
    }
  };
  await enqueue("intake", { slot }, slot);
  await enqueue("digest", { slot }, slot);
  const today = slot.slice(0, 10);
  // First run of the month drafts the newsletter; later ticks in the same month dedupe on the key.
  await enqueue("newsletter_draft", { trigger: "monthly", month: slot.slice(0, 7), manifest: await newsletterManifest(repo, new Date(scheduledTime)) }, "monthly:" + slot.slice(0, 7));
  for (const req of await repo.overdueRequests(today)) {
    await enqueue("draft_followup", { request_id: req.request_id, due: req.determination_due }, req.request_id + ":" + today);
  }
  await repo.finishRun(run.run_id, { jobs_created: created, summary_json: JSON.stringify({ slot, job_ids: jobIds }) });
  return { run_id: run.run_id, slot, jobs_created: created, job_ids: jobIds };
}
