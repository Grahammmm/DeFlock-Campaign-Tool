# Records admin panel (`records panel`)

A private, local, stdlib-only dashboard over the records pipeline. It **sends nothing, publishes nothing and runs
nothing**. Approving a mail draft only marks it approved-to-send in the approval queue; a human sends separately.
Review-copy approval is a draft-copy decision only, never permission to send or publish. It is
not installed, enabled or started by this repository.

```sh
records panel --project PROJECT --run-id RUN_ID [--run-report FILE] [--kit-root KIT] [--as-of YYYY-MM-DD] \
              [--port 8765] --actor LABEL [--timer-unit records-run.timer] [--no-system]
records panel --project PROJECT --run-id RUN_ID ... --render out.html     # static snapshot, no socket
```

## Prepared export review context (CONDRAFT-03)

The owning project's export prepares `console-model/queue.json`'s `review_context`
map, keyed by stable queue id. Each entry binds `source_project_id`, `queue_id`,
`revision` and SHA-256 of the canonical signed row evidence. It carries the resolved
recipient or website/page/section target, subject, typed citations, source section
and attachment hash, purpose and limitations. The imported correspondence envelope
is resolved by the same validator/thread rules as mailbox placement; it is not an
editable preview override. This map never authorizes placement or sending.

`SnapshotSource.review_context(id)` reads only the sealed model. It does not open
originals, mail or mailbox configuration or recompute the source project's identity.
Old exports without context, or entries whose binding is stale, ask **Refresh the
export to see recipient and sources**. A producer whose recipient cannot be resolved
shows **Unknown / Not send-ready**; a missing citation list means sources unknown,
not zero citations. A resolved recipient is not send authorization.

Home's queue index, dashboard queue and `/review?id=...` show every citation and
its original/tracker type, locator, as-of, verification and supplied caveats outside
the signing JSON. Source text is escaped; no original hash, date or verification
is invented. The complete draft body remains available at the existing size cap.
**Leave pending** and **Next** follow sorted stable pending ids (wrapping to the
first remaining id). They are GET navigation only: no decision, snooze, deletion
or bulk action. Rejection changes only that row and leaves others reachable.

## Review-only agency summaries and corrections (CONDRAFT-02)

### Implementation scope amendment

The CONDRAFT-02 fix task `t_e19fbb3e` applies the operator's SCOPE-3 standing
file-scope grant to the following two additions to the frozen owned-file list:

- `campaign_tool/records/mail_preview.py`: the review-copy-only preview avoids
  mail lookup/configuration and shows no mail recipient, sender or subject.
- `campaign_tool/records/mail_send.py`: the explicit `review_copy_not_mail`
  refusal prevents either review-copy kind from entering send preparation.

These narrowly scoped additions implement the existing requirement that agency
summaries and corrections are not email, including after copy approval. This
amendment records file ownership only; it does not authorize a send, change the
approval chain, relax source/privacy/redaction checks, or change what may be
published. The existing refusal behavior is retained unchanged by this fix.

`records draft-import --project PROJECT --file INPUT [--apply]` also accepts one
`records-console-draft-input-v1` instance of kind `draft-agency-summary` or
`draft-correction-notice`. Dry-run remains the default; apply adds only one pending
full-text row through the existing approval queue. There is no new store or packet
edit API. Use synthetic or operator-checked input, not a batch array.

These kinds use `target: {website, page, section}` **instead of** `envelope` (an
envelope is refused). All three target values are non-empty, single-line strings;
they are inert display evidence, not URLs to fetch or permission to write a site.
A correction additionally requires `correction: {prior_statement, prior_version,
linkage}`, each a non-empty, single-line string identifying the earlier wording,
its version and the correction relationship. A summary cannot carry correction fields.

Keep the existing `draft_key`, agency/request, full `body`, `why`, `source_run_id`,
`source_attachment_sha256` and `source_section`. The attachment hash identifies the
supplied source attachment, **not** an unavailable original. For these two kinds,
`citations` is a non-empty list of exact objects with `source_type`, `source_id`,
`locator`, `as_of`, `verification`. `source_type` is a non-empty single-line string;
`verification` is exactly `verified`, `unverified`, `tracker-only` or `missing-original`.
The other three values are non-empty single-line strings, or may be JSON null only
for non-verified citations. Supply only known locators/dates/identities; never invent
original hashes/pages or upgrade a tracker reference to verified. Correspondence
citations retain CONDRAFT-01's typed validation unchanged; the review-copy rules do
not relax mail intake.

