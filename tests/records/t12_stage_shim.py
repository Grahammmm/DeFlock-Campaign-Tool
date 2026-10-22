"""Test-only stage entry for the T12s real-adapter harness test (synthetic fixtures only).

``python -B tests/records/t12_stage_shim.py STAGE --project DIR ...`` runs the real
``campaign_tool.records.run_stages`` adapter. Only one thing differs from the
container: the portal allowlist. In production it is a root-owned file that
``policy.load_approval`` reads. A test cannot create a root-owned file, so here
the loader returns a synthetic approval for ``portal.example.test``, parsed by
the real ``policy.parse_approval``. Everything else (adoption, portal intake,
fetch inventory, native extraction, gates, outputs) is the real adapter code.
"""
import json
from pathlib import Path
import stat
import sys
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from campaign_tool.records import run_stages  # noqa: E402
from campaign_tool.records.portal.policy import parse_approval  # noqa: E402

PORTAL_HOST = "portal.example.test"
APPROVAL = {"version": 1, "enabled": True, "purpose": "portal-document-retrieval",
            "expires_utc": "2099-01-01T00:00:00+00:00", "portal_hosts": [PORTAL_HOST],
            "redirect_hosts": [], "path_prefixes": {PORTAL_HOST: ["/documents/"]}}


def synthetic_approval(*_args, **_kwargs):
    return parse_approval(json.dumps(APPROVAL), uid=0, mode=stat.S_IFREG | 0o644,
                          now=datetime.now(timezone.utc))


def main(argv):
    with mock.patch("campaign_tool.records.portal.policy.load_approval", synthetic_approval):
        return run_stages.main(argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
