# Records admin panel (`records panel`)

A private, local, stdlib-only dashboard over the records pipeline. It **sends nothing, publishes nothing and runs
nothing**. Approving a draft only marks it approved-to-send in the approval queue; a human sends separately. It is
not installed, enabled or started by this repository.

```sh
records panel --project PROJECT --run-id RUN_ID [--run-report FILE] [--kit-root KIT] [--as-of YYYY-MM-DD] \
              [--port 8765] --actor LABEL [--timer-unit records-run.timer] [--no-system]
records panel --project PROJECT --run-id RUN_ID ... --render out.html     # static snapshot, no socket
```

## Roots, run and report are distinct

- `PROJECT` holds `processing-v1/ledger.sqlite` (canonical ledger), `run-outputs/RUN_ID/` (the selected run's
  outcomes) and `approvals/queue.jsonl` (the queue). The runtime directory of `run-records.sh` is **not** the project:
  pass its `reports/RUN_ID.json` (or `run-report.json`) explicitly with `--run-report`; nothing is guessed from mtimes.
- `RUN_ID` must be a literal safe id; a missing run shows as unavailable, never as an empty success.
- The page labels current ledger state and the selected run's outcomes as separate sources.

## Sections (one page, `GET /`)

| id | source |
| --- | --- |
| `mail` | ledger `mail_messages`; attachment counts only from ledger occurrences |
| `documents` | ledger originals (corrected effective fields), per-document stage state, blocked stages by recorded cause, page state |
| `stages` | validated run report (the eight orchestration stages, `needs_g` unchanged), canonical seven evidence stages, `schedule.health` (last run, status), timer state (`systemctl --user is-active`, read only) and disk |
| `findings` | `run-outputs/RUN_ID/outcomes/received-items.json`: agency/request/item ids, page + quote, rule/policy refs, documentary rating |
| `deadlines` | `kit_track.due` when `--kit-root` is given (overdue first, undated last); plus dates stated in received mail, passed first |
| `drafts` | approval queue projection with audit line (last actor and time, revision, creation time) |

Each source fails on its own with a fixed code (`ledger_missing`, `report_invalid_json`, `received_oversize`,
`queue_invalid`, ...). Missing, skipped, held or unknown is never shown as passed. `gap` is documentary, not a legal
finding; a blank findings list means none reported in that source and run, not compliance. Dates stated in mail and
kit dates are provisional and are not a legal deadline calculation. Mail text, quotes, reasons and drafts are escaped
plain text; no document-supplied link is clickable. The panel reads no raw `.eml`, document bytes or portal links.

## Drafts and decisions

Use **Edit** to remove draft banners, `# Draft (` headings, `[EDIT ME]` placeholders,
reviewer notes, `BEFORE SENDING` instructions and template footers before approving.
Placement and sending refuse remaining residue with `body_has_draft_residue`;
approval alone does not make a working draft ready to send.

- **Approve** follows the approval queue: the form shows the exact `approval_payload` bytes; sign them elsewhere
  (`ssh-keygen -Y sign -n deflock-approval`) and paste the signature. The panel holds no key and never signs.
  Result: `approved`, marked approved-to-send, nothing sent. A typed label never approves.
- **Reject** needs a reason; **Edit** saves new text (the queue returns it to pending; it is never auto-approved).
  Both record the startup `--actor` label, which is local single-operator attribution, not authenticated identity.
- Approved and rejected drafts are read-only here. The decisions file is the queue's append-only journal; the panel
  adds no second store. State is reloaded on every GET and success is confirmed by reading the queue back.
- No stale-revision protection beyond the queue's own state rules; concurrent CLI edits keep upstream semantics.

## Review page

