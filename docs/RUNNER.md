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
tmpfs scratch, a private persistent volume for result envelopes, dropped capabilities,
and a stop grace period longer than the lease). The volume must survive container
replacement. Outside that example, set `RUNNER_RESULT_DIR` to private persistent storage;
the default underneath `/tmp` does **not** survive host restart. Deployment and volume
backup/retention remain operator decisions. Never store this private directory in Git.
`runner/cloudflare-container.md` records how the same image would map onto Cloudflare
Containers and what to verify first. CI: `.github/workflows/runner.yml`.

Exit codes with `--once`: 0 job done, 1 job failed or blocked, 3 nothing queued (or the
workspace is unreachable and the loop backed off). Without `--once` the loop runs until
SIGTERM/SIGINT, which stops lease renewal and refuses subsequent handler mutations. A
parser/model call already in progress may finish before the process exits; its late result
is discarded after claim loss. Stop does not forcibly terminate in-process computation.

## Environment

| Variable | Default | Meaning |
| --- | --- | --- |
| `WORKSPACE_URL` | required | Workspace Worker origin; `/api/runner` is appended |
| `RUNNER_TOKEN` | required | Bearer token from the workspace Settings screen (shown once) |
| `PRIVACY_TIER` | `redacted_cloud` | `redacted_cloud` or `strict_local`; the stricter of this and the job's own tier (from the campaign row) applies per job |
| `MODEL_BASE_URL` | empty | OpenAI-compatible chat endpoint; empty means detector-only digests and rule-only classification |
| `MODEL_API_KEY`, `MODEL_ID` | empty, `local` | Sent only to `MODEL_BASE_URL` |
| `CAMPAIGN_JURISDICTION` | empty | Fallback law package id (`us-ca`) when a job carries none |
| `JURISDICTIONS_DIR` | repo `jurisdictions/` | Where law packages are read from (the image sets `/app/jurisdictions`) |
| `REDACTION_ALLOWLIST`, `REDACTION_DENYLIST` | empty | Comma-separated terms the redactor must keep / must always mask |
| `POLL_INTERVAL`, `LEASE_SECONDS` | `15`, `300` | Idle sleep and lease length |
| `MAX_JOB_SECONDS` | `3600` | Absolute handler ownership budget, 30–86400 seconds; renewal cannot run forever |
| `RUNNER_WORKDIR`, `KEEP_WORKDIR` | `/tmp/deflock-runner`, unset | Private scratch root (0700/0600); set `KEEP_WORKDIR=1` only while debugging synthetic data |
| `RUNNER_RESULT_DIR` | `$RUNNER_WORKDIR/results` | Private result outbox; point to persistent storage for container/host restart recovery |

`LEASE_SECONDS` must be an integer from 30 through 3600. The initial lease is capped by
`MAX_JOB_SECONDS`; a heartbeat runs every third of the lease while the handler is live.
Keep the runner and workspace clocks synchronized. `MAX_JOB_SECONDS` bounds lease
renewal and permission to mutate, not the execution time of a stuck parser/model call.

`strict_local` with a `MODEL_BASE_URL` that is not loopback, `*.ts.net` or a Tailscale
address refuses to start (`python -m runner` exits with a usage error) and fails any digest or
classification job before a request is made.

## Privacy tiers

`redacted_cloud` (default): records stay on the box; `campaign_tool/digest/redact.py` replaces plates, e-mail
addresses, phone numbers, street addresses, SSN-like numbers, dates of birth after a label,
names after a rank title or a name-like label (`Name:`, `Requester:`, `Sincerely,` ...) and
every `REDACTION_DENYLIST` phrase with stable placeholders before any text reaches the
model; attachment member names and sheet titles are never sent (the model sees unit numbers
and page numbers only). **A bare personal name in running text is not recognised by any
regex**, so every name the organizers know of (requesters, clerks, officers named in earlier
records) belongs in `REDACTION_DENYLIST`. Over-redaction is harmless; the digest records
counts, never the mapping. `strict_local`: text never leaves the box; a model, if any, must
be loopback or on the tailnet, and the runner refuses to start otherwise.

## Loop semantics (`runner/loop.py`)

- `GET /jobs?lease=N` returns an immutable `claim_token` and `leased_until` → handler →
  `POST /jobs/:id/result {claim_token, status, outputs, receipt, error}`.
- `POST /jobs/:id/renew {claim_token, lease_seconds}` extends only that unexpired claim.
  Renewal uses one HTTP attempt with a timeout of at most ten seconds. Any renewal error,
  local expiry, stop request, or runtime budget exhaustion fences subsequent workspace
  mutations, including every retry of an HTTP write. The heartbeat stops on handler
  completion or exception; an already in-flight renewal may finish within its timeout.
- The Worker's atomic completion fence is authoritative. Non-queue writes (originals,
  receipts, proposals, and staged site files) already in flight are not rolled back or
  atomically fenced with completion. A handler also cannot be forcibly interrupted here.
  These guards do not provide exactly-once handler execution or transactional uploads.
- A handler exception becomes `failed` with its exception class and a fixed message;
  exception messages that may contain record text are omitted. The Worker requeues `failed` jobs
  until `max_attempts`, then raises an incident.
- 5xx and connection errors back off exponentially to 300 s. Identical accepted result
  replay returns the original acknowledgment, including after expiry or a later attempt.
  A conflicting result or unaccepted stale claim returns 409.
- Each invocation gets a unique `RUNNER_WORKDIR/scratch/job-*` directory (0700), with
  files written at 0600 using `O_NOFOLLOW`. Different attempts cannot remove each other's
  scratch. `KEEP_WORKDIR=1` retains it only for synthetic debugging.
- Before posting, the runner atomically saves and fsyncs a private result envelope under
  `RUNNER_RESULT_DIR/<workspace-token-scope>/result-*.json` (default below the workdir). It contains only the
  final result request and a scratch identifier: no downloaded originals, copied job
  inputs, or scratch files. Result metadata may still be private. Scratch is removed
  after the envelope is durable; failed persistence preserves scratch and stops the run.
- Pending envelopes are retried before leasing new work, including on restart, without
  rerunning handlers. Successful acknowledgment removes the envelope. A crash between
  saving and scratch cleanup is recovered during delivery. A local file lock prevents
  simultaneous delivery by runners sharing a workdir.
- A 409 preserves the envelope with a `.conflict` suffix for operator inspection and
  ends automatic retry for that envelope. Other nonretryable errors stop the runner with
  the envelope retained. Repair authentication/configuration before restarting. Token
  rotation changes the local scope: reconcile or explicitly migrate the old private
  envelopes for the same campaign; do not discard them or replay them to another campaign.
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
backoff, exit codes, SIGTERM, workdir permissions, JSON logs), claim fencing and renewal
with a synthetic clock, transport retries after ownership loss, result outbox recovery
after timeout/lost acknowledgment/process interruption, isolated scratch and cleanup,
every handler over synthetic
fixtures (`examples/synthetic-mail/*.eml.txt`, generated PDF/XLSX/zip, the fictional
campaign content), the digest library (redaction counts, placeholders, detectors, model
path with a loopback fake server, strict_local refusal) and an end-to-end
classify → extract → digest chain through the default registry. The Worker side is covered
in `workers/workspace/test/runner.test.ts`. No test opens a network connection beyond
loopback.
