#!/usr/bin/env python3
"""Read-only acceptance check for a built flock-ocr image or recreated container.

Runs only version/inspection commands through `docker exec <container>` or
`docker run --rm --network none --entrypoint <cmd> <image>` and compares them
with pins.json: tool versions, held apt package versions, ENGINE_COMMIT, the
installed wheel's release manifest, and the engine OCR doctor. Exit 0 means
every check passed. Tool readiness only; not corpus, fidelity or review
acceptance, and it changes nothing.
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

RECIPE = Path(__file__).resolve().parent
VENV_PY = "/opt/campaign-tool/venv/bin/python"


def docker_runner(container=None, image=None):
    def run(argv):
        if container:
            command = ["docker", "exec", container, *argv]
        else:
            command = ["docker", "run", "--rm", "--network", "none", "--entrypoint", argv[0], image, *argv[1:]]
        result = subprocess.run(command, capture_output=True, text=True, timeout=60, check=False)
        return result.returncode, result.stdout, result.stderr
    return run


def first_line(stdout, stderr):
    for text in (stdout, stderr):
        lines = text.strip().splitlines()
        if lines:
            return lines[0].strip()
    return ""


def checks(pins, run):
    """Yield (name, ok, detail) for every acceptance check."""
    for command, expected in pins["expected_versions"].items():
        code, out, err = run(command.split())
        line = first_line(out, err)
        yield command, code == 0 and line.startswith(expected), f"{line!r} (want prefix {expected!r})"
    fmt = "${Package}=${Version}\\n"
    code, out, _ = run(["dpkg-query", "-W", "-f", fmt, *pins["apt"]])
    found = dict(line.split("=", 1) for line in out.split() if "=" in line)
    for name, version in pins["apt"].items():
        yield f"apt {name}", code == 0 and found.get(name) == version, f"{found.get(name)!r} (want {version!r})"
    code, out, _ = run(["apt-mark", "showhold"])
    held = set(out.split())
    missing = sorted(set(pins["apt"]) - held)
    yield "apt holds", code == 0 and not missing, f"not held: {missing}"
    engine = pins["engine"]
    code, out, _ = run(["cat", "/opt/campaign-tool/ENGINE_COMMIT"])
    yield "ENGINE_COMMIT", code == 0 and out.strip() == engine["commit"], repr(out.strip())
    code, out, err = run([VENV_PY, "-I", "-m", "campaign_tool.records", "version", "--json"])
    try:
        report = json.loads(out) if code == 0 else {}
    except ValueError:
        report = {}
    for key, want in (("installation_kind", "installed_wheel"), ("commit", engine["commit"]),
                      ("source_dirty", False), ("manifest_sha256", engine["manifest_sha256"]),
                      ("dependency_lock_sha256", engine["dependency_lock_sha256"]),
                      ("package_version", engine["package_version"])):
        yield f"records version {key}", report.get(key) == want, f"{report.get(key)!r} (want {want!r})"
    code, out, _ = run([VENV_PY, "-I", "-m", "campaign_tool.records.extract.ocr", "--doctor"])
    try:
        doctor = json.loads(out)
    except ValueError:
        doctor = {}
    yield "ocr --doctor ready", code == 0 and doctor.get("ready") is True, \
        f"missing={doctor.get('missing')} unverified={doctor.get('version_unverified')}"


def main(argv=None, run=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    target = parser.add_mutually_exclusive_group(required=run is None)
    target.add_argument("--container", help="running container name or ID (docker exec)")
    target.add_argument("--image", help="built image ID or new tag (docker run --rm, no network)")
    parser.add_argument("--pins", default=str(RECIPE / "pins.json"))
    args = parser.parse_args(argv)
    pins = json.loads(Path(args.pins).read_text())
    run = run or docker_runner(args.container, args.image)
    failed = 0
    for name, ok, detail in checks(pins, run):
        failed += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {name}: {detail}")
    print(f"verify: {'OK' if not failed else f'{failed} check(s) FAILED'}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