Target, correction linkage, citations, source section/hash and original body hash
are bound into signed evidence. `/review` shows the entire editable draft, target,
correction context and source caveats. Saving an edit retains the instantiated
original and invalidates the old approval signature. Stable duplicate/conflict
handling is unchanged: identical imports do not duplicate or reopen rejection;
changed body/evidence under the same identity refuses for operator resolution.

**Approve copy (draft-copy decision only; nothing is posted, sent or published)**
records only the copy decision, even when sending is enabled. Both kinds are
explicitly non-mail before admission: they cannot be placed or sent, and cannot be
converted by `public_packets.from_queue` or automatically become `next-site-update`.
Copy approval never makes an eligible public packet or supplies human/legal/source
acceptance. No public-content write, fact resolution or publication is performed.

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

## Import one correspondence draft (queue only)

`records draft-import --project PROJECT --file INPUT.json` validates one draft and
prints a metadata-only `would_queue`/`existing` summary. It is dry-run by default:
no files are created and no network is contacted. Add `--apply` to queue one
**pending** `correspondence-draft` through the existing approval journal. This
never approves, signs, places, sends, changes settings or starts a records run.
Use a private input outside the engine repository; never commit real drafts.

The input is exactly one UTF-8 JSON object (not an array or JSONL), schema
`records-console-draft-input-v1`, with these exact keys:

- `schema`, `kind` (`correspondence-draft`), `draft_key` (stable operator key),
  `agency_id`, `request_id`, `source_run_id`, `source_section` (safe identifiers,
  1–128 characters: letters/digits first, then letters/digits or `_.:/-`).
- `body`: the full instantiated letter, nonblank, no NUL, at most 64 KiB in UTF-8.
  It is retained exactly, including the tail, Unicode and line endings. The
  console's existing edit history retains this original after operator edits.
- `why`: a nonblank single-line reason for the operator to review.
- `source_attachment_sha256`: lowercase 64-hex digest of the source attachment.
- `citations`: 0–64 structured objects with required `source_type`, `source_id`,
  `locator`, `as_of` (ISO date or null), and `verification`. Source types are
  `original`, `tracker`, `timeline`, `public`, `missing-original`; verification is
  `verified`, `unverified`, or `missing-original`. Source ID/locator are nonblank,
  control-free, at most 1024 UTF-8 bytes. Optional `original_sha256` is lowercase
  hex64, `page` a positive integer (not boolean), and `limits` control-free
  at most 4000 UTF-8 bytes. Original citations require the hash; missing-original
  citations cannot claim verified or supply hash/page. Unknown keys refuse.
  Order and limits are retained; `[]` means no citations, not source completeness.
- `envelope`: exactly `{ "to": "custodian@agency.example", "subject": "Records follow-up",
  "thread_item_id": null, "recipient_basis": { "source_type": "custodian",
  "source_id": "contact-1", "locator": "records desk", "checked": true } }`.

To/Subject must be **operator-checked custodian facts or source-header facts**,
not editable `To:`/`Subject:` body lines or guessed tracker labels. Use one bare
email address, not a display name or recipient list. All envelope values reject
CR/LF and control characters; subject is at most 998 UTF-8 bytes. The basis has
exactly those four keys, requires actual `true`, and source ID/locator are nonblank,
control-free, at most 1024 UTF-8 bytes. With no thread, basis must be `custodian`;
this deliberately remains unthreaded even if the project has
other messages. For a proven reply, supply the received item's lowercase 64-hex
`thread_item_id`: read-only ledger/original lookup must resolve
one unambiguous source message, and To plus reply Subject must match that source
(`Re: ` is added only if absent). Unresolved or contradictory linkage refuses.
For a `source-header` basis, `source_id` must equal that item ID and `locator`
must be the selected `Reply-To` header, or `From` only when Reply-To is absent.
Custodian basis records operator attestation, not machine proof of authority.
The same validation and envelope feed import, console preview and later placement;
existing draft kinds keep their current header behavior.

