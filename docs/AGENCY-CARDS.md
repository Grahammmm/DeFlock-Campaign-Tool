# Agency, finding and evidence-link renderer

This compatibility slice extracts the existing profile renderer: agency identity,
status, summary, count, public findings list, uncertainty flags, next-action text,
and two links to source notes. The campaign supplies county-wide copy and source
routing through the schema; selected agencies still come from the campaign-owned
CITIES object. The controller calls the generated setCityText function.

No legal conclusion is generated or promoted by this renderer. All wording must
already have completed the campaign's evidence, privacy and publication review.
Evidence-note links are not a claim that original records are downloadable.

## Contract and boundaries

campaign_tool.agency_cards.render(config) accepts schemas/agency-cards.schema.json.
The county profile uses the existing name/vendor/status/summary/cameraCount/facts/
open/flags fields. Source destinations must be local HTML filenames and safe
fragment slugs. Explicit alias mappings handle a group's differently named source
section without hard-coding a jurisdiction in the engine.

New county text rejects angle-bracket markup. The selected-agency CITIES object
remains **trusted, reviewed campaign source**, not an arbitrary JSON upload or
record-extraction result. Its legacy list rendering uses innerHTML for compatibility;
this is not an HTML sanitization boundary. Do not wire user input or AI drafts
directly to it. A later data-only adapter must validate every selected-agency field
before this compatibility global is retired.

The synthetic example makes no real agency allegation and supplies no real
contact address, subscriber, production configuration or private record.

## Pilot preservation and remaining work

The pilot builds its profile renderer from campaign/agency-cards.json and a pinned
engine template. It keeps its old profile function as a frozen comparison fixture.
All app.js, other public-asset hashes and security headers must remain unchanged.

Static source-library pages, individual source metadata/provenance cards, full
agency data schemas, and a complete fresh-campaign wiring wizard are not extracted
in this slice. It must not be presented as a finished evidence publication system.

Tests cover deterministic configuration, rejected markup/paths/schema versions,
single-pass replacement, selected/default profiles, source aliases, counts and
optional link elements. Browser, independent factual review and live signup tests
are separate; these offline tests send no network requests.
