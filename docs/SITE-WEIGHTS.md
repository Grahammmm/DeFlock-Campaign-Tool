# Site page weights (`build --budget`)

`campaign build --directory DIR --budget KB` prints the first-party weight of every page in `public/` and exits
`3` when any page is above `KB * 1024` bytes (exactly `KB * 1024` bytes is within budget). Everything is measured
offline from the files on disk; nothing is fetched.

```
campaign build --directory DIR --budget 500
campaign build --directory DIR --check --budget 500
campaign build --directory DIR --budget 500 --weights-out ../private/weights.json
campaign build --directory DIR --budget 500 --weights-baseline ../private/weights.json
```

`--check` runs first. A broken internal link (including a missing `srcset` candidate) stops the command with exit
`1` before any weight table is printed or record written.

## What counts

A page's weight is its own HTML bytes plus the on-disk (uncompressed) size of each distinct site-local file its
markup references:

- stylesheets, `preload` and `modulepreload` links, and `<script src>`;
- `src` of `<img>` and `<source>`;
- **every candidate URL in the `srcset` of `<img>` and `<source>`**, using the same tokenizer as the link
  checker (a `data:` URL keeps its internal commas; width and density descriptors are not file names).

References are normalized (percent-decoding, query and fragment stripped, relative and root-relative paths
resolved against the page) and deduplicated by the resulting path: a file named by `src`, `srcset`, `<source>`
and a preload counts once on that page. A shared file counts again on each page that references it.

External URLs (any scheme, `//host`), `data:` URLs, missing files, paths that escape `public/` and symlinks are
never counted and never opened.

## Conservative, not an estimate of one visit

The measurement is a conservative inventory of first-party markup references. It does **not** simulate viewport
width, device pixel ratio, `sizes`, `media`, MIME `type` support, lazy loading or the browser's candidate choice.
A `<picture>` with three alternatives counts all three even though a browser downloads one, so a page can read
heavier than any single visit; ignoring them would let a page whose images are all in `srcset` pass any budget.
Do not quote the figure as exact transfer size, a real browser load, compression, or accessibility conformance.

Not measured: external providers, files a stylesheet pulls in (`@import`, `url()`), and anything a script fetches
(`fetch`, XHR, dynamic `import()`).

## Baselines after upgrading

Records are private `page-weights-v1` files; their shape is unchanged and they now include the `srcset`
candidates. An older baseline recorded before responsive candidates were counted is still readable, but a page
with `srcset` images will show a positive difference ("+N since baseline") that comes from the corrected
measurement, not from content growth. Record a fresh baseline with `--weights-out` after upgrading. Baselines are
never rewritten or migrated by the tool, and the difference never changes the exit code; only the budget does.
