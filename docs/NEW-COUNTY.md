# Start a second California county offline

Run from the engine checkout with Python 3.11+; no extra packages are needed.
Keep campaign directories outside the engine. These commands create private
suggestions and local previews, not a deployed site or sent requests.

```sh
python3 -m campaign_tool init --directory ../humboldt-demo --name "Synthetic Humboldt" --county Humboldt --state CA
python3 -m campaign_tool kit --directory ../humboldt-demo
python3 -m campaign_tool doctor --directory ../humboldt-demo
python3 -m campaign_tool init --directory ../fresno-demo --name "Synthetic Fresno" --county Fresno --state CA
python3 -m campaign_tool kit --directory ../fresno-demo
python3 -m campaign_tool doctor --directory ../fresno-demo
```

For a synthetic multi-page preview, copy `examples/fictional-campaign/content`
into each campaign's `content/` directory. In each copy's `site.json`, replace
`Cedar County` in the mission/about text with `Humboldt County` or `Fresno County`.
Keep the fictional disclaimers and synthetic evidence: this is a demonstration,
not reviewed evidence about those real counties. Do not publish the example.

```sh
python3 -m campaign_tool build --directory ../humboldt-demo --check
python3 -m campaign_tool build --directory ../fresno-demo --check
```

Inspect `campaign.json`, `kit/agencies.json`, `kit/requests/` and `public/`.
The agency list must match the chosen county's packaged seed. Seed entries are
unverified suggestions, not proof of ALPR use. Verify official contacts, scope,
dates and fee caps before any separate organizer-approved send. Doctor and the
public-tree pattern scan do not supply legal, privacy or publication approval.

`HUMBOLDT` and `humboldt ` select the same agencies as `Humboldt`.
For a city/county name such as Fresno, omit `--location` for the county or use
`--location "Fresno County"`; bare `--location Fresno` is ambiguous and refused.
Unknown counties and states without a seed stop without writing a partial kit.
Never repeat `init` to update an existing campaign: it refuses to replace it.

## Optional: start from the draft glossary

`examples/glossary.json` is a draft starter of 25 plain-language terms for a
California ALPR records campaign. It is opt-in: `init` and `content init` never
copy it, and nothing changes until you copy it yourself. Open it and read it
first, then copy it into each preview after the fictional content is in place
(repeat explicitly for `fresno-demo`):

```sh
cp examples/glossary.json ../humboldt-demo/content/glossary.json
python3 -m campaign_tool content-check --directory ../humboldt-demo
python3 -m campaign_tool build --directory ../humboldt-demo --check
```

Do not overwrite a `content/glossary.json` you already edited; compare first.
Inspect and edit the copied JSON, check the current law and every official
source it cites, then review `glossary.html` and the "Terms used on this page"
links on the finding pages. The starter is California-oriented draft prose:
copying it is not approval, the page notice stays until you have checked every
definition, and another state needs its own checked law package and citations.
It is not legal advice, and it must not be used to skip review or to publish the
fictional county evidence.

## Executable offline proof

```sh
python3 -B -W error::ResourceWarning -m unittest tests.test_second_county_e2e tests.test_kit tests.test_discovery tests.test_discovery_suggest -v
```

The integration test creates both temporary campaigns and runs exactly
`init --directory ROOT --name "Synthetic COUNTY" --county COUNTY --state CA`,
`kit --directory ROOT`, `doctor --directory ROOT`, and
`build --directory ROOT --check` for each county after copying the example.
Each subprocess uses the current Python executable with `-B -W
error::ResourceWarning tests/fixtures/no_network_run.py` before those arguments.
The wrapper runs `campaign_tool` as `__main__` and makes socket connects fatal;
a deliberate loopback connect negative control proves the guard is active.
No geocoder, mailbox, deployment or other live service is used.
