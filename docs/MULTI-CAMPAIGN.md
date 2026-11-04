# Several campaigns from one install

An organizer covering three counties should not run three copies and keep them in step by hand. The campaign directory
is already external to the repository; this adds a name for each one and one status across all of them.
Local only. Nothing is published, sent, copied or uploaded.

```sh
python3 -m campaign_tool campaigns add slo --directory ~/campaigns/slo
python3 -m campaign_tool campaigns add humboldt --directory ~/campaigns/humboldt
python3 -m campaign_tool campaigns list
python3 -m campaign_tool status --campaign slo          # the same as: status --directory ~/campaigns/slo
python3 -m campaign_tool campaigns status
python3 -m campaign_tool campaigns remove humboldt      # unregisters; the directory is not touched
```

## The register

- One file, `$XDG_CONFIG_HOME/deflock-campaign/campaigns.json` (default `~/.config/...`; `DEFLOCK_CAMPAIGN_REGISTER`
  names another), mode 0600, never inside the repository. It holds `name`, `directory` and `added_at` and nothing else:
  no agency, request, document or record text.
- `add` refuses a duplicate name, a missing or non-directory path, a symlinked path, a path inside (or around) the
  repository, a directory with no readable `campaign.json`, and a directory that is the same as, inside or around one
  already registered. A name is 1-40 letters, digits, `.`, `_` or `-`; names are case-sensitive.
- `remove` only unregisters. There is no purge, move or delete.
- The register is written to a same-directory temporary file and swapped in with `os.replace`. Its read-modify-write is
  serialized by an advisory lock on the register file itself, so two concurrent `add`s both land. This is the one lock in
  the feature: it is taken only for that transaction, never inside a campaign directory, and never while a campaign
  command runs or a campaign is read for `campaigns status`. There is no cache, index or database across campaigns.

## `--campaign NAME`

Every command that takes `--directory D` also takes `--campaign NAME`; giving both is a usage error, and an unknown name
lists the registered names. The command then behaves exactly as with `--directory <that path>`.

Isolation is enforced rather than assumed. A command given `--campaign A` cannot open, list, write or rename a path under
any other registered campaign, whether a symlink, a `..` or a path inside `campaign.json` points there; the attempt is
refused with `a command for one campaign may not reach another registered campaign`. `--directory` is unchanged.

## `campaigns status`

One private line per campaign, read from that campaign's own recorded files (`kit/sent.json`, `kit/requests/`,
findings and the law package), each campaign confined to its own directory while it is read:

```
campaign | NAME | requests=N open=N waiting-send=N waiting-publish=N law=STATUS | private; review-required
```

`waiting-send` is a drafted request not yet recorded as sent; `waiting-publish` is a finding with every review gate met
that has not been published. The view shows the gate and acts on none. A campaign whose state cannot be read prints
`NOT CURRENT: <ErrorType>` and the others still print; the command exits 3 then. No record text, name, address, email,
phone or plate appears. Review receipts stay bound to the campaign that issued them, so an approval recorded in one
campaign never releases anything in another.

## Open question: the hosted workspace

The hosted workspace is one campaign per deployment (`CAMPAIGN_ID` in `workers/workspace/src/env.ts`). Hosting several
campaigns means either several deployments or a schema change. Either costs money, so it is the owner's decision, with
the cost shown first. Nothing is provisioned or implemented here.
