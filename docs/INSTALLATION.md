# Installing the campaign CLI without a source-checkout dependency

This installs an offline alpha, not a hosted campaign or a reviewed law package.
It does not send requests, subscribe contacts, deploy infrastructure or publish findings.
The bundled California agency seed remains unverified and its law package remains draft.

## Build from an identified clean commit

Use Python 3.11 or newer. These commands are for Linux/macOS. Put virtual environments,
wheels and campaign data outside the checkout: the existing release guard rejects
untracked files and uncommitted source. Do not disable that guard.

```sh
python3 -m venv ../deflock-build-env
../deflock-build-env/bin/python -m pip wheel --no-deps . --wheel-dir ../deflock-wheels
python3 -m venv ../deflock-campaign-env
../deflock-campaign-env/bin/python -m pip install --no-deps ../deflock-wheels/*.whl
```

Use a fresh wheel directory with only the intended version. Wheel building downloads
the pinned build dependencies declared in pyproject.toml. The installed offline
campaign CLI itself requires no third-party Python dependencies.

## Run outside the checkout

```sh
cd ..
deflock-campaign-env/bin/python -I -m campaign_tool init --directory ./example-campaign --county Alameda --state CA --name "Example Campaign"
deflock-campaign-env/bin/python -I -m campaign_tool kit --directory ./example-campaign
deflock-campaign-env/bin/python -I -m campaign_tool doctor --directory ./example-campaign
deflock-campaign-env/bin/python -I -m campaign_tool build --directory ./example-campaign --check
```

This example is a synthetic campaign configuration. Agency suggestions are not
verified custodian contacts and do not assert ALPR use. For another state, a
corresponding supported seed/law package is required; this change does not
introduce all-state support. The public tree scan is a pattern check, not privacy
or legal certification.

## Maintainer resource contract

Canonical public inputs remain under data/, schemas/, jurisdictions/ and templates/.
Five explicitly allowlisted copies are committed under campaign_tool/_resources/
so installed wheels contain the exact same bytes and the existing release
manifest can bind them to a clean commit.

After changing a canonical input, run:

```sh
python3 -B tools/sync_campaign_resources.py --write
python3 -B tools/sync_campaign_resources.py
```

Review and commit both sides. The sync helper does not contact a network, accept
arbitrary inputs, ingest campaign files or change review status. Do not add private
campaign content to its allowlist. The packaged scanner is reused without changing
the records-processing implementation.

## Clean-install acceptance

The Installed campaign CLI workflow builds from the checked-out commit, installs
the wheel in a separate environment, and runs init, kit, doctor and build --check
with Python isolated mode from a temporary directory. It verifies installed
provenance and preserves unreviewed/legal/send holds. It does not deploy Workers,
test email delivery, certify capacity or run campaign document intake.
