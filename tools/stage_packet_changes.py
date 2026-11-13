"""Prepare a separate, operator-reviewed site PR from a trusted host export.

No git add/commit/push, PR, upload, merge or deploy. On a failed check the draft
is left for diagnosis, with a nonzero exit. Never run against the live site.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from campaign_tool.records import public_packets as pp
from campaign_tool.records.packet_changes import directory, read_export

HOOK = "await writeLibrary();"
BRIDGE = "\nconst {writePacketUpdates} = await import('../scripts/render-packet-updates.mjs');\nawait writePacketUpdates();"


def encoded(value):
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()


def stage(export, site):
    export, site = directory(export), directory(site)
    pp._need(not export.is_relative_to(site) and not site.is_relative_to(export), "stage_boundary")
    pp._need((site / ".git").exists(), "site_checkout_required")
    status = subprocess.run(["git", "status", "--porcelain"], cwd=site, check=True, capture_output=True)
    pp._need(not status.stdout, "site_checkout_dirty")
    changes = read_export(export)
    # Fixed paths only; never trust the input target as a filesystem path.
    build_path = site / "migration/build.mjs"
    baseline_path = site / "docs/asset-baseline.json"
    receipt_path = site / "docs/intentional-asset-changes.json"
    for relative in ("migration", "docs", "scripts"):
        directory(site / relative)
    build = (pp._read(build_path, 128 * 1024) or b"").decode()
    pp._need(build.count(HOOK) == 1, "site_build_contract")
    pp._need(BRIDGE not in build or build.count(BRIDGE) == 1, "site_build_contract")
    renderer = Path(__file__).with_name("site-packet-updates.mjs").read_bytes()
    installed = pp._read(site / "scripts/render-packet-updates.mjs", 128 * 1024)
    pp._need(installed is None or installed == renderer, "site_renderer_conflict")
    baseline = json.loads(pp._read(baseline_path, 256 * 1024) or b"{}")
    receipts = json.loads(pp._read(receipt_path, 256 * 1024) or b"{}")
    original = next((r for r in baseline.get("files", []) if r["path"] == "/records.html"), None)
    if original is None or receipts.get("schema_version") != 1:
        raise pp.PacketError("site_baseline_contract")
    # Do not bless a pre-existing records-page mismatch by updating its receipt.
    # A broken site base must be repaired by its owner, not hidden by this bridge.
    subprocess.run(["npm", "run", "check"], cwd=site, check=True)
    directory(site / "content/updates", create=True)
    # Immutable same-revision content; later revisions replace only the same
    # packet's public projection. Unrelated existing packets are preserved.
    for change in changes:
        target = site / "content/updates" / (change["packet"] + ".json")
        prior = pp._read(target, 128 * 1024)
        if prior is not None:
            old = json.loads(prior)
            pp._need(old.get("packet") == change["packet"] and type(old.get("revision")) is int
                     and (old["revision"] < change["revision"] or old == change), "site_revision_conflict")
    for change in changes:
        pp._replace(str(site / "content/updates" / (change["packet"] + ".json")), encoded(change))
    pp._replace(str(site / "scripts/render-packet-updates.mjs"), renderer)
    pp._replace(str(build_path), (build if BRIDGE in build else build.replace(HOOK, HOOK + BRIDGE)).encode())
    # Build the real site first. Update only the records-library asset receipt,
    # preserving all baseline roots and all other intentional changes.
    subprocess.run(["npm", "run", "build"], cwd=site, check=True)
    html = pp._read(site / "migration/.build/public/records.html", 16 * 1024 * 1024)
    if html is None:
        raise pp.PacketError("site_build_missing")
    digest = hashlib.sha256(html).hexdigest()
    entry = next((r for r in receipts["changes"] if r["path"] == "/records.html"), None)
    if entry is None:
        entry = {"path": "/records.html", "from_sha256": original["sha256"], "reason": ""}
        receipts["changes"].append(entry)
    pp._need(entry["from_sha256"] == original["sha256"], "site_baseline_contract")
    entry["to_sha256"] = digest
    reason = "PUB-BRIDGE-1: render approved packet text and source hashes; no record downloads or deployment."
    if reason not in entry["reason"]:
        entry["reason"] = (entry["reason"] + " " + reason).strip()
    pp._replace(str(receipt_path), encoded(receipts))
    subprocess.run(["npm", "run", "check"], cwd=site, check=True)
    subprocess.run(["npm", "run", "scan:secrets"], cwd=site, check=True)
    return {"prepared": len(changes), "site_check": "passed", "publication": "not_performed"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--changes", required=True, type=Path)
    parser.add_argument("--site", required=True, type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(stage(args.changes, args.site), sort_keys=True))
        return 0
    except (pp.PacketError, OSError, ValueError, subprocess.CalledProcessError) as error:
        print(getattr(error, "code", "site_stage_failed") + "; draft may remain; nothing published", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
