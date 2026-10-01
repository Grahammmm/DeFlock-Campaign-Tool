// Public site: serves sites/<site_version>/<path> from the public R2 bucket. The version
// is the one an approved deploy_site card wrote to KV; nothing here reads private data.
export interface Env {
  PUBLIC_BUCKET: R2Bucket;
  CACHE: KVNamespace;
}

const DEFAULT_HEADERS: Record<string, string> = {
  "x-content-type-options": "nosniff",
  "referrer-policy": "strict-origin-when-cross-origin",
  "x-frame-options": "DENY",
};

/** Parse a Cloudflare Pages style `_headers` file: one "/*" or path block per section. */
export function parseHeaders(text: string, path: string): Record<string, string> {
  const out: Record<string, string> = {};
  let active = false;
  for (const line of text.split("\n")) {
    if (!line.trim() || line.trim().startsWith("#")) continue;
    if (!/^\s/.test(line)) {
      const pattern = line.trim();
      active = pattern === "/*" || pattern === path || (pattern.endsWith("*") && path.startsWith(pattern.slice(0, -1)));
      continue;
    }
    if (!active) continue;
    const idx = line.indexOf(":");
    if (idx > 0) out[line.slice(0, idx).trim().toLowerCase()] = line.slice(idx + 1).trim();
  }
  return out;
}

export function normalizePath(pathname: string): string {
  let decoded: string;
  try {
    decoded = decodeURIComponent(pathname);
  } catch {
    return "/404.html"; // malformed percent-escape is a 404, not a 500
  }
  let p = decoded.replace(/\/{2,}/g, "/");
  if (p.includes("..")) return "/404.html";
  if (p.endsWith("/")) p += "index.html";
  else if (!/\.[a-z0-9]+$/i.test(p)) p += ".html";
  return p;
}

export default {
  async fetch(request: Request, env: Env, _ctx: ExecutionContext): Promise<Response> {
    if (request.method !== "GET" && request.method !== "HEAD") return new Response("method not allowed", { status: 405 });
    const version = await env.CACHE.get("site_version");
    if (!version) return new Response("site not published yet", { status: 503, headers: DEFAULT_HEADERS });
    const path = normalizePath(new URL(request.url).pathname);
    const prefix = `sites/${version}`;
    let object = await env.PUBLIC_BUCKET.get(prefix + path);
    let status = 200;
    if (!object) {
      object = await env.PUBLIC_BUCKET.get(prefix + "/404.html");
      status = 404;
      if (!object) return new Response("not found", { status: 404, headers: DEFAULT_HEADERS });
    }
    const headersFile = await env.PUBLIC_BUCKET.get(prefix + "/_headers");
    const headers = new Headers({ ...DEFAULT_HEADERS, ...(headersFile ? parseHeaders(await headersFile.text(), path) : {}) });
    object.writeHttpMetadata(headers);
    headers.set("etag", object.httpEtag);
    headers.set("x-site-version", version);
    if (!headers.has("cache-control")) headers.set("cache-control", "public, max-age=300");
    return new Response(request.method === "HEAD" ? null : object.body, { status, headers });
  },
} satisfies ExportedHandler<Env>;