The signed review evidence contains the envelope, citations, reason, source
attachment/section/run, draft key and original body hash. These record supplied
provenance, not machine verification of the custodian facts or legal acceptance.
Identity is **section + request + draft_key**, not the body hash. Identical imports
return the existing row without an event, including a rejected or operator-edited
row; changed original body, attribution or evidence under that identity refuses
`source_conflict` (exit 3) for operator resolution. Other invalid inputs exit 2
with a content-free `draft-import: <reason>` line. Duplicate JSON keys, unknown or
missing fields, nonregular/multiply-linked input, symlink leaves or ancestors,
and input over 128 KiB are refused. Reading is bounded and nonblocking.

Review the full draft and edit it in the console before approval. Pending rows
cannot be placed or sent. Queue approval still only enables the existing separate
placement step; later sending still requires G's **separate recipient/text outbox
signature**, every send switch, kill file and cap. Import grants none of those.

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
`--access-keys FILE` (DFB-11) adds the signature check for every POST; see "Hosted decisions" below.

## Static snapshot

`--render out.html` writes the same view with no forms, token or endpoint links, banner "Static snapshot — decisions
unavailable". Output is owner-only 0600, replaced atomically in an existing symlink-free directory; it refuses
symlink/FIFO outputs and any path that is a source file. `--no-system` skips the timer and disk checks for
deterministic output.

## Errors

Unknown draft 404, invalid transition 409, missing/invalid signature 403, malformed source 503; failed requests append
nothing. A review-page form refused for an empty reason, empty edit text or
empty signature (400) shows the review page again with the reason beside the form and the typed text kept (HTML-escaped);
a missing CSRF token gets a plain "form expired" message with nothing echoed. Other error pages use the shared layout
(banner in the header, `main`, skip link). `/documents?agency=` for an agency not in the run is a 404.

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

### Automation overview

`GET /automation` (More: Automation) combines the configured `--run-report`'s
eight orchestration stages, the systemd unit list, and saved open health alarm
codes in one read-only page. It includes missing pilot companion units as gaps,
not just the installed timers. `--timer-unit` adds campaign-specific timers.
Only `systemctl --user show` is used; `--no-system` leaves unit state unknown.
This page does not use GitHub credentials, call the network, inspect the ledger
or record contents, or run the health writer.

Pass `--run-report RUNTIME/TARGET/run-report.json` to see the latest report and
the sibling `alerts/` folder. A historical `reports/RUN_ID.json` is also accepted
for stage evidence, but saved alarms then show unknown. No report path is guessed.
In hosted/export mode, the same explicit option is needed and must be readable by
the service; absent it, stage and alarm evidence are unknown. Unit state is current
host state, not part of the pinned export. This change installs nothing.

Last run is the **containing run's finish**, not an invented per-stage timestamp.
Next opportunity comes from `records-run.timer`, not a hard-coded schedule, and
does not promise a held/skipped stage will execute. A report older than 26 hours
is labelled historical/stale (an age warning, not a missed-slot verdict). Missing,
invalid and future reports cannot produce green stage evidence. Arrival, OCR,
facts, comparison, gaps, follow-ups, findings, packets, export and decision apply
remain explicitly unknown where the report has no separate stage result; an
`outputs=ok` is not proof of those substeps. Available OCR counters are labelled
as counters, not acceptance.

Only open `records-health-alert-v1` warn/stop files are listed; cleared files are
excluded. Missing, unreadable, malformed or over-cap alarm sources show unknown,
never zero. Notify failure markers are counted separately as historical markers
with resolution unknown. Even an empty alarm folder is not an all-clear: a stopped
monitor cannot refresh it. The page neither creates nor clears an alarm.
Rollback: revert the Automation page commit; existing Jobs/Oversight stay intact.

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

## Console export (DFGL-16)

`records console-export --project P (--run-id R | --latest) (--out DIR | --stdout)` runs in the container and copies only the
console's plain input files for one run: `run-outputs/<R>/**`, `processing-v1/ledger.sqlite` (SQLite backup into a standalone
file, no `-wal`/`-shm`), `agency-retention.csv`, `agency-aliases.tsv` and `agency-roster.tsv`, plus `EXPORT.json`
(`records-console-export-v1`: run id, `source_project_id`, per-file sha256). The approval, mail-draft and send journals and locks,
`public-packets.*`, `public-updates.*`, `mail/`, blobs and credentials are never copied, and a symlink in the source is refused.

### Read model (DFGL-16R)

