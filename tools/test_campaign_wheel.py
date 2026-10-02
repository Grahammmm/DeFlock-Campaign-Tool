"""Exercise an already-installed wheel outside the checkout using a synthetic campaign."""
import argparse
import json
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def selected_python(value):
    # Resolving a venv symlink selects the base interpreter and loses the venv.
    return str(Path(value).absolute())


def require_virtual_environment(python, identity):
    if selected_python(identity["executable"]) != python:
        raise RuntimeError("Child interpreter does not match the selected executable")
    if identity["prefix"] == identity["base_prefix"]:
        raise RuntimeError("Wheel smoke requires a separate virtual environment")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", required=True, help="Python in a separate environment with the wheel installed")
    parser.add_argument("--expected-commit", required=True)
    args = parser.parse_args()
    python = selected_python(args.python)
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

        identity = json.loads(run("interpreter-identity", ["-c",
            "import json,sys; print(json.dumps(dict(executable=sys.executable, "
            "prefix=sys.prefix, base_prefix=sys.base_prefix)))"]))
        require_virtual_environment(python, identity)
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
                      "interpreter": identity, "provenance": provenance, "steps": results}, indent=2))


if __name__ == "__main__":
    main()
