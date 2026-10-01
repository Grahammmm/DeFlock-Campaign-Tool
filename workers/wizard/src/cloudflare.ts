// Typed Cloudflare REST client. Every call goes through `call()`, which records a receipt.
// With dryRun, nothing is sent: the call is recorded and a synthetic result returned.
export interface CloudflareEnvelope<T = unknown> {
  success: boolean;
  errors: { code: number; message: string }[];
  messages?: unknown[];
  result: T;
}

export interface CallRecord {
  method: string;
  path: string;
  body: unknown; // already redacted by the caller
  dry_run: boolean;
  status: number | null;
  ok: boolean;
  at: string;
}

export interface CallResult<T = unknown> {
  status: number;
  ok: boolean;
  data: CloudflareEnvelope<T> | null;
  error: string | null;
}

export interface WorkerModule {
  name: string;
  content: string;
  type?: string;
}

export class CloudflareClient {
  readonly calls: CallRecord[] = [];
  constructor(
    private readonly token: string,
    readonly opts: { dryRun: boolean; fetcher?: typeof fetch; base?: string; dryResult?: (method: string, path: string, body: unknown) => unknown } = { dryRun: false },
  ) {}

  get base(): string {
    return (this.opts.base ?? "https://api.cloudflare.com/client/v4").replace(/\/$/, "");
  }

  private record(method: string, path: string, body: unknown, status: number | null, ok: boolean): void {
    this.calls.push({ method, path, body, dry_run: this.opts.dryRun, status, ok, at: new Date().toISOString() });
  }

  private syntheticResult(method: string, path: string, body: unknown): unknown {
    if (this.opts.dryResult) return this.opts.dryResult(method, path, body);
    const tail = path.split("/").filter(Boolean).pop() ?? "resource";
    const id = "dry-" + tail.replace(/[^a-z0-9]+/gi, "-").toLowerCase();
    return { id, uuid: id, queue_id: id, aud: "dry-aud-" + id, name: (body as { name?: string })?.name ?? tail, status: "active" };
  }

  /** JSON API call. `bodyForRecord` is what gets recorded (redacted); `body` is what is sent. */
  async call<T = unknown>(method: string, path: string, body: unknown = null, bodyForRecord: unknown = body): Promise<CallResult<T>> {
    if (this.opts.dryRun) {
      this.record(method, path, bodyForRecord, null, true);
      return { status: 0, ok: true, data: { success: true, errors: [], result: this.syntheticResult(method, path, body) as T }, error: null };
    }
    return this.send<T>(method, path, body === null ? undefined : JSON.stringify(body), { "content-type": "application/json" }, bodyForRecord);
  }

  /** Multipart Worker upload: metadata + one ES module. */
  async uploadWorker<T = unknown>(path: string, metadata: Record<string, unknown>, module: WorkerModule): Promise<CallResult<T>> {
    const recorded = { metadata, module: { name: module.name, bytes: module.content.length } };
    if (this.opts.dryRun) {
      this.record("PUT", path, recorded, null, true);
      return { status: 0, ok: true, data: { success: true, errors: [], result: this.syntheticResult("PUT", path, metadata) as T }, error: null };
    }
    const form = new FormData();
    form.append("metadata", new Blob([JSON.stringify(metadata)], { type: "application/json" }), "metadata.json");
    form.append(module.name, new Blob([module.content], { type: module.type ?? "application/javascript+module" }), module.name);
    return this.send<T>("PUT", path, form, {}, recorded);
  }

  private async send<T>(method: string, path: string, body: BodyInit | undefined, headers: Record<string, string>, bodyForRecord: unknown): Promise<CallResult<T>> {
    const fetcher = this.opts.fetcher ?? fetch;
    let res: Response;
    try {
      res = await fetcher(this.base + path, { method, body, headers: { authorization: "Bearer " + this.token, ...headers } });
    } catch (e) {
      this.record(method, path, bodyForRecord, null, false);
      return { status: 0, ok: false, data: null, error: "network: " + String((e as Error).message ?? e) };
    }
    let data: CloudflareEnvelope<T> | null = null;
    try {
      data = (await res.json()) as CloudflareEnvelope<T>;
    } catch {
      data = null;
    }
    const ok = res.ok && Boolean(data?.success);
    this.record(method, path, bodyForRecord, res.status, ok);
    const error = ok ? null : data?.errors?.map((e) => `${e.code}: ${e.message}`).join("; ") || `HTTP ${res.status}`;
    return { status: res.status, ok, data, error };
  }
}

/** "result.id" -> value from the envelope, as a string. */
export function pick(data: unknown, dotted: string): string | null {
  let cur: unknown = data;
  for (const key of dotted.split(".")) {
    if (cur === null || typeof cur !== "object" || !(key in (cur as object))) return null;
    cur = (cur as Record<string, unknown>)[key];
  }
  return cur === null || cur === undefined ? null : String(cur);
}
