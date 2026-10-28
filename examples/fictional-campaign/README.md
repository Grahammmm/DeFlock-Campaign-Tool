# Fictional shell example

Cedar County is fictional. Its agencies, finding, meeting, source records and
three map points (in the ocean near 0,0) are synthetic; there are no subscribers,
tracking, external fonts, real allegations or live newsletter endpoint.

Run from the repository root:

```sh
python3 -m campaign_tool build --directory examples/fictional-campaign
python3 -m http.server 8080 --bind 127.0.0.1 --directory examples/fictional-campaign/public
```

Generated public output is ignored by Git. `content/` exercises the full content
model from [docs/SITE-CONTENT.md](../../docs/SITE-CONTENT.md): the build writes
index, agencies, findings, sources, meetings and about pages, the Atom feed,
sitemap, CSP headers, `site-data.js`, `agency-cards.js` and `app.js`. Add
`--check` to run the public-tree leak scan on the output. The top-level
`agency-cards.json` and `map-config.json` remain the renderer fixtures used by
the unit tests. MapLibre is not vendored, so the map section shows its static
fallback. No accounts or external sends occur.
