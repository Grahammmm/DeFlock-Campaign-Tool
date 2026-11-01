---
name: flock-page
description: Use when turning one agency's checked Flock report into records-page story data. Builds <slug>-story.json from a model-written CARDS.json plus SOURCES.json and lints it fail-closed against the site rules (records emails only, one fact per card, number chains, cite everything, not-posted files invisible).
skill_version: 1.0.1
---

# Flock page

`tools/flock_page.py` assembles the data for a records page in the same keys as the site's
`morrobay-story.mjs`, and checks it against site rules 1, 2, 3, 4, 7, 8 and 9 (agency-records-page
skill). A clean lint is machine-valid and review-required, not editorial or legal acceptance.

## Inputs
- The agency's flock-report output: `SOURCES.json` (numbered sources; records and rules).
- The site's location JSON (`requests`, `documents`) and thread JSON (`documents`, `messages`), read only.
- The base-source count N: how many numbered sources the location copy already prints (`[1]`…`[N]`;
  Morro Bay 8). Take it from the location copy, never guess it.
- The not-posted ids and titles from the agency config / INVENTORY `analysis_only=yes` rows.

## Steps
1. Run flock-report for the agency; use only its checked SOURCES.json.
2. The page card's model writes private `CARDS.json` (schema 1): `meta`, `programEvents`, `keyDates`,
   `admissions`, `findings`, `retention`, `milestones`, `extra_sources`. Each card lists `sources`
   as SOURCES numbers (`2`) or extra keys (`"x:meeting-2026-10-13"`). One fact per card; a change
   in a number is a chain (`107 → 80 → 75`), never "27 cut". Card text is 2–4 sentences.
3. Build (output outside the repository, 0600; an existing file needs `--replace`, which moves it aside):
   `python3 tools/flock_page.py build --cards CARDS.json --sources SOURCES.json --location L --thread T
   --base-sources N --not-posted ID… --not-posted-title TITLE… --out <private>/<slug>-story.json`
4. Fix every error in CARDS.json and build again. Errors read `<area> <index>: <rule> <reason>`;
   the tool never prints card text. Re-check a story at any time with `lint --story … --thread …
   --location … --base-sources N --not-posted …`.
4a. Before adopting a story, run `check` with build's flags (`--story F` for `--out`): it rebuilds in memory, writes
   nothing, and must end `state=current` (stale/missing/link/invalid exit 1; rebuild with `--replace`).
5. A reviewer checks rules 5 and 6 by hand: say what is missing with dates; the policy card names
   what the policy fails to limit.
6. Hand the story JSON to the operator's site card. Adoption and publication go through the site's
   PR, checks and the G-gated deploy.

## What build does
- Cites become `"[a, b]"`, numbered N+1, N+2, … in first-use order over admissions, findings,
  retention, milestones. A milestone with no sources gets `cite: ""`.
- A record source on a posted document reads `Request R, <title>, page(s) P; inventory document ID.`;
  a record not in the location documents reads `Request R, City records held by DeFlock SLO, not
  posted here.` with no id and no title. A rule keeps its SOURCES label.
- It lints its own result first and writes nothing on any error.
- A failed build leaves no temp file: the fresh `.tmp` is removed on any write, link or replace error (a
  `.superseded-*` copy already made is kept).

## Lint rules
- R1: thread messages only on location requests or `outreach` (outreach is dropped); no outreach in the story.
- R2: `programNote` says Day 1; no event before `programStart`; valid dates. R3: key dates are events; short ≤ 40 chars.
- R4: no two cards with the same figure, label or near-same text; number chains; 2–4 sentences.
- R7: every admission, finding and retention row cited; cites resolve; extras contiguous and all cited.
- R8: no not-posted id or title anywhere; every `inventory document N` is posted and verified.
- R9: no email, phone or access-link parameter in the story. Tone: no flock-compare banned wording.

## Never
- Hand-edit the story JSON; fix CARDS.json and rebuild.
- Cite a not-posted record by id or title, or add an outreach section, lane or filter.
- Deploy, publish, send email or edit a site checkout.
