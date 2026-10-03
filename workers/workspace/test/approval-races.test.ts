import { describe, expect, it, vi } from "vitest";
import { approveAction, editAction, rejectAction } from "../src/approvals.ts";
import type { AccessIdentity } from "../src/auth.ts";
import type { Repo } from "../src/db.ts";
import { seedCampaign } from "./helpers.ts";

const ORG: AccessIdentity = { email: "organizer@example.invalid", sub: "s", issued_at: 0, expires_at: 0 };

/** Reproduce a second transaction after the first operator's SELECT, before its UPDATE. */
function afterRead(repo: Repo, mutate: () => Promise<unknown>) {
  const read = repo.action.bind(repo);
  return vi.spyOn(repo, "action").mockImplementationOnce(async id => {
    const snapshot = await read(id);
    await mutate();
    return snapshot;
  });
}

describe("conditional approval transitions", () => {
  it("does not approve a draft edited after its read", async () => {
    const repo = await seedCampaign();
    const { row } = await repo.propose("deploy_site", null, { site_version: "old" }, "race-approve", "fixture");
    const spy = afterRead(repo, () => editAction(repo, row.action_id, ORG, { site_version: "new" }));
    try { await expect(approveAction(repo, row.action_id, ORG)).rejects.toThrow(/changed during approval/); }
    finally { spy.mockRestore(); }
    const current = (await repo.action(row.action_id))!;
    expect(current.state).toBe("proposed");
    expect(current.approved_by).toBeNull();
    expect(JSON.parse(current.proposal_json).site_version).toBe("new");
  });

  it("does not reject an execution already claimed after its read", async () => {
    const repo = await seedCampaign();
    const { row } = await repo.propose("deploy_site", null, {}, "race-reject", "fixture");
    await approveAction(repo, row.action_id, ORG);
    let claimMarker: string | null = null;
    const spy = afterRead(repo, async () => { const claim = await repo.claimForExecution(row.action_id); claimMarker = claim!.error; });
    try { await expect(rejectAction(repo, row.action_id, ORG, "stop")).rejects.toThrow(/changed during rejection/); }
    finally { spy.mockRestore(); }
    expect((await repo.action(row.action_id))!.state).toBe("executing");
    expect(claimMarker).not.toBeNull();
    expect((await repo.action(row.action_id))!.error).toBe(claimMarker);
  });

  it("does not edit a proposal concurrently approved", async () => {
    const repo = await seedCampaign();
    const { row } = await repo.propose("deploy_site", null, { site_version: "reviewed" }, "race-edit", "fixture");
    const spy = afterRead(repo, () => approveAction(repo, row.action_id, ORG));
    try { await expect(editAction(repo, row.action_id, ORG, { site_version: "unreviewed" })).rejects.toThrow(/changed during editing/); }
    finally { spy.mockRestore(); }
    const current = (await repo.action(row.action_id))!;
    expect(current.state).toBe("approved");
    expect(JSON.parse(current.proposal_json).site_version).toBe("reviewed");
  });

  it("preserves the winning edit instead of losing it to a stale merge", async () => {
    const repo = await seedCampaign();
    const { row } = await repo.propose("deploy_site", null, {}, "race-two-edits", "fixture");
    const spy = afterRead(repo, () => editAction(repo, row.action_id, ORG, { winning: true }));
    try { await expect(editAction(repo, row.action_id, ORG, { stale: true })).rejects.toThrow(/changed during editing/); }
    finally { spy.mockRestore(); }
    const proposal = JSON.parse((await repo.action(row.action_id))!.proposal_json);
    expect(proposal.winning).toBe(true);
    expect(proposal.stale).toBeUndefined();
  });

  it("does not overwrite a successful receipt with a late rejection", async () => {
    const repo = await seedCampaign();
    const { row } = await repo.propose("deploy_site", null, {}, "race-receipt", "fixture");
    await approveAction(repo, row.action_id, ORG);
    const spy = afterRead(repo, () => repo.updateAction(row.action_id, { state: "executed", provider_receipt: "synthetic receipt" }));
    try { await expect(rejectAction(repo, row.action_id, ORG, "stop")).rejects.toThrow(/changed during rejection/); }
    finally { spy.mockRestore(); }
    const current = (await repo.action(row.action_id))!;
    expect(current.state).toBe("executed");
    expect(current.provider_receipt).toBe("synthetic receipt");
  });

  it("allows only one concurrent approver to win", async () => {
    const repo = await seedCampaign();
    const { row } = await repo.propose("deploy_site", null, {}, "race-two-approvers", "fixture");
    const results = await Promise.allSettled([approveAction(repo, row.action_id, ORG),
      approveAction(repo, row.action_id, { ...ORG, email: "second@example.invalid" })]);
    expect(results.filter(r => r.status === "fulfilled")).toHaveLength(1);
    expect(results.filter(r => r.status === "rejected")).toHaveLength(1);
    expect((await repo.action(row.action_id))!.state).toBe("approved");
  });
});
