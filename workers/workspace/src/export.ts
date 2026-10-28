// GET /api/export.json: every D1 table for this campaign as one JSON document, streamed
// table by table so a large campaign does not buffer in memory. Originals are not
// included (they live in R2 and are fetched by hash); campaign_tool.backup.export_hosted
// combines both into the tar format documented in docs/BACKUP.md.
import { nowIso } from "@deflock/shared/ids";
import { EXPORT_TABLES, type Repo } from "./db.ts";

export const EXPORT_SCHEMA_VERSION = 1;

export function exportStream(repo: Repo, campaignId: string): ReadableStream<Uint8Array> {
  const enc = new TextEncoder();
  const tables = [...EXPORT_TABLES];
  let index = 0;
  return new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(enc.encode(JSON.stringify({ schema_version: EXPORT_SCHEMA_VERSION, campaign_id: campaignId, exported_at: nowIso(), tables: tables }).slice(0, -1) + ',"rows":{'));
    },
    async pull(controller) {
      if (index >= tables.length) {
        controller.enqueue(enc.encode("}}\n"));
        controller.close();
        return;
      }
      const table = tables[index];
      const rows = await repo.exportTable(table);
      controller.enqueue(enc.encode((index ? "," : "") + JSON.stringify(table) + ":" + JSON.stringify(rows)));
      index += 1;
    },
  });
}

export function exportResponse(repo: Repo, campaignId: string): Response {
  return new Response(exportStream(repo, campaignId), {
    headers: {
      "content-type": "application/json; charset=utf-8",
      "cache-control": "private, no-store",
      "content-disposition": `attachment; filename="d1-export-${campaignId}.json"`,
    },
  });
}
