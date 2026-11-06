# Records console: Board and Agency timeline (REC-CON-1)

Private, read-only pages served by `records panel` (stdlib, loopback only):

- `GET /board[?filter=overdue|waiting|ours|closed]` — Screen A. One row per request across all agencies: tier-1 (logs / audits) status, stage, last contact, statutory due date and basis, promised date, labelled overdue mark, next action, owner. Sorted needs-action-first (overdue, then ball in our court, then waiting on agency, then closed). Counters: requests, overdue, drafts pending, release links expiring within 14 days.
- `GET /agency?agency=KEY` — Screen B. Per request: clocks with basis, overdue marks, the "extension total exceeds 14 days on our records" flag, the stage ladder, next action and owner, asked items with tier and state, and the events oldest first with their source (message id, tracker row, approval journal, manual entry). Links to Documents (Screen C) and Drafts.

Data: `run-outputs/<run>/outcomes/timeline.json` (REC-TL-1), plus `release-links.json` and `response-items.json`. Each source fails alone and is named unavailable. Nothing is sent, fetched, approved or published; overdue marks are arithmetic on our records, not a legal finding. Agency keys are data and are matched exactly against the timeline.