Each export also carries `console-model/` (`records-console-model-v1`, listed in `EXPORT.json.files`; `EXPORT.json.model` names the
schema, `source_project_id` and `generated_at`). Its five files are plain JSON computed in the container by the existing readers on the
live project, so approval signatures are verified there and bound to the live project id: `queue.json` (rows with verified approvals,
payloads of pending rows, history), `packets.json` (pending, status, payloads of proposed packets, switch), `public-updates.json`,
`mail-drafts.json` and `sends.json`. A reader that fails writes its file with `state: "unavailable"` and its own code
(`queue_invalid`, `journal_busy`, `packets_unavailable`, ...) and no list; an unavailable file is never an empty list. `sends.json` reads
`mail_send.results(P, strict=True)`: an unreadable or corrupt journal is `send_io` or `send_results_invalid`, and only an absent journal
(or an absent `approvals/` directory, `journal: "absent"`) is an empty `ok` list. The packet readers take a `read_only=True` keyword
(`pending`, `status`, `payload`) that opens the lock read-only and creates nothing; the default behaviour is unchanged. No raw
signature is written (only `signature_sha256` and the signer name). The live project is only read, and the model grants no authority.

`records console-import --out DIR --from-tar FILE|-` runs on the host. `DIR` must be absolute, owner-only 0700 and symlink-free
(else `EXPORT_DIR_INVALID`). The stream is verified against `EXPORT.json`, unpacked to `DIR/exports/<UTC>-<run_id>/` (directories
0500, files 0400) and `DIR/current` is repointed with a relative link `exports/<name>`. Earlier exports are kept; a failed import leaves
`current` unchanged. `current` is only a pointer: the panel is to be pointed at the resolved `exports/<name>` path (`--export-root DIR`,
DFGL-16A), never at `DIR/current` with `--project`. The existing journal writers fail closed on a copy.

`run-records.sh` gets an optional post-step when `CONSOLE_EXPORT_DIR` is in the render values (it also needs `CONTAINER_USER`,
`CONTAINER_ENGINE_DIR`, `CONTAINER_PYTHON` and `CONTAINER_PROJECT_DIR`). It pipes `console-export --latest --stdout` from the container
into `console-import` after the run's report and notice, inside the run lock; a failure is logged and never changes the exit status.
Both ends of that pipe are bounded by the invocation deadline the work stages use (at most 300 s), so the post-step never spends the
notify/backstop reserve inside the unit timeout; a run or arrival follow-up that ends with under 10 s left skips it ("console export SKIPPED").

### Serving an export (DFGL-16A)

`records panel --export-root DIR --run-id auto --actor NAME` serves the export `DIR/current` points at; `--project` is not given.
It is refused (`export_mode_invalid`, exit 2) with `--project`, `--run-id` other than `auto`, `--send-config`, `--mail-drafts-config` or
`--render`, and when `DIR` is not absolute, not owner-only 0700 or not its own `realpath` (a symlink). `--manual-events DIR2` is allowed
only when `DIR2` is disjoint from `DIR`; without it the manual-event POST is refused like every other POST.

**One export per request.** Each request reads `current` once (it must be exactly `exports/<name>`), opens `DIR`, `exports`, `<name>`
without following a symlink, and checks the export directory is owner-only and read-only (no group or other bits). `EXPORT.json` (schema,
run id, `source_project_id`) and the sha256 of every listed file are verified once per `(device, inode, mtime)` of the export directory
(exports are immutable) and the file set must match exactly. The request then runs on a new options dict pinned to that export (its path,
its run id, a `SnapshotSource`), so a swap of `current` during a request cannot mix two exports, and the next request follows the new
export without a restart. A pin that fails (a bad `EXPORT.json`, a hash mismatch, a broken link) logs `export_pin_failed: <code>` on stderr
and keeps serving the last good pin; with none yet the page is 503 `export_unavailable`. Every page footer names the run, the export and
its export time.

**Snapshot readers.** Queue rows, pending payloads and history, packets (pending, status, payloads, switch), public updates, mail-draft
placements and send results are read only through `console_snapshot.SnapshotSource`, which opens
`<export>/console-model/<file>.json` with `O_NOFOLLOW` relative to the pinned directory (cap 64 MiB, strict JSON) and never locks, folds,
verifies a signature, recomputes a project id or creates a file. A file that is missing (`console_model_missing`), invalid
(`console_model_invalid`), bound to another project than `EXPORT.json` (`console_model_mismatch`) or exported unavailable raises the error
class the live reader raises, so each page shows "unavailable" with the code and never an empty list or a zero. In particular an
unavailable `sends.json` (`send_results_invalid`, `send_io`) is a `MailSendError`: the review page of an approved mail row answers 503
and no status is built from an empty list. Live mode uses `LiveSource`, which calls the existing readers unchanged. Packet ages are the
ages at export time.

