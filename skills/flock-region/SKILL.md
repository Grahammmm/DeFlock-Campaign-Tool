---
name: flock-region
description: Use when starting Flock work in a new county. Drafts a region config from the engine agency seed, checks it, and scaffolds the private store (agency folders, WATCHLIST, ROSTER, PLAN.md) so one config and one skill run reach flock-sweep status.
skill_version: 1.2
---

# Flock region

`tools/flock_region.py` validates and scaffolds; it makes no network call, never edits or verifies the
seed and never writes FACTS, POLICY, REQUESTS or LAW (F1, F2 and F5a own those).

## Inputs
- The engine agency seed (`us-ca`), the county FIPS and a region slug such as `sb-county`.
- The private store, outside the repository. Region configs live at `<store>/regions/<region>.json`.

## Steps
1. `python3 tools/flock_region.py new --county-fips FIPS --region SLUG --out <store>/regions/SLUG.json`:
   one `hold` agency per seed sheriff and police entry. CHP, campuses and state departments are added by
   hand with `seed_agency_id: null` and a `seed_note`.
2. Legwork card (haiku-worker, L3 procedure) proposes `official_domain` and watch rows (agenda, minutes,
   news: https index pages inside the domain, no PDFs; social: a query only) from official pages only.
   A 403 or bot wall is a recorded gap, never bypassed.
3. The operator sets `in_scope` (yes, no, hold), `applicability` (max 200 chars, required for yes) and
   `site_slug`. Keep existing legwork slugs.
4. `python3 tools/flock_region.py --check <config>`: fix every `agency N: <field> <reason>` line.
   Then `python3 tools/flock_region.py coverage <config>` (read-only; exit 3 while any is left): add or consciously
   mark (`in_scope: no` or `hold`) every `unlisted` seed sheriff/police agency.
5. `python3 tools/flock_region.py init <config> --store <store>`: creates `agencies/<slug>/` for yes and
   hold, and writes `regions/<region>/WATCHLIST.csv`, `ROSTER.csv` and `PLAN.md` (0600, atomic). Rerun
   after each config edit: unchanged input is byte-identical and `last_seen_hash` values are kept.
   After a config edit, run `init … --dry-run` first; check `would drop watch row` lines before the real init.
6. Work the PLAN.md steps per agency. Step 5 starts with `flock_monitor.py check` (no
   network, anyone); `scan` stays operator-run.

## Golden case
`tests/fixtures/flock/region-sample.json` on the synthetic seed `tests/fixtures/flock/seed/us-ca.json`:
`init` prints `agencies=3 folders_created=2 watch_rows=3 watch_dropped=0 roster_rows=3`.

## Never
- Add a host to any allowlist, or download from a watch URL (F7 `scan` is operator-run; `check` is the no-network dry run).
- Mark seed entries verified, or edit the seed.
- Write FACTS, POLICY, REQUESTS or LAW, or commit a real config, WATCHLIST, ROSTER or PLAN.md.
- Reuse a slug across regions (`init` refuses it).
- Start compare runs before the agency's applicability line exists.

## Changelog

- 1.1: PLAN step 5 starts with F7 check (dry run)
- 1.2: step 5 runs `init --dry-run` first after a config edit
