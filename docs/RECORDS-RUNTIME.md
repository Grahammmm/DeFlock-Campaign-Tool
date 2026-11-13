# Records runtime

The portable `campaign_tool` starter and the full `records run` extraction engine
have different requirements. Full intake is supported on **Linux with Python
3.11 or newer**, on a filesystem supporting descriptor-relative no-follow opens,
directory fsync and atomic `renameat2(RENAME_NOREPLACE)` publication. macOS and
Windows are not supported intake hosts. Do not replace this primitive with
check-then-rename: concurrent writers must never replace an existing receipt.

On an isolated Linux host, from a checkout pinned to the desired commit:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-records-parsers.txt
.venv/bin/python -B -m campaign_tool.records doctor --root /private-work/records
.venv/bin/python -B -m campaign_tool.records run --root /private-work/records --inbox /private-work/inbox
```

Use an existing private work filesystem outside the software checkout. The doctor
uses the nearest existing parent of the planned root, creates a temporary synthetic
probe there, and removes it. It does not create the records root, read originals,
inspect credentials, contact a service or send data. It tests initial publication,
refusal to replace a published directory, preservation of the original contents,
and cleanup. A symlink ancestor or unsupported filesystem fails with a stable
reason code. If the missing root will later be a new mount, rerun doctor after
mounting it. Every manual and unattended `records run` also probes before creating
its root, ledger or intake outputs. Library callers constructing `Pipeline` directly
must call `runtime.require(root)` themselves; this is not a cross-platform library
compatibility layer.

Exit 0 means the supported platform and tested filesystem primitives passed;
exit 2 means unsupported or unavailable. Parser package versions are reported
separately, with absent packages represented as null. This does not certify parser
coverage, OCR tools, legal review, hosted services or production readiness. Install
the pinned parser requirements for full format support. OCR additionally needs
Tesseract and Poppler; their existing extraction doctor checks those executables.

The `Records run slice` CI uses disposable Ubuntu 24.04 hosts, Python 3.11/3.12/3.13,
pinned parser packages and synthetic fixtures. It runs the real filesystem probes
and the end-to-end intake/publication/OCR suite with **zero skips**. This is the
supported validation path when developing on macOS. CI does not read a live
campaign or reuse development/production services. The runner container is a
separate scheduler runtime; building it alone is not validation of full records
intake.

## Pipeline health watch

`records pipeline-health --runtime-dir ABS --target NAME --root RECORDS_ROOT [--mail-store DIR] [--analysis-root DIR [--analysis-max-minutes 60]] [--console-export-root DIR] [--now ISO] [--json] [--record-alerts]`
reads the existing state files and prints one line, `Run health: <state> - <reason>`, for the console, the run summary or a
Telegram line elsewhere. It never runs, retries, restarts, notifies or clears anything, and reads no credentials. **This unit
installs no timer or service**; wiring it to one is a separate deploy step. Exit codes: `ok` 0, `warn` 1, `unknown` 2, `stop` 3
(a bad `--target` or `--now` also exits 2 with a fixed refusal). Unknown is never reported as ok. The only write is
`--record-alerts`: one `alerts/health-<check>-<reason>.json` per open problem (mode 0600, `count` and `last_seen` updated on each pass,
so a frequent timer does not flood), renamed to `.cleared.json` once the problem is gone. Alert files written by the run wrapper
are never touched; a symlinked or foreign-owned alert directory or file is refused (`alerts_unsafe`).

| Reason code | State | What to check |
| --- | --- | --- |
| `arrival_not_consumed` | stop | A mail or drop-folder arrival stamp has waited over 30 min: is a run being started on arrival (`records arrival`)? |
| `arrival_unreadable` | unknown | The `arrival/` state is malformed or unsafe: inspect it; do not delete stamps. |
| `mail_poll_stale` | warn over 6 h, stop over 24 h | `mail-poll/state.json` has not advanced: check the poll job and mailbox login. |
| `mail_poll_never`, `mail_poll_unreadable`, `mail_poll_not_configured` | unknown | No poll state to judge (or no `--mail-store` given). |
| `no_recent_run` | stop | The latest run report finished over 26 h ago. |
| `run_report_invalid` | stop | `run-report.json` fails the run-report schema. |
| `run_report_future` | stop | `run-report.json` is schema-valid but finished after the assessment clock (compared as UTC instants; finishing exactly at now is accepted). Check the host clock and the report's source; nothing corrects it. No older report is used instead, and the oversight panel shows Runs unavailable and the latest pending count unknown. |
| `run_report_missing` | unknown | No run report yet. |
| `repeated_failures` | stop | The last 3 run reports all ended `fail`. `latest_run_failed` (warn): only the latest did. |
| `notify_failed` | warn | A `notify-failed-*` alert is newer than the last successful report. |
| `schedule_missed`, `schedule_failed`, `schedule_stalled`, `schedule_gaps`, `schedule_held` | stop | The slot health from `records health`; `schedule_degraded` is warn, `schedule_never` unknown. |
| `lane_off` | ok | `--analysis-root` has no `.analysis/LANE-ON`: the analysis lane is switched off, so nothing is expected of it. `analysis_ticking` (ok): on and recent. |
| `analysis_stale` | stop | LANE-ON exists but `.analysis/cycle.log` (appended by `run-analysis.sh` every cycle it holds the lock; LANE-ON's own time until the first cycle) is older than `--analysis-max-minutes` (60): check `records-analysis.timer`, a lock held by a dead process, docker. |
| `analysis_error` | warn | In the last 256 KiB of `.analysis/analysis-ledger.jsonl`, the newest entry of some agency tick, packet, received run or run x agency pair failed (`ERROR ...` other than `ERROR LOCKED`, `EXPORT_INVALID`, `EXPORT_CONFLICT`, `TEXT_CONFLICT`, `CONFIG_INVALID`): read `cycle.log`. |
| `console_export_stale` | stop | `--console-export-root`'s `current/EXPORT.json` (absent counts) names another run than `run-report.json`, which finished over 15 min ago: the console shows an older run; check the run log's `console export` line. `console_export_pending` (ok): within the 15 min. |
| `analysis_unreadable`, `console_export_unreadable`, `console_export_no_run` | unknown | The analysis root, cycle log or ledger, or the export manifest, cannot be read; or there is no valid run report to compare. |
| `analysis_not_configured`, `console_export_not_configured` | unknown | The root was not given. These two are listed in `--json` but, unlike the other unknowns, never change the overall state or the line, so a unit rendered before the lane or export exists keeps its old result. `render.py` adds the flags to `records-health.service` when `ANALYSIS_ROOT` / `CONSOLE_EXPORT_DIR` are rendered. |

Historical reports in `reports/` that finished after the assessment clock are ignored by the failure-streak and notify checks and by the oversight run table, so a future-dated success cannot reset a failure streak or suppress a notify alert; they count with the skipped reports. Limits are in `pipeline_health.LIMITS`. A machine-valid report is review-required, not semantic acceptance of the run.

## Request playbook and banned-phrase lint

`campaign_tool/_resources/playbooks/records-alpr-v1.json` is the campaign-agnostic request playbook (jurisdiction-tagged; agencies, dates and policy
sections are `[placeholders]`). It holds the three tiers (1 audit records and search/use logs, 2 policy, retention, access and
sharing, 3 contracts and vendor terms) with their keywords, the first-request item order (exit ramp, then the lead items: native log
export for a short named window with a pre-offered plate/case-number redaction, completed audits under the agency's own policy
section, sharing lists and MOUs; then policy, retention, users and training; then contracts), the 8 follow-up patterns (extension,
partial production, blanket denial, fee, no records, records contradict policy, silence, vague answer; each with when / goal /
questions / cite / attach / do-not), the wording rules, a citation registry (`verified-in-package` or `unverified`; the law package
stays the source of truth) and the banned-phrase list. `campaign_tool.records.playbook` loads and validates it (`load`, `pattern`,
`tier_for`, `lint`). `records playbook check [--playbook FILE]` validates a playbook (exit 0, or 2 with a fixed refusal code);
`records playbook lint FILE|-` prints each hit and `pass` (exit 0) or `needs edit` (exit 1). The lint is strict and cannot tell a
quoted agency phrase from our own wording. Nothing here sends, posts or publishes, and a pass is not legal or semantic review.

## First-request drafter

`records first-request --agency SLUG --agency-name NAME --policies agency-policies.csv --window-start YYYY-MM-DD --window-end YYYY-MM-DD --basis TEXT [--requester TEXT] [--all-items] [--queue PROJECT --request-id ID]`
builds one DRAFT letter from the playbook and the agency policy register: the exit ramp first, then the lead items in tier order (a native log
export for the named window, which must be at most 92 days, with its fields and the pre-offered plate/case-number redaction; completed audits
under the agency's own policy section, quoted from the register; sharing lists and MOUs), and with `--all-items` the rest. Only
`verified-in-package` citations reach the body. The draft prints on standard output; exit 0 = ready for approval, 1 = needs edit (banned
phrase, no audit policy row for the agency, or an unfilled `[EDIT ME: ...]` placeholder), 2 = refused (bad window, agency or text). `--queue`
appends it as a pending `first-request` draft to the private approval journal (repeat runs add no second row). It never sends, and approval
still needs G's SSH signature.

`records items [--json] [--playbook FILE] RESPONSE-ITEMS.json` (`campaign_tool.records.item_tiers`) reads the REC-AUTO-8 `response-items.json`, adds the playbook tier and a state word (produced, withheld, exemption cited, no records stated, not addressed, unclear, not rated) to each asked item, orders tier 1 (audit and search/use logs) first, and prints a per-request logs-and-audits line. Read-only, private, review required; it writes nothing and sends nothing.
