# Running the runner on Cloudflare Containers (planned, not exercised)

The runner is an ordinary long-running container (`runner/Dockerfile`) that needs only
outbound HTTPS to the workspace Worker and, optionally, to a model endpoint. It can run
anywhere Compose runs (`runner/compose.example.yml`). This note records how it would map
onto Cloudflare Containers so the choice can be made deliberately. Nothing here has been
deployed; treat every Cloudflare-specific detail as something to verify against current
Cloudflare documentation before use.

## Shape

- One container class bound to the workspace Worker through a Durable Object. The Worker's
  `scheduled()` handler (which already enqueues jobs) would also `start()` the container
  when the `job` table has queued rows, and the container exits on its own when `--once`
  returns 3 (idle) a few times in a row. Runs are therefore bursty rather than permanent.
- The container image is built from this repository with `docker build -f runner/Dockerfile .`
  and pushed to the registry Wrangler manages. No campaign data is in the image.
- `WORKSPACE_URL` points at the workspace Worker's hostname; the request still goes through
  Access-exempt `/api/runner/*` routes with the bearer token. Container-to-Worker traffic is
  ordinary HTTPS; the runner does not need a service binding.

## Secrets

Pass `RUNNER_TOKEN` and `MODEL_API_KEY` as container environment variables set from Worker
secrets at `start()` time, never as image layers or in `wrangler.jsonc`. Rotating the token
on the workspace Settings screen invalidates running containers at their next request; they
exit with a non-retryable 401 and the next scheduled start picks up the new value.

## Privacy tier consequences

- `redacted_cloud` works as on any host: text is redacted before any model call and the
  redaction count is logged.
- `strict_local` requires `MODEL_BASE_URL` on loopback, `*.ts.net` or a Tailscale address.
  A Cloudflare container has neither a local GPU model nor a Tailscale interface by default,
  so `strict_local` with a model means running the runner on the organizer's own hardware
  instead (or without a model: detector-only digests still work and are gated
  `needs_attorney_review`). Do not point `MODEL_BASE_URL` at a public endpoint under
  `strict_local`; the runner refuses to start.

## Limits to check before choosing this path

- Instance memory and disk: the extract worker enforces its own resource limits, but a
  large PDF or workbook still needs working space under `RUNNER_WORKDIR`. Size the instance
  for the largest expected original, or keep large originals on a self-hosted runner.
- Maximum run duration and idle shutdown behaviour; the lease is 300 s and SIGTERM lets the
  current job finish, so the platform's stop grace period must exceed that.
- Egress pricing and whether outbound connections to the organizer's model host are allowed.
- Image size limits; the current image is Python 3.12 slim plus `pypdf` and `openpyxl`.

## What stays the same

Approval boundaries do not move: the container proposes `send_followup` and `deploy_site`
cards and never holds mail, MuckRock or Brevo credentials. Production deployment, DNS, Access
and Brevo changes still require explicit owner approval in chat (AGENTS.md).
