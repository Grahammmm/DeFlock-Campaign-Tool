# Records console: Project oversight and Public updates (REC-CON-3)

Private pages served by `records panel` (stdlib, loopback only). Nothing is sent, fetched, approved or published.

- `GET /oversight` — Screen E.
  - *Now:* runs finished in the last 24 h (status, items in, items processed from the extract stage) and arrivals waiting. Needs `--run-report <runtime>/<target>/run-report.json`; the `reports/` folder beside it supplies the earlier runs.
  - *Stuck:* the REC-HEALTH-1 "Run health" line with its non-ok checks (silent stop, missed slots), PRESERVE_PENDING count (read only), overdue requests, open requests with the ball in our court and no dated event for more than 72 h, release links expiring within 14 days.
  - *Needs G:* pending approvals by age, open G decisions from the timeline, portals that hold release links (a login may be needed there), the public-update hold queue and the auto-post switch state.
- `GET /public-updates` — Screen F. Status lines as proposed / holding / posted / retracted, refused lines and oversight alerts, approval packets awaiting G, and the switch (OFF, ON, KILLED).
- `POST /public-updates/retract` (Undo a posted line or veto a held one), `/public-updates/off` and `/public-updates/kill`. They need the page's CSRF token and the loopback Origin, and call only the REC-PUB-1 functions that make things safer. There is no route that turns the switch ON or approves anything: ON stays G's SSH-signed command-line step.

Data: `run-outputs/<run>/outcomes/timeline.json` and `release-links.json`, the run reports, the approval queue and `public-updates.jsonl`. Each source fails alone and is named unavailable. Agencies, requests and campaign are data; "stuck" and "overdue" are arithmetic on our records, not a legal finding.
