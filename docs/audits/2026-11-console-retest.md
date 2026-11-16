# DeFlock draft-console retest — 2026-11-16

## Verdict and boundary

**Audit executed; NOT an interactive-console acceptance or release approval.**
The admitted export-root/no-decisions configuration is deliberately view-only.
It preserves the full draft/context and refuses writes, but cannot demonstrate a
successful Save, a two-tab stale-save **409**, an unsaved-text-specific approval
refusal, or CONDRAFT-05's approved-draft Revise journey. Those checks are **FAIL
(not run to their required acceptance boundary)** below, not product regressions
inferred from a read-only guard. Workers-only checks and unavailable browser
engines remain explicitly unverified. Do not use this report as an all-clear.

- Tested source: `fa4114d4b3d6a988ddf6bc04a431cea113c1641e`, CONDRAFT-05
  PR #1676; report branch `stack/flk-aud-2`. The card's STACK-1 instruction
  supersedes the original spec's main-based checkout. No other branch was merged.
- Audit began `2026-11-16T19:07:45Z`. The only committed change is this report;
  no production code, test, Worker, runner, service or live export was changed.
- Synthetic input: existing `tests/records/test_console_home.py::fixture`, using
  `test_console_board.py::build`, plus two invented queue rows (mail and review
  copy). Run `run_1`; installed export `20261116T190745Z-run_1`. No real records,
  inbox, mail store, credential, source download or private S2 evidence was used.
- `console_export.build/write_tar/install` produced a sealed export. A separate
  degraded export omitted synthetic `received-items.json` and refreshed its
  manifest before installation. No installed export was edited.
- All servers bound to `127.0.0.1` with an OS-assigned free port, `system=False`,
  no send/mail-drafts config, no decisions directory, no manual-events writer.
  One server also advertised the synthetic public origin `https://admin.example.test`;
  this was only a Host/claims test, not a deployed host or tunnel.
- `approval.anchor_state` was injected as `unknown`; real trust reads,
  verification, send and mailbox-placement functions were trapped. No key was
  generated and no approval was performed. This tests the unknown-banner display,
  not the installed anchor's state or cryptographic/Access integration.
- Chrome `156.0.8078.79`, sandbox enabled, fresh Playwright context, 36 GET page
  loads: nine paths at widths 1280, 640, 390 and 360, height 844. Browser routes
  admitted only GET/HEAD to that exact loopback origin; zero forbidden requests
  were attempted. Screenshots/DOM evidence are synthetic; not real-phone evidence.

## Evidence key and commands

Evidence names below are members of the task attachment `flk-aud-2-evidence.zip`.
The task-local scripts are **not** repository changes. `$W` means the isolated
card workspace, `$PY` the existing provisioned records-test interpreter, and
`$CAP` the installed 8 GiB memcap wrapper. Host-home prefixes are normalized here;
raw script paths remain in the local attachment. Commands ran with cwd `$W/repo`.
Use a fresh task workspace for replay: the scripts intentionally do not overwrite
an earlier run's fixture directories.

| Key | Executed command | Output / evidence |
|---|---|---|
| H | `$CAP -- $PY $W/audit.py` | exit 0; `evidence/results.json`, `requests.json`, named HTML files, `browser.json`; all POSTs refused, final source/export tree and mtimes unchanged, server closed |
| D | `$CAP -- $PY $W/supplement.py` | exit 0; `evidence/supplement.json`, degraded HTML; missing received source reports unknown; degraded listener refused a new connection after shutdown |
| P | `$CAP -- $PY -B -m unittest tests.records.test_console_phone -v` | exit 0; `Ran 6 tests in 0.012s`, `OK` |
| R | `$CAP -- $PY $W/render_checks.py` | exit 0; `PASS CalendarSummaryTests.test_drafting_off_agencies_are_one_summary_line_plus_details`; `PASS StatusLineRowTests.test_two_status_lines_show_template_and_date` |

R invokes those two unchanged render-only assertion methods without the inherited
HTTP fixture setup; it is not a full unittest run or a hosted journey. P uses only
synthetic temporary trees and performs no HTTP POST. Full
`test_console_access`/`test_console_review` suites were **not run as audit
acceptance**, because they include successful approval/rejection and configured
mail/send cases outside this audit's allowed POST set. Repository push-gate
checks, if run, are separate software-fixture validation, not audit journey proof.

Two initial probe assumptions were wrong and are preserved in `results.json`:
`GET-manual` expected 503 but observed **400** for an incomplete URL; `anchor`
expected a literal live-source phrase but observed the correct pinned-export
unknown/deferred text. The final verdict uses actual response content, not those
initial boolean labels. Likewise H's PASS labels on refused edits mean **guard
safety only**, not successful editor, revision or CSRF-specific acceptance.

## Draft flow, anchor and guards

