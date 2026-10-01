# Runner

The runner is the container that does the engine work the Workers cannot: parsing
preserved mail, sandboxed PDF/workbook extraction, redaction and digests, follow-up
drafting and the public-site build. It is a poll loop over the runner API in
[CONTRACTS.md](CONTRACTS.md): lease a job, run the handler for its kind, post the result.
It holds exactly one bearer token bound to one campaign. It never receives mail, MuckRock
or Brevo credentials; every external effect it wants becomes an `external_action` card that
an organizer approves in the workspace. `send_request` jobs are answered `blocked`.

Status: implemented and tested offline against an in-memory fake of the Worker and against
the Worker itself in vitest. No runner has been attached to a deployed workspace.

## Run it

```
python3 -B -m runner --once --fake           # smoke test, no network; exit 3 means "idle"
WORKSPACE_URL=https://workspace.example RUNNER_TOKEN=... python3 -B -m runner
docker build -f runner/Dockerfile -t deflock-runner .   # image with its own smoke test
```

`runner/compose.example.yml` shows a hardened single-service deployment (read-only root,
tmpfs work directory, dropped capabilities, a stop grace period longer than the lease).
`runner/cloudflare-container.md` records how the same image would map onto Cloudflare
Containers and what to verify first. CI: `.github/workflows/runner.yml`.

Exit codes with `--once`: 0 job done, 1 job failed or blocked, 3 nothing queued (or the
workspace is unreachable and the loop backed off). Without `--once` the loop runs until
SIGTERM/SIGINT, which finishes the current job before exiting.

## Environment

| Variable | Default | Meaning |
| --- | --- | --- |
| `WORKSPACE_URL` | required | Workspace Worker origin; `/api/runner` is appended |
| `RUNNER_TOKEN` | required | Bearer token from the workspace Settings screen (shown once) |
| `PRIVACY_TIER` | `redacted_cloud` | `redacted_cloud` or `strict_local`; the job's own tier (from the campaign row) wins per job |
| `MODEL_BASE_URL` | empty | OpenAI-compatible chat endpoint; empty means detector-only digests and rule-only classification |
| `MODEL_API_KEY`, `MODEL_ID` | empty, `local` | Sent only to `MODEL_BASE_URL` |
| `CAMPAIGN_JURISDICTION` | empty | Fallback law package id (`us-ca`) when a job carries none |
| `JURISDICTIONS_DIR` | repo `jurisdictions/` | Where law packages are read from (the image sets `/app/jurisdictions`) |
| `REDACTION_ALLOWLIST`, `REDACTION_DENYLIST` | empty | Comma-separated terms the redactor must keep / must always mask |
| `POLL_INTERVAL`, `LEASE_SECONDS` | `15`, `300` | Idle sleep and lease length |
| `RUNNER_WORKDIR`, `KEEP_WORKDIR` | `/tmp/deflock-runner`, unset | Private scratch root (0700/0600); set `KEEP_WORKDIR=1` only while debugging synthetic data |

`strict_local` with a `MODEL_BASE_URL` that is not loopback, `*.ts.net` or a Tailscale
address refuses to start (`python -m runner` exits with a usage error) and fails any digest or
classification job before a request is made.

## Loop semantics (`runner/loop.py`)

- `GET /jobs?lease=N` → handler → `POST /jobs/:id/result {status, outputs, receipt, error}`.
- A handler exception becomes `failed` with the exception class and a 300-character message;
  record text is never put in a job error or log line. The Worker requeues `failed` jobs
  until `max_attempts`, then raises an incident.
- 5xx and connection errors back off exponentially to 300 s; 4xx errors are not retried.
- Each job gets `RUNNER_WORKDIR/<job_id>` (0700), files written at 0600 with `O_NOFOLLOW`,
  removed when the job finishes.
- Logs are JSON lines on stderr with identifiers and counts only (`job.start`, `job.finish`,
  `classify.redacted`, `digest.redacted`, `site.built`, `runner.backoff`, …).
- `outputs.followups` (`[{kind, idempotency_key?, inputs}]`) is the only way a handler
  enqueues more work. The Worker drops unknown kinds and `send_request`, requires a 64-hex
  key or derives `sha256(kind + JSON(inputs))`, and enqueues idempotently. `failed` and
  `blocked` results enqueue nothing.

## Handlers (`runner/handlers/`)

| Kind | Reads | Writes | Proposes |
| --- | --- | --- | --- |
| `classify_mail` | raw MIME original (`raw_sha256`) | one original + receipt (`<message-id>#<n>`) per attachment; `correspondence_update` with classification, confidence and a summary that contains no subject text, names or addresses | `extract` follow-up per attachment |
| `extract` | one original | `units.jsonl` as a private original (`text_sha256`); child originals and receipts for zip members and nested mail parts | `extract` per child, `digest` when text was extracted |
| `digest` | `text_sha256`, law package | validated digest JSON as a private original; `digest_row` fields for the D1 `digest` table | nothing |
| `draft_followup` | request/correspondence fields from the job, `templates/follow-up.md`, law package | nothing | `send_followup` card |
| `build_site` | content manifest in the job | `sites/<version>/<path>` in the public bucket via `PUT /api/runner/site/…` | `deploy_site` card |
| `send_request` | — | — | answered `blocked`: sends run in the workspace outbox after approval |
| `intake`, `newsletter_draft`, `backup` | — | — | answered `blocked` with "no handler in this runner version" |

Classification is rule-based (`classify_mail.RULES`, ordered denial → extension → fee
estimate → partial production → clarification → acknowledgement, with attachments turning a
bare acknowledgement into `production`). A configured model may refine the label from
redacted subject/body text only; disagreement lowers the confidence instead of overriding
the rule, and a model summary that still trips the redactor is dropped.

Extraction reuses `campaign_tool.records.intake.folder`'s `_worker` subprocess (resource
limits, container safety checks, `pypdf`/`openpyxl`) unchanged; the handler only stages the
bytes and reads the worker's `digest.json`. Pages with no text are reported as OCR needed,
not silently skipped.

Digests follow `campaign_tool.digest`: redact every unit with one shared placeholder table,
run the versioned detectors against the rules in force on the event date, then, only if a
model is configured, ask for a narrative over the redacted text and validate the reply
against `digest/schema.py`. Without a model every conclusion is `needs_attorney_review`,
which blocks `publish_finding`. Hits are not findings.

`build_site` writes the manifest under a private temp campaign directory with path checks,
runs `campaign_tool.site.build_site`, scans every emitted file with
`tools/check_public_tree.violations`, and uploads nothing if the scan finds anything. The
Worker re-validates each path (plain segments, allowlisted extension, 16 MiB cap) and the
`x-object-sha256` header before writing. Nothing is served until a `deploy_site` card is
approved.

## Tests

`python3 -B -m unittest discover -s tests/runner -t .` runs the client (scripted opener:
retries, 204, hash and path checks), loop (lease/dispatch/followups, exception handling,
backoff, exit codes, SIGTERM, workdir permissions, JSON logs), every handler over synthetic
fixtures (`examples/synthetic-mail/*.eml.txt`, generated PDF/XLSX/zip, the fictional
campaign content), the digest library (redaction counts, placeholders, detectors, model
path with a loopback fake server, strict_local refusal) and an end-to-end
classify → extract → digest chain through the default registry. The Worker side is covered
in `workers/workspace/test/runner.test.ts`. No test opens a network connection beyond
loopback.
