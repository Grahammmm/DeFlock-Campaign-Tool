# Publication workflow

## Press drafts

`python3 -m campaign_tool press draft --directory D --finding ID [--finding ID] [--date YYYY-MM-DD]`
creates one private `kit/press/` Markdown draft from published findings only. Review its facts,
limitations, corrections, links and contact before any use. It sends and publishes nothing.
Edited drafts are kept; the fresh version goes to `kit/press.suggested/`.

### Letters to the editor

`python3 -m campaign_tool press letter --directory D --finding ID [--finding ID] [--words N] [--outlet NAME] [--date YYYY-MM-DD]`
writes one private, never-sent first-person letter to `kit/press/letters/<date>-<first-slug>.md` from one to
three published findings (`--words` 150 to 400, default 250; `--outlet` is plain text for the salutation only).
Each finding gets one short paragraph: its summary exactly as published, `(finding ID)`, `Corrected DATE.` with the
latest correction date when it has any, and its finding link when `base_url` is set. The closing points to the next
scheduled staged meeting of the FIRST finding's agency (no speaking or comment claims), else to the signup section
(`/#participate`). `[YOUR NAME]` and `[YOUR CITY]` are left for the organizer.

Finding paragraphs are dropped whole from the end until the visible body fits N words (whitespace tokens with a
letter or digit; the two placeholders and the initial draft comment are not counted); only a first paragraph that
cannot fit is refused. The count is printed as `Letter word count: X/N.` The legal-claim wording guard is alert-only:
rule names are printed to stderr as `review-required` notes and nothing is blocked. Edited letters are kept and the
fresh version goes to `kit/press/letters.suggested/`. The letter record is `kit/press/letters/.written.json`, separate
from the release draft's. Nothing is sent or published; the release `press draft` is unchanged.

How a finding goes from draft to the public site in the workspace Worker, and how it is
corrected or withdrawn afterwards. Code: `workers/workspace/src/review_state.ts`,
`publication.ts`, `manifest.ts`, `executors/publish_finding.ts`, `executors/deploy_site.ts`.
Nothing here is a substitute for the independent review the gate records; the workflow only
refuses to publish without it.

## States

| State | Meaning |
| --- | --- |
| `draft` | No review receipt is bound to the finding's current `content_sha256`. |
| `in_review` | Receipts exist for the current content; only more reviews are missing. |
| `blocked` | A `reject` or `changes_requested` receipt on the current content, or a structural blocker (missing evidence, invalid hash) after at least one approval. |
| `ready` | `review_blockers()` (the shared port that mirrors `campaign_tool/review.py`) returns nothing. |
| `published` | A `publication` row exists with the exact content hash and public path. |
| `corrected` | A `correction` row exists; the finding stays on the site with the correction note. |
| `withdrawn` | Removed from the content manifest; the site is rebuilt without it. |

`published`, `corrected` and `withdrawn` are terminal for the recompute: review receipts
posted after publication are rejected with 409. Editing a finding (no edit route exists yet; today a finding is replaced by a new one through a correction) would change its content hash,
which unbinds every earlier receipt and returns it to `draft`.

## Review receipts

`POST /findings/:id/review` with `role` (`factual | legal | privacy`), `decision`
(`approve | reject | changes_requested`) and a non-empty `rationale`. The reviewer identity is
the verified Access JWT email, never a form field. Authors cannot review their own finding
(403). Each receipt is bound to the content hash at submission time; the state is recomputed
from all receipts after every submission.

## Offline review receipts (CLI)

Start a draft with `python3 -m campaign_tool finding new --directory D --agency ID --title T
[--source PREFIX ...] [--author NAME]`. It writes a new `D/private/drafts/fnd_<12 hex>.json` (directory 0700, file
0600; never overwrites; a symlinked `private/` or `private/drafts/` is refused) with every required field, the
agency, and each `--source` (12–64 hex of exactly one `content/sources.json` record) resolved to its hash and
title. Hand-written fields hold `[EDIT ME: ...]` markers; `review-status` names one `edit_marker:<field>`
blocker per marked field (`edit_marker:sources[i].locator` for sources), and `build` refuses a published
finding that still carries one. Nothing is published.

    python3 -m campaign_tool finding review DRAFT --directory D --role factual|legal|privacy \
        --decision approve|reject|changes_requested --reviewer NAME --rationale TEXT
    python3 -m campaign_tool finding review-status DRAFT --directory D

