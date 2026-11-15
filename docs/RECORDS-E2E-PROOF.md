# Records end-to-end proof (`records e2e-proof`)

`campaign_tool records e2e-proof` replays ONE stored incoming message (an `.eml`, attachments inside) through every
stage in a fresh private sandbox and writes a proof report. It never fetches mail, sends, posts or publishes, reads no
credential or env file, and never opens the live project, mailbox or ledger for writing.

```
campaign_tool records e2e-proof --message <file.eml> --campaign-root <campaign> --out <private-dir> [--label replay-1] [--as-of YYYY-MM-DD]
campaign_tool records e2e-proof --mail-store <dir of .eml> --nth 1 ...      # newest message; --nth 2 = next
```

`--ocr` runs extract with local OCR (needs tesseract and poppler): stage timeout 600 s, page timeout 120 s, document
budget 300 s, so one scanned page is not cut by the stage or document timeout. The report records `ocr: true`.
`--with-adoption` also rehearses the real delta and adoption stages on a synthetic export (see below).

Exit 0 only when every stage passed and `manual_steps` is empty. `--out/<label>` must not exist yet.

A stage that reports ok or warn but left work undone is still a manual step, so the result is FAIL: `extract` with any
item left `AWAITING_OCR`, `EXTRACT_ERROR`, `EXTRACT_TIMEOUT`, `EXTRACT_NOT_ACCEPTED`, `PARSER_MISSING`,
`DECODER_NEEDED` or `WORKBOOK_POLICY_REQUIRED` (listed as `extract:<CODE>`; the last three mean this host lacks a
parser, decoder or workbook policy), and `outputs` ending `warn` with `OUTPUTS_*_UNAVAILABLE` or
`OUTPUTS_DIGEST_UNRESOLVED` (listed as `outputs:<CODE>`). `NO_NEXT_STEPS_FOR_THIS_MESSAGE` stays a plain warning.

## Unread originals (`unread`)

Both reports list every replayed original whose extract outcome is not `EXTRACTED`: `proof-report.json` has an
`unread` array (always present, `[]` when everything was read) and `proof-report.md` an `Unread originals` section
(`- none` when empty). Each row is exactly `{sha256_prefix, kind, outcome}`: the first 12 hex characters of the
original's sha256, its native form (`unknown` when none), and the native extract code. The rows come from this run's
own `processing-v1/runs/extract-<run>.json` (`records-extract-outcomes-v1`), one row per original (including the
`.eml` itself when it is unread), sorted by full hash; two originals sharing a prefix both appear. No file name, path,
subject, diagnostic or text is ever included.

- Tool gaps (`PARSER_MISSING`, `DECODER_NEEDED`, `WORKBOOK_POLICY_REQUIRED`, plus the older four codes) fail the
  proof with the manual step `extract:<CODE>`.
- Agency-side gaps (`CORRUPT_DOCUMENT`, `EXTRACT_INCOMPLETE`) are listed and marked `agency-side` in the Markdown, but
  do not fail the proof alone. PASS can therefore coexist with unread rows and is not a claim that every supplied byte
  was read. Other outcomes (for example `PRESERVE_PENDING`, `DEFERRED_RUN_BUDGET`) are listed too without a manual step.
- If extract finished (ok or warn) but its list is missing, unreadable or malformed, the proof fails with the manual
  step `extract:PROOF_EXTRACT_LIST_UNAVAILABLE` rather than reporting an empty inventory.
- If extract itself failed or did not run, its own failure stays the manual reason and `unread` is `[]`: that means
  coverage is unknown, not that everything was read.

## Synthetic seed (`--seed FILE`)

`--seed FILE` (`records-e2e-seed-v1`, at most 64 KiB) is synthetic sandbox only; it refuses any other project. A new
`seed` stage runs after `save_mail`: in the replay sandbox ledger it inserts the listed agencies and requests and one
typed join (`join_type e2e_proof_seed`) per replayed original, and writes the optional `fact_sheets` object to
`<sandbox project>/agency-fact-sheets.json` where the comparison reads it. A bad seed (`SEED_INVALID`), a non-sandbox
project (`SEED_NOT_SANDBOX`) or a ledger without exactly one message fails the stage before anything is written.
In production canonical identity still comes only from the pipeline. Without `--seed` there is no `seed` stage.

## Adoption rehearsal (`--with-adoption`)

By default the replay starts after the live mailbox-side stages (sync, delta, adoption, portal), so the real adoption
path is never exercised. `--with-adoption` (sandbox only, as `--seed`; there is no config, account, mailbox or receipt
argument) rehearses the two stages that need no mailbox:

1. `adoption_setup` builds, under the replay's own `project/`, a synthetic mail-history export of the replayed
   message (`mail-history/objects/`, `mail-history/receipts/`: the exact EML bytes, its decoded attachments and their
   sha256, sizes and MIME part descriptors from the real `mail_delta` classification) and an owner-only
   `mail-history-adoption-v1` config (`.e2e-adoption-config.json`: enabled, no marker pin, because the fresh sandbox
   has no disable marker), and initialises the empty canonical ledger. The export identity is fixed and fictional
   (account `owner@adoption.example.invalid`, folder `Sandbox.example.invalid/INBOX`, uidvalidity 1, uid 1); it is never
   taken from the message headers, a live mailbox, a setting or the environment. The config schema has no account or
   folder key, so those live only in the synthetic receipt. Everything is 0600 files in 0700 directories.
