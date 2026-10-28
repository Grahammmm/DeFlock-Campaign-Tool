# Bounded first-party starter capacity

Issue #7 / roadmap item 14 had no repeatable first-party HTTP load receipt.
Run from the public source checkout:

`python3 -B -m tools.benchmark_starter --requests 100 --concurrency 2`

The tool generates a synthetic neutral starter using the existing CLI and leak
check, serves only its public directory on an ephemeral 127.0.0.1 port, requests
index.html and deletes the fixture. No target URL, credentials or production
configuration are accepted. HTTPConnection connects directly without proxy use.
Bounds: 1–1000 total requests, 1–8 workers, two-second request socket timeout,
2 MiB response bound. Work is finite; the executor drains issued requests before
server shutdown. This is not a hard wall-clock deadline or an adversarial server.

The JSON reports completed requests/sec, p50/p95/p99 milliseconds (nearest rank),
errors including HTTP failures and transport exceptions, concurrency and scope.
Timing is machine-dependent evidence, not a release threshold. Nonzero errors
produce a nonzero CLI exit. Record interpreter and source revision with results.

This measures the generated first-party static starter and Python fixture server.
It does not measure Cloudflare Worker/D1 throughput, authenticated APIs, extraction,
model services, external providers, mobile rendering or production capacity.
Those issue #7 acceptance requirements remain open and need their own bounded
owned/stubbed targets. No third-party load or live deployment is authorized.
