# Site shell extraction: first slice

This slice extracts the pilot's original CSS and document frame into a reusable
Python-only renderer. The existing `build` command now uses that renderer rather
than its earlier unrelated placeholder. Intake, doctor and review gates are not
replaced. Maps, agency cards, story data, signup and analytics are still later
slices; their style selectors remain in the shared stylesheet for output parity.

## Contract and trust boundary

`campaign_tool.shell.render` accepts schema version 1:
language, four reviewed HTML fragments (head, header, main, footer), and font
family/font-face CSS. See `schemas/site-shell.schema.json`. Runtime validation
rejects unknown keys, invalid language tags and non-string slots. The renderer is
deterministic and performs a single substitution pass.

**Raw fragments are trusted source code, not user data.** This function is not an
HTML sanitizer or a publication approval. Do not supply emails, originals,
extracted text, AI drafts or visitor input. Campaign maintainers must approve
public content, privacy and asset rights before assembling fragments. The normal
CLI only exports escaped name/county/state fields and fixed neutral wording.
It does not accept raw fragments from campaign.json.

## Fonts and rights

The original source-code owner approved Apache-2.0 publication of this extracted
code. No logo, portrait, font binary, original record, account identifier or pilot
newsletter endpoint is included. Font declarations stay campaign-owned; the
fictional preview uses Georgia and no downloaded font. Third-party asset rights
remain separate from source-code rights.

## Pilot adoption and pinning

The private campaign vendors only this renderer and its two templates plus this
repository's LICENSE/NOTICE. Its lock records an exact engine commit and per-file
SHA-256 digests. The build verifies those digests before invoking the renderer;
it never downloads latest code during a deployment. Updates require an explicit
small PR that replaces the pin, reviews changed files and repeats output checks.
A release tag can follow review; this branch is not a published release.

The private campaign retains its old index.html and styles.css as frozen comparison
references during this slice, while builds use campaign fragments through the
engine. All public-asset hashes and security headers must remain unchanged. No
production deployment, provider changes or subscriber operations are part of it.

## Tests and limits

`python3 -m unittest discover -s tests -v` adds 12 new offline checks. They are not
the missing historical suite and do not constitute an independent legal/security
audit. Browser acceptance, Worker behavior, signup, traffic capacity and subsequent
component extraction are separate gates.
