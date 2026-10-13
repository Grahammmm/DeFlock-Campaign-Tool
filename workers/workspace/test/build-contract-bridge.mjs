// Local subprocess bridge for the Python contract suite. It runs production code
// against ephemeral Miniflare D1/R2 and exchanges HTTP requests over stdin/stdout.
// No provider account, remote endpoint, persisted corpus, or deployment is used.
import { build } from "esbuild";
import { Miniflare, Log, LogLevel, convertV4MiniflareOptions } from "miniflare";
import { readD1Migrations } from "@cloudflare/vitest-plugin";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { createInterface } from "node:readline";
import { pathToFileURL } from "node:url";

const temporary = await mkdtemp(path.join(tmpdir(), "build-contract-"));
let mf;
try {
  const bundled = path.join(temporary, "worker.mjs");
  await build({ entryPoints: [path.join(import.meta.dirname, "build-contract-worker.ts")], outfile: bundled,
    bundle: true, format: "esm", platform: "browser", target: "es2022", logLevel: "silent" });
  mf = new Miniflare(convertV4MiniflareOptions({
    host: "127.0.0.1", port: 0, inspectorPort: 0, log: new Log(LogLevel.ERROR),
    workers: [{ name: "build-contract", modules: true, script: "export default { fetch() { return new Response('test storage'); } };", compatibilityDate: "2026-09-30",
    d1Databases: { DB: "synthetic-build-contract" }, r2Buckets: ["PUBLIC_BUCKET", "ORIGINALS"], kvNamespaces: ["CACHE"],
    bindings: { CAMPAIGN_ID: "01test0000000000000000000a", RUNNER_TOKEN: "synthetic-build-contract-token" } }] }));
  const db = await mf.getD1Database("DB");
  const app = (await import(pathToFileURL(bundled).href)).default;
  const env = { DB: db, PUBLIC_BUCKET: await mf.getR2Bucket("PUBLIC_BUCKET"),
    ORIGINALS: await mf.getR2Bucket("ORIGINALS"), CACHE: await mf.getKVNamespace("CACHE"),
    CAMPAIGN_ID: "01test0000000000000000000a", RUNNER_TOKEN: "synthetic-build-contract-token" };
  for (const migration of await readD1Migrations(path.join(import.meta.dirname, "../migrations"))) {
    await db.batch(migration.queries.map((query) => db.prepare(query)));
  }
  process.stdout.write(JSON.stringify({ ready: true }) + "\n");
  for await (const line of createInterface({ input: process.stdin })) {
    try {
      const request = JSON.parse(line);
      const response = await app.fetch(new Request("http://localhost" + request.path, {
        method: request.method, headers: request.headers,
        ...(request.body === null ? {} : { body: Buffer.from(request.body, "base64") }),
      }), env);
      process.stdout.write(JSON.stringify({ status: response.status, headers: Object.fromEntries(response.headers),
        body: Buffer.from(await response.arrayBuffer()).toString("base64") }) + "\n");
    } catch (error) { process.stdout.write(JSON.stringify({ error: String(error) }) + "\n"); }
  }
} finally {
  await mf?.dispose();
  await rm(temporary, { recursive: true, force: true });
}
