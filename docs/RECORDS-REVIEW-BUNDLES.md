# WP8: private immutable proposal and review-bundle adapter

Infrastructure candidate only. No model/provider calls, corpus processing,
ledger promotions, ports, timers, sends or publication. WP1 remains an external
dependency; this slice does not write its schema or stage API.

## Composition and portable gate

The adapter imports campaign_tool.records.gates.validate_findings directly.
Every assessment invokes its gate with check_files=True. No parallel substitute
for its author, source-byte, classification, date/applicability, privacy-artifact,
review-content or independent-role checks is implemented.

The portable gate retains its default tier B and three required roles. An explicit
tier A supports only CONFIRMED_DOCUMENTARY_FACT, REDACTION and
REDACTION_OR_EXPORT_LIMIT, with no rules; it requires factual and privacy review
by at least two distinct reviewers beyond the author. Tier B requires factual,
legal and privacy, also with at least two distinct independent reviewers.
Tier classification is a trusted workflow decision, not semantic proof that prose
contains no legal claim. The owner/review process must reject misclassified prose.
All existing default portable-gate callers keep their prior three-role policy.

## Trusted startup and authentication boundary

Instantiate review_bundle.Authority at trusted startup with an immutable-use
principal registry, opaque-session authenticator and private signing key of at
least 32 bytes. Registry entries bind stable ASCII identity, provider, model and
allowed roles. The authenticator, not submitted review JSON, resolves the actor.
Do not expose registry creation, signing, authenticator selection or raw principal
key selection as an untrusted endpoint. Never derive the principal from a receipt.

Signing keys are not stored in bundles or receipts. This module does not provision,
retrieve or print credentials. The deployment must supply and retain its own key
and authenticated sessions. HMAC envelopes protect stored identity attestations;
hash filenames alone are not authentication. Hostile installed Python or a
compromised signing key is outside this boundary.

Profiles default to test_only=True. Synthetic receipts may complete a synthetic
review but never count as production_review_complete or handoff_ready. Setting
test_only=False is reserved for trusted deployment with actual authenticated
reviewers; this library does not establish provider authentication on its own.
Provider/model metadata proves only the installed binding, not review quality.
Policy hashes include the registry, adapter version, test mode and portable gate
source bytes. Policy/key changes fail closed for old receipts; no silent adoption.

## Immutable artifacts and exact scope

create_bundle(root, public_content, proposal, authority=..., auth_context=...)
takes exact UTF-8 JSON public bytes with only title, claim and limitations.
Values are strings, with limitations a list of strings. Private keys, nested
objects and unknown fields are rejected. This is structural separation, not a
semantic privacy scanner: sensitive prose inside an allowed string still needs
independent privacy review.

The private proposal has explicit proposal_id/revision/tier/agency/classification,
next_action/event_date, primary_evidence, rules, counterevidence, missing_evidence,
exceptions_checked, bindings, coverage and supersedes fields. The signed private
manifest binds the exact public-content hash and all private substantive fields.
It never serializes private fields into the public-content file.

Bindings have explicit originals, units, digests, rules, sources and
counterevidence categories. Each entry has id, canonical absolute path, sha256 and
bytes. Assessment streams and verifies actual bound artifacts; absent or changed
bytes block. Original/unit/digest/source categories cannot be empty for completion.
Rules carry source_sha256 bound to the rule artifacts. Counterevidence is retained,
not suppressed, and its observations must cover its bound hashes.

Coverage entries bind original_sha256, unit_sha256, locator and scope. All declared
original/unit hashes must be covered. A scope is selected/full_text/full_visual/
all_rows as in the portable gate; selected evidence does not certify a whole
corpus or original. This adapter checks declared scope consistency, not whether
a reviewer actually read it or whether a locator resolves. WP1 unit provenance,
parser verification and canonical source adapters remain integration work.

The private root must already be owner-only, absolute, non-symlink and outside
Git repositories. Bundle directories are 0700 and files 0600. A root flock,
fsynced staged directory and rename expose only complete bundles. Replays compare
exact existing bytes. Public content remains inside the private bundle, not in a
deployable tree. No mutable accepted/current pointer exists in this slice.

## Receipts and owner decision

review_target supplies the exact finding digest, public-content hash, binding
hash, coverage list, original locators and public-artifact binding to inspect.
Its internal portable-gate finding targets privacy_status=cleared so the eventual
privacy receipt can bind that exact proposed state; this is NOT actual clearance.
The manifest itself never asserts a completed review.

record_review accepts that exact target plus role, verdict, rationale, reviewed_at
and reviewed_primary=True from an authenticated reviewer. All self-asserted
identity/provider/model fields are rejected. Unknown roles and author self-review
are rejected; the portable gate also enforces identity and independent reviewer
counts. Receipt coverage, checked locators and artifacts must match exactly.
Receipts are HMAC-authenticated, content-addressed and append-only. No replacement
or receipt deletion endpoint is supplied. Challenges remain blocking even if
other receipts pass.

assess_bundle revalidates immutable bytes, identity envelopes, scope and all gate
checks each time. Missing evidence is blocking even for otherwise valid factual
classifications. A changed public text, source/rule binding or private analysis
changes bundle/finding identity; old approvals cannot carry forward.

Owner approval is separate: record_owner_decision requires an authenticated owner,
exact bundle/finding/public hashes and exact review-set hash. An approval cannot
precede complete review. A later receipt makes the old owner decision stale.
Changed or challenged work is reissued as a new immutable revision, preserving
prior receipts; this slice deliberately has no silent challenge dismissal.
Owner approval defaults to awaiting_owner. publication_ready is always false,
stage_promotions is always zero, and even an owner-approved production bundle
only produces a handoff indicator for a separately authorized integration.

## Bounds and testing

JSON envelopes are at most 2 MiB, public content 128 KiB, registry 128 principals,
bindings 256 total items / 256 MiB declared total / 64 MiB each, and each receipt
directory 256 committed receipts. Larger artifacts require a future reviewed
streaming/bound policy; no full-corpus or large-original throughput claim is made.
Source files must remain owner-controlled and stable while assessed.

Synthetic tests cover independent role/author checks, trusted identity selection,
tier policy, exact coverage, changed original/unit/digest/rule/source and public
bytes, counterevidence, mixed private fields, immutable replay, interruption,
forged envelopes and owner approval invalidation. Run:
python3 -B -m unittest tests.records.test_review_bundle -v

Remaining: independent code/security review, production authenticated principal
and key lifecycle wiring, canonical WP1 adapters and source coverage validation,
actual substantive factual/legal/privacy reviews, release composition and CI.
Parent owns the single WP8 PR. This module grants no deployment or publication
authorization and performs no owner action on real records.
