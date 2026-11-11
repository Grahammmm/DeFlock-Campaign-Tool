# kit-muckrock-import: when a saved MuckRock JSON counts as already recorded

`kit-muckrock-import` reads one MuckRock request JSON you saved by hand and proposes the `kit-response` step it
implies. Nothing is fetched, sent, paid or published, and the saved file is never changed. This page describes how a
re-import is compared with what the kit already holds.

## Latest-step identity

Only the request's **last** recorded step is compared, never its whole history. The saved JSON is skipped with
`already recorded` (exit 0, no write) only when the step it maps to means the same as that last step:

| Mapped step | Same as the last step when |
| --- | --- |
| `fee_estimate` (`payment`) | same date and the same amount in whole cents |
| `closed` (`no_docs`, `abandoned`) | same date and the same generated note |
| `produced` | same date and the same partial or final kind |
| `acknowledged`, `denied`, `clarification_requested` | same kind and date |

`recorded_at`, linked records or correspondence, references and notes you added to a step other than `closed` are
ignored. If a different step sits between an older matching step and the end of the timeline, the older one is not
looked at: the new proposal goes through the normal step rules (including the rule that a step may not be dated before
the previous one).

## Equivalent prices

The price is compared as integer cents, so `245`, `245.0`, `245.00`, `"245"` and `245.00` as a JSON number are the
same estimate. Zero is a real amount, not a missing price. A JSON with no price (absent or empty) still stops with
`no price in the JSON; record the estimate by hand`.

## Validation before skipping

The price is checked with the normal `--amount` rule before anything is compared. An invalid price never turns into
`already recorded`, with or without `--apply`: a malformed value stops with
`--amount must be US dollars like 245 or 245.50 (no sign, commas or exponent)`, an out-of-range one with
`--amount must be from 0 to 100000`, and a wrong JSON type with `price must be a decimal string or number`. The
command exits 1 (`Stopped: ...`) and writes nothing.

## Selected dates are validated whole

The date of the step comes from the first field the status uses: `datetime_done` for finished statuses (falling back
to `datetime_updated` only when it is absent, null or empty), otherwise `datetime_updated`. The whole selected string
must be `YYYY-MM-DD`, or that date followed by `T`, `t` or one space and `HH:MM`, optionally `:SS` and, with seconds,
`.` and digits, optionally `Z` or a `+HH:MM`/`-HH:MM` offset (hours 00-23, minutes and seconds 00-59, ASCII only, no
surrounding whitespace). Anything else, such as `2026-03-10garbage` or `2026-03-10T25:99:99Z`, stops with
`<field> must be an ISO 8601 date or datetime` (no value echoed), exits 1 and writes nothing. A malformed `datetime_done`
never falls back to `datetime_updated`.

The step is dated with the written calendar date; no time or offset is stored and nothing is converted to UTC. The check
runs before the `already recorded` comparison, so a malformed timestamp is never reported as a replay. Statuses that
produce no step (`started`, `submitted`, `ack`, `appealing`, `lawsuit`) and fields the status does not select are not
checked.

## A revised estimate on the same day

A different price on the same date is a new proposal, not a replay. The dry run previews it and writes nothing;
`--apply` appends one more `fee_estimate` step. The old estimate stays in the timeline, and `kit-status` and
`kit-fees` use the latest one. Importing the same JSON again is then a no-op.

```sh
# synthetic example: 245.50 was imported, then MuckRock shows 300.00 on the same day
python3 -m campaign_tool kit-muckrock-import --directory campaign --agency ca-example --from-json saved.json
python3 -m campaign_tool kit-muckrock-import --directory campaign --agency ca-example --from-json saved.json --apply
```

## A different closure reason

`no_docs` (`no responsive documents (MuckRock)`) and `abandoned` (`withdrawn (MuckRock)`) are different closures. If
the request is already closed for one reason and the saved JSON says the other, the import is **not** called
`already recorded`: the changed closure is passed to the normal step rules, which refuse it
(`... is closed; no step can follow closed`, exit 1, nothing written, dry run or `--apply`). The request is never
reopened and the closed timeline is never silently corrected. Repeating the same closure is still a no-op.

## Recovery

Nothing here rewrites history. To correct a wrong earlier import, use the existing `kit-response-undo` or record the
correct step by hand with `kit-response`.
