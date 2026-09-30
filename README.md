# DeFlock Campaign Tool

Open-source tools to help local organizers investigate automated license plate reader (ALPR) use, preserve public records, publish carefully reviewed findings, and participate in local government.

**Status: early development, not a production-ready campaign platform.** The initial implementation is an offline starter. It does not contact agencies, send email, provision infrastructure, determine legal violations, or publish findings automatically.

## Start locally

Requires Python 3.11 or newer. No third-party Python packages are required.

```sh
git clone https://github.com/Grahammmm/DeFlock-Campaign-Tool.git
cd DeFlock-Campaign-Tool
python3 -m campaign_tool init --directory ../my-campaign --county "Your County" --state CA --name "Your Campaign"
python3 -m campaign_tool kit --directory ../my-campaign
python3 -m campaign_tool doctor --directory ../my-campaign
python3 -m campaign_tool build --directory ../my-campaign
python3 -m http.server 8080 --bind 127.0.0.1 --directory ../my-campaign/public
```

`kit` resolves the county (or a city given with `init --location "Morro Bay"`) against the offline California agency seed and writes `../my-campaign/kit/`: an organizer-owned `agencies.json`, one draft records request per sheriff, police and CHP entry, governing bodies, and a law-package summary. Nothing is sent and no agency is asserted to use ALPR; see [docs/AGENCY-DISCOVERY.md](docs/AGENCY-DISCOVERY.md) for what to verify before sending.

Open http://127.0.0.1:8080. The generated site is a neutral campaign starter, not a claim that any agency violated a law. Use an external campaign directory so private records do not live in the software repository.

## Preserve a document

```sh
python3 -m campaign_tool ingest --directory ../my-campaign --file examples/synthetic-county/policy.txt --source-id demo-production-001
python3 -m campaign_tool status --directory ../my-campaign
```

Original bytes are preserved under their SHA-256 hash. Separate receipt identities record where an identical document was received. Repeating the same source ID and bytes does not duplicate a receipt. A hash establishes byte identity, not authenticity or truth.

## Included in this alpha

- County/state configuration with safe defaults and local setup diagnostics.
- Offline agency discovery for all 58 California counties (`data/agencies/us-ca.json`, unverified seed) and a drafted-request kit.
- Static, mobile-friendly website generation: a neutral starter from `campaign.json` alone, or a multi-page campaign site (findings with hashed sources, agencies, source library, meetings, Atom feed, agency cards and optional MapLibre map) from a reviewed `content/` directory with a generated Content-Security-Policy; see [docs/SITE-CONTENT.md](docs/SITE-CONTENT.md).
- Private local document storage and SQLite receipt ledger.
- A review-gate library binding independent review receipts to finding content.
- Synthetic policy, search log, agreement, and agency reply fixtures.
- Portable records-analysis and review skills.
- Architecture, implementation roadmap, operator checklists, request templates, and contribution guidance.

An offline test suite (synthetic fixtures only, no network) runs in GitHub Actions on every pull request and push to `main`. Run it locally with `python3 -B -m unittest discover -v`, then `python3 -B tools/check_public_tree.py` and `node scripts/scan-secrets.mjs` before pushing. A passing suite checks structure and safeguards; it is not independent legal, privacy, or capacity validation, and no production-readiness claim is made.

## Jurisdiction law packages

`jurisdictions/us-ca/package.json` encodes California's Public Records Act deadlines and the SB 34 ALPR statute (Civ. Code § 1798.90.5 et seq.) as data, with a primary source and effective date on every rule. It ships with `status: draft`; `doctor` reports `reviewed_law_package: false` until an independent reviewer marks it `reviewed`. Preview a package with `python3 -m campaign_tool.law show us-ca`. See [jurisdictions/README.md](jurisdictions/README.md) and [docs/LAW-PACKAGES.md](docs/LAW-PACKAGES.md).

## Cloudflare Workers (tested offline, not deployed)

`workers/` contains the setup wizard, the Access-protected organizer workspace and the public-site Worker described in [docs/WORKERS.md](docs/WORKERS.md): resumable setup with an encrypted one-hour session, a provisioning plan with per-resource receipts and a rollback list (`DRY_RUN=1` records calls without performing them), Access JWT validation, the runner API from [docs/CONTRACTS.md](docs/CONTRACTS.md), approval cards, cron run receipts and inbound-mail preservation. Run `cd workers && npm ci && npm run check`. No campaign has been deployed with it; sending executors are interfaces only.

## Planned, not yet implemented

Runner container and job execution; mailbox (send), MuckRock and Brevo executors behind approval cards; site build job and queue consumers; sandboxed PDF/spreadsheet extraction in the runner; independently reviewed state-law packages (California is drafted, not reviewed); publication/correction workflows beyond the review gate; meeting agenda import; monitored scheduling and cost controls; any production deployment of the Workers.

See [roadmap](docs/ROADMAP.md), [architecture](docs/ARCHITECTURE.md), [data handling](docs/DATA-HANDLING.md), and [Cloudflare launch checklist](docs/CLOUDFLARE.md).

## Evidence before conclusions

Downloaded is not analyzed. Extracted is not reviewed. Missing text is not automatically proof of misconduct. Compare conduct with the rule and policy in force on the event date, consider exceptions and counterevidence, and retain exact source locators. AI assistance is not legal advice or a guarantee of accuracy.

## Accounts and costs

The offline starter needs no subscriptions. A live campaign will normally need a domain, hosting account, mailbox, and newsletter provider. AI/OCR, map services, and records fees may add costs. Organizers own their accounts and data. Paid provisioning and external sends require explicit authorization.

## Independence and license

Inspired by practical lessons from DeFlock SLO. No private SLO records, subscribers, secrets or account settings are included. The original page frame and CSS are the first extracted engine components; campaign content and third-party assets remain separate. This is an independent project; no endorsement by the broader DeFlock project is implied. Explore the existing [DeFlock map ecosystem](https://github.com/FoggedLens/deflock) before duplicating map work.

New repository code and documentation are Apache-2.0 licensed. Third-party data, source documents, logos, and map databases retain their own rights.

## Optional records intake port

The filesystem intake v3.2 port and its synthetic tests are documented in
[Records intake](docs/RECORDS-INTAKE.md). Unlike the standard-library starter,
PDF/workbook extraction requires the pinned parser environment. The port is
not an enabled mailbox service, OCR pipeline, or website publisher. Campaign
configuration and every original remain outside this public repository.

## Records pipeline M0a

The reusable records gates and synthetic regression suite are now in the engine.
See [records pipeline implementation status](docs/RECORDS-PIPELINE.md) for commands,
import provenance, limitations, and the ordered M0b-M5 work. Run the suite with
`python3 -B -m unittest discover -v`. This tested offline slice does not mean the
complete campaign platform, automatic intake, or a live deployment is ready.

## Engine/campaign split

The next development stage separates a reusable public engine from private campaign configuration and approved public content. See [the staged split plan](docs/SPLIT-PLAN.md). The first extraction is the shared site shell and CSS, with owner approval of original-code rights. See [shell contract and limitations](docs/SITE-SHELL.md) and the [fictional example](examples/fictional-campaign/README.md). Maps, signup, analytics and deployment extraction remain unfinished.
