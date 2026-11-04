# SVG assets

One shared gate (`campaign_tool/doc_assets.py`, `svg_kinds`) checks every SVG the tool publishes or copies: files under
`content/public-assets/`, the `site.json` `brand.logo` and `brand.favicon`, and the private flyer logo
(`kit/flyer/logo.svg`, copied from the brand logo). Building, `content-check` and `flyer` all use it; there is no
separate switch for any of them.

## What the profile allows

A self-contained SVG: shapes, gradients, text, and references that stay inside the file (`href="#id"`,
`xlink:href="#id"`, `url(#id)` in an attribute or in a `<style>` element). Nothing is fetched, rewritten or
sanitised; a file either passes unchanged or is refused.

It refuses scripts, event handlers, `foreignObject`, a DOCTYPE or ENTITY, any reference that leaves the file,
and any **XML Base** override.

## Why XML Base is refused

`xml:base` changes the base address that relative references resolve against. With one on the root or on any
descendant, a reference that looks local (`href="#mark"`) can mean a fragment of a different document. The tool
does not resolve addresses, so it cannot tell a harmless base from a harmful one.

The refusal is therefore deliberately conservative: an `xml:base` attribute (exact XML namespace) on **any**
element is refused whatever its value (absolute, protocol-relative, relative, path-only, query-only,
fragment-only, empty or blank) and whether or not anything uses it. A nested empty override does not cancel a
base on an ancestor. An ordinary `base` attribute, a `base` in another namespace, uppercase `xml:Base`, and the
text `xml:base` in comments or text are not XML Base and are not refused for that reason.

The error is the usual one and never shows the value:

```text
public-assets/mark.svg: SVG (external reference)
site.json.brand.logo: SVG (external reference)
site.json.brand.favicon: SVG (external reference)
```

## What happens on a refusal

Validation finishes before anything is written. A fresh `build` creates no `public/`; an existing `public/` stays
as it was. A refused flyer logo creates no `kit/flyer/`, and `flyer --replace` leaves the previous flyer, its
logo and private modes untouched. `content-check` only reads. The SVG you supplied is never edited.

## Recovery

Re-export the artwork as a self-contained SVG without `xml:base`, then run the checks again. Do not simply delete
the attribute by hand: if the drawing depends on the base (for example its references point into another file),
removing it changes what is drawn. Inline what the drawing needs, or export it again from the design tool with
"no external links".

```sh
python3 -m campaign_tool content-check --directory my-campaign
python3 -m campaign_tool build --directory my-campaign
python3 -m campaign_tool flyer --directory my-campaign --replace
```

`content-check` exits 3 when a problem is listed (also with `--json`); `build` and `flyer` exit 1.
