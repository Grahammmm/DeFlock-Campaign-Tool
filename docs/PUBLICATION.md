# Publication workflow

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

    python3 -m campaign_tool finding publish DRAFT --directory D --approved-by NAME

`finding publish` writes `D/content/findings/<slug>.json` with `state: "published"`, `published_at`
(now, UTC) and empty `corrections` only when the full `review_blockers` (receipts included) is empty,
`--approved-by` is not the author, `content.load_finding` accepts the document, the slug and id are
new (not in `content/findings/` or `private/withdrawn/`), and every cited `sha256` is in
`content/sources.json`, the ledger and `private/objects/` (refusals name a 12-hex prefix and
`unregistered`, `not_ingested` or `object_missing`). The write is atomic; one line
`{at, action: "publish", id, slug, content_sha256, approved_by}` is appended to
`D/private/publication-log.jsonl` (0600). It never builds, deploys or contacts anything: run
`build` and review the site before any deploy.

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

## Not done

No reviewer directory or role assignment beyond the Access policy; no diff view between
content versions; no automatic notification to reviewers. The runner that executes
`build_site` is a separate component (see [ROADMAP.md](ROADMAP.md)).
