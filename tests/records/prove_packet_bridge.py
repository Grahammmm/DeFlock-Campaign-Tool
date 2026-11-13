"""PUB-BRIDGE-1 offline acceptance against a disposable, clean real site clone.

The approval verifier is stubbed ONLY to create a synthetic written fixture.
The candidate CLI, export files, staging, npm checks and built HTML are real.
"""
import argparse
import json
from pathlib import Path
import sys
import tempfile
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from campaign_tool.records import cli, public_packets as pp
from tests.records.test_public_packets import T0, doc, fake_verify
from tools.stage_packet_changes import stage


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="pub-bridge-") as tmp:
        project, export = Path(tmp) / "project", Path(tmp) / "export"
        sign = lambda payload: f"SIG:fixture-signer:{payload.hex()}"
        with mock.patch.object(pp.approval, "verify", fake_verify):
            pp.init(str(project))
            ident = pp.propose(str(project), doc(title="Synthetic packet bridge fixture"), now=T0)["id"]
            pp.approve(str(project), ident, sign(pp.payload(str(project), ident)), now=T0)
            pp.enable(str(project), sign(pp.switch_payload(str(project))), now=T0)
            pp.write_change(str(project), ident, now=T0)
        (project / "original.pdf").write_bytes(b"SYNTHETIC PRIVATE RECORD; NEVER EXPORT")
        for _ in range(2):
            with mock.patch.object(sys, "argv", ["records", "public-packets", "export-changes", "--project", str(project), "--out", str(export)]):
                if cli.main() != 0:
                    raise SystemExit("candidate export failed")
        result = stage(export, args.site)
        public = (args.site / "content/updates" / (ident + ".json")).read_bytes()
        html = (args.site / "migration/.build/public/records.html").read_bytes()
        if b"Synthetic packet bridge fixture" not in html or b'id="packet-updates"' not in html:
            raise SystemExit("fixture missing from real built site")
        if any(token in public or token in html for token in (b"fixture-signer", b"SYNTHETIC PRIVATE RECORD")):
            raise SystemExit("private fixture metadata leaked")
        print(json.dumps(result, sort_keys=True))
        print("PASS: fixture visible in built /records.html; no signer or record bytes; no PR or deployment")


if __name__ == "__main__":
    main()
