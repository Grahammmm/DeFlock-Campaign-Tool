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
  checks that the records container is running. It makes
  `@DOCKER_WAIT_ATTEMPTS@` attempts, sleeping `@DOCKER_WAIT_DELAY@` seconds
  and doubling up to `@DOCKER_WAIT_MAX_DELAY@`. If Docker is still not ready
  it logs a clear error, writes the run report with status
  `docker_unavailable`, attempts a notification and exits 75. No records work
  is attempted. This covers the Oct 17 case, where the timer fired after a
  reboot before rootless Docker was up.
- Non-fatal notify: `notify.py` sends one metadata-only message. The failure
  wording is used only when the run did not succeed. Any delivery problem
  (bad token, HTTP error, network down, missing credentials) is logged to
  `run-records.log`. It also writes `alerts/notify-failed-<run_id>.json` and
  sets `"notify_failed": true` in the run report. The run keeps the
  pipeline's own exit status. This covers the Oct 16 case, where a Telegram
  delivery failure failed the whole run.

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
`@DOCKER_WAIT_MAX_DELAY@` `@RUN_RECORDS_SH@`

Values must be shell-safe words, with no spaces or quotes. Suggested wait:
12 attempts, 5 s initial delay, 60 s cap, about 9 minutes in total, well
inside the 47 minute unit timeout.

Tests: `python3 -B -m unittest tests.test_records_runtime -v`. They use a
fake docker shim and a loopback fake Telegram, with no external network.
