# Fork and run: a cold start for another organizer

This page takes you from a fork of this repository to a built local preview of your
own campaign site, offline. Nothing here sends mail, publishes a page or needs an
account other than GitHub. The README's "Start locally" block is run from a clean
copy of the repository by `tests/test_cold_start_readme.py`, so the first commands
below are checked, not just written down.

You need Python 3.11 or newer and git. No third-party Python packages are required.

## 1. Fork and clone

Fork `Grahammmm/DeFlock-Campaign-Tool` on GitHub, then clone your fork next to
where you want your campaign to live:

```sh
git clone https://github.com/YOUR-NAME/DeFlock-Campaign-Tool.git
cd DeFlock-Campaign-Tool
```

## 2. Keep the campaign directory outside the repository

This repository is the reusable engine and is public. Your campaign directory holds
your agency list, draft requests, private review receipts and the built site. Keep
it in a sibling folder such as `../my-campaign`, never inside the clone, so that
`git add .` in the engine cannot publish it and an engine update never touches it.
See [AGENTS.md](../AGENTS.md) for the split between engine and campaign.

## 3. Create the campaign and the first drafts

Replace the county, state and name with your own (see [NEW-COUNTY.md](NEW-COUNTY.md)):

```sh
python3 -m campaign_tool init --directory ../my-campaign --county "Your County" --state CA --name "Your Campaign"
python3 -m campaign_tool kit --directory ../my-campaign
python3 -m campaign_tool doctor --directory ../my-campaign --checklist
python3 -m campaign_tool build --directory ../my-campaign
python3 -m http.server 8080 --bind 127.0.0.1 --directory ../my-campaign/public
```

Then open http://127.0.0.1:8080/ in a browser. The guided alternative is
`python3 -m campaign_tool setup --directory ../my-campaign`.

## 4. What to change first

1. `../my-campaign/campaign.json`: check `name`, `county` and `state`. Leave the
   status fields (`external_sends`, `law_package_status`) as `init` wrote them.
2. `../my-campaign/content/site.json`: your site's words. Create the placeholders
   with `python3 -m campaign_tool content init --directory ../my-campaign`, then
   replace every `[EDIT ME: ...]` and run
   `python3 -m campaign_tool content-check --directory ../my-campaign`.
3. `../my-campaign/kit/agencies.json`: the seeded agencies are unverified
   suggestions. Add, select or deselect with `python3 -m campaign_tool kit-agency --help`.

## 5. Verify each agency's contact

Open the agency's official records page yourself, then record what you saw:

```sh
python3 -m campaign_tool kit-verify-agency --directory ../my-campaign --agency-id ca-your-county-sheriff --source-url https://example.org/records --checked-on 2026-01-31 --records-email records@example.org
```

Nothing is fetched or sent. See [AGENCY-DISCOVERY.md](AGENCY-DISCOVERY.md).

## 6. Never commit these

`private/`, `kit/` and `public/` in a campaign directory, and any real mailbox,
credential, subscriber list or signed link anywhere. If your campaign directory is
outside the clone, as in step 2, there is nothing to commit.

## 7. Pull upstream fixes

```sh
git remote add upstream https://github.com/Grahammmm/DeFlock-Campaign-Tool.git
git pull upstream main
```

Keep your own changes to the engine small and in separate commits so the pull stays
clean. Re-run step 3's `doctor` afterwards.

## 8. What needs other people

This page does not replace any review. Law package review is described in
[LAW-PACKAGES.md](LAW-PACKAGES.md), approval before anything is sent in
[APPROVALS.md](APPROVALS.md), and publication approval in
[PUBLICATION.md](PUBLICATION.md). Attorney review of any legal conclusion is yours
to arrange; this tool cannot supply it. Contribute fixes back through
[CONTRIBUTING.md](../CONTRIBUTING.md).
