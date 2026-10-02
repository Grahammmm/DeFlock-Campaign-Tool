"""Exercise an already-installed wheel outside the checkout using a synthetic campaign."""
import argparse
import json
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", required=True, help="Python in a separate environment with the wheel installed")
    parser.add_argument("--expected-commit", required=True)
    args = parser.parse_args()
    python = str(Path(args.python).resolve())
    results = []
    with tempfile.TemporaryDirectory(prefix="deflock-wheel-smoke-") as folder:
        root = Path(folder)
        campaign = root / "fixture"

        def run(label, arguments):
            process = subprocess.run([python, "-I", *arguments], cwd=root,
                                     text=True, capture_output=True, timeout=60)
            results.append({"step": label, "exit_code": process.returncode})
            if process.returncode:
                raise RuntimeError(label + " failed:\n" + process.stdout + process.stderr)
            return process.stdout

        provenance = json.loads(run("installed-provenance", ["-c",
            "import json; from campaign_tool.records.release_manifest import version_report; "
            "r=version_report(); print(json.dumps({k:r[k] for k in "
            "('commit','source_dirty','installation_kind')}))"]))
        assert provenance["installation_kind"] == "installed_wheel", provenance
        assert provenance["source_dirty"] is False, provenance
        assert provenance["commit"] == args.expected_commit, provenance
        run("init", ["-m", "campaign_tool", "init", "--directory", str(campaign),
                     "--name", "Fixture Campaign", "--county", "Alameda", "--state", "CA"])
        run("kit", ["-m", "campaign_tool", "kit", "--directory", str(campaign)])
        doctor = json.loads(run("doctor", ["-m", "campaign_tool", "doctor", "--directory", str(campaign)]))
        assert doctor["law_package_status"] == "draft", doctor
        assert doctor["production_ready"] is False, doctor
        assert doctor["safe_to_send_automatically"] is False, doctor
        assert doctor["kit_built"] is True, doctor
        run("build-with-scan", ["-m", "campaign_tool", "build", "--directory", str(campaign), "--check"])
        summary = json.loads((campaign / "kit" / "summary.json").read_text())
        assert summary["sent"] is False and summary["generated_offline"] is True, summary
        assert summary["requests_drafted"] > 0, summary
        assert (campaign / "public" / "index.html").is_file()
        assert not (campaign / "public" / "private").exists()
    print(json.dumps({"checked_at": datetime.now(timezone.utc).isoformat(),
                      "synthetic_only": True, "external_sends": False,
                      "provenance": provenance, "steps": results}, indent=2))


if __name__ == "__main__":
    main()
