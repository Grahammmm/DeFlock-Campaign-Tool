"""Offline two-locality configuration rehearsal using synthetic originals only."""
from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace

from campaign_tool import cli
from campaign_tool.kit import build_kit
from contextlib import closing
from tools.synthetic_content import write_synthetic_content


def require(condition, reason):
    if not condition:
        raise RuntimeError(reason)


def _public_files(public, forbidden, reason):
    """sha256 per public file; refuses symlinks and any file containing a forbidden byte string."""
    files = {}
    for path in sorted(public.rglob("*")):
        require(not path.is_symlink(), "public_symlink")
        if path.is_file():
            content = path.read_bytes()
            require(not any(item in content for item in forbidden), reason)
            files[str(path.relative_to(public))] = hashlib.sha256(content).hexdigest()
    return files


def _check_readiness(root):
    output = io.StringIO()
    with redirect_stdout(output):
        cli.doctor(root)
    doctor = json.loads(output.getvalue())
    require(doctor["law_package_status"] == "draft"
            and doctor["production_ready"] is False
            and doctor["safe_to_send_automatically"] is False
            and doctor["jurisdiction_verified"] is False, "readiness_hold_lost")


def _content_build(root, county, workspace, raw):
    """Publish synthetic findings, rebuild, and scan every public file of the content site."""
    with redirect_stdout(io.StringIO()):
        written = write_synthetic_content(root, "".join(county.split()))
        cli.build(root, check=True)
    _check_readiness(root)
    forbidden = [raw, b"private/objects/", str(workspace).encode(), str(workspace.resolve()).encode(),
                 *written["source_bytes"]]
    files = _public_files(root / "public", forbidden, "content_private_bytes_in_public")
    slugs = [json.loads(path.read_text(encoding="utf-8"))["slug"]
             for path in sorted((root / "content" / "findings").glob("*.json"))]
    expected = [f"findings/{slug}.html" for slug in slugs] + ["sources.html", "feed.xml"]
    require(len(slugs) == written["findings"] and all(name in files for name in expected),
            "content_build_missing_page")
    return {"content_public_files": files, "content_findings": written["findings"]}


def rehearse():
    reports = []
    with tempfile.TemporaryDirectory(prefix="synthetic-locality-rehearsal-") as folder:
        workspace = Path(folder)
        original = workspace / "synthetic-original.txt"
        raw = b"Synthetic private rehearsal document; not a public finding.\n"
        original.write_bytes(raw)
        digest = hashlib.sha256(raw).hexdigest()
        for index, county in enumerate(("San Luis Obispo", "Alameda")):
            root = workspace / ("campaign-" + str(index))
            with redirect_stdout(io.StringIO()):
                cli.initialize(SimpleNamespace(name="Synthetic " + county + " rehearsal",
                    county=county, state="CA", location=None), root)
                kit = build_kit(root, online=False)
                for source_id in ("synthetic-delivery-1", "synthetic-delivery-1",
                                  "synthetic-delivery-2"):
                    cli.ingest(SimpleNamespace(file=str(original), source_id=source_id), root)
                cli.build(root, check=True)
            _check_readiness(root)
            cfg = cli.read_config(root)
            require(cfg["county"] == county and cfg["state"] == "CA", "wrong_locality")
            require(cfg["external_sends"] == "disabled" and cfg["publication"] == "manual",
                    "unsafe_configuration")
            require(kit["generated_offline"] and kit["sent"] is False
                    and kit["requests_drafted"] > 0, "offline_kit_incomplete")
            with closing(sqlite3.connect(root / "private" / "ledger.sqlite")) as db, db:
                objects = db.execute("SELECT COUNT(*) FROM objects").fetchone()[0]
                receipts = db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0]
            require((objects, receipts) == (1, 2), "preservation_replay_mismatch")
            require((root / "private" / "objects" / digest).read_bytes() == raw,
                    "original_changed")
            public = _public_files(root / "public", [raw, str(root / "private").encode()],
                                   "private_bytes_in_public")
            require("index.html" in public, "starter_missing")
            require(not (root / "public" / "private").exists(), "private_directory_export")
            reports.append({"county": county, "state": "CA", "synthetic_only": True,
                "objects": objects, "delivery_receipts": receipts,
                "requests_drafted": kit["requests_drafted"], "law_package_status": "draft",
                "jurisdiction_verified": False, "production_ready": False,
                "safe_to_send_automatically": False, "public_files": public,
                **_content_build(root, county, workspace, raw)})
        require(reports[0]["public_files"]["index.html"] != reports[1]["public_files"]["index.html"],
                "locality_output_not_distinct")
        require(reports[0]["content_public_files"]["index.html"]
                != reports[1]["content_public_files"]["index.html"], "locality_output_not_distinct")
    return {"schema": "offline-locality-rehearsal-v1", "synthetic_only": True,
            "live_pilot_completed": False, "external_sends": False, "content_build": True,
            "localities": reports}


if __name__ == "__main__":
    print(json.dumps(rehearse(), indent=2))
