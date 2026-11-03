# Portable records skills

`skills/` holds the records-analysis and review skills, but they link repository docs by
relative path (for example `../../docs/RECORDS-STAGE-HANDOFF.md`), so a copied skill
folder has dead links. `tools/skills_bundle.py` builds a self-contained copy.

```
python3 -B tools/skills_bundle.py --output-dir /path/to/new-folder
```

The output folder must not exist and its parent must. The command prints one JSON line,
`{"ok": true, "skills": 9, "docs": N}`, or on refusal a JSON line with an `error` code
and exit status 2, and writes nothing (no partial folder, no temporary sibling).

## What the bundle holds

- The nine skills: `records-analysis`, `records-applicability`, `records-capture`,
  `records-content-review`, `records-correspondence`, `records-extraction`,
  `records-preservation`, `records-publication` and `independent-review`. The `flock-*`
  skills are campaign-operations skills tied to this deployment and are left out.
- Every `docs/` file those skills link, copied once into `docs/`, with each link or bare
  `../../docs/<file>.md` path rewritten to `../docs/<file>.md`. External links are untouched.
- `MANIFEST.json` (`schema_version` 1): each file's path, sha256 and source path, and per
  skill a `needs_checkout` list. A link to anything else in the repository (`tools/`,
  `campaign_tool/`, `runner/`) stays as plain text and is listed there; that part needs
  a checkout.
- `README.md`, `LICENSE` and `NOTICE`. Output is byte-identical between runs.

## Refusal codes

`output_exists`, `parent_missing`, `missing_skill`, `missing_doc`, `symlink`,
`bad_frontmatter`, `not_utf8`, `link_outside_repo`, `missing_license`, `bad_arguments`,
`build_failed`.

## Using it

Copy the skill folders you want, with `docs/` beside them, into your agent's skills
directory. Producing a bundle is local; nothing is uploaded or published.