| id | check | PASS/FAIL | evidence (command + status code / excerpt) |
|---|---|---|---|
| DF-01 | Full body, including Unicode and tail | PASS | H GET `/review?id=<synthetic-mail-id>` → 200; HTML-unescaped textarea equals all 17,051 UTF-8 bytes; SHA-256 `75a006d838aad74bfec0a1f0e34726ff79f449b66f4d4450d03227f5491c9d2b`; `FULL-BODY-TAIL` retained; textarea is readonly. |
| DF-02 | One draft at a time and leave/next navigation | PASS | H review → 200, exactly one textarea, zero forms; `Leave pending`, `Next`, `Queue index` present; other draft bodies not loaded into this editor. Display/navigation only. |
| DF-03 | Recipient, target and citations | PASS | H mail/target review GETs → 200; mail says `Recipient: Unknown`, `Not send-ready` (no thread was seeded); review copy shows `synthetic-site` and `/synthetic-only`; cited page 2, synthetic document digest, escaped `<Synthetic quote>&`, `synthetic, unverified` and limitation visible. No known-recipient resolution claim. |
| DF-04 | Save and reload changed body | FAIL | **Not run successfully:** H `/approvals/edit` with synthetic changed body → 503, `This console is view-only ... Nothing was saved or decided.` No Save control; no data changed. A successful save is unavailable without changing the prohibited decisions-dir boundary. |
| DF-05 | Stale-tab 409, including duplicate retry | FAIL | **Not run to revision check:** H stale and duplicate stale edit POSTs, revision `999` → 503, not 409; export guard runs first. No two successful editable tabs exist in this configuration. State/mtimes unchanged after both requests. |
| DF-06 | Approve refused specifically for unsaved text | FAIL | **Specific check not reached:** H changed-body approve POST with invalid synthetic signature → 503 view-only, no file/directory mtime changed. General refusal is safe, but neither `approve_unsaved_text` nor its expected 409 was demonstrated. |
| DF-07 | Cancel / leave without a decision | PASS | H GET review then GET `/` → 200; original export tree/content/modes/mtimes unchanged; zero approve/reject/save successes. |
| DF-08 | Approved-draft Revise; clears approval; recovery notice | FAIL | **Not run:** no approved row was created; no approvals permitted by this unit. Read-only mode also withholds Revise. Source `console_review.py:250-267` conditions it on an editable live/journal surface. Neither Revise nor verification-unavailable recovery is accepted from source inspection alone. |
| AN-01 | Unknown anchor banner and snapshot caveat | PASS | H Home and Settings → 200; `id="approval-anchor"`, `unknown — approvals are recorded but DEFERRED until the trust anchor is available.` Review says status is `as of export; not proof of current trust or delivery.` Unknown value was injected; other anchor states not run. |
| GU-01 | Wrong Host | PASS | H GET `/`, Host `evil.example` → 403; `wrong-host.html`; no state change. |
| GU-02 | POST without Origin / wrong Origin | PASS | H edit with Origin omitted and with `https://evil.example` → 403 each; unchanged export tree/mtimes. |
| GU-03 | POST without CSRF / expired token is refused | PASS | H missing/expired synthetic CSRF → 503 view-only, no write. This proves refusal in the admitted mode **only**; CSRF validation itself is not reached because the export guard precedes body parsing. |
| GU-04 | Send absent | PASS | H GET `/send` → 404 with no send config. POST `/send` not attempted: its generic export refusal adds no send-route evidence. No mail transport called. |
| GU-05 | Public Host without Access claims | PASS | H GET `/`, Host `admin.example.test`, no claims → 403, with all network traffic still loopback. Authenticated public-host/JWKS acceptance not run. |

### Complete audit POST receipt

All eleven requests below went only to the audit-created loopback instance.
For each, H checked the sealed export tree immediately before/after, including
file hashes, directory/file mtimes, sizes, modes and symlink targets: unchanged.
The synthetic source tree was also unchanged across the complete session.

| POST label | route | status | reached boundary |
|---|---|---|---|
| save | `/approvals/edit` | 503 | read-only export, before writer |
| stale-save | `/approvals/edit` | 503 | read-only export, before revision check |
| duplicate-stale-save | `/approvals/edit` | 503 | read-only export |
| unsaved-approve | `/approvals/approve` | 503 | read-only export, before signature verification |
| missing-csrf | `/approvals/edit` | 503 | read-only export, before CSRF parsing |
| expired-csrf | `/approvals/edit` | 503 | read-only export, before CSRF parsing |
| missing-origin | `/approvals/edit` | 403 | Origin guard |
| wrong-origin | `/approvals/edit` | 403 | Origin guard |
| empty-reason | `/approvals/reject` | 503 | read-only export, before reason validation |
| blank-reason | `/approvals/reject` | 503 | read-only export, before reason validation |
| empty-body | `/approvals/edit` | 503 | read-only export, before body validation |

## S2 findings, all sixteen

Titles below are one-line descriptions from the original S2 executive table;
no real-snapshot evidence paths or identities are copied. **FAIL — not run** means
verification remains open, not that a defect has been reproduced. PASS is bounded
to the explicitly named synthetic check, not a blanket accessibility or UX signoff.

