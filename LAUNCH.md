# Launch guide

How to run the DeFlock Campaign Tool today, what each path delivers, and what has to be true
before the project calls itself launched. Status as of 2026-09-30: everything below runs
offline with synthetic data and passing tests; no Worker, runner or campaign built from this
engine has been deployed to a real account yet. The SLO pilot still runs its own private code.

## What exists

| Path | What an organizer gets | Verified by |
| --- | --- | --- |
| Offline CLI (`python3 -m campaign_tool`) | `init` → `kit` (county or city → selected agencies, one drafted CPRA request per law-enforcement agency, governing bodies, law summary) → `build --check` (multi-page site with agency cards, map, findings, sources, meetings, Atom feed, strict CSP) → `backup`/`verify`/`restore` | 411 unittest cases, 18 node tests, CI on Python 3.11–3.13 |
| California law package (`jurisdictions/us-ca`) | CPRA deadlines (10 days + 14-day extension), Civ. Code §§ 1798.90.5–.55, Veh. Code § 2413, SB 54, AG bulletin 2023-DLE-06; 9 rules verified against leginfo text, 2 marked likely; status `draft` until a second reviewer signs | `tests/test_law.py`, `docs/LAW-PACKAGES.md` |
| Agency seed (`data/agencies/us-ca.json`) | 58 counties, 1,043 agencies (sheriff, board, DA, CHP per county; 329 police departments; 482 city councils). Custodian emails, portals and transparency slugs are `null` until an organizer verifies them | `tests/test_discovery.py` |
| Wizard Worker (`workers/wizard`) | Seven screens: location → campaign → agencies → requests → accounts → deploy (plan, apply with per-resource receipts and rollback list) → handoff. Encrypted 1-hour session; no token kept after handoff | 13 vitest cases incl. a full DRY_RUN flow |
| Workspace Worker (`workers/workspace`) | Access-protected organizer app: dashboard, requests with statutory clocks, inbox, records, findings with two-reviewer three-role gate, approvals (every external effect is a card), publish and corrections, subscribers (counts only), meetings and comment kits, settings. Runner API, cron (`0 7,13 * * *` and `30 20 * * *`, evaluated in UTC by Cloudflare: 00:00/06:00/13:30 Pacific until the wizard writes a local-time schedule), inbound mail handler, export.json | 40 vitest cases |
| Public-site Worker | Serves the versioned static site from the public bucket with the site's `_headers` CSP | 3 vitest cases |
| Runner (`runner/`) | Container that leases jobs and runs the engine: classify mail, store attachments by hash, sandboxed extraction, redaction, deterministic detectors, optional model over redacted text only, follow-up drafts, site builds. `strict_local` tier refuses non-local model hosts | `tests/runner/*`, end-to-end pipeline test |
| Outbox (`campaign_tool/outbox.py`) | The only code that sends records requests: journal with idempotency keys, organizer approval and identity required, per-agency daily cap, fee cap, MuckRock 402 → blocked, ambiguous failures block resends. (Newsletters go through the `send_newsletter` executor and Brevo, under an approval card; an ambiguous send there fails the card with the Brevo campaign id and must be checked at Brevo before a new draft is proposed) | `tests/test_outbox.py`, `workers/workspace/test/executors.test.ts` |

## Try it in ten minutes (no accounts)

```sh
git clone https://github.com/Grahammmm/DeFlock-Campaign-Tool.git && cd DeFlock-Campaign-Tool
python3 -m campaign_tool init --directory ../my-campaign --county "San Luis Obispo" --state CA --name "My Campaign"
python3 -m campaign_tool kit --directory ../my-campaign        # agencies + drafted requests, nothing sent
python3 -m campaign_tool doctor --directory ../my-campaign
python3 -m campaign_tool build --directory ../my-campaign --check
python3 -m http.server 8080 --bind 127.0.0.1 --directory ../my-campaign/public
python3 -B -m runner --once --fake                                # exit 3 = idle, pipeline wired
```

Edit `../my-campaign/kit/agencies.json` to untick agencies; re-run `kit` and your edits persist.
Add `content/` (see `docs/SITE-CONTENT.md` and `examples/fictional-campaign/content/`) to turn
the starter into a real site.

## Hosted path (what the wizard does)

1. Organizer opens the wizard, enters a county or city, confirms the agency list and request
   drafts, pastes a Cloudflare API token with the ten listed scopes, a mailbox or Email Routing
   choice, Brevo list id, and a model provider or `strict_local`.
2. The wizard shows the plan (D1, two R2 buckets, KV, Queue, two Worker uploads, migrations,
   seed rows, DNS, Access app and policy, Email Routing rule, secrets, cron) and applies it in
   the organizer's account, writing a receipt per resource.
3. Handoff shows the workspace URL, the public site URL and the runner token once. The
   organizer starts the runner (Cloudflare Container, a VPS, or their own machine) with
   `WORKSPACE_URL` and `RUNNER_TOKEN`.
4. From then on the cron creates jobs, the runner proposes, the organizer approves in the
   workspace, and the outbox sends.

To stand up the wizard itself: `cd workers && npm ci && npm run check`, copy
`wizard/dev.vars.example` to `wizard/.dev.vars`, set `SESSION_KEY`, and `npx wrangler dev`
(then `wrangler deploy` to the project's own account when ready). Run the first real apply
with `DRY_RUN=1` and read the recorded plan before running it live.

## Gates before "launched"

| Gate | Status | What closes it |
| --- | --- | --- |
| Kit demo for three CA counties | Passed offline (SLO, Monterey, Santa Barbara resolve and draft) | — |
| Fresh account live in under one hour | Not run | Deploy the wizard, run apply against a throwaway Cloudflare account and domain, record timing and cost in this file |
| SLO cut over to the engine | Not started | Export the pilot's originals and ledger; write the import into D1 and R2 (`backup restore` extracts to a local directory and `export_hosted` only exports from a hosted workspace — a hosted restore is not written, see docs/BACKUP.md "Not done"); point the records mailbox at the workspace; retire the old exporter after one clean week |
| Second organizer live, owner's in-chat review passed | Not started | Recruit from the DeFlock victories list or Rural Privacy Coalition; the review is the owner's in-chat review with Claude per the owner's decision, not an independent security or privacy review (ROADMAP item 15 still lists that as open) |
| Law package `reviewed` | `draft` | A second reader checks each rule against the cited primary text and signs `reviewed_by` |

## Known gaps (also listed in README "Planned")

- Worker executors for `send_request` and `send_followup` fail `sender_not_configured` until
  the engine outbox is bound to the `MailSender` port; the outbox works as a CLI today.
- Runner answers `newsletter_draft` and `backup` jobs with `blocked` (no handler yet).
- No OCR; image-only pages are reported `ocr_needed`.
- The CLI kit selects all discovered agencies by default while the wizard unticks CHP and the
  district attorney; pick one default.
- `locate --online` (Census geocoder) and `tools/refresh_agency_seed_online.py` have not been
  exercised against the live services.
- The Docker image has not been built in CI yet (no daemon in the build sandbox); the
  workflow exists.
- Windows is unsupported for the runner and intake (POSIX-only sandboxing).

## Decisions recorded 2026-09-30

Keep the name DeFlock Campaign Tool; runner default is a Cloudflare Container in the
organizer's account; newsletter provider is Brevo; privacy tier default `redacted_cloud`
(records stay local, only redacted text reaches a model; known names go in the denylist
because regex redaction cannot catch every bare name) with `strict_local` as a switch; SLO
cuts over at the phase 2 gate;
review is done in-chat with the owner, no outside reviewer.
