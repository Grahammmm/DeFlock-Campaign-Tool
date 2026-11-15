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

## Second tier: advisory wording warnings (DF2007-15)

Two further wordings fail a correction (RESEARCH-CAMPAIGNS-1 section 6): an agency's **intent** stated as fact, and a camera
**count with no date**. They are not legal conclusions, so they are **not** in `legal_claims` and never join its rules; they are a
separate, advice-only tier beside it. The tier never refuses: it adds a `warning:` line, never an error, and no build, export, draft,
send, approval state or exit code changes because of it. A warning is not permission to publish or send either: the human gate decides.
Nothing here checks whether a claim is true, rewrites copy or proves a draft is safe.

**API.** `campaign_tool/legal_claims.py`: `legal_advisories(text)` returns the sorted, unique categories (`[]` for empty or non-string
input); `advisory_notes(items)` returns warning lines for `(where, text)` items, in item order then sorted category. Both are re-exported
beside, not through, `legal_claims` by `tools/flock_privacy.py`. `legal_notes(items)` returns its old `review-required` notes first, in
their old order, then the advisory warnings; text with no advisory hit gets exactly the old list (including its `guard-error` behaviour).

**Output** (the place and category only, never the text, a number, a name or an exception message):

| Category | Warning line |
|---|---|
| `intent` | `warning: <where>: wording advisory (intent). Fix: Describe what the record says; do not infer intent.` |
| `undated-count` | `warning: <where>: wording advisory (undated-count). Fix: Add a date or describe the count as documented or mapped.` |
| scan fault | `warning: wording advisory (guard-error). Fix: Review the wording manually.` (a fault never discards the old legal notes and never reads as a clean result) |

**`intent`.** The whole words or phrases `secretly`, `covertly`, `hid`, `hiding`, `covered up`, `in secret` (any case, any ordinary
whitespace between phrase words), said of an agency or official. This is an English heuristic, not named-entity recognition and not a
claim that intent is unproven in fact. The subject anchor is one of agency/agencies, department, police, sheriff, city, county, council,
mayor, chief, official(s), officer(s) or staff (so named and possessive forms such as `Sheriff Smith's office` count), found earlier in the
same comma segment (so `The agency collected plates and secretly shared them` warns), or a passive `by <anchor>` later in it (`was
covered up by city officials`). A camera that hid a plate, a resident hiding a bicycle and a bare word do not warn, and no pronoun is
followed across a sentence. No synonyms (`concealed`, `quietly`) are matched.
- Negation (`not`, `never`, `no`, `nor`, `n't`, but not `not only`) within three words before the phrase suppresses that match; a denial
  elsewhere in the sentence does not hide a later allegation.
- `if`, `whether`, `unless`, a question, or an attribution (`alleges`, `according to`, `claims that`) suppresses only the clause it governs,
  as in the legal guard; a later joined claim (`..., and the sheriff hid the readers`) is a new statement and still warns.
- `The agency can share data` has no intent wording and passes. `The agency can secretly share data` warns: a capability hedge does not
  substantiate secrecy.

**`undated-count`.** A number (digits, comma-grouped thousands or a decimal) immediately followed by whitespace and the whole plural
word `cameras`, `readers` or `ALPRs` (any case) with none of the following in the **same sentence**: `as of`, `documented`, `mapped`
(whole words) or a date. One warning per place and category however many counts match. Not covered, deliberately: word numbers
(`forty cameras`), ranges (`40-60 cameras`), singular nouns, `license plate readers`. A negated count (`there are not 40 cameras`)
passes. A cue or date in another sentence qualifies nothing. A capability hedge does not qualify a count (`The agency can share data
from 40 cameras` warns), and `mapped 40 cameras` is silent: the rule needs a cue, not proof that the number is true.
- A date is one of: a real calendar ISO `YYYY-MM-DD`, `M/D/YYYY`, `Month D, YYYY` (comma optional, `Jan`..`Dec` or full names), `Month
  YYYY`, or a bare year 1900-2099. An invalid calendar date (`2026-02-30`, `2/29/2026`) does not qualify. The number that is itself the
  count never counts as a year (`2026 cameras` warns). The system clock is never read and freshness is never checked. This is a finite
  list, not a date parser. An abbreviation period ends a sentence under the guard's sentence contract, so `Mar. 5, 2026` after the
  count is a different sentence: write `Mar 5, 2026`.

**Quotations.** Complete spans in `"..."`, `“...”`, `‘...’` and `'...'` (apostrophes inside words are not quote marks) are blanked
before matching, on one line, with their sentence-ending `.`, `!` or `?` kept. Quoted record wording never warns, and a quoted cue or date
never qualifies an unquoted claim. The legal guard keeps its own, narrower quote matcher, so this changes none of its results.

**Where it shows.** Every surface that already reports `legal_notes` carries the warnings in the same place as its other notes: the
report, page, publish and publish-stage lints (`review_notes`), the newsletter, site, social, press, kit, FAQ, elsewhere, statement
and comment-ask notes (`review_flags` or stderr), and `content-check` (stderr, outside its error count). The surfaces that format their
own guard lines add the warnings alongside their old lines and change nothing else:

| Surface | Where the warnings show |
|---|---|
| `tools/flock_deadlines.py` | stderr, after the old `review-required: generated text N` lines, same `generated text N` labels |
| `tools/collection_triage.py` | `triage-notes.json` notes, after the old notes, same `sentence:N` / `member:ID` labels |
| `tools/rehearse_agency.py` | `REHEARSAL.md` and `rehearsal.json` `wording_warnings` (key present only when a warning fired), labels `finding N`; stages, verdict, withholding and exit code are unchanged |
| `tools/custody_report.py` | stdout after the `custody | ...` line, label `custody page`; the generated files, verdict and return code are unchanged |

Tests: `tests/test_legal_claim_advisories.py` (patterns, quotes, negation, capability wording, dates, API and fault contract, the
unchanged legal corpus) and `tests/flock/test_legal_advisory_surfaces.py` (every reporting surface: advisory-only input keeps its
exit code, artifacts and state).

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

