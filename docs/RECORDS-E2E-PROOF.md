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

Exit 0 only when every stage passed and `manual_steps` is empty. `--out/<label>` must not exist yet.

A stage that reports ok or warn but left work undone is still a manual step, so the result is FAIL: `extract` with any
item left `AWAITING_OCR`, `EXTRACT_ERROR`, `EXTRACT_TIMEOUT` or `EXTRACT_NOT_ACCEPTED` (listed as `extract:<CODE>`), and
`outputs` ending `warn` with `OUTPUTS_*_UNAVAILABLE` or `OUTPUTS_DIGEST_UNRESOLVED` (listed as `outputs:<CODE>`).
`NO_NEXT_STEPS_FOR_THIS_MESSAGE` stays a plain warning.

## Stages (each timed)

| Stage | What it proves | Output |
| --- | --- | --- |
| arrival_trigger | an arrival stamp is seen and claimed as one batch | `runtime/arrival` |
| save_mail | message and attachments saved to the ledger | `project/processing-v1/ledger.sqlite` |
| extract | OCR/extract of every attachment | `project/processing-v1/extract` |
| digest_gates | digest gates run | none |
| outputs | inventory, classification, policy comparison, gap proposals, follow-up drafts, next steps | `project/run-outputs/<run>/outcomes/*.json`, `project/approvals` |
| next_steps | social post, website diff and follow-up probe are queued (warns, not fails, if the message has no gap) | `project/approvals` |
| telegram_text | summary text written to a file, never sent | `telegram-summary.txt`, `project/run-report.json` |
| console | Inbox, Documents, Findings, Deadlines, Drafts, Next steps, Run health render; a Deny POST writes the decision and sends nothing | `console/*.html` |

Reports: `proof-report.json` and `proof-report.md` (stage table, seconds per stage, output files, manual steps).

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