`finding review` appends one receipt to `D/private/reviews/<finding id>.json` (directory 0700,
file 0600, atomic write) under an exclusive lock on `D/private/reviews/.lock` (0600), so
concurrent reviews never drop each other's receipts, and writes nothing else. Reviewer names
are **self-asserted**: offline
there is no identity check, unlike the hosted flow, where the identity is the Access JWT email.
The tool only refuses a reviewer whose name matches the draft `author` (case and spacing
ignored) and a duplicate receipt. Each receipt is bound to the hash of the draft without its
publication fields (`state`, `published_at`, `updated_at`, `corrections`), so changing those
keeps it bound, while any edit to a reviewed field (for example `summary`) unbinds every earlier
receipt. `finding review-status` is read-only and prints the receipts for the current content,
the stale ones and the `review_blockers` list (two independent approvals covering factual, legal
and privacy, with no reject or changes_requested on the current content).

Campaign binding: each campaign holds a random id in `D/private/campaign-id.json` (0600, written once by `init`,
or by the first `finding review` or `finding reviewers add` in an older campaign, and never replaced). Every
receipt records it as `campaign_id`, and the `review-payload` bytes a reviewer signs include it, so a receipt or
signature made in one campaign does not approve the same finding in another. Only approvals carrying this
campaign's id count; a current approval with another campaign's id adds the blocker `foreign_campaign_receipt`,
and one with no id adds `unbound_legacy_receipt`, so neither is accepted or dropped silently. A reject or
changes_requested receipt blocks whatever its binding. `review-payload` refuses in a campaign that has no id yet.

Receipts written before binding have no `campaign_id`, so after upgrading they approve nothing until the organizer
binds them: `finding bind-receipts --directory D` lists, per finding, how many receipts are unbound or bound to
another campaign and changes nothing; check that every listed receipt was granted in this campaign, then rerun
with `--confirm`. That stamps each unbound receipt with this campaign's id and `campaign_bound: {by, at}` (every
other field kept), never touches a receipt bound to another campaign, and appends one line per finding to
`D/private/reviews/binding-log.jsonl` with the sha256 of each receipt as it was. A signed receipt bound this way
keeps verifying against the bytes it was signed over. Binding is an attestation: do not run it on receipts copied
from another campaign.

Wording: a `title`, `summary` or `body_md` containing `violat*`, `illegal*`, `unlawful*`, `criminal*` or
"broke the law" (any case; NFKC-normalised with zero-width and soft-hyphen characters removed) adds the blocker
`legal_conclusion_wording_in_<field>` (the field name only, never the words), so `review-status` lists it and
`publish` and `build` refuse. The one exception is a `confirmed_conflict` finding with a legal-role approve receipt
on its current content. Quoting an agency is still flagged: rephrase as "the record shows ...", or classify it
`confirmed_conflict` and get legal review. This is a wording gate, not legal validation.

Preview before you approve: `finding preview DRAFT --directory D --out DIR` (DIR outside `public/` and `content/`) writes `DIR/findings/<slug>.html` and `DIR/style.css`,
the page `build` would render, with a "Draft - not published" banner and `noindex`; it never touches `public/`, the feed or the sitemap.
The draft is rendered alongside the published findings (cited-rule citations and law-package status as build shows them); a slug already used by another finding or a cited rule build would refuse stops the preview.

Sending a packet to an outside reviewer (an attorney or a partner): `finding packet DRAFT --directory D --out DIR` makes a new
private folder (0700, files 0600; DIR must not exist and must be outside `public/`, `content/` and `private/`) with
`draft.json`, `preview.html`, each cited record as `records/<sha256>` (re-hashed first), `receipts.json` (its ledger
receipt rows), `reviews.json`, `review-status.json`, `README.txt` and `MANIFEST.sha256` (`sha256sum -c MANIFEST.sha256`).
A missing or altered record, a ledger with a journal/WAL file beside it, or records over 500 MiB stop it with nothing
written. The tool never zips, uploads or sends it: hand it over yourself, privately, and treat it as confidential.

