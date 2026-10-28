// Applies a plan step by step, capturing outputs, writing one receipt per resource, and
// stopping on the first failure with the rollback list of everything created so far.
import { randomHex } from "@deflock/shared/ids";
import { CloudflareClient, pick } from "./cloudflare.ts";
import { redactStep, resolvePlaceholders, unresolved, type PlanStep, type Receipt, type RollbackStep } from "./plan.ts";
import type { DeployResult } from "./state.ts";

export interface Bundles {
  workspace: string;
  public_site: string;
  migrations: { name: string; sql: string }[];
}

export interface ApplyOptions {
  client: CloudflareClient;
  bundles: Bundles;
  generated?: { runner_token: string; download_key: string };
  /** Perform the rollback calls on failure (default: list them only). */
  rollbackOnFailure?: boolean;
}

export const GENERATED_KEYS = ["generated.runner_token", "generated.download_key"] as const;

export async function applyPlan(plan: PlanStep[], opts: ApplyOptions): Promise<DeployResult & { generated: { runner_token: string; download_key: string } }> {
  const generated = opts.generated ?? { runner_token: "rt_" + randomHex(32), download_key: randomHex(32) };
  const outputs: Record<string, string> = {
    "generated.runner_token": generated.runner_token,
    "generated.download_key": generated.download_key,
    migrations: opts.bundles.migrations.map((m) => m.sql).join("\n"),
  };
  const receipts: Receipt[] = [];
  const rollback: RollbackStep[] = [];
  let failed: string | null = null;
  let error: string | null = null;

  for (const step of plan) {
    const path = resolvePlaceholders(step.path, outputs);
    const body = step.literal_body ? step.body : resolvePlaceholders(step.body, outputs);
    const recordBody = step.literal_body ? redactStep(step).body : resolvePlaceholders(redactStep(step).body, outputs);
    const missing = unresolved(step.literal_body ? [path] : [path, body]);
    let result;
    if (missing.length) {
      result = { status: 0, ok: false, data: null, error: "unresolved placeholders: " + missing.join(", ") };
    } else if (step.kind === "worker_upload") {
      const b = body as { bundle: "workspace" | "public_site"; metadata: Record<string, unknown> };
      result = await opts.client.uploadWorker(path, b.metadata, { name: "index.mjs", content: opts.bundles[b.bundle] });
    } else {
      result = await opts.client.call(step.method, path, body, recordBody);
    }
    const captured: Record<string, string> = {};
    if (result.ok) {
      for (const [key, dotted] of Object.entries(step.captures)) {
        const v = pick(result.data, dotted);
        if (v !== null) {
          captured[key] = v;
          outputs[key] = v;
        }
      }
    }
    receipts.push({
      step_id: step.id,
      resource: step.resource,
      method: step.method,
      path,
      status: result.status || null,
      ok: result.ok,
      dry_run: opts.client.opts.dryRun,
      outputs: captured,
      error: result.error,
      at: new Date().toISOString(),
    });
    if (result.ok && step.rollback) {
      rollback.unshift({
        step_id: step.id,
        resource: step.resource,
        method: step.rollback.method,
        path: resolvePlaceholders(step.rollback.path, outputs),
        body: step.rollback.body,
      });
    }
    if (!result.ok) {
      failed = step.id;
      error = result.error;
      break;
    }
  }

  let status: DeployResult["status"] = failed ? "failed" : "applied";
  if (failed && opts.rollbackOnFailure) {
    for (const rb of rollback) await opts.client.call(rb.method, rb.path, rb.body ?? null);
    status = "rolled_back";
  }
  const publicOutputs: Record<string, string> = {};
  for (const [k, v] of Object.entries(outputs)) if (!k.startsWith("generated.") && k !== "migrations") publicOutputs[k] = v;
  return { dry_run: opts.client.opts.dryRun, status, receipts, rollback, failed_step: failed, error, finished_at: new Date().toISOString(), outputs: publicOutputs, generated };
}