**Read-only.** An approved row's review page says "Approved by <principal>: signature verified on the live project (<id>) at export
<time>" with the signed revision and the signature hash; pending rows show the exported payload (its `project:` line is the live project
id). No approve, reject, edit, packet or oversight form or button is rendered, and every POST is answered 503 `read_only_export` before
any handler or writer runs (with `--decisions-dir` they are journaled instead, below).

### Decision journal (DFGL-16B)

`records panel --export-root DIR --run-id auto --actor NAME --decisions-dir J` renders the decision forms again and records each
organizer decision instead of writing it. `--decisions-dir` without `--export-root` is refused (`journal_needs_export`, exit 2); `J` must
be absolute, owner-only 0700, its own `realpath` and disjoint from `DIR` (else `decisions_dir_invalid`). Without `--decisions-dir` the
export stays read-only (every POST is 503 `read_only_export`).

**What runs.** Approve, reject and edit (`/approvals/*`), packet approve and reject (`/packets/*`) and oversight Undo, OFF and kill
(`/public-updates/*`) keep their parsing, CSRF and field checks. The decision is then checked against the **pinned snapshot** with the
writer's rules (row exists; `pending` for approve and reject; the posted `expected_revision` or `revision` equals the snapshot's; a packet
is `proposed` and unexpired to approve, `proposed` or `approved` to reject; an Undo names an open line) and refused with the same status as
today (e.g. 409 for a stale edit), journaling nothing. A passing decision appends one line to `J/decisions.jsonl` (0600, `O_APPEND`,
`flock` on `J/decisions.lock`): `records-console-decision-v1` with `entry_id` (sha256 of the line without it), `at`, `actor`, `route`,
`action`, `target_id`, `revision_seen`, `value` (signature, reason, edited body or packet reason as the writer would receive it, `\r\n`
folded to `\n`; a message-link approve also carries `choice`), `source_project_id`, `export_run_id` and `export_name`, and answers with
the redirect it always did. No writer, signature check, `approval_queue` or `public_packets` function runs, and nothing is written under
`DIR` or the live project; `/send` is 404 and mail-draft placement stays unavailable. Signature checks happen only when DFGL-16C applies the
entries to the live project.

**Signing text** is the exported live payload (`console-model/queue.json` / `packets.json`), byte for byte what the live project would
ask G to sign, with its `project:` line the live project id.

**Pending apply.** On each request the journal is read (an unreadable or corrupt one answers 503 `journal_unavailable`; a half-written
last line is ignored). Each entry of this project not listed in the pinned `EXPORT.json.applied_journal_ids` shows `pending apply: <action>`
on its row (review, dashboard and next steps, packets, public updates); a later entry for the same target replaces the text, and every
entry stays in the journal. An entry in `EXPORT.json.apply_outcomes` as `{"refusal": "<code>"}` shows `not applied: <code>`. This survives
export swaps because the journal lives outside the export. OFF and kill add that they take effect at the next apply (records run); for an
immediate stop run `records public-updates off|kill --project <live project> --actor NAME` on the host that owns the project.

### Applying the journal (DFGL-16C)

