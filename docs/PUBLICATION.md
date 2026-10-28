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

## Not done

No reviewer directory or role assignment beyond the Access policy; no diff view between
content versions; no automatic notification to reviewers. The runner that executes
`build_site` is a separate component (see [ROADMAP.md](ROADMAP.md)).
