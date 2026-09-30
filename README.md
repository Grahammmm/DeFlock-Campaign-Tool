# DeFlock Campaign Tool

Open-source tools to help local organizers investigate automated license plate reader (ALPR) use, preserve public records, publish carefully reviewed findings, and participate in local government.

**Status: early development, not a production-ready campaign platform.** The initial implementation is an offline starter. It does not contact agencies, send email, provision infrastructure, determine legal violations, or publish findings automatically.

## Start locally

Requires Python 3.11 or newer. No third-party Python packages are required.

```sh
git clone https://github.com/Grahammmm/DeFlock-Campaign-Tool.git
cd DeFlock-Campaign-Tool
python3 -m campaign_tool init --directory ../my-campaign --county "Your County" --state CA --name "Your Campaign"
python3 -m campaign_tool doctor --directory ../my-campaign
python3 -m campaign_tool build --directory ../my-campaign
python3 -m http.server 8080 --bind 127.0.0.1 --directory ../my-campaign/public
```

Open http://127.0.0.1:8080. The generated site is a neutral campaign starter, not a claim that any agency violated a law. Use an external campaign directory so private records do not live in the software repository.

## Preserve a document

```sh
python3 -m campaign_tool ingest --directory ../my-campaign --file examples/synthetic-county/policy.txt --source-id demo-production-001
python3 -m campaign_tool status --directory ../my-campaign
```

Original bytes are preserved under their SHA-256 hash. Separate receipt identities record where an identical document was received. Repeating the same source ID and bytes does not duplicate a receipt. A hash establishes byte identity, not authenticity or truth.

## Included in this alpha

- County/state configuration with safe defaults and local setup diagnostics.
- Static, mobile-friendly website generation from an explicit public-field allowlist.
- Private local document storage and SQLite receipt ledger.
- A review-gate library binding independent review receipts to finding content.
- Synthetic policy, search log, agreement, and agency reply fixtures.
- Portable records-analysis and review skills.
- Architecture, implementation roadmap, operator checklists, request templates, and contribution guidance.

The initial code has not yet completed independent validation. No passing-test or capacity claim is made.

## Planned, not yet implemented

Resumable account setup; Cloudflare deployment automation; authenticated workspace; mailbox and MuckRock connectors; sandboxed PDF/spreadsheet extraction; reviewed state-law packages; authenticated review receipts; publication/correction workflows; newsletter integration; meeting tools; monitored scheduling and cost controls.

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

## Records release identity

`python3 -m campaign_tool.records version --json` reports the exact source build
and distinguishes candidates from tagged releases. An installed wheel exposes
`records` and verifies its embedded package manifest. See [WP0 release identity,
strict public scanning and isolated installation](docs/RECORDS-RELEASE.md).