Signed receipts (optional): `finding reviewers add ... --ssh-key FILE.pub` stores an `ssh-ed25519` or
`ecdsa-sha2-nistp256` key and rewrites `D/private/reviewers.allowed_signers` (0600; principal = name with
spaces as `_`). `finding review-payload DRAFT` (same flags as `finding review`) prints the exact bytes; sign
them with `ssh-keygen -Y sign -n deflock-finding-review -f KEY payload` and pass `--signature payload.sig`.
The tool verifies before writing; `review-status` reports `signed`, `self_asserted` and `bad_signatures`
(re-verified each time), and `publish` prints the same counts. This trust root is the organizer-held
campaign file, **weaker than** the root-owned `/etc/deflock` approval file; `approval.verify` and
`/etc/deflock` are not used or changed. Without `ssh-keygen` (OpenSSH 8.2+) only unsigned review works.

    python3 -m campaign_tool finding publish DRAFT --directory D --approved-by NAME

`finding publish` writes `D/content/findings/<slug>.json` with `state: "published"`, `published_at`
(now, UTC) and empty `corrections` only when the full `review_blockers` (receipts included) is empty,
`--approved-by` is not the author, `content.load_finding` accepts the document, the slug and id are
new (not in `content/findings/` or `private/withdrawn/`), and every cited `sha256` is in
`content/sources.json`, the ledger and `private/objects/` (refusals name a 12-hex prefix and
`unregistered`, `not_ingested` or `object_missing`). A campaign-wide lock
(`D/private/.publish.lock`) holds the uniqueness checks, write and log entry together, so of two
concurrent publishes of the same slug or id only one succeeds. The write is atomic; one line
`{at, action: "publish", id, slug, content_sha256, approved_by}` is appended to
`D/private/publication-log.jsonl` (0600). It never builds, deploys or contacts anything: run
`build` and review the site before any deploy.

    python3 -m campaign_tool finding correct ID --directory D --date YYYY-MM-DD --note TEXT --approved-by NAME [--from DRAFT]

`finding correct` appends one `{date, note}` to the published finding whose `id` is ID (exactly one
file in `content/findings/`, never a symlink) and sets `updated_at` (now, UTC). The date must be a real
`YYYY-MM-DD`, not after today (UTC), not before the `published_at` date and not before the last
correction's date (equal is fine); the note is non-empty, at most 1000 characters, no control characters.
With `--from DRAFT` the reviewed fields are replaced by the draft's (same id and slug), which must clear
the same review blockers and cited-record check as `publish`; `id`, `slug`, `published_at` and earlier
corrections are kept. One `{at, action: "correct", id, slug, date, content_sha256, text_changed}` line is logged.
A finding about several agencies (`also_agency_ids`, see SITE-CONTENT.md) is hashed with that list as entered, so adding,
removing or reordering an agency on a published finding needs a correction from a freshly reviewed draft; `publish` and
`correct` refuse an unknown agency id before anything is written.

    python3 -m campaign_tool finding withdraw ID --directory D --reason TEXT

`finding withdraw` moves the one published finding whose `id` is ID from `content/findings/<slug>.json` to
`D/private/withdrawn/<slug>.json` (folder 0700, file 0600) with `withdrawn: {at, reason}` added, and logs
`{at, action: "withdraw", id, slug}` (the reason stays in the private copy only). The copy is kept and its id
and slug stay taken: a later `publish` of either is refused. The next `build` omits it; no notice page is made.
If the content file cannot be removed both copies remain and the leftover is named. It never builds or deploys.

    python3 -m campaign_tool finding status --directory D [--draft PATH ...] [--json]

`finding status` is read-only (it opens nothing for writing) and prints one row per finding: every
`D/private/drafts/*.json` (one level, symlinks not followed) and `--draft PATH`, every `content/findings/` file and every
`private/withdrawn/` copy, with the states above applied to these files, current/stale receipt counts and blocker names
(never reviewer names, rationales or notes). A draft whose id is published is a second `pending correction` row; a withdrawn
id wins (a leftover content file is named); an unreadable file is an `unreadable` row with its path and a fixed reason
(not JSON, a symlink, too large, ...). A reason that can quote the file, such as an unknown key, is withheld. Exit 0.

### Drafting a gap finding from a request

