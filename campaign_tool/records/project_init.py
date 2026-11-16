"""records project-init: create an empty private records project for an existing campaign.

Writes only the project tree (0700), campaign.json (0600), the .campaign-root pointer (0600), an empty ledger, an
empty portal queue and safe disabled starters: followup-settings.json (every agency autonomy off, no signature),
a header-only agency-retention.csv and README-only agency-asks/ and followup-coverage/ (0700). No mailbox,
credential, target, send or service is configured. There is no rollback: a failed start may leave a private partial
project, which a rerun refuses as nonempty.
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

from . import agency_retention, campaign_config
from .followup_clock import SETTINGS_FILE, SETTINGS_SCHEMA
from .ledger import store as ledger_store
from .portal.store import Queue
from .run_stages import CAMPAIGN_ROOT_POINTER

_ID = re.compile(r"[a-z0-9-]{2,40}")


class Refused(Exception):
    pass


def _campaign(root):
    from ..cli import read_config
    try:
        return read_config(Path(root))
    except (OSError, ValueError):
        raise Refused("not_a_campaign") from None


def _slug(config):
    text = re.sub(r"[^a-z0-9]+", "-", ("%s-%s" % (config["county"], config["state"])).lower()).strip("-")
    return text[:40] if len(text) >= 2 else "campaign"


_README_TAIL = (" are unconfirmed until a person supplies the matching ID.tsv here; a .proposed.tsv never counts as\n"
                "confirmed. Creating this folder sent, approved and enabled nothing. Formats: "
                "docs/RECORDS-FOLLOWUP-ENGINE.md.\n")
_README_ASKS = "# agency-asks\n\nPer-agency asks" + _README_TAIL
_README_COVERAGE = "# followup-coverage\n\nPer-agency coverage tables" + _README_TAIL


def _write(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        out.write(text)


def init_project(project, campaign_root, campaign_id=None):
    root = os.path.abspath(campaign_root)
    config = _campaign(root)
    campaign_id = campaign_id or _slug(config)
    if not _ID.fullmatch(campaign_id):
        raise Refused("campaign_id_invalid")
    project = Path(os.path.abspath(project))
    if os.path.lexists(project) and (project.is_symlink() or not project.is_dir() or any(project.iterdir())):
        raise Refused("project_exists")
    if project.is_symlink() or any(p.is_symlink() for p in project.parents):
        raise Refused("project_path_symlink")
    document = {"schema_version": 1, "campaign_id": campaign_id,
                "display_name": config["name"].strip()[:80], "law_package": "us-%s" % config["state"].lower()}
    project.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(project, 0o700)
    _write(project / "campaign.json", json.dumps(document, indent=2) + "\n")
    try:
        campaign_config.load(project)
    except ValueError:
        (project / "campaign.json").unlink()
        raise Refused("campaign_config_invalid") from None
    _write(project / CAMPAIGN_ROOT_POINTER, root + "\n")
    ledger_store.initialize(project / "processing-v1" / "ledger.sqlite")
    Queue(str(project / "portal-queue-v1")).close()
    _starters(project)
    return document


def _starters(project):
    """Safe disabled scaffolding: exclusive creates only, never replaces, repairs or enables anything."""
    try:
        _write(project / SETTINGS_FILE, json.dumps(
            {"schema": SETTINGS_SCHEMA, "default": {"autonomy": "off"}, "agencies": {}}, indent=2) + "\n")
        _write(project / "agency-retention.csv", ",".join(agency_retention.HEADER) + "\n")
        for name, text in (("agency-asks", _README_ASKS), ("followup-coverage", _README_COVERAGE)):
            folder = project / name
            os.mkdir(folder, 0o700)
            os.chmod(folder, 0o700)
            _write(folder / "README.md", text)
    except OSError:
        raise Refused("project_starter_unavailable") from None


def main(argv=None):
    parser = argparse.ArgumentParser(prog="records project-init", description=__doc__.split("\n")[0])
    parser.add_argument("--project", required=True, help="new private project directory (must not exist or be empty)")
    parser.add_argument("--campaign-root", required=True, help="existing campaign created by campaign_tool init")
    parser.add_argument("--campaign-id", help="id for campaign.json (default: county and state)")
    args = parser.parse_args(argv)
    try:
        init_project(args.project, args.campaign_root, args.campaign_id)
    except Refused as error:
        print("records project-init: refused (%s)" % error, file=sys.stderr)
        return 2
    print("Project ready: %s\nNext: render a TEST target for this campaign, then run records e2e-proof with "
          "--campaign-root %s. No mailbox or credentials were configured." % (args.project, args.campaign_root))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