Open a pending draft from Home or the dashboard's **Review** link at
`/review?id=DRAFT_ID`. The page shows the full editable body, read-only placement
headers (using the server's Drafts configuration), threading, attachment names,
residue warnings and body-header line warnings. Attachment names are evidence
metadata, not a promise that placement attaches files. Unknown headers stay Unknown.
The reason and evidence limits are escaped text, never document-provided links.

**Save edit** returns to this page with the journal's edit history, actor/time,
SHA-256 hashes and bounded unified diff. **Reject with reason** records the reason;
**Approve** requires the existing detached signature over the exact current payload.
A signature from before an edit is refused. No key is held here and approval does
not send or publish. Approved/rejected drafts have a read-only body and no forms.
**Back** returns Home. These forms use the existing decision routes and CSRF/Origin
checks; only the literal `return=review` adds a new success redirect. Static
`--render` output is unchanged and includes no editor.

## Sending an approved draft

With `--send-config`, the separate `/send` review and human POST send one approved,
placed draft; draft approval alone never sends it. `records mail-send init` writes
`enabled: false`, `live: false` and the SEND-STOP file; `--test-recipient ADDR`
sets the only allowed recipient in test mode. Omitted `live` also means test mode.
Other recipients are refused `test_mode_recipient`; without a test recipient,
sending is refused `test_recipient_missing`. The draft stays approved, not sent.
Only G may set `"live": true` in owner-only `approvals/send.json`; there is no
console switch or CLI flag for that. Every existing switch, kill file, cap,
fingerprint, typed-confirmation and outbox-signature check still applies.

## Local-only model

Bound only to literal `127.0.0.1` (no host option). Every request must carry `Host: 127.0.0.1:PORT`; POSTs also need
`Origin: http://127.0.0.1:PORT` and the per-start CSRF token (in hidden form fields only, never URL or logs).
To reach it from another tailnet device use an SSH/`tailscale` port forward to the **same port** so the Host header
matches; the panel never listens on another address. The token stops cross-origin browser requests; it does not stop
another process of the same account that can already read these files (trusted local account). Only `GET /` and the
three `POST /approvals/{approve,reject,edit}` routes exist; bodies are urlencoded UTF-8, at most 256 KiB, with a
read timeout. Responses are `no-store` with a strict CSP; request logging is off.

`--public-origin https://admin.example.test` adds exactly one case-sensitive Host/Origin pair:
`Host: admin.example.test` with POST `Origin: https://admin.example.test`. Loopback remains accepted,
but the pairs cannot mix. The hostname must be lowercase DNS with at least two labels; no port,
path, query, fragment or trailing dot is accepted. Missing or duplicate Host/POST Origin is refused.
The bind stays literal `127.0.0.1`; `--render` ignores this flag.
This is only safe behind Cloudflare Access with **cloudflared Access JWT validation**.
`--access-config FILE` is required with `--public-origin` (and refused without it): an owner-only (0600, regular,
non-symlink, at most 4096 bytes) JSON object with exactly `aud` (64 lowercase hex), `email` and `team_domain`
(`<team>.cloudflareaccess.com`). Every request on the public Host must then carry one `Cf-Access-Jwt-Assertion`
whose header is RS256 and whose `aud`, `iss`, `exp`, `iat` and `email` claims match; otherwise it gets the generic 403.
This check does **not** verify the signature: alone it is only a tripwire against a missing or misconfigured edge, never
authentication. Loopback requests are exempt. Never commit the real values.
Never add an edge rule that rewrites Origin. Existing CSRF and action checks still apply.

## Static snapshot

`--render out.html` writes the same view with no forms, token or endpoint links, banner "Static snapshot — decisions
unavailable". Output is owner-only 0600, replaced atomically in an existing symlink-free directory; it refuses
symlink/FIFO outputs and any path that is a source file. `--no-system` skips the timer and disk checks for
deterministic output.

## Errors

Unknown draft 404, invalid transition 409, missing/invalid signature 403, malformed source 503; failed requests append
nothing and reflect no input.

## Next steps

The `/next-steps` page lists recommended social, site and follow-up drafts with Approve and Deny; see [RECORDS-NEXT-STEPS.md](RECORDS-NEXT-STEPS.md). Static `--render` includes the same sections without forms.

### Manual events with a read-only snapshot

Use `records panel --manual-events DIR` to keep manual form events outside the
project. DIR must already exist, be absolute, owned by the running uid, have no
group/other permissions (for example 0700), and not be a symlink. Its real path
must neither contain nor be inside the project. Events append to
`DIR/manual-events.jsonl` under `DIR/manual-events.lock`; nothing in the project
is rewritten. The confirmation links back to the agency; the event appears on
the timeline after the next data refresh. Snapshot-builder merging of this
journal is staged separately in CDATA-MANUAL-MERGE-1. Without the flag the
existing project journal and immediate timeline refresh remain unchanged.

## Navigation

Every console page shares a plain left navigation: Home, Approvals, Agencies,
Calendar, Timeline and Library. The small More line retains Dashboard, Oversight,
Public updates, Packets, and Drafts and next steps. On narrow screens the nav
stacks above the page; overdue notices are bold red (on our records, not a legal finding).

## Jobs

`GET /jobs` (More: Jobs) is a read-only list of every scheduled job. The list is discovered from the unit files under
`deploy/` (`*.timer`, `*.path`, `.in` templates included) plus any `--timer-unit`, so another geography with its own units
needs no code change. For each job it runs only `systemctl --user show` and reports installed, enabled, last trigger,
last result (`Result` and `ExecMainStatus` of the triggered service) and next run, read from systemd at request time
(schedules are never hard-coded). Only an installed, enabled timer-driven unit whose service has a recorded completed execution (`ExecMainExitTimestamp`) with success and exit 0 reads `ok`; a service that never ran reads "never run" even though systemd defaults its result to success. Arrival (`.path`) units never read `ok`: `run-records.sh --arrival` exits 0 both when it processed work and when a held overlap lock only queued the arrival, so they read `UNKNOWN`.
Not installed, disabled, never run, exit 73 (a held lock skipped the slot), failed, `systemctl` missing and unparseable
output each render as their own explicit state, never as passed. There are no start, stop or enable controls.

The optional GitHub Actions section shows the latest run of each workflow (default `check`, `camera-source-review`,
`production`; override with `DEFLOCK_GITHUB_WORKFLOWS`, comma separated). It needs `DEFLOCK_GITHUB_REPO` (`owner/name`) and a
read-only token in `DEFLOCK_GITHUB_READ_TOKEN`, or an owner-only (0600) regular file named by
`DEFLOCK_GITHUB_READ_TOKEN_FILE`. Without them it shows "not configured" and makes no network call. The token is never
rendered, and a failed call shows only "GitHub unavailable". Tests inject a fake fetcher and never use the network.

Home, Agencies (Board) and Calendar name any source they could not load in a banner at the top and show "unknown" instead of a count that depends on it; an unknown count is never printed as 0.