When an agency denied a request or closed it without producing records, start the draft from the request
itself: `python3 -m campaign_tool finding new --directory D --gap AGENCY [--request NAME] [--title T] [--author NAME]`
(`--request` defaults to `main`; `--gap` cannot be combined with `--agency` or `--source`). It reads
`kit/sent.json` and writes the same private 0600 draft, with `classification` `information_gap`, `event_date`
the date of the denial or closure, and a facts block at the start of `body_md`: when and how the request was
sent, the agency's answer and date, and each stated withheld basis as the agency wrote it (angle brackets and
line breaks flattened so it stays plain text). The request's correspondence records that are in
`content/sources.json` become the sources; any that are not are printed as `blocker: <12 hex> is not in the
source library` and listed in an `[EDIT ME: add these sources before review: ...]` limitations line, so run
`sources add-batch --from-request` or `sources add` first. Everything interpretive (summary, confidence,
limitations, counterevidence, the finding text, source locators) stays an `[EDIT ME: ...]` marker. A request
that is still open (no answer, or only an acknowledgement, extension or partial answer) or that produced
records is refused; write a regular finding for those. Nothing is published.

## Propose, approve, execute

1. `POST /findings/:id/propose` (ready findings only, confidence not
   `needs_attorney_review`) creates a `publish_finding` approval card, idempotent on
   `finding_id + content_sha256`. The proposal records the hash, the public path and the
   approving reviewers so the card shows what will be published.
2. An organizer approves the card (`approved_by` = Access email). Approving and executing are
   separate actions; a different person can execute.
3. `publish_finding` re-checks everything at execution time: the finding exists, confidence is
   not `needs_attorney_review`, the content hash still matches the proposal
   (`content_changed` otherwise), the state is `ready`, and the review gate has no blockers.
   It then writes the `publication` row, sets the state to `published`, enqueues one
   `build_site` job whose inputs carry the full content manifest assembled from D1, proposes
   the matching `deploy_site` card (proposed, never auto-approved) and enqueues a
   `newsletter_draft` job (see [NEWSLETTER.md](NEWSLETTER.md)).
4. The runner builds the site from the manifest (`campaign_tool build --check`) into
   `sites/<site_version>/` in the public bucket and marks the job done.
5. An organizer approves and executes `deploy_site`, which flips the `site_version` setting and
   KV key the public-site Worker serves. It refuses while the build job is not `done`.

Executing an already-executed card returns the existing receipt. Executing a `publish_finding`
card for a finding that is already published with the same hash returns the existing
publication (`already_published: true`) instead of writing a second row.

## Content manifest

`assembleManifest(repo)` produces everything `campaign_tool.content.load_content` reads from a
`content/` directory: site document, published and corrected findings (only the keys
`FINDING_KEYS` allows; anything else in `finding_json` stays private), meetings, sources,
agencies and the optional map. Withdrawn findings never appear. `site_version` is the manifest
hash, so an unchanged manifest re-uses the same build job and deploy card. The manifest carries
no originals, correspondence, subscribers or credentials. `GET /api/manifest.json` returns it.

## Before publishing: evidence check

`python3 -m campaign_tool evidence --directory D --check` reads `content/` and, read-only,
`private/ledger.sqlite`. For every published finding and every record it cites it prints one
problem row (finding slug, first 12 hex of the hash, code); `--check` exits 3 when any is listed:

- `unregistered`: the hash is not in `content/sources.json`.
- `not_ingested`: no ledger row; the original was never ingested (every citation without a ledger).
- `object_missing`: a ledger row exists but `private/objects/<sha256>` is missing or a symlink.

It never reads receipts or re-hashes objects (`status --check` does that). Invalid content stops
with exit 1.

A finding page shows a "Legal basis" section when the finding cites a rule or sets `rule_version`, `duty` or
`exceptions`: those fields, then each cited rule's package `citation` (never other package text) and the label
"draft, not legally reviewed" unless the package status is `reviewed` (then "reviewed on DATE").

## Corrections and withdrawal

- `POST /findings/:id/correct` with `reason` and optional `replacement_finding_id` (which must
  be a different, already published finding). Writes a `correction` row on the publication,
  sets the state to `corrected`, enqueues a rebuild and proposes a new `deploy_site` card.
  The finding stays public with the correction text and date; the original is never silently
  rewritten.
- `POST /findings/:id/withdraw` with `reason`. Writes a correction row prefixed `withdrawn:`,
  sets the state to `withdrawn`, enqueues a rebuild without the finding and proposes
  `deploy_site`.

Both require an authenticated identity and a non-empty reason; both are visible in the
Approvals list as the rebuild they trigger.

