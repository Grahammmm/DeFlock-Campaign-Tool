# Admin console Screen C: documents and findings (`records documents`)

Read-only, per agency. Nothing is fetched, sent or published. Every finding is labelled "Review required, not
publishable until G approves a public version."

```sh
records documents --project PROJECT --run-id RUN_ID [--agency ID] [--now ISO] [--json | --html NEW_FILE]
```

The same page is served by `records panel` at `GET /documents[?agency=ID]` (loopback, host-checked).

Sources, all under `PROJECT/run-outputs/RUN_ID/`; each is read, bound-checked and run-id checked on its own, and a
missing, stale, symlinked or wrong-schema source is named as unavailable rather than shown as empty:

| Section | Source |
| --- | --- |
| received items: date, pages, text state, asked items answered | `outcomes/received-items.json`, `text-state.json`, ledger page count (fails soft to "not recorded"), `outcomes/response-items.json` |
| release links (host, first seen, expiry, status; never fetched) | `outcomes/release-links.json` |
| asked items with tier and disposition, tier 1 (audit and search/use logs) first | `outcomes/response-items.json` through `item_tiers` and the playbook |
| findings and gap proposals (quote, page, policy section, confidence, review state), high confidence first | `outcomes/gap-proposals.json` |

Agencies, requests and the playbook are data from those files. Material not linked to any agency is shown in its own
section.
