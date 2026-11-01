# Legal-claim wording guard (DF-4, minimum version)

The README promises the tool "does not determine legal violations". This is the lint that checks OUR generated wording
against that promise. It never judges whether a violation happened.

**Alert-only.** A flag is a `review-required: <place>: legal-claim wording (<rules>)` note. It is not an error: no build,
export, draft, send or exit code changes, and the note names the place and rule, never the text. If the guard itself faults
it reports `guard-error` (a flag), not a crash. A false positive costs a human glance at the publish gate.

**Code.** Rules and boundary: `campaign_tool/legal_claims.py` (packaged, so the newsletter and site build can use it).
Exposed beside the email/phone/plate patterns as `tools/flock_privacy.py: legal_claims(text)`, `legal_notes(items)`.
Corpus: `tests/fixtures/flock/legal_claim_corpus.json`. Tests: `tests/flock/test_legal_claim_guard.py`.

## Where the boundary is

Flag a sentence that **asserts, or speculates about, a violation by a party**: `X violated`, `X is in violation of`,
`X broke the law`, `X failed to comply with <law>`, `X is non-compliant`, `X shared data unlawfully`, `it was illegal`,
and hedged versions (`may have violated`, `appears to be non-compliant`): a hedge about a named party is still an accusation.

Pass a sentence that only: states what a law requires (`SB 34 requires ...`, `it is unlawful to share ...`); states an
observed fact (`declined to answer`, `did not respond in 10 days`, `failed to comply with our request`); attributes the claim
to someone else (`alleges`, `according to`); asks it or makes it conditional (`whether`, `if`, `?`); negates it
(`did not violate`, `no violation`, `does not determine`, `not a claim`); or sits inside quotation marks (a quoted statute).
Test is per sentence, after quoted spans are blanked.

Known gaps (accepted for an alert-only lint): an assertion with none of those verb shapes (`a breach by X`) is missed;
`the complaint alleges X violated` passes though a reader may take it as ours; a sentence containing both a neutral cue
and an assertion passes as a whole.

## Generated surfaces and coverage

| Surface | Generator | Where the note shows | Covered |
|---|---|---|---|
| Flock report draft | `tools/flock_report.py build`/`check` | stdout before the summary, per `line N` | yes |
| Flock region report draft | `tools/flock_report.py region` | stdout before the summary | yes (build; `--check` not) |
| Flock page story (findings cards, dek, admissions, ...) | `tools/flock_page.py build`/`lint`/`check` | stdout, `<area> <index>` | yes |
| Flock publish findings (PUBLISH.json) | `tools/flock_publish.py export`/`--check` | stdout; entry stays `pending`, the human approval is the gate | yes |
| Newsletter draft | `campaign_tool/newsletter/draft.py` | `review_flags` in the draft JSON; stderr from the CLI | yes |
| Site build (finding pages and list) | `campaign_tool/cli.py build` | stdout after the file list | yes (finding content) |
| Social post drafts | `campaign_tool/social.py` | stderr from `social` command | yes |
| Workspace Worker (TypeScript): publish/approval cards, `post_social` | `workers/workspace` | not wired | **no**: `post_social` is `not_implemented`; the send_newsletter card does not yet display `review_flags` |

## Not changed (and why)

`compare.GATED` (`violat*`, `illegal*`, `admit*`) is a separate, older word ban that already **fails closed** in the report,
page and publish lints (and in `flock_compare`). It was left alone: weakening it is not this card's job. The consequence is
that a sentence using those exact words is refused by the older check before this guard's note shows; this guard adds the
other phrasings (`non-compliant`, `unlawfully`, `broke the law`, `failed to comply with`, ...) as notes.