In `feed.xml` (and each per-agency feed) a corrected finding's entry `updated` is the latest of
`published_at`, `updated_at` and its correction dates (a date counts as midnight UTC) and its summary
starts `Corrected YYYY-MM-DD.` with the newest correction date, so feed readers show it again. The build
refuses a correction dated before the calendar date of `published_at` and an `updated_at` earlier than it.

## Social drafts (never posted)

`python3 -m campaign_tool social --directory D` writes one private draft per published finding
to `kit/social/<slug>.md`: classification label, title, finding URL and finding id, at most 280
characters (only the title is shortened) plus `Corrected <latest date>.` when corrected. An
edited draft is kept and the fresh one goes to `kit/social.suggested/`; drafts for slugs no
longer published are listed as `stale`, never deleted. Nothing is posted and nothing goes
under `public/`; posting stays an organizer decision outside this tool.

## Campaign flyer (never printed or posted)

`python3 -m campaign_tool flyer --directory D [--max-findings N] [--replace]` writes a private one-page
flyer to `kit/flyer/flyer.html` and a plain `flyer.txt` twin (folder 0700, files 0600): the `site.json`
logo (copied beside it as `logo.<ext>`), campaign name, tagline, county and state, up to N (1-6, default 3)
published findings newest `published_at` first with their classification and confidence labels and public
URL from `base_url`, "N more on the site" when cut, a "Get involved" link to `base_url`, and the
not-legal-advice line. With no published finding it says records are requested and nothing is published
yet; without `base_url` it prints no links. Draft, withdrawn and challenged findings never appear. The
HTML has one inline `<style>`, no script and no external load; its only links are the campaign's own
`base_url`. An existing flyer is refused without `--replace`; a symlinked folder or file, or a logo
named in `site.json` but missing, is refused. Nothing is printed, sent or published and nothing is
written under `public/` or `content/`; printing and handing it out stay an organizer decision.

## Reviewer roster (role assignment, not authentication)

`finding reviewers add --directory D --name N --role R [--role R]` (R: factual, legal, privacy) merges
roles into `private/reviewers.json`, keeping `added_on`; `finding reviewers list` prints it. With a
roster, `finding review` refuses an unlisted name or unassigned role and `review-status` lists
`unrostered_receipts`; without one nothing changes. Names stay self-asserted: roles, not identity.

### Declared interests (private)

`finding reviewers add --directory D --name N --role R --interest-agency ID [--interest-agency ID ...]
[--interest-note T]` records, in `private/reviewers.json`, the agencies the reviewer works for, is employed by
or otherwise declares (0 to 20 known agency ids, each distinct). Supplied `--interest-agency` flags replace the
list (never merge); with none the declaration is kept. `--interest-note` (1 to 200 characters, no control
characters, kept verbatim) needs a declared agency and is stored only in that file: `reviewers add`, `reviewers
list` and every refusal omit it. `--clear-interests` removes the agencies and the note and cannot be combined
with either. A roster without interests reads and behaves as before.

The roster is evaluated as it stands now. `finding review` and `review-payload` refuse every decision and signed
receipt from a rostered reviewer whose declared agencies include the finding's `agency_id` or any
`also_agency_ids` (exact ids; the first match is named), before anything is written: `reviewer has declared
interest in agency ID; receipt refused`. Earlier receipts stay as written. `review-status` lists those from a
reviewer who now declares a matching agency under `declared_interest` (reviewer, role, decision, time, agency
ids; no note, rationale or hash), and `review_blockers` leaves their approvals out of the role and two-reviewer
counts at every gate (status, ready, publish, `correct --from`). Challenges (reject, changes_requested) still
block. Changing a declaration reclassifies earlier receipts either way; this is declared-interest exclusion, not
proof of independence, and nothing is re-signed, withdrawn or rebuilt. Nothing about a reviewer's interests is
published; `methods.html` carries only the generic sentence that reviewers declare interests and a declared
interest excludes their review from that agency's findings.

## Publication gate (one check at every publishing path)

`campaign_tool/publication_gate.py` holds one check, `assert_publishable(artefact_id, content_hash, approvals)`,
and every path that emits publishable content calls it before writing: `finding publish` and `finding correct`
(`campaign_tool/findings.py`), `flock_publish.py stage` and `flock_publish.py brief`. It returns nothing or
raises `PublicationRefused` (a `ValueError`) naming the artefact id and one reason:

