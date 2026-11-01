---
name: flock-sweep
description: Use when tracking every watch-list agency through flock-facts, flock-compare and flock-verify. Shows each agency's stage, blocker and exact next command, plans the next run cards, and builds the cross-agency topic x agency rating matrix from finished runs.
---

# Flock sweep

`tools/flock_sweep.py` is read-only glue over the merged F1-F4 and F7 tools. It never runs a model, never
calls kanban or hermes and never writes into the store. Each model run stays its own private `[RUN]` card.

## Inputs
- `--watchlist`: the F7 header `agency,kind,url,query,official_domain,last_seen_hash`. The L3 provisional
  header is refused; convert L3 to the F7 header first (legwork step).
- `--roster`: `watch_agency,slug,in_scope,applicability,site_slug`. `watch_agency` is a unique agency name; it need not have a
  watch-list row (that agency is simply not monitored by F7). `slug` is the store folder (F1 `slugify()` of the FACTS agency), `in_scope` is yes, no or hold,
  and `applicability` (max 200 chars, required for yes) becomes flock-compare's `--applicability`.
- `--store`: the private fact store, outside the repository.

## Convert
- Convert L3 to the F7 header, then write or refresh the ROSTER (the operator confirms `in_scope`).

## Status
- `python3 tools/flock_sweep.py status --watchlist W --roster R --store S [--out FILE]`: one row per
  agency with `stage` and the exact `next` command, then `agencies=N finished=F ready=R blocked=B unmapped=U unwatched=W`
  (unwatched = in-scope roster agencies with no watch row; add one with flock-monitor if it should be monitored). When W > 0, the line before it names those agencies.
- After each card, run `status` again.

## Plan
- `python3 tools/flock_sweep.py plan ... --max 2`: paste the printed `CARDS` block into card bodies. The
  operator creates compare cards (opus-coder-bopus) and verify cards (codex-reviewer), each within
  40 turns / 2 h, chunked by document, with `REMAINING.md` on cap.

## Matrix
- Once 2 or more agencies are `finished`: `python3 tools/flock_sweep.py matrix ... --out DIR` writes
  COMPARE-MATRIX.csv and COMPARE.md (0600, outside the repository). They hold ids, slugs and ratings only.
  Keep the last matrix folder: `--prev OLD` also writes COMPARE-CHANGES.csv (up/down/added/removed per cell), a
  research diff, not a legal conclusion.

## Report
- Private cited draft: `python3 tools/flock_report.py region --matrix DIR --agency SLUG=CONFIG ... --out DIR2` (one
  `--agency` per column; stale agency drafts are excluded and counted; `--replace` supersedes an earlier draft).

## Stages (first match wins)
unmapped, out-of-scope, scope-hold, no-facts, facts-unreadable, no-policy, no-law, store-errors,
ready-compare, stale-inputs, compare-open, ready-verify, family-refused, verify-open, finished. A store folder no
roster row names is `store-only` (data, not an error).
- `stale-inputs` = FACTS/LAW/POLICY/law package changed after the latest run was scaffolded; start a new dated run, never edit the old one.

## Never
- Run a model from the tool, or create cards from it.
- Skip the Morro Bay golden gate: while a `morro-bay` roster row is not `finished`, `plan` prints only its step.
- Dispatch more than 2 compare runs at once (Claude share watcher).
- Verify in the author's family. The family comes from RUN.json and VERIFY.json, never a profile name.
- Treat `finished` or the matrix as publishable. Publication goes through F9, redaction and G.
- Bypass a 403 or bot wall, add an allowlist host or download anything.

## Roster
- A ROSTER agency without a watch-list row is accepted and assessed from the store; all other roster checks stay.
- The `status` summary ends with `unwatched=W` (in-scope roster agencies with no watch-list row, exact name match).
