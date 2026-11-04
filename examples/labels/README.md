# Spanish labels draft (`es.json`)

**Machine-made draft. A fluent Spanish reader must check every line before a campaign uses it; nothing here is reviewed.**

`es.json` holds a Spanish version of every fixed site string in `campaign_tool/labels.py` (`DEFAULTS`, including the
`js.*`, `classification.*`, `confidence.*` and `kind.*` keys). It keeps each default's `{placeholders}`, is plain text and
passes the same checks as an organizer's own `content/labels.json`. The validator refuses extra top-level fields, so the
draft marking lives here and not in the file.

Points for the reviewer: the wording is neutral Latin-American Spanish; "ALPR" is kept; legal-weight words
(`classification.*`, `confidence.*`, the "not legal advice" notices) are translated literally and must not be
strengthened (for example `classification.redaction` is "Información tachada", which a legal reader should confirm).

To use it after review, copy it into the campaign repository:

```
cp examples/labels/es.json CAMPAIGN/content/labels.json
python3 -m campaign_tool content-check --directory CAMPAIGN
```

`<html lang>` still comes only from `content/site.json` `language`; set it to `es` yourself. Do not publish the draft as
reviewed text. The tool translates nothing and calls no translation service.