| Reason | Meaning |
| --- | --- |
| `no_approval` | no approval record was given |
| `approval_for_other_content` | approvals exist, none for this artefact id |
| `content_changed_after_approval` | the artefact's content hash differs from every approval's (one byte is enough) |
| `status_not_approved` | the matching approval's `status` is not `approved` |
| `approver_unnamed` | `approved_by` is empty, a placeholder (`-`, `n/a`, `tbd`, `auto`, `system`, `agent`) or a model or lane name |
| `approval_consumed` | the matching approval carries a truthy `consumed` |
| `redaction_unsigned` | the approval carries `redaction_signed` and it is not true |
| `approval_unbound` | (`stage`/`brief`) the approved `PUBLISH.json` entry records no `review.content_hash` |

The gate reads those fields, never prose in the artefact or its file name. `redaction_signed` is set by no path
yet; it is reachable once a redaction sign-off exists. A legal-claim flag is recorded on a refusal and never causes
one. A refusal prints only the id and reasons, exits non-zero and writes nothing. No code writes an approval: the
gate has no writer, and `tests/test_publication_gate.py` fails if any other module writes an approval field outside
the existing human review surface (Python modules by syntax tree, other languages by a literal pattern).
`finding publish` and `finding correct` (`--approved-by`, refused without a named person) bind the approval to the exact
draft content, re-read just before the write.
For `stage` and `brief` the approval is the `PUBLISH.json` review fields, bound to the content as it stood when the
person approved it (DF-APPROVAL-TIME-BIND): `review.content_hash` is the sha256 of the entry without `review`, which
the person records with the approval (`flock_review_queue.py show` prints it beside the text). The gate compares that
recorded hash with the entry the run re-derives now, and the filed entry must equal that re-derivation; a hash is
never recomputed from the file to stand in for the recorded one, so editing an approved entry together with the run
that derives it voids the approval. **Legacy approvals** (recorded before this field existed) carry no hash: they
stay in the file untouched, `--check` still accepts them, and the review queue lists them again with an
`approval-unbound` warning, but they approve nothing (`approval_unbound`) until a person re-reads the text and records
its hash. Nothing stamps them automatically. A clean gate does not prove an artefact is fit to publish: redaction
sign-off, the privacy lint and the legal-claim notes are separate conditions.

## Not done

No reviewer signatures or identity check (roster names are self-asserted); the workspace Worker has
no reviewer directory or role assignment beyond the Access policy; no automatic notification to
reviewers. `finding assign DRAFT --directory D --role R --reviewer NAME --on D [--replace]` only records, privately in
`private/review-assignments/`, who was asked to review which role since which date (refused for the draft's author, and
with a roster for an unlisted name or a role they do not hold); nothing is sent, an assignment is never a review and no
blocker, receipt count or publication check reads it. `finding review-status` shows each role as assigned since D,
receipt_recorded or unassigned, and `today --review-days N` (default 7) lists assignments with no receipt after N days.
`finding diff ID --directory D --from DRAFT` (or `OLD NEW`) prints a read-only field-level
diff of the public fields (including `agency_id` and `also_agency_ids`; a reorder of the extra agencies prints the old and
the new ordered list) before a correction; exit 3 when they differ. The runner that executes
`build_site` is a separate component (see [ROADMAP.md](ROADMAP.md)).

The offline starter has a page-level preview: `python3 -m campaign_tool build --directory D --preview`
renders into memory and prints, as JSON, every `public/` file a rebuild would add, remove or change
(path and byte delta; equality by SHA-256), then `Preview only; public/ was not changed.` It exits 3
when anything differs and 0 when the rebuild would be identical. Symlinks inside `public/` are listed
as `unreadable` and never followed. Nothing is written to the campaign folder.

### Social drafts per platform (never posted)

`social --directory D --platform P` (repeatable: `x`, `bluesky`, `mastodon`; duplicates collapsed,
order kept) sizes each draft by that platform's own count into `kit/social/<P>/`: X 280 weighted
(most non-Latin characters and emoji count 2, every link 23), Bluesky 300 code points with the full
link, Mastodon 500 code points with 23-character links. Only the title is shortened. Every draft for
every chosen platform is made before anything is written, so one that cannot fit stops the run with
the platform named. Each folder keeps its own `.written.json`, kept edits go to
`kit/social/<P>.suggested/` and stale slugs are listed per platform. Without `--platform` the run is
unchanged (`len()` within 280 into `kit/social/`); `--check` reads that folder only.