| id | check | PASS/FAIL | evidence (command + status code / excerpt) |
|---|---|---|---|
| S2-01 | Unavailable inputs read as all-clear | PASS | D Home/Board/Calendar → 200. Missing received input: `Some information could not be loaded ... received documents`, `waiting on agencies: unknown`; `false_all_clear: false`. P `test_nothing_loaded_means_unknown_everywhere` passes. The historical real snapshot was not opened. |
| S2-02 | Internal source codes instead of plain explanations | PASS | D Board → 200: `received documents: unavailable. This file has not been created yet; run the records step that writes it.` H Documents explains unavailable findings/text status. P plain-name/sentence assertions pass. Unknown codes can still be named with an explanatory sentence. |
| S2-03 | Bare errors and lost input on refused draft submits | FAIL | **Not run to field validation:** H empty/whitespace reason, empty body, missing CSRF all → 503 view-only with shared layout, not an editable form. Export guard prevents proving inline field errors or typed-text preservation. No write. |
| S2-04 | Workers tables overflow small screens | FAIL | **Not run:** Workers is a different console; workers/ is frozen and no Workers instance, real copy or service was opened. Python measurements do not verify Workers tables. |
| S2-05 | Workers scrolling pre blocks not keyboard reachable | FAIL | **Not run:** no Workers request/finding detail fixture was served; Python keyboard results do not establish Workers scroll-region focusability. |
| S2-06 | Workers phone navigation targets too small | FAIL | **Not run:** no Workers instance. The Python nav measurements below are not substituted for Workers evidence. |
| S2-07 | Python dashboard horizontal overflow | FAIL | Chromium subset passes: H `/dashboard` → 200; document width equals viewport at 1280/640/390/360, phone controls all at least 44×44. **Firefox/WebKit not run**: installed Playwright engines missing, no download authorized. Original finding included these engines and editable forms; read-only Chromium alone cannot close it. |
| S2-08 | Banner/error pages lack landmarks | PASS | H browser DOM: all 36 states have one main, banner inside header and skip link first; includes 404 `/nope` and 400 incomplete `/manual-event`. Saved refused POST 503 uses the shared shell. No axe run; a manual-event 503 state was not reproduced (the bare URL returns 400). |
| S2-09 | No Python skip link | PASS | H keyboard on Home at all four widths: first Tab = `Skip to content`; Enter focuses `#content`. All 36 states contain the first-body skip link. Not a full keyboard tour. |
| S2-10 | Python phone touch targets too small | PASS | H DOM at 640/390/360 across nine pages: nav targets meet 44×44; dashboard's visible controls also meet 44×44. CSS/model checks P pass. No editable Approve/Reject controls or physical phones assessed. |
| S2-11 | Identical public-status decision labels | PASS | P identical-status test passes: `(L1, 2026-11-04) (item 1 of 2)` / `(item 2 of 2)`. R distinct-template/date test passes. HTTP export's public-updates switch is unavailable, so those rows are not claimed observed there. |
| S2-12 | Unknown agency returns misleading 200 | PASS | H GET `/documents?agency=nope` → 404, `Unknown agency`; unfiltered `/documents` → 200. |
| S2-13 | Repeated drafting-off notes crowd Calendar | PASS | R actual renderer given three synthetic drafting-off notes: one `Follow-up drafting is off for 3 agencies` summary, names in details, no repeated per-agency phrase. This is render-only evidence; H calendar lacks a follow-up register and therefore does not exercise that repeated-notes state over HTTP. |
| S2-14 | Settings explains status only through CLI commands | PASS | H `/settings` → 200: `Nothing can be sent from this console: sending is not set up.` Owner-only CLI guidance remains underneath; it was not executed. |
| S2-15 | Workers bare unknown-path error | FAIL | **Not run:** no Workers instance; Python `/nope` → 404 shared layout is not evidence for this different route handler. |
| S2-16 | Workers empty Signed-link table header | FAIL | **Not run:** no Workers records table or axe run. No inference from Python headers. |

## Findings and disposition

1. **Configuration/acceptance mismatch:** do not claim draft-edit readiness from an
   export-root console without a decisions directory. A separately admitted
   synthetic editable/journal audit is needed to close DF-04/05/06/08 and S2-03;
   this report does not authorize that setup or any live apply.
2. **Coverage open:** five Workers findings and Firefox/WebKit coverage remain
   not run. No package/browser installation, Worker changes or extra service
   startup was done to hide these gaps.
3. **Trust/semantic limits:** no real anchor state, known mail-recipient resolution,
   approved/rejected terminal state, legal correctness, citation authenticity,
   redaction/publication readiness or authenticated hosted journey was accepted.
   No Lighthouse or axe score is claimed. One synthetic 390px screenshot was
   visually inspected: no obvious overlap/horizontal clipping; long text wraps.
4. Audit servers were shut down; no persistent console was installed. Nothing
   was sent, published, deployed or approved. Sends/publication remain G-approved.
   Report completion only supplies evidence for the later release/review owner;
   no PR or merge is opened by this stacked unit.
