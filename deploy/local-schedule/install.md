# Campaign-side local schedule: attended install

Six systemd **user** `.service`/`.timer` pairs for the gate-safe campaign jobs. Nothing in
this repository installs, enables or starts them: the install is one attended operator step
on AgentOrange (CLAUDE.md: automation runs natively there, never in a cloud scheduler).

| Slot | Days and time (America/Los_Angeles) | Command (all read-only or draft-only) |
| --- | --- | --- |
| `deflock-backup` | daily 03:00 | `campaign_tool backup --into %h/deflock-backups` (local tar, no upload) |
| `deflock-stale-check` | Mon..Fri 08:00 | `tools/stage_stale_check.py check --reports REPORTS` (records run reports, read-only) |
| `deflock-deadlines` | Mon..Fri 09:00 | `tools/flock_deadlines.py run --date <today, America/Los_Angeles>` (drafts only) |
| `deflock-reconcile` | Mon..Fri 10:00 | `campaign_tool status --check` (exit 3 fails the unit: the alert) |
| `deflock-run-report-review` | Mon..Fri 16:00 | `tools/run_report_review.py summary --dir REPORTS` (records run reports, read-only) |
| `deflock-site-build` | Mon..Fri 17:00 | `campaign_tool build` (build only; publishing stays a human gate) |

The records engine runs Mon..Fri at 07:00 and 15:00 (records requests do not
arrive on weekends), so every job except the backup is weekday-only and follows a
records run: stale-check, deadlines and reconcile after the 07:00 run, run-report
review and site build after the 15:00 run. The backup stays daily. Deadline drafts
are weekday-only but the deadline arithmetic is unchanged and still counts calendar
days: the rule is checked as of the run date, so Monday's run drafts anything that
fell due over the weekend. The stale-check's 36 hour silence rule is frozen and is
why it must not run on weekends (Friday afternoon to Sunday morning is 40 hours).

Each slot is at least 50 minutes from every other slot and from the records engine's
07:00 / 15:00 slots, fires up to 5 minutes late (`RandomizedDelaySec`), catches up
after downtime (`Persistent=true`, so a weekday missed while the machine was off runs
once on the next start) and holds its own `flock` lock (`%t/deflock-SLOT.lock`;
a held lock skips that slot with exit 73). No slot sends, publishes or spends.

An install made before this change keeps its old timers until the operator re-runs the
install below (re-render the copies, `systemctl --user daemon-reload`, then
`systemctl --user restart deflock-SLOT.timer`).

## Check first (read-only)

    python3 -B tools/local_schedule_check.py --store /ABS/STORE --reports /ABS/RUNTIME_DIR/TARGET/reports

Every slot must be `available`; exit 1 names a missing command, a collision, a broken unit
or a gate-unsafe command (which refuses the whole check). `deflock-stale-check` and
`deflock-run-report-review` read the records run reports, which the records runtime writes to
`RUNTIME_DIR/TARGET/reports/` (deploy/records-runtime/README.md, "Receipts"); the campaign
store holds none. While that directory is absent both slots are `unavailable` (each would fail
every run), so install them only after the records target has written its first report.

## Install (attended; the operator runs this, never a builder)

Set `STORE` (campaign store), `REPORTS` (the records target's `RUNTIME_DIR/TARGET/reports`,
absolute; read only, never created here), `PY` (absolute python3) and `WORK` (this checkout), then:

    mkdir -p ~/.config/systemd/user && for f in deploy/local-schedule/deflock-*.service deploy/local-schedule/deflock-*.timer; do sed -e "s|@STORE@|$STORE|g" -e "s|@REPORTS@|$REPORTS|g" -e "s|@PYTHON@|$PY|g" -e "s|@WORKDIR@|$WORK|g" "$f" > ~/.config/systemd/user/"$(basename "$f")"; done

Then verify the rendered copies before enabling anything (a leftover placeholder fails):

    python3 -B tools/local_schedule_check.py --store "$STORE" --reports "$REPORTS" --installed ~/.config/systemd/user

Enable after that check exits 0: `systemctl --user daemon-reload`, then
`systemctl --user enable --now deflock-SLOT.timer` for each slot. Create `%h/deflock-backups`
(mode 0700) before enabling `deflock-backup`.

## Stop

`systemctl --user disable --now deflock-SLOT.timer` stops one slot; remove its two files from
`~/.config/systemd/user` and `systemctl --user daemon-reload` to uninstall. Report any pause:
it is never silent.
