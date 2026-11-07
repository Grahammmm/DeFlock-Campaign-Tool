# Volunteer camera map guide

This guide is for the volunteer who looks after a campaign's camera map: refresh the map from a reviewed snapshot of OpenStreetMap data, read what changed, plan a field survey, record what you found with a source, and rebuild the local site. It follows the work in the order you do it. Every step names its command as it exists today and links the reference section in [SITE-CONTENT.md](SITE-CONTENT.md), which stays the reference; nothing is moved out of it.

**What this tool never does.** It edits no OpenStreetMap data and uploads nothing to OpenStreetMap or DeFlock. It fetches only when the organizer runs `fetch_osm_alprs.py` with `--relation` or `--bbox`; `--from-file`, `--check` on a saved file, `map survey-sheet`, `map survey-import`, `content-check` and `build` never use the network. It does not decide who owns a camera: a mapped point says a camera was observed, and a check says one organizer looked once. It sends, posts, publishes and deploys nothing; putting a rebuilt site on the internet is a separate, approved step this guide does not cover.

## Before you start

- **A campaign folder outside this repository**, written `$CAMPAIGN` below, made by `init` and `content init` (see the [README](../README.md#start-locally)) and already set up for a map: `content/map/map-config.json`, `content/map/cameras.geojson` and reviewed city boundaries in `content/map/city-boundaries.geojson`. This guide does not create a map from nothing; the setup is in [Map data and MapLibre](SITE-CONTENT.md#map-data-and-maplibre) and [MAP-CONTROLLER.md](MAP-CONTROLLER.md). The refresh never edits `map-config.json`; its `initial_point_count` is campaign-owned, so read it against the new total yourself.
- **A cloned checkout of this tool.** Run every command from the checkout's root. `python3 tools/fetch_osm_alprs.py` is a script in the checkout; `python3 -m campaign_tool` is the tool itself.
- **A saved answer you reviewed**, `$SAVED`: a JSON file saved from the Overpass API (the OpenStreetMap query service) for `man_made=surveillance` and `surveillance:type=ALPR` nodes. Working from a saved file keeps every step below repeatable and offline.
- **Privacy.** In the field: `Stay on public property, do not touch equipment, and never photograph people or license plates.` Do not record people, plates, addresses or phone numbers in any note.

Task checklist: inspect, preview a refresh, write it, plan a survey, record sourced checks, validate and rebuild, help residents contribute.

## 1. Inspect the current map

Reference: [Map data and MapLibre](SITE-CONTENT.md#map-data-and-maplibre).

Look at `content/map/` in your campaign folder:

| File | What it is |
|---|---|
| `cameras.geojson` | One Point feature per mapped camera, with `url` (the OpenStreetMap node link), `manufacturer`, `operator` and, when tagged, `direction`. |
| `attribution.txt` | The attribution text the site must show. |
| `snapshot.json` | `osm_base`, the OpenStreetMap data time of the saved answer. The site shows it as "Map data as of DATE (OpenStreetMap)". A missing or invalid one means the date is unknown; the tool never uses the day you fetched or built. |
| `history.json` | One dated record per refresh (see step 3). |
| `point-checks.json` | Optional. Organizer checks recorded in steps 4 and 5. |
| `city-boundaries.geojson` | Optional. The reviewed city boundaries that group points and name survey cities. |

A point's link identifies an OpenStreetMap node. Its coordinates are where a mapper put it, not evidence of who owns or runs the camera. Start with the read-only check of the whole `content/` folder:

```sh
python3 -m campaign_tool content-check --directory "$CAMPAIGN"
```

It prints `content/: N problem(s)` and exits 0 when clean, 3 when it lists anything.

## 2. Preview a refresh

Reference: [Map data and MapLibre](SITE-CONTENT.md#map-data-and-maplibre) (the paragraph on `--check`).

`--check` compares the answer with the current `cameras.geojson` by node link and writes nothing: not the map, not the snapshot, not the history. The reproducible main path uses a saved answer:

```sh
python3 tools/fetch_osm_alprs.py --directory "$CAMPAIGN" --from-file "$SAVED" --check
```

It prints the counts, then up to 20 node links for each kind of change:

- **added**: a node link that is in the answer and not in the current map.
- **removed**: a node link that is in the current map and not in the answer.
- **moved**: the same node, more than 10 metres away. Change the distance with `--move-metres M`, which only works together with `--check`.
- **retagged**: the same node with a different manufacturer, operator or `direction` value, compared exactly as written (`NE` to `ne` is retagged). A landed facing change shows here.

One node can be both moved and retagged, so the counts can overlap. At most 20 links per kind are printed, so the list is not the full count; read the number on the first line. A point in the current map without a usable node link is only counted in `unkeyed_old`; it is not compared.

```sh
python3 tools/fetch_osm_alprs.py --directory "$CAMPAIGN" --from-file "$SAVED" --check --move-metres 25
```

**Exit 3 means at least one point would be removed.** It is not an instruction to retry. Find out why: the mapper may have fixed a duplicate, or the saved answer may be partial. A clean `--check` (exit 0) does not mean the observations are true; it only says what differs.

Optional and online, organizer only. These forms contact the Overpass API, so run them yourself, once, and save the answer for review; give exactly one of the three scopes (`--relation`, `--bbox` or `--from-file`), never two together:

```bash
python3 tools/fetch_osm_alprs.py --directory "$CAMPAIGN" --relation "$RELATION_ID" --check
python3 tools/fetch_osm_alprs.py --directory "$CAMPAIGN" --bbox "$SOUTH" "$WEST" "$NORTH" "$EAST" --check
```

`--relation` takes the OpenStreetMap relation id of a county boundary; `--bbox` takes SOUTH WEST NORTH EAST in degrees.

## 3. Write a reviewed snapshot

Reference: [Map data and MapLibre](SITE-CONTENT.md#map-data-and-maplibre) (the paragraphs on a writing run and on `history.json`).

Only after you have read the preview, repeat the identical scope without `--check`:

```sh
python3 tools/fetch_osm_alprs.py --directory "$CAMPAIGN" --from-file "$SAVED"
```

A writing run needs `$CAMPAIGN/campaign.json` (exit 2 otherwise, before anything is read). It writes `cameras.geojson`, `attribution.txt`, `snapshot.json` and `history.json` under `content/map/`, then prints `points=`, `skipped=`, the required attribution, `snapshot osm_base=` and `history: N records`.

- **An answer with no points never replaces a map that has points** (exit 3). `--allow-empty` overrides this. It is a deliberate decision to replace the map with nothing, not the way to repair a bad answer.
- **Refused outputs exit 2**: a symlinked `content`, `content/map` or output file.
- **Each file is replaced atomically on its own. The run is not one transaction across files.** If the history step fails, the new `cameras.geojson` stays written. Stop, read the message and inspect `content/map/`; do not assume the run rolled back.
- **The history** adds one record: the data day (`osm_base`, else the run's UTC day), the total, points per city boundary when `city-boundaries.geojson` exists, and how many nodes were added and removed. A second run on the same day replaces that day's record; the newest 120 are kept. A map day older than the last record is not recorded (exit 2, the new map is still written), and a history file `build` would refuse is never overwritten.
- **History keeps counts of added and removed nodes only.** Moved and retagged points are not stored, so save the `--check` output of any refresh you may want to explain later.
- **`build` never refreshes OpenStreetMap data.** Only the fetch script does, and only when you run it.

After a write, run `content-check` again as in step 1. With two or more history records, the built `cameras.html` shows a "Mapped points over time" table.

## 4. Plan a field survey

Reference: [Map data and MapLibre](SITE-CONTENT.md#map-data-and-maplibre) (the paragraph "Field survey").

```sh
python3 -m campaign_tool map survey-sheet --city "Synthetic Cedar" --directory "$CAMPAIGN"
```

`--city` must be the exact name of a boundary in `city-boundaries.geojson` (case-sensitive); an unknown name is refused (exit 1, `Stopped: --city: unknown city boundary name`) and nothing is written. The command writes a private, printable page and a matching spreadsheet, `kit/survey/<city-slug>-<date>.html` and `.csv`. Both are private worksheets: they are not under `public/`, nothing is built or published, and the same city on the same day is refused rather than overwritten.

Each row lists, read-only, the node link, latitude, longitude, manufacturer, operator, direction and the latest organizer check. Four blank columns are for the volunteer's new evidence: `status`, `checked_on`, `source`, `note`. The displayed map tags and earlier checks are what the map already says; they are never the source of a new check. Print the page for the walk, then fill in the CSV copy afterwards. There is no on-device sync or upload.

In the field: `Stay on public property, do not touch equipment, and never photograph people or license plates.` This guide does not ask for photographs.

## 5. Record sourced checks

Reference: [Map data and MapLibre](SITE-CONTENT.md#map-data-and-maplibre) (the paragraphs on `point-checks.json` and "Field survey").

Fill in rows of the CSV copy. A row whose four blank columns are all still blank is skipped. Any other row needs all of:

| Column | Rule |
|---|---|
| `status` | `seen_in_person`: an organizer saw a camera there (shown as "Seen in person"). `in_agency_record`: an agency record lists the point (shown as "Listed in an agency record"); it is never proof of ownership. `not_found_on_site`: no camera was found at the surveyed location ("Not found on site"). `duplicate`: the point looks like a duplicate of another mapped point ("Duplicate of another point"). |
| `checked_on` | A real calendar day (`YYYY-MM-DD`), not after today. |
| `source` | Yours, for this observation: an `https://` link (no username or password, at most 500 characters), or the first 12 hex characters of exactly one entry in `content/sources.json`. |
| `note` | Optional plain text, 200 characters, no address or phone-like number. |

**The tool never supplies, defaults, copies or infers a source** from the node link, an earlier check or the manufacturer. The sheet's `latest_source` column is not read. If you have no source of your own, leave the row blank. Do not invent a link to get past a refusal.

Also required: the node link exactly as the sheet wrote it, a node that is on the map, and no more than five checks for one node. Preview first; it writes nothing:

```sh
python3 -m campaign_tool map survey-import --csv "$FILLED" --directory "$CAMPAIGN"
```

Read the printed checks. Then record them; this atomically writes `content/map/point-checks.json` and refuses a symlink:

```sh
python3 -m campaign_tool map survey-import --csv "$FILLED" --directory "$CAMPAIGN" --apply
```

One bad row refuses the whole import (exit 1). The message names the row (the header is row 1) and the field, never the value you supplied. Edit the CSV and run the preview again. Importing does not build, publish or touch OpenStreetMap.

**What a check may and may not say.** A check is a dated note by one volunteer about one point. It is not a reviewed finding and has not been through the process in the [review guide](REVIEW-GUIDE.md). `in_agency_record` does not establish ownership. `duplicate` and `not_found_on_site` never remove or edit a point: the point stays on the map and in every count. A check is not a statement about legality, coverage or what a camera records.

## 6. Validate, rebuild and inspect

Reference: [Map data and MapLibre](SITE-CONTENT.md#map-data-and-maplibre) and [Generated pages and files](SITE-CONTENT.md#generated-pages-and-files).

```sh
python3 -m campaign_tool content-check --directory "$CAMPAIGN"
python3 -m campaign_tool build --directory "$CAMPAIGN" --check
```

`build --check` writes the local site to `$CAMPAIGN/public/` and then scans it. It sends and deploys nothing. Open and read:

- `public/cameras.html`: the list of mapped points, the date line ("Map data as of ..." or "date unknown"), the "Organizer check" column once a `point-checks.json` exists and the "Mapped points over time" table with two or more history records.
- The map popup on the built page, if MapLibre is set up.
- `public/data/cameras.csv`, and its row in `public/data.html`.

`data/cameras.csv` has one row per point, none dropped, with the header `osm_url,latitude,longitude,manufacturer,operator,direction,city,check_status,checked_on`. Rows are sorted by `osm_url` text (`node/10` before `node/2`). `manufacturer`, `operator` and `direction` are blank when missing; `city` is the alphabetically first boundary name that holds the point, blank when none, and never an agency claim; `check_status` and `checked_on` are the newest check for that node, blank when none. Notes and sources are never exported. A cell that a spreadsheet would run as a formula (a leading `=`, `+`, `-` or `@`) gets one leading `'`, so a negative coordinate reads `'-120.5`. The bytes are the same on every build of the same input. With no map there is no `cameras.csv`. Every public output is registered in `campaign_tool/publish_paths.py`.

**Keep the attribution.** The map data is `Map data (c) OpenStreetMap contributors, ODbL 1.0`, with the OpenStreetMap copyright page at https://www.openstreetmap.org/copyright and DeFlock as the community source of most points. Attribution and share-alike apply to the map data and to `cameras.csv`, which is always listed under ODbL 1.0 in `data.html`. That is not a blanket licence on the campaign's own pages or records. Keep `attribution.txt` and the attribution on the page.

## 7. Help residents contribute

Reference: [Help-map page starter](SITE-CONTENT.md#help-map-page-starter).

A fresh `content init` writes a draft page for neighbours who want to add or correct a point themselves. It is the packaged copy of [examples/pages/help-map.json](../examples/pages/help-map.json). There is no separate command for it:

```sh
python3 -m campaign_tool content init --directory "$NEW_CAMPAIGN"
```

Run it in a new folder just made by `init` (README, "Start locally").

This writes `content/pages/help-map.json`, and the next build produces `/help-map.html`. Every section starts with `[EDIT ME: verify for this campaign]`; the marker stays visible until you replace it and is not an approval. Read the page, edit it for your campaign, or delete the file or set `"nav": false` to publish no such page. `content init` refuses a folder that already has `content/`, so an existing campaign copies the JSON file into `content/pages/` by hand after checking that nothing is already there; there is no overwrite recipe.

Adding or correcting a camera on OpenStreetMap or the DeFlock reporting page is something a person does outside this tool. This tool never edits OpenStreetMap, and a new point appears on your map only after you refresh (steps 2 and 3) and rebuild. The page names the methods page in words; see "How to read our findings" in the generated site, described under [Generated pages and files](SITE-CONTENT.md#generated-pages-and-files).

## Troubleshooting

| You see | Meaning and what to do |
|---|---|
| `current cameras.geojson is not readable GeoJSON` (exit 2) | `--check` found a damaged map file. Restore it from your campaign backup; do not run a write over it to "fix" it. |
| `--directory is not a campaign directory (no campaign.json)` (exit 2) | A write needs `campaign.json` in the folder. Check the path. |
| `answer has no points; current map kept` (exit 3) | An empty answer will not replace a map with points. Check the saved file. `--allow-empty` is a decision to wipe the map, not a repair. |
| `--check` exits 3 | Removals are listed. Inspect each; do not retry or write blindly. |
| `history not updated` (exit 2) | The new `cameras.geojson` was written but the history was not (a map day older than the last record, or a history file `build` would refuse). Read the message, inspect `content/map/history.json`, and do not assume the run rolled back. |
| symlink refused (exit 2) | A `content`, `content/map` or output path is a symlink. Replace it with a real folder. |
| `--city: unknown city boundary name` (exit 1) | The name is not an exact, case-sensitive boundary name in `city-boundaries.geojson`. |
| `kit/survey/...: exists; nothing was overwritten` (exit 1) | The sheet for this city and day exists. Use it, or move it. |
| `row N source: required` or an invalid `source` (exit 1) | The volunteer's own source is missing or not an `https://` link or a unique 12-hex prefix. Find the real evidence; never invent a link and never loosen the check. |
| `row N node_url: edited` or `not a mapped point` (exit 1) | The node link was changed or is not on the map. Restore it exactly as the sheet wrote it. |
| `row N: duplicate of an existing check` (exit 1) | The same status, day, source and note are already recorded for that node. |
| `checked_on` in the future (exit 1) | The day must be a real day not after today. |
| a sixth check, or a note with an address or phone-like number (exit 1) | At most five checks per node; take the personal detail out of the note. |
| `map/point-checks.json: symlinks are not read` (exit 1) | `point-checks.json` is a symlink; it is never followed. |

The messages above name a row and a field, not the value. Import refusals leave `content/map/point-checks.json` exactly as it was.
