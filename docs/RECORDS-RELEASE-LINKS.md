# Release-link register (REC-LINK-1)

Agency mail often delivers records as a portal link (NextRequest, SharePoint,
OpenGov, GovQA) that expires. The outputs stage now writes
`run-outputs/<run>/outcomes/release-links.json` (0600, directory 0700) listing
every such link found in the extracted text of admitted originals.

**Lists only. Nothing is fetched, opened, resolved or tested.** Fetching stays
with the owner and the root-owned portal allowlist. The module imports no
network library.

- **Platforms** are the `PLATFORMS` table in `outcomes/release_links.py` (data;
  add a row for another vendor). Outlook safelink wrappers are unwrapped to the
  real destination. Other hosts are ignored.
- **Privacy:** query strings and token-like path segments are never stored.
  A link is kept as host + masked path + a one-way hash used to merge duplicates
  across messages. Evidence quotes have every URL redacted the same way.
- **Expiry:** a stated date ("expires on November 17, 2026", ISO, m/d/yyyy) or a
  relative period ("expires in 14 days", counted from the mail time) within 300
  characters of the link. Two different statements equally close to one link give
  `ambiguous`; a relative period with no mail time gives `unresolved`; nothing
  stated gives `none_stated`. Nothing is guessed. Conflicting dates for the same
  link in several messages use the earliest and carry `expiry_conflict_earliest_used`.
- **Clock-free artifact:** the file never contains "expired/soon" so replaying
  the stage is byte-identical. The status is computed when you list.
- Unpaginated mail bodies are read too (page `null`); this uses
  `received_items._current_units(..., paged=False)`, with the default unchanged.
- **Retrieval (DF2307-09):** each link carries an optional `retrieval: {state, reason}`
  (`retrieved`, `pending`, `refused`, `not_in_portal`, `unknown`), read offline from the
  portal store (`portal-queue-v1/portal.sqlite`, opened `mode=ro` only if it is an owner-private
  regular file with `portal_meta` version 1; never through `portal.store.Queue`). Identity is the
  exact URL string as matched in the mail (the string the portal hashes); those hashes stay in
  memory and are never written, and a same-path link with a different query, host case or `:443`
  is a different link. Rules, first match wins: store unusable `unknown/store_unavailable`;
  exact hashes reaching different items, or both a refusal and an item, `unknown/link_identity_conflict`;
  exact refusal `refused` (reason code(s)); exact URL of a fetched generation with a current version
  `retrieved`; exact URL on an item fetched through another URL `unknown/item_retrieved_via_other_link`,
  on an item with no version `pending`; no exact match but the portal saw the same message
  `unknown/link_identity_unproven`; otherwise `not_in_portal`. Older v1 files without the key list as
  `unknown`. Nothing is fetched and no portal run is made.

```
records release-links --project PROJECT --run-id RUN [--warn-days 7] [--now ISO] [--json]
```

Prints one line per link, soonest expiry first, marked `EXPIRED Nd ago`,
`EXPIRES in Nd`, `active` or `expiry_unknown`. Exit 0 when listed, 2 when refused
(missing, stale or malformed register). Output is review-required, private, and
not for publication.
