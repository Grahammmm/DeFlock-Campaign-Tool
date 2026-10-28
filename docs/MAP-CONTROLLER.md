# Map controller extraction

The shared controller implements polygon/hole and MultiPolygon membership,
configured bounding-box fallback, candidate-area filters, selection counts,
viewport padding, reduced-motion fitting, map initialization, escaped popup text,
source links and graceful loading failure. MapLibre and GeoJSON remain
campaign-supplied; their licenses are not transferred by this extraction.

## Explicit dependencies

The generated fragment is assembled inside the existing application closure.
The campaign provides CAMERA_DATA_URL, BOUNDARY_DATA_URL, COUNTY_BOUNDS, MAP_STYLE
and CITIES (name and bounds per slug), a trusted setCityText function, and the
existing county-map/city-picker/map-selection-count DOM contract. The current
profile renderer and stories remain campaign-owned. This is an incremental
compatibility interface, not the final launch wizard.

Use campaign_tool.map_controller.render(config, profile_renderer). The JSON
contract is in schemas/map-controller.schema.json. Settings select a group slug,
candidate flag, fallback locality slugs, source-map URL, local city boundary file,
initial display count and explicit uncertainty label. Runtime validation rejects
unknown fields, unsafe identifiers/URLs, traversal and invalid counts. This
renderer is build-time code generation: the profile hook is trusted reviewed
source, never record text, user input or an AI draft.

Geography and missing operator tags are research leads, not evidence of agency
ownership. A group's label must say ownership is unconfirmed. The pilot retains
its original classification behavior; changing evidence or classification is
a separately reviewed change.

## Privacy and safe defaults

The fictional config uses an invalid example domain and invented place names.
Tests use synthetic square polygons and mocked map/DOM objects, never external
tiles or email delivery. No SLO records, coordinates, profiles, provider keys,
MapLibre binary, font or photo is included.

**Status (Phase 0c):** `campaign_tool build` wires this controller when
`content/map/map-config.json` exists: it emits `site-data.js` with the
compatibility globals, `agency-cards.js`, and `app.js` (controller plus page
bootstrap), copies `cameras.geojson` and the city boundary file into
`public/data/`, and extends the CSP to the declared style and tile hosts. See
[SITE-CONTENT.md](SITE-CONTENT.md). MapLibre is included only when the campaign
places it in `content/vendor/`; otherwise the static fallback renders. The
neutral starter without `content/` still has no map. No hosted example or
provider capacity claim is made here; a later slice will retire the
compatibility globals.

## Validation

Run Python tests with python3 -m unittest discover -s tests -v, then
node --test tests/*.test.mjs. These cover the new renderer contract and focused
controller behavior offline. They do not validate arbitrary malformed GeoJSON,
dateline/polar geometry, exact boundary-point policy, real WebGL rendering or
mobile performance. Campaign source provenance, attribution and browser review
remain separate requirements.

The pilot build must regenerate app.js byte-for-byte and then pass all existing
asset, header, link and Worker tests. No public output change is intended.

## Optional trusted popup context

`campaign_tool.map_controller.render(config, profile_renderer, point_context_renderer="")`
accepts an optional campaign-authored JavaScript function expression. The CLI accepts
the same optional `point_context_renderer` field. The function receives the clicked
point properties and returns an HTML fragment appended after the source link.
Omitting the hook preserves the existing controller bytes exactly.

This is build-time trusted source code, like the profile renderer. Never fill it
from a remote API, user text or a camera property. Escape every data-derived value
with `mapText`; do not interpolate unchecked URLs. The renderer does not collect,
validate, join, or independently authenticate camera provenance. Campaigns must
validate reviewed metadata before publishing it and distinguish source edit dates,
successful checks, failed attempts and physical observation dates. Unknown evidence
must remain unknown. Private source records and campaign-specific content do not
belong in the reusable engine.

Tests exercise the actual map click/popup path using synthetic data, including
markup escaping, and pin the no-hook output hash to the prior accepted renderer.
No campaign output or deployment is changed by this extension alone.
