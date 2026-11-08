# Put the Flock admin console online (plain-words guide)

You do three things. The script does the rest and tells you what it is doing at each step.

Before you start (once): use your **own computer** (Mac or Linux) with Node 22 or newer, git and a browser, and
**Tailscale switched on** (the script copies three small files from AgentOrange). It costs $0 on Cloudflare's free plans.
If any Cloudflare screen ever offers to sell you something, do not accept; the script also stops by itself if it sees one.

The person who hands you this guide fills in `<REVIEWED_SHA>` (the 40-character code of the reviewed version) and
confirms that the three seed files are waiting on AgentOrange in
`/home/user/agent-work/reports/admin-deploy/seed-out/` (`seed.sql`, `seed-summary.txt`, `seed-review.txt`).

## Thing 1: paste one command

Open Terminal and paste (one line, with `<REVIEWED_SHA>` replaced by the same code in both places):

```
curl -fsSL https://raw.githubusercontent.com/Grahammmm/DeFlock-Campaign-Tool/<REVIEWED_SHA>/tools/workspace_deploy.sh -o workspace_deploy.sh && bash workspace_deploy.sh --sha <REVIEWED_SHA> --apply
```

Leave off `--apply` (just `bash workspace_deploy.sh --sha <REVIEWED_SHA>`) to see what it would do without doing anything.

It makes one folder, `~/deflock-admin-deploy`, and stops with a plain message at the first problem.
If it stops, fix what it says and run the same command again with `--resume` added at the end.
If the deploy step says a workers.dev subdomain must be registered: in the Cloudflare dashboard open Workers & Pages, set your subdomain, then run the same command again with `--resume`.

While it runs it asks you a few short questions:

- "Type the word free": check that Cloudflare's Workers & Pages > Plans page says Free, then type `free`.
- "Is that the DeFlock SLO Cloudflare account? Type yes": it shows only the account name.
- The seed approval (Thing 2 below).

## Thing 2: click Allow in the browser, and approve the data by its code

1. **Cloudflare login.** Your browser opens. Click **Allow**. Come back to Terminal.
2. **Seed approval.** The script shows `seed-review.txt`: every line of text that will be uploaded to Cloudflare,
   and a long code (the sha256). Read it. It should contain no requester names, no staff emails and no keys.
   If it looks right, type the **first 8 characters** of the code and press Return. Anything else stops the script
   and nothing is uploaded.

## Thing 3: the sign-in list (a few clicks in the Cloudflare website)

Near the end the script prints eight numbered clicks. In short: Cloudflare dashboard > Zero Trust > Access >
Applications > Add > Self-hosted > use the address it shows > add **one Allow policy** listing the email addresses
that may sign in > login method One-time PIN > save. Then copy two values it names (your team name and the
"Application Audience tag") and paste them into Terminal when asked. You may also type a comma list of organizer emails
(a second lock inside the app), or press Return to skip.

Press Return when the clicks are done. The script then checks the site from the outside. You want to see **PASS**.

## What you should see at the end

The script prints the web address. Open it: Cloudflare's sign-in page appears first (not the console). Enter an allowed email,
type the code Cloudflare emails you, and the console opens on "Waiting on you". Settings says outbound sending is OFF.
A different email must be refused. The data is a one-time snapshot and does not update itself. The site cannot send email.

## If something goes wrong (rollback)

If the final check says **FAIL**, the script prints the rollback and stops. Fastest first:

1. Lock everyone out: Zero Trust > Access > Applications > the app > Policies: remove the Allow rule.
2. Take the new version down: the script prints `deployments list` / `rollback` / `delete` commands for your folder.
3. Undo the data: the script saved a rollback point in `~/deflock-admin-deploy/d1-bookmark-before-seed.txt`.
4. The old copy on AgentOrange (Tailscale, port 8445) is untouched and keeps working.

## What the script will never do

It never prints or saves a password, token or key. Its private notes file (only you can read it) holds the reviewed commit, the resource ids, the Access team name and audience tag, and the organizer emails you typed, but never a key. It makes the signing key itself, sends it straight to Cloudflare
and shows only "set". It never turns on sending of email, newsletters or social posts. It never touches DNS.
It never uses the AgentOrange copy of the repository.
