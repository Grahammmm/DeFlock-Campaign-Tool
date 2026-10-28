# workers/

Cloudflare Workers for the DeFlock Campaign Tool. See [docs/WORKERS.md](../docs/WORKERS.md)
for what each Worker does and [docs/CONTRACTS.md](../docs/CONTRACTS.md) for the interfaces.

```sh
npm ci
npm run check   # sync:check + typecheck + tests, all offline
```

- `shared/` runtime-free TypeScript used by every Worker (ports of `campaign_tool` pieces).
- `wizard/` public setup flow; `DRY_RUN=1` records Cloudflare API calls without performing them.
- `workspace/` Access-protected organizer app, runner API, cron, inbound mail.
- `public-site/` serves the approved site version from the public bucket.
- `schema/d1.sql` D1 schema; `npm run sync` copies it (and seeds, law packages, CSS) into the packages.
- `scripts/` `sync-data.mjs`, `build-workspace-bundle.mjs`.

Never commit `.dev.vars`, `node_modules/`, `.wrangler/` or `wizard/src/generated/bundles.ts`.
`wrangler.jsonc` files contain no account values.