`records decisions-apply --project P --from-journal FILE|-` runs in the container, inside the records run's lock. Each entry, in order, goes
through the one existing function its organizer action calls on the live console: `approval_queue.approve|reject|edit` (an edit passes
`revision_seen` as `expected_revision`; an approve is verified by the existing code against the exported live payload, so G's signature is
checked on the live project and nothing is verified or trusted anew), `public_packets.approve|decide` with `revision_seen`, and
`public_updates.retract|turn_off|kill`. A message-link approve also appends its link as the console does. No mail-draft placement and no send runs.
Outcomes print one line per entry and, when final, are appended to `P/approvals/decisions-applied.jsonl` (0600, `{entry_id, outcome, at}`):
`APPLIED`, `STALE_REVISION`, `SIGNATURE_REFUSED`, `INVALID_TRANSITION` or `REFUSED <code>`. A receipted entry is never retried (`ALREADY`, no event);
the organizer decides again on the next export. Each unreceipted entry first gets a write-ahead `INTENT` line (`{entry_id, outcome: "INTENT", at, base}`;
`base` is the target's queue revision or its packet / public-updates journal length, and an INTENT is never an outcome nor listed in the export). If a run dies after the
live mutation but before its outcome line (queue busy, disk full, timeout), the replay finds the INTENT, looks for that entry's own mutation after `base` (same target,
revision, actor and recorded value) and receipts `APPLIED` instead of running it again against a revision it has itself moved past; when nothing landed it applies the
entry as normal; a retry that finds the target has moved on (a deferred attempt, then another organizer's change) writes a fresh INTENT with the new `base` first, so the
latest INTENT always bounds the attempt that may have landed. If the INTENT cannot be written, nothing is mutated (`DEFERRED`).
`PROJECT_MISMATCH` (the entry's `source_project_id` is another project's) applies nothing and writes no
receipt; a transient lock or I/O failure prints `DEFERRED` and is tried again at the next run. A damaged journal or receipt file exits 1 before anything is applied.

`console-export` then writes `applied_journal_ids` (the `APPLIED` entries) and `apply_outcomes` (`{"outcome": code}`, plus `"refusal": code` when not
applied) into `EXPORT.json`, so the panel's pending overlay clears or shows `not applied: <code>`. `run-records.sh` runs the apply just before the
export when `DECISIONS_DIR` (the host directory of `records panel --decisions-dir`) is also in the render values; the render refuses it without
`CONSOLE_EXPORT_DIR`, and without it the rendered script is unchanged. A failure is logged (`decisions apply rc=N ...`) and never changes the run's exit status.

### Finding review (DF2007-06)

`--analysis-root ROOT` (absolute, a real directory, not a symlink and disjoint from `--export-root` and `--decisions-dir`, else `analysis_root_invalid`)
adds `GET /findings/review`: each finding the analysis packet step holds as `FINDING_UNREVIEWED`, with its agency, topic, claim, cited facts
(quote, 12-hex record prefix and page), the F4 verdict (a model check, not a human review) and the exact `finding-review-v1` text in a wrapping
block. Rejected, deferred and unreadable agencies are counted separately; an unreadable view is said, never shown as an empty list. Without the
option the page says configuration is required. The approve form (pasted signature) and reject form (reason) exist only with
`--export-root` and `--decisions-dir`; a POST needs the same origin, Access and CSRF checks as every decision, re-reads the finding and
refuses a changed one (409) or a pending decision for it, and only appends a `finding_review` journal entry (nothing in ROOT is created or
changed). Apply it with `records analysis-tick --root ROOT --from-journal J/decisions.jsonl` (see CON-FIND-1 in
`docs/RECORDS-CONSOLE-APPROVALS.md`). `--render OUT.html --project P --run-id RUN --analysis-root ROOT` writes the same list with no form, token or
submit URL; OUT must be outside ROOT.

### Redaction sign-off (DF2007-08)

`--flock-store STORE` (absolute, link-free, owner-only 0700, outside the repository and disjoint from `--export-root`, `--decisions-dir` and the
other console directories, else `flock_store_invalid`) adds `GET /redactions`: each proposal made by `tools/flock_redact.py propose` that has no
`SIGNOFF.json` (a dangling link counts), with the agency, the 12-hex document prefix, the full identity, the proposed categories and line/byte
locators, the blind-spot note and the exact text `flock_redact.py payload` prints. STORE is read-only: nothing in it is created, locked, repaired or
written, no original is opened and no derivative is built. A missing or unreadable store is said, never shown as an empty list; an unsafe proposal
is counted and has no form. The form (a pasted signature) exists only with `--export-root` and `--decisions-dir`; a POST takes exactly
`csrf_token`, `slug`, `sha`, `proposal_sha256`, `signature`, re-reads the proposal and refuses a changed one, a new sign-off or a pending entry
(409), and appends a `redaction_signoff` journal entry. Nothing is verified in the console: the page then says "Sign-off submitted; awaiting
signoff-apply" and the proposal stays unsigned. An operator replays it attended:

```
python3 tools/flock_redact.py signoff-apply STORE SLUG SHA DOCUMENT --decisions J
```

DOCUMENT stays explicit (a proposal carries no original location). Each processed entry gets one final receipt in `J/redaction-signoffs-applied.jsonl`
(APPLIED, STALE_PROPOSAL, SIGNATURE_REFUSED, INVALID_ENTRY or SIGNOFF_CONFLICT); a second run prints ALREADY. If the trust anchor is unavailable the
entry is DEFERRED with no receipt and a rerun retries. `records decisions-apply` skips these entries (`SIGNOFF_APPLY_REQUIRED`).

### Hosted decisions behind Access (DFB-11)

`records panel --export-root DIR --run-id auto --decisions-dir J --public-origin URL --access-config C --access-keys K --actor G` is the
hosted admin page that takes decisions. It is the journal mode above with one more rule: **every POST must come through Cloudflare Access
with a token whose signature the console verifies itself**, so the write path does not rest on the edge alone.

- `--access-keys K` is required with `--public-origin` and `--decisions-dir` together (else `access_keys_required`, exit 2) and refused
  without `--public-origin` (`access_keys_unused`). `K` is an owner-only (0600, regular, non-symlink, at most 64 KiB) copy of the team's
  Access signing keys, the JWKS document at `https://<team>.cloudflareaccess.com/cdn-cgi/access/certs` (public keys only; no secret). RSA
  keys of 2048 to 8192 bits with a unique `kid` are used; other members (`public_cert`, non-RSA keys) are ignored. A bad file stops the
  start (`access_keys_invalid`). The console never fetches it: the operator refreshes the copy (Cloudflare rotates the keys); the file is
  re-read on every POST, so a refresh needs no restart. While the copy is unusable every POST answers 503 and nothing is saved; while it
  is out of date (a rotated key) a decision gets the 403 below. Viewing is unaffected either way.
- A POST is taken only on the public Host with exactly one `Cf-Access-Jwt-Assertion` that passes the claims check above **and** whose
  RS256 signature (RSASSA-PKCS1-v1_5, SHA-256, verified with the standard library against the key its `kid` names) is valid. Anything
  else, including a POST on the loopback pair, gets 403 "decisions here need the Access sign-in" before the body is read; nothing is
  journaled. GETs keep the claims tripwire only.
- The decision's `actor` is then `<configured email> via Access` (the email the verified token carries) instead of the `--actor` label,
  in the journal entry and, after the apply, in the live project's events. The `--actor` label stays for pages only.
- Unchanged: the CSRF, Origin and field checks; validation against the pinned snapshot; `/send` is 404; no mail-draft placement; no
  write under `DIR` or the live project. **Approve (and tying a message) still needs G's detached SSH signature over the shown text**;
  an Access login alone never approves anything: `records decisions-apply` verifies the signature at the next records run and receipts
  `SIGNATURE_REFUSED` otherwise.
- The next records run applies the journal (`DECISIONS_DIR` in the `run-records.sh` render values, DFGL-16C above); the export after it
  shows the outcome.

**Rollback:** start the unit without `--decisions-dir` (and without `--access-keys`). The page is then read-only again (every POST 503
`read_only_export`); the journal and its receipts stay on disk and nothing already applied changes.


### Visible edits and approval (CONDRAFT-04)

Save edit and Approve share the draft textarea and its `expected_revision`.
Approve is not Save: the server refuses unsaved words (`approve_unsaved_text`,
409) or an older revision (409) before verifying or recording a signature.
CRLF/LF-only differences are normalized; other text differences must be saved
and reviewed first. The live writer rechecks both preconditions under its lock.

On a hosted export, **Waiting for edit to apply** replaces the signing payload
and approval button while any same-item edit awaits application anywhere in the
journal history. A later action cannot hide it. Save does not recompute a payload,
change project identity, or sign. Wait for live apply and a fresh live-derived
export, review the complete edited words and new digest, then sign that payload.
The pre-edit signature cannot approve the edited draft. `approve_edit_pending`
refuses a direct approval POST (409) without adding a journal entry.

DEFERRED/no receipt remains waiting; even a completed live apply remains waiting
on an old export. A fresh export with APPLIED resolves that edit; a final refusal
resolves the pending attempt but remains a distinct not-applied diagnostic, not a
successful edit. Review the actual saved draft before deciding again. Other queue
items remain independent. Read-only exports still refuse POSTs (503), hosted send
remains unavailable (404), and all authentication/origin/CSRF and signature checks
remain in force. Nothing is auto-signed, sent, or published.
