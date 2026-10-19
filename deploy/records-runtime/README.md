# Records runtime orchestration templates

Versioned, placeholder-only templates for the host-side records run. The
private runtime is a deploy target: rendered copies are installed there at
cutover. Never commit rendered files, host paths, image digests, chat IDs or
tokens to this repository.

Files

- `run-records.sh.in`: POSIX sh wrapper for one records run.
- `notify.py`: stdlib-only, non-fatal Telegram notifier used by the wrapper.
- `records-run.service.in`, `records-run.timer.in`: systemd user units.

Robustness behaviour (plan T10)

- Docker wait: before any records work the wrapper polls `docker info` and
  checks that the records container is running. It makes up to
  `@DOCKER_WAIT_ATTEMPTS@` attempts, sleeping `@DOCKER_WAIT_DELAY@` seconds
  and doubling up to `@DOCKER_WAIT_MAX_DELAY@`, and stops retrying once the
  overall `@DOCKER_WAIT_DEADLINE@` (seconds since the wait began) is reached.
  Every docker call (`info`, both `inspect`s) runs under GNU `timeout`
  (`@TIMEOUT@`) with a `@DOCKER_CMD_TIMEOUT@` second deadline plus a 2 s
  TERM-to-KILL grace; `timeout` signals the whole process group, so a hung
  client and its children are terminated. If Docker is still not ready, or
  the image inspect fails or times out, the wrapper logs a clear error,
  writes the run report with status `docker_unavailable`, attempts a
  notification and exits 75. No records work is attempted. This covers the
  Oct 17 case, where the timer fired after a reboot before rootless Docker
  was up, and an unresponsive daemon.
- Non-fatal notify: `notify.py` sends one metadata-only message. The failure
  wording is used only when the run did not succeed. Any delivery problem
  (bad token, HTTP error, network down, missing credentials) is logged to
  `run-records.log`. It also writes `alerts/notify-failed-<run_id>.json` and
  sets `"notify_failed": true` in the run report. The run keeps the
  pipeline's own exit status. This covers the Oct 16 case, where a Telegram
  delivery failure failed the whole run.
- Helper fallback: the helper runs under a `@NOTIFY_TIMEOUT@` second
  deadline. If it cannot execute, exits non-zero, times out, or exits 0
  without recording a `notify` outcome in the report, the wrapper itself
  writes the same receipts: `notify_failed: true`, a `notify` object with
  reason `helper_failed`, `helper_timeout` or `helper_no_outcome`, and the
  alert file. The exit status is still the pipeline's own.

Run report: `RUNTIME_DIR/reports/run-<run_id>.json`, copied to
`RUNTIME_DIR/run-report.json`. It holds only enums, integers and
timestamps. Plan T11 extends it with per-stage status and adds the overlap
lock.

Exit codes: pipeline status passed through; 75 Docker not ready; 78
configuration, identity or image check failed.

Placeholders

`@RELEASE_COMMIT@` `@RELEASE_DIR@` `@RECORDS_ROOT@` `@PARSERS_DIR@`
`@PRIVATE_TMP@` `@DOCKER@` `@CONTAINER@` `@EXPECTED_IMAGE@` `@RUN_UID@`
`@RUNTIME_DIR@` `@HOST_PYTHON@` `@NOTIFY_PY@` `@NOTIFY_ENV_FILE@`
`@NOTIFY_LABEL@` `@DOCKER_WAIT_ATTEMPTS@` `@DOCKER_WAIT_DELAY@`
`@DOCKER_WAIT_MAX_DELAY@` `@DOCKER_WAIT_DEADLINE@` `@DOCKER_CMD_TIMEOUT@`
`@NOTIFY_TIMEOUT@` `@TIMEOUT@` `@RUN_RECORDS_SH@`

Values must be shell-safe words, with no spaces or quotes. `@TIMEOUT@` is
the absolute path of GNU coreutils `timeout` (checked executable, else 78).

True maximum durations (D = DOCKER_WAIT_DEADLINE, T = DOCKER_CMD_TIMEOUT,
K = 2 s kill grace, N = NOTIFY_TIMEOUT). Sleeps are clipped so they never
run past D; at most one more attempt (info + running inspect) can start
before D and finish after it, and the image inspect follows:

- Docker readiness, including all command deadlines: at most D + 3(T + K).
- Notify step: at most N + K.

Suggested values: 12 attempts, 5 s initial delay, 60 s cap, D = 600,
T = 20, N = 60. Their sleeps alone total 495 s (5+10+20+40, then 7 x 60),
so D rarely binds for a down daemon, but it does when calls hang. Worst
case: 600 + 66 = 666 s of readiness plus 62 s of notify, about 12.1
minutes, inside the 47 minute unit timeout and leaving the pipeline its own
budget.

Tests: `python3 -B -m unittest tests.test_records_runtime -v`. They use a
fake docker shim and a loopback fake Telegram, with no external network.
