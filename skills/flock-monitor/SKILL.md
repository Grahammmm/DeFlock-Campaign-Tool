---
name: flock-monitor
description: Use when checking official agency agenda, minutes or news index pages for new ALPR-related links. Records public, unreviewed MONITOR.csv rows and a daily digest; never fetches PDFs or off-domain pages.
---

# Flock monitor (watch list, index-page scan, daily digest)

Fetch scope is **index pages only** (R1 option `index`) until G answers otherwise. Link targets,
PDFs included, are recorded by URL and never fetched. Monitor items are leads, not facts or findings.

## Inputs

- Private `WATCHLIST.csv`, header exactly `agency,kind,url,query,official_domain,last_seen_hash`.
  `kind` is agenda, minutes, news or social. Social rows have an empty `url` and a `query`.
  Other rows have an `https://` index-page `url` on `official_domain` (bare lowercase host), never a `.pdf`.
- A private store directory outside every repository.

## Check

- Check: `python3 tools/flock_monitor.py check WATCHLIST.csv` (no network, no writes): fix every
  `row N:` error; the `would fetch` lines are exactly the pages `scan` will request.

## Convert

- Convert the roster (e.g. the L3 legwork list) to the header above, by hand or in a legwork card.

## Scan

- Scan: `python3 tools/flock_monitor.py scan WATCHLIST.csv --store STORE [--date YYYY-MM-DD]`.
  Fix every `row N: <field> <reason>` (exit 1, nothing fetched). `row N: refused <reason>` is data.
  Each scan also appends one row per non-social row to private `STORE/SCAN-LOG.csv` (`ok|unchanged|refused` + reason; last 60 per page kept).

## Digest

- Digest: `python3 tools/flock_monitor.py digest --store STORE --date YYYY-MM-DD` writes
  `STORE/digest/<date>.md` (40 lines at most before any Leads section). Agencies by slug; items whose title or URL path match the shared
  `campaign_tool.meetings.KEYWORDS` come first, marked `[ALPR: terms]`, and are never cut while an unmarked one shows.
  Under the store lock, each relevant item not yet in private `STORE/LEADS.csv` is added as `new` and listed once in
  a trailing `Leads (N new)` section (omitted when N is 0); a link first flagged on an earlier date is never listed again.
  `--unreachable-after N` (2..30, default 3) adds a last `Unreachable` section for pages whose last N logged scans were refused: an unreachable page is a reason to check the agency's site by hand; the monitor never retries or changes the watch list.

## Lead

- Triage: `python3 tools/flock_monitor.py lead --store STORE --item M-xxxxxxxxxxxx --state requested|ignored --by NAME`
  (moves `new → requested|ignored`, `ignored → requested`; `requested` is final; who and when are recorded).

## Deliver

- Delivery is operator-owned (existing healthbot route). Social `query` rows belong to the
  operator's Grok `hermes cron` job; `scan` only counts them.

## Outputs

`STORE/MONITOR.csv` (`kind=public`, `status=unreviewed`, URL + title + page hash only), the
updated `last_seen_hash` in WATCHLIST.csv, and the digest. All files are 0600.

## Never

- Fetch a PDF or any document body; fetch off-domain or follow an off-domain redirect.
- Bypass a 403, robots rule or bot wall, or retry with other headers.
- Add an allowlist host, send anything, or mark an item checked.

The scheduled timer and the Grok cron job are operator installs after merge; this repo adds no timer or workflow.

## Golden case

`python3 -B -m unittest tests.flock.test_monitor` (synthetic `.example` fixtures, no network).
