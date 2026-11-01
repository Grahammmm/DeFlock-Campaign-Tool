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
- Denials and statute text (`did not violate`, `no violation`, `this section`, a trailing `?`) neutralise their whole clause.

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
| Newsletter draft | `campaign_tool/newsletter/draft.py` | `review_flags` in the draft; stderr from the CLI | **yes**: Worker `Approve & send` copies `review_flags` into the `send_newsletter` proposal; the draft screen and the approval card both show a "Wording review required" notice above the unchanged Approve button | yes |
| Site build, CLI | `campaign_tool/cli.py build` | stdout after the file list | stdout of the author's run | yes (finding content) |
| Site build, runner job | `runner/handlers/build_site.py` | `review_flags` in the job outputs and (manual path) in the `deploy_site` proposal; one `site.legal_claim_review` log line | **yes**: the deploy_site approval card shows the notice; for cards created before the build (`proposeRebuild`), the `/approvals` page reads the flags from the card's `build_job_id` job outputs | yes (finding content) |
| Social post drafts | `campaign_tool/social.py` | stderr from the `social` command | author's run only. The Worker `post_social` executor is `not_implemented`, so no card exists to carry flags yet | yes at generation; **no card path to cover** |

Behaviour shared by every row: a flag is advisory. Exit codes, the build, the upload, the card state and the Approve button are
unchanged; the card text carries rule names and places, never the offending wording. Tests: `tests/runner/test_build_site*.py`
(runner flags, producer-owned envelope path, a scan fault is a flag), `workers/workspace/test/workflow-http.test.ts` (flags
shown on the real `/subscribers` and `/approvals` pages while the card stays proposed and approvable), and
`tests/flock/test_legal_claim_guard.py`.

## Existing fail-closed word ban (needs a scoped owner disposition)

`compare.GATED` (`violat*`, `illegal*`, `admit*`) is a separate, older word ban that **fails closed** in the Flock report,
page and publish lints (and in `flock_compare`). This card asked for alert-only, so the two conflict for those exact words.
It was left alone: weakening it is not this card's call and has not been dispositioned by the task/admission owner.

Contract as it stands today, pinned by `ExplicitExampleTest`, for `Agency X violated SB 34.`:
- Flock report, page, publish: **refused** by the older ban (not new behaviour); this guard's note never shows for those words.
- Newsletter, site build (CLI and runner), social: **noted** by this guard (new, advisory), reaching the approval cards above.
- Other phrasings (`non-compliant`, `unlawfully`, `broke the law`, `failed to comply with`): **noted** on every surface.

OPEN: whether the owner wants the Flock lints' hard refusal kept (my recommendation: keep; it is stricter than the card
requires and only adds a human glance) or converted to a note. Full "alert-only everywhere" acceptance depends on that answer.