2. `delta` then `adoption` run the real adapters unchanged (`--require-sandbox`; adoption with the generated config
   and its normal pre-adoption backup). Their real status, code, counts and seconds appear in the stage table and in
   the run report in place of the two `REPLAY_STARTS_AFTER_MAILBOX_SYNC` skips; `sync` and `portal` stay skipped.
3. `save_mail` becomes a checkpoint: it does not run `ingest_inbox`, it checks that the ledger now holds exactly the
   replayed message (else `SAVE_MAIL_NOT_ADOPTED`, for example when the real adopter set it aside as a coded gap) and
   opens the empty portal queue. A `seed`, if given, then runs against the genuinely adopted originals.

Stage order: `arrival_trigger, adoption_setup, delta, adoption, save_mail, [seed], extract, ...`. The report gains
`"with_adoption": true` and a Markdown note that sync, portal transport, live credentials and owner approval were NOT
rehearsed. A failing setup, delta or adoption leaves every later stage `not_run` and the proof FAIL with that stage's own
code as the manual step (a malformed generated config gives `ADOPTION_REFUSED`; `ADOPTION_BLOCKED` and the rest pass
through unchanged). Without the flag nothing changes: no new stage, key, file or report byte.

Refusals: the rehearsal writes only if the project is the replay's own `project` directory (not a symlink) holding an
owner-only regular `.t12-sandbox-v1` marker with the exact text `e2e-proof sandbox` (otherwise `ADOPTION_NOT_SANDBOX`,
checked again before each adapter, before anything is written). Existing generated files are reused only when
byte-identical; a different, linked or non-owner-only file or directory, or an existing adoption disable marker, is
refused (`ADOPTION_SETUP_REFUSED`) and never modified, repaired or pinned.

Idempotence is the adapters' own: running delta and adoption again on the same sandbox with a new run id reports
output 0 and skipped 1 and leaves one canonical message. The same `--label` still fails `OUT_EXISTS`; two fresh labels
each adopt their own message.

Limits: synthetic and local only. It does not rehearse mailbox sync or any exporter, portal transport, credentials,
owner approval or the wrapper deployment, reads no live config, and does not make a real message adoptable.

## Stages (each timed)

| Stage | What it proves | Output |
| --- | --- | --- |
| arrival_trigger | an arrival stamp is seen and claimed as one batch | `runtime/arrival` |
| adoption_setup (`--with-adoption`) | synthetic export and owner config written in the sandbox | `project/mail-history/objects`, `project/mail-history/receipts`, `project/.e2e-adoption-config.json` |
| delta (`--with-adoption`) | the real delta adapter classifies the synthetic receipt | none |
| adoption (`--with-adoption`) | the real adoption adapter adopts it once | `project/processing-v1/ledger.sqlite` |
| save_mail | message and attachments saved to the ledger (with `--with-adoption`: verified as adopted) | `project/processing-v1/ledger.sqlite` |
| extract | OCR/extract of every attachment | `project/processing-v1/extract` |
| digest_gates | digest gates run | none |
| outputs | inventory, classification, policy comparison, gap proposals, follow-up drafts, next steps | `project/run-outputs/<run>/outcomes/*.json`, `project/approvals` |
| next_steps | social post, website diff and follow-up probe are queued (warns, not fails, if the message has no gap) | `project/approvals` |
| telegram_text | summary text written to a file, never sent | `telegram-summary.txt`, `project/run-report.json` |
| console | Inbox, Documents, Findings, Deadlines, Drafts, Next steps, Run health render; a Deny POST writes the decision and sends nothing | `console/*.html` |

Reports: `proof-report.json` and `proof-report.md` (stage table, seconds per stage, output files, manual steps, unread originals).

## Console screens

The console (`records panel`, localhost or Tailscale only) shows, for the replayed message: Inbox: mail received;
Documents (received items, release links, asked items, findings and gap proposals); Stage status and run health;
Findings; Deadlines (dates stated in received mail); Drafts; and Next steps (draft social post, website update
diff, follow-up probe), each with Approve and Deny. Approve and Deny only write the decision file.

## Fixes found by the replay

- `outputs` never queued next steps; they came only from a separate command. The stage now queues them from
  `--campaign-root` (or the `<project>/.campaign-root` pointer).
- The replay sandbox needs an empty portal queue for the outputs snapshot (the live portal stage creates it).

## Live status

On AgentOrange the mail store and ledger are owned by the container user, so a live replay is run as
`docker exec -u coder agentorange-flock` with this engine on the path. The arrival units
(`records-arrival.path/.service`) are not installed on AgentOrange yet; that install is operator-owned.
Screenshots of live pages contain private record text and are kept out of the public repo.
