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

Pass wording that only: states what a law requires (`SB 34 requires ...`, `it is unlawful to share ...`); states an
observed fact (`declined to answer`, `did not respond in 10 days`, `failed to comply with our request`); attributes the claim
to someone else (`alleges`, `according to`); asks it or makes it conditional (`whether`, `if`, `?`); negates it
(`did not break the law`, `did not act unlawfully`, `is not non-compliant`, `no violation`, `does not determine`, `not a claim`);
or sits inside quotation marks (a quoted statute).

**How the test is applied (round 2).** Not per sentence: a sentence is cut into clauses at commas, semicolons, colons and
joining words (`and`, `but`, `while`...), and each clause is tested alone, so a neutral clause cannot hide an assertion
next to it. `declined to say whether it kept logs, and it broke the law` flags (the second clause); `X violated SB 34,
even if the disclosure was accidental` flags (the assertion comes before the `if`). Cue scope:
- `whether / if / unless / alleges / according to / claims that` only neutralise what FOLLOWS them in their clause. An
  attribution also covers later comma segments of its sentence (`According to counsel, the program was in violation...`)
  but not a clause opened by a joining word.
- A negation (`not`, `never`, `no`, `n't`; but not `not only`) within three words before the matched wording negates that
  match, whatever the predicate, instead of a fixed list of negated verbs.
- Requirement and no-determination cues (`requires`, `prohibits`, `forbids`, `determined that`, `finding that`, `this section`) neutralise only what FOLLOWS them, like the cues above. A conditional or question that OPENS a comma segment (`If X broke the law and violated SB 34, ...`) covers the whole segment across and/but. Denials (`did not violate`) are handled by negation next to the matched wording, so unrelated denial words (`did not determine retention`) cannot hide an accusation. Only a trailing `?` neutralises a whole clause.

Known gaps (accepted for an alert-only lint): an assertion with none of those verb shapes (`a breach by X`) is missed;
`the complaint alleges X violated` passes though a reader may take it as ours; a negation more than three words before the
wording is not seen (so `not, as far as anyone can tell, ...` still flags, which is the safe direction).

## Generated surfaces and coverage

| Surface | Generator | Where the note shows | Reaches the human gate? | Covered |
|---|---|---|---|---|
| Flock report draft | `tools/flock_report.py build`/`check` | stdout, per `line N` | stdout of the author's run (no card) | yes |
| Flock region report draft | `tools/flock_report.py region` | stdout | same | yes (build; `--check` not) |
| Flock page story | `tools/flock_page.py build`/`lint`/`check` | stdout, `<area> <index>` | same | yes |
| Flock publish findings (PUBLISH.json) | `tools/flock_publish.py export`/`--check` | stdout; entry stays `pending` | the human approval of `pending` is the gate | yes |
| Newsletter draft | `campaign_tool/newsletter/draft.py` | `review_flags` in the draft (key present only when flagged, so clean drafts stay byte-identical); stderr from the CLI | **yes**: Worker `Approve & send` copies `review_flags` into the `send_newsletter` proposal; the draft screen and the approval card both show a "Wording review required" notice above the unchanged Approve button | yes |
| Site build, CLI | `campaign_tool/cli.py build` | stdout after the file list | stdout of the author's run | yes (finding content) |
| Site build, runner job | `runner/handlers/build_site.py` | `review_flags` in the job outputs and (manual path) in the `deploy_site` proposal; one `site.legal_claim_review` log line | **yes**: the deploy_site approval card shows the notice; for cards created before the build (`proposeRebuild`), the `/approvals` page reads the flags from the card's `build_job_id` job outputs | yes (finding content) |
| Campaign comment asks | `campaign_tool/meeting_stage.py` (`meetings-stage`, `site.json.comment_asks` and `--ask`), site build scan | `review_flags` in the `meetings-stage` JSON and the same notes on stderr, place `--ask[2]` or `site.json.comment_asks[2]`; site build notes list the site asks | author's run; advisory, the kit is still written | yes |
| Social post drafts | `campaign_tool/social.py` | stderr from the `social` command | author's run only. The Worker `post_social` executor is `not_implemented`, so no card exists to carry flags yet | yes at generation; **no card path to cover** |

Behaviour shared by every row: a flag is advisory. Exit codes, the build, the upload, the card state and the Approve button are
unchanged; the card text carries rule names and places, never the offending wording. Tests: `tests/runner/test_build_site*.py`
(runner flags, producer-owned envelope path, a scan fault is a flag), `workers/workspace/test/workflow-http.test.ts` (flags
shown on the real `/subscribers` and `/approvals` pages while the card stays proposed and approvable), and
`tests/flock/test_legal_claim_guard.py`.

## The older Flock word ban is now alert-only too (operator ruling 1, 2026-11-02)

`compare.GATED` (`violat*`, `illegal*`, `admit*` without a matching quote) used to **refuse** in the Flock lints. The operator
ruled under rule 29 that the card's alert-only requirement governs, with no exemption for that ban. It now adds a
`review-required: <place>: quote-gated wording` note, never the text, and the run carries on, in:
- `tools/flock_report.py` build, check, region and region `--check`;
- `tools/flock_page.py` lint, build and check;
- `tools/flock_publish.py` export, `--check` and stage (the finding is exported with `review.status` `pending`, so the human
  approval still decides);
- `tools/flock_compare.py --check` (row note, exit 0);
- `tools/flock_verify.py` `--check` and finish.

Refusals that are not legal-claim wording are unchanged: always-banned wording, vendor-gated wording, denylisted strings,
email/phone/plate patterns and analysis-only cites still fail closed. The Telegram receipt fed55a8d (a bare "approve") is not
the authority for this change; the operator ruling is.

For `Agency X violated SB 34.` (`ExplicitExampleTest`, driving the real commands): report build, page lint and publish export
each exit 0 with both the quote-gated note and this guard's `violation-verb` note, and the draft/export is written; the
newsletter draft carries it in `review_flags`.

## Retained, separately owned: DF2037-02 site review blocker (not changed here)

`campaign_tool/review.py` `LEGAL_TERMS` (`violat*`, `illegal*`, `unlawful*`, `broke the law`, `criminal*`), merged from card
DF2037-02 after this work began, makes those words in a site finding's `title`, `summary` or `body_md` a review blocker, so
`content.load_finding` refuses the finding and the site build and social drafts stop on it. Ruling 1 named `compare.GATED`
only, so DF-4 does not change DF2037-02. On those surfaces, for those words, the behaviour is still fail-closed; the DF-4
scan does not raise and gives the human one `review-required` note saying the content could not be scanned
(`DF2037RetainedTest`). Other wording (`out of compliance with`, `non-compliant`, `failed to comply with`, ...) gets the
normal advisory note there and the build continues. Whether DF2037-02 should also become alert-only is for its owner.

