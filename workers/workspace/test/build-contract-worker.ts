// Test-only entry point. Bundled solely by build-contract-bridge.mjs into a temp file;
// never included in the workspace Worker or deployed. All rows and identities are synthetic.
import { Hono } from "hono";
import { contentHash, type JsonObject } from "@deflock/shared/review";
import { Repo } from "../src/db.ts";
import { runnerApi } from "../src/runner.ts";
import { approveAction, executeAction } from "../src/approvals.ts";
import { issueCorrection, proposePublication, submitReview, withdrawFinding } from "../src/publication.ts";
import { proposeRebuild } from "../src/manifest.ts";
import type { AccessIdentity } from "../src/auth.ts";
import type { Env } from "../src/env.ts";

const app = new Hono<{ Bindings: Env }>();
const findingId = "fnd_0000000000000001";
const identity = (email: string): AccessIdentity => ({ email, sub: email, issued_at: 1, expires_at: 9999999999 });
const organizer = identity("organizer@example.invalid");
app.onError((error, c) => c.json({ error: error.message }, (error as { status?: 400 }).status ?? 500));
app.route("/api/runner", runnerApi);

app.post("/__fixture/:operation", async (c) => {
  const repo = new Repo(c.env.DB, c.env.CAMPAIGN_ID);
  const operation = c.req.param("operation");
  if (operation === "seed") {
    await repo.createCampaign({ name: "Synthetic County Records", jurisdiction: "us-ca", county_fips: "06000",
      county_name: "Synthetic", place_fips: null, place_name: null, public_hostname: "campaign.example.invalid",
      workspace_hostname: "workspace.example.invalid", privacy_tier: "redacted_cloud", law_package_status: "draft",
      external_sends: "approval_required", schedule_cron: "0 7 * * *", timezone: "Etc/UTC" });
    const doc: JsonObject = { id: findingId, title: "Synthetic policy produced", slug: "synthetic-policy",
      author: "author@example.invalid", summary: "The fictional agency produced a synthetic policy.",
      body_md: "Synthetic test material only.", classification: "documented_fact", confidence: "verified",
      event_date: "2026-01-01", sources: [{ sha256: "a".repeat(64), locator: "page:1", title: "Synthetic policy" }],
      limitations: ["Synthetic fixture only."], counterevidence: [] };
    await repo.createFinding({ finding_id: findingId, author: String(doc.author), summary: String(doc.summary),
      classification: "documented_fact", confidence: "verified", finding_json: JSON.stringify(doc),
      content_sha256: await contentHash(doc), state: "draft" });
    for (const [email, role] of [["factual@example.invalid", "factual"], ["legal@example.invalid", "legal"], ["privacy@example.invalid", "privacy"]]) {
      await submitReview(repo, (await repo.finding(findingId))!, identity(email), { role, decision: "approve", rationale: "Synthetic independent check" });
    }
    return c.json({ finding: await repo.finding(findingId) });
  }
  if (operation === "publish") {
    const proposed = await proposePublication(repo, (await repo.finding(findingId))!, organizer);
    let unapprovedStatus = 0;
    try { await executeAction(repo, c.env, proposed.row.action_id, organizer); }
    catch (error) { unapprovedStatus = (error as { status: number }).status; }
    await approveAction(repo, proposed.row.action_id, organizer);
    const action = await executeAction(repo, c.env, proposed.row.action_id, organizer);
    if (action.state !== "executed") throw new Error(action.error ?? "fixture publication failed");
    const again = await executeAction(repo, c.env, proposed.row.action_id, organizer);
    return c.json({ ...JSON.parse(action.provider_receipt!), unapproved_status: unapprovedStatus,
      repeat_receipt: again.provider_receipt === action.provider_receipt });
  }
  if (operation === "correct") return c.json(await issueCorrection(repo, (await repo.finding(findingId))!, organizer,
    { reason: "Internal synthetic correction detail", replacement_finding_id: null }));
  if (operation === "withdraw") return c.json(await withdrawFinding(repo, (await repo.finding(findingId))!, organizer,
    "Internal synthetic withdrawal detail"));
  if (operation === "unsupported-map") {
    await repo.putSetting("map_config", { center: [-120.75, 35.25], zoom: 11 });
    return c.json({ configured: true });
  }
  if (operation === "rebuild") return c.json(await proposeRebuild(repo, organizer.email, "Repeat synthetic build"));
  if (operation === "snapshot") {
    const objects = await c.env.PUBLIC_BUCKET.list();
    return c.json({ jobs: await repo.jobs(), actions: await repo.actions(), finding: await repo.finding(findingId),
      site_version: await repo.setting("site_version"),
      files: await Promise.all(objects.objects.map(async ({ key }) => ({ key, text: await (await c.env.PUBLIC_BUCKET.get(key))!.text() }))) });
  }
  return c.json({ error: "unknown fixture operation" }, 404);
});

export default app;
