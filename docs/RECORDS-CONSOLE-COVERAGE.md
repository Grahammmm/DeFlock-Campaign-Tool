# Console coverage page: a short reading note

`GET /coverage` (menu: More, "Coverage", between Run history and System health) shows how far processing has got, run by run. It is read-only and sends, publishes and runs nothing. It is processing metadata, not evidence review or approval.

- **Where it reads**: the reports archive beside `--run-report`, the same archive as Run history, in the project console and in `--export-root` mode alike. Without `--run-report` it says "coverage: not configured on this console"; a missing or unreadable archive says "Coverage: unavailable (code)". Neither is ever a zero summary.
- **Newest run** (top block): the five counts (adopted, adopted this run, coded gaps, awaiting OCR, portal held), the run's mode and whether a stub backend was used, each with one sentence. A stub run is labelled "Stub run": its counts are not evidence of real processing. How each count is worked out belongs to the coverage contract, not this page.
- **History** (table): the newest 30 valid runs, newest first, start time in Pacific time, with a link to each run's details at `/run?id=`. Reports that are invalid or finished in the future are counted below the table, not listed.
- **Change marks**: each count is compared with the immediately older valid run (for the oldest visible row, the 31st valid run if there is one): `↑ up`, `↓ down` or `= same`. A count that stays `= same` row after row has stopped moving.
- **unknown** means the report gave no usable value for that count. It is never shown as `0` or blank, and a comparison that involves an unknown has no mark. An actual `0` is shown as `0`.
