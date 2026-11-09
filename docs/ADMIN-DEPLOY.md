# Deploying the admin workspace to Cloudflare

The workspace Worker (`workers/workspace`) can run on Cloudflare behind Cloudflare Access.
`workers/workspace/wrangler.jsonc` is the **local development** config (ids are `local`, it binds
example R2 buckets and a queue, and it has cron triggers). Do not deploy with it. Use a separate,
untracked deploy config built from the template.

## Make your deploy config

```sh
cd workers/workspace
cp wrangler.deploy.example.jsonc wrangler.deploy.jsonc
```

`wrangler.deploy.jsonc` is git-ignored, so account values never reach a commit. Edit it by hand:

- replace `<D1_DATABASE_ID>` with the `database_id` printed by `npx wrangler d1 create deflock-campaign -c wrangler.deploy.jsonc`
- replace `<KV_NAMESPACE_ID>` with the id printed by `npx wrangler kv namespace create CACHE -c wrangler.deploy.jsonc`

If wrangler offers to edit a config file for you, answer No and paste the id yourself.

## What the template fixes

| Setting | Value | Why |
|---|---|---|
| bindings | `DB` (D1) and `CACHE` (KV) only | no R2 buckets, no queue: nothing leaves the box and no extra resource has to exist |
| `vars` | none | runtime values are secrets (`wrangler secret put`) |
| `triggers.crons` | `[]` | scheduled ticks would queue jobs that no runner takes |
| `workers_dev` | `true` | the `workers.dev` hostname is the one public address |
| `preview_urls` | `false` | no extra preview hostnames |
| `observability.logs.invocation_logs` | `false` | no per-request logs |

## Rules

- Every production wrangler command carries `-c wrangler.deploy.jsonc`. Without it wrangler reads
  `wrangler.jsonc`, whose id is the literal `local`, and the command is aimed at the wrong database.
- Never commit `wrangler.deploy.jsonc`, and never put secrets in either file.
- Check the template builds without logging in:
  `npx wrangler deploy --dry-run --outdir <scratch-dir> -c wrangler.deploy.example.jsonc`

The full step-by-step runbook (account, login, Access policy, secrets, seed, rollback) is kept
by the operator; this page only covers the config file.
