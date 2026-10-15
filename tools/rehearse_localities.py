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


def require(condition, reason):
    if not condition:
        raise RuntimeError(reason)


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
            output = io.StringIO()
            with redirect_stdout(output):
                cli.doctor(root)
            doctor = json.loads(output.getvalue())
            cfg = cli.read_config(root)
            require(cfg["county"] == county and cfg["state"] == "CA", "wrong_locality")
            require(cfg["external_sends"] == "disabled" and cfg["publication"] == "manual",
                    "unsafe_configuration")
            require(kit["generated_offline"] and kit["sent"] is False
                    and kit["requests_drafted"] > 0, "offline_kit_incomplete")
            require(doctor["law_package_status"] == "draft"
                    and doctor["production_ready"] is False
                    and doctor["safe_to_send_automatically"] is False
                    and doctor["jurisdiction_verified"] is False, "readiness_hold_lost")
            with sqlite3.connect(root / "private" / "ledger.sqlite") as db:
                objects = db.execute("SELECT COUNT(*) FROM objects").fetchone()[0]
                receipts = db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0]
            require((objects, receipts) == (1, 2), "preservation_replay_mismatch")
            require((root / "private" / "objects" / digest).read_bytes() == raw,
                    "original_changed")
            public = {}
            for path in sorted((root / "public").rglob("*")):
                require(not path.is_symlink(), "public_symlink")
                if path.is_file():
                    content = path.read_bytes()
                    require(raw not in content and str(root / "private").encode() not in content,
                            "private_bytes_in_public")
                    public[str(path.relative_to(root / "public"))] = hashlib.sha256(content).hexdigest()
            require("index.html" in public, "starter_missing")
            require(not (root / "public" / "private").exists(), "private_directory_export")
            reports.append({"county": county, "state": "CA", "synthetic_only": True,
                "objects": objects, "delivery_receipts": receipts,
                "requests_drafted": kit["requests_drafted"], "law_package_status": "draft",
                "jurisdiction_verified": False, "production_ready": False,
                "safe_to_send_automatically": False, "public_files": public})
        require(reports[0]["public_files"]["index.html"] != reports[1]["public_files"]["index.html"],
                "locality_output_not_distinct")
    return {"schema": "offline-locality-rehearsal-v1", "synthetic_only": True,
            "live_pilot_completed": False, "external_sends": False,
            "localities": reports}


if __name__ == "__main__":
    print(json.dumps(rehearse(), indent=2))
