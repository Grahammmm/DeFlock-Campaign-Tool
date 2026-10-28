# Portal fetch-plan regression fixtures

These are synthetic test inputs, not installation settings or authorization to
retrieve records. `plan.json` retains the supported stage-plan shape but every
installation locator is a fictional `/synthetic/` path. Its argv is inspected as
data only. `tests.portal_fetch_plan_helpers` is a pure test plan constructor,
not a deployment tool.

`tests.test_records_portal_fetch_plan` upstreams all 44 numbered cases from the
offline PR189 candidate acceptance suite. The assertions still cover actual
`download.run_download`, `policy.fetch_settings`, `run_orchestrator.stage_argv`,
the portal queue, bounded HTTP streaming, canonical-store adapter, and replay.
Repository adaptations:

- Temporary fixture roots and per-test loopback/DNS guards replace a global
  external output directory. No external service is contacted.
- T01 runs `guard_smoke.py` in its own interpreter. The audit guard is installed
  before production imports; it is not left installed in the shared unittest
  process. This is an accident-prevention guard, not a hostile-code sandbox.
- The actual repository law/schema dependency replaces the records-only
  acceptance harness's missing-dependency sentinel. T39 validates actual stage
  output and its persisted report with the real schemas; only process dispatch
  and transport/policy loading use explicit synthetic seams.
- T30 uses the frozen baseline invariant `(received, versions=1,
  ledger_pending=1, requests=2)` rather than vendoring an old engine. T38 compares
  against the baseline's captured stdout and against the explicit false plan.
- `baseline-dry-stdout.txt` was measured with the baseline `run_stages.py` at
  080f891a16ae58089acf0ec859a864217ff7d792. Source SHA256:
  9b49a06c94b261f17fa2d8ca94f2b8fdd56ee1a16a9353886e42228c06682da5.
  Output SHA256: 579db9ad275497b9e6eae2639cae09f3692295321fbc0fb0f1f745cae92698a0.
- The loopback server uses a short shutdown poll interval, without changing
  response delays, request deadlines or timeout assertions.

T22–24, T28, T34–35, T40 and T44 retain their isolated deciding-gate negative
controls. In particular T41–44 reject incompatible caps and prove durable
head/peer progress for declared and unknown lengths, exact caps and overflow.

Clock fix (no baseline fixture delta)

`tests.test_records_portal.PortalTests.test_deadline_during_stream` remains
unchanged, including `iter([0, 0, 26])`, `reason == 'timeout'`, and zero versions.
The first item reuses the run-start sample for its item deadline; later items
sample once each and never restart the shared run deadline. The pre-open clock
check remains after preflight/authorization, so setup time cannot authorize a
late request. The first per-item timeout conservatively includes setup time.

`tests.test_records_portal_clock_contract` proves the timeout occurs after open,
response/partial cleanup, pre-stream expiry, preflight budget accounting,
later-item deferral, sanitized clock-error propagation and interrupted-attempt
recovery. Clock errors abort the run rather than becoming transport failures;
the existing interrupted-attempt accounting (including its attempt cap) remains
in force. Disabling only the clock adapter is a negative control reproducing
`transport_or_storage_failure` on the same injected exception.

Run from the repository root:

    python3 -B -m unittest tests.test_records_portal_fetch_plan tests.test_records_portal_clock_contract
    python3 -B -m unittest discover -s tests -t .

The portal workflow also explicitly selects both modules on Python 3.11–3.13.
Test success is review-required, not live qualification, preservation approval,
policy acceptance, or permission to publish/download.