### Social drafts for meetings (never posted)

`social --directory D --meetings [--within-days N] [--platform P ...]` (N from 1 to 60, default 14)
writes one private draft per staged meeting that is `scheduled`, starts between now and N days
ahead and carries organizer-verified `participation`, to `kit/social/meetings/<id>.md` (or
`kit/social/meetings/<P>/<id>.md` sized by that platform's rule; kept edits go to
`kit/social/meetings.suggested/` or `kit/social/meetings/<P>.suggested/`). The post gives the body,
the date and time in the meeting's own UTC offset (as the meeting page prints it), the agenda item,
the verified written-comment deadline and how, how to attend, and `<base_url>/meetings/<id>.html`
when `base_url` is set. Only the agenda item is shortened. Cancelled, past, later-than-window and
unverified meetings get no draft and are counted under `skipped`; an unreadable meeting file is
counted and named, never fatal. `--meetings` cannot be combined with `--check`; finding drafts and
`--check` are unchanged. Nothing is posted.

### Social drafts for outcomes (never posted)

`social --directory D --outcomes [--within-days N] [--platform P ...]` (N from 1 to 60, default 14)
writes one private draft per staged meeting whose organizer-recorded `outcome` is dated in the last
N calendar days, to `kit/social/outcomes/<id>.md` (or `kit/social/outcomes/<P>/<id>.md` sized by that
platform's rule; kept edits go to `kit/social/outcomes.suggested/` or `kit/social/outcomes/<P>.suggested/`).
The window is on the recorded `outcome.on` date, inclusive: today and the N-1 days before it (exactly N
dates), not the meeting start, the verification date or the file time. The post reads
`<body> — <outcome.on>: <agenda item>. Result: <result>.`, then ` Vote: <vote>.` only when a vote line was
recorded, then ` Official record: <outcome.source_url>` and `<base_url>/meetings/<id>.html` when `base_url`
is set. The result token and the vote line are quoted exactly as recorded; no tally is derived from
per-member votes and no decision is inferred. Only the agenda item is shortened; if the fixed fields
alone cannot fit the platform's limit the run is refused, naming the meeting and platform, before any
file is written. A meeting without a recorded outcome, a cancelled one, or one outside the window gets
no draft and is counted under `skipped` (`cancelled`, `no_outcome`, `outside_window`, `unreadable`);
an unreadable meeting file is counted and named, never fatal. Participation is not required. A draft
you edited is kept (the fresh text goes to the suggestions folder); a corrected outcome updates only
untouched drafts; drafts that left the window are listed as `stale`, never removed. `--outcomes` cannot
be combined with `--meetings` or `--check`. Nothing is posted.

## Posting one approved social draft (Worker `post_social`)

A social post is a message to the public and cannot be recalled, so the approval is **per post**.
The CLI above still never posts. The workspace Worker posts only through a `post_social` approval
card whose subject is the finding id and whose proposal names `finding_id`, `platform`, the exact
`text` and its `content_sha256`. At execution the executor refuses by name, posting nothing, unless:

- the card is approved by an Access identity, not yet used, and approved within 7 days
  (`no_approval`, `approval_consumed`, `approval_expired`);
- the card names this finding and the one configured platform (`approval_mismatch`);
- sha256 of the exact text equals the approved hash, so an edited draft needs a new approval
  (`content_hash_mismatch`);
- the finding is `published` with a publication record for its current content, by field, never by
  wording such as "APPROVED" in the draft (`finding_not_published`, `finding_not_approved`);
- the text matches no email, phone or plate pattern (`privacy_lint`) and fits the platform.

DF-4 legal-claim flags (`review_flags`) show on the card and are counted on the receipt
(`legal_flag_count`); they never block. One adapter (Mastodon) is configured by `MASTODON_URL`
and the `MASTODON_TOKEN` secret; without them every card fails `social_not_configured`
(`social_posting: not configured`). `(finding, platform)` has one receipt slot taken before the
call: a second card is `already_posted`. A timeout, 5xx or reply without a post id is recorded
`post_uncertain`, raises one incident and is never retried; check the platform by hand. The receipt
holds ids, hashes, counts and enums only, never post text, handle or personal data. There is no
batch, schedule, edit, delete or recall path.
