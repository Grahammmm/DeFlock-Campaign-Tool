#!/usr/bin/env python3
"""Operator wheel build for container/flock-ocr (stdlib only; installs nothing).

Builds the campaign_tool wheel from the commit pinned in pins.json, twice, in two
fresh detached checkouts, with SOURCE_DATE_EPOCH set to the commit time. It
requires both wheels to be byte-identical and requires the wheel's embedded
release manifest to equal the reviewed manifest_sha256. Only then is the wheel
copied into container/flock-ocr/dist/ and its sha256 printed for preflight.py.

The build interpreter (--build-python) must already provide the pinned build
requirements (pins.json wheel_build.requirements); this script checks them and
never installs, upgrades or downloads anything (pip runs with --no-index).
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

RECIPE = Path(__file__).resolve().parent
MANIFEST_MEMBER = "campaign_tool/records/_release_manifest.json"
VERSION_PROBE = (
    "import importlib.metadata as m, json; "
    "print(json.dumps({n: m.version(n) for n in ('setuptools', 'wheel')}))"
)


class BuildError(Exception):
    pass


def load_pins(path=RECIPE / "pins.json"):
    pins = json.loads(Path(path).read_text())
    engine = pins["engine"]
    if not re.fullmatch(r"[0-9a-f]{40}", engine["commit"]):
        raise BuildError("pins.json engine.commit must be 40 lowercase hex")
    if not re.fullmatch(r"[0-9a-f]{64}", engine["manifest_sha256"]):
        raise BuildError("pins.json engine.manifest_sha256 must be 64 lowercase hex")
    return pins


def canonical_sha256(value):
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(data).hexdigest()


def wheel_manifest(wheel):
    """Return the release manifest embedded in a wheel (as `records version` reads it)."""
    with zipfile.ZipFile(wheel) as archive:
        return json.loads(archive.read(MANIFEST_MEMBER))


def check_wheel(wheel, pins):
    engine = pins["engine"]
    if wheel.name != engine["wheel_filename"]:
        raise BuildError(f"wheel name {wheel.name!r} != pinned {engine['wheel_filename']!r}")
    manifest = wheel_manifest(wheel)
    if manifest.get("commit") != engine["commit"] or manifest.get("source_dirty") is not False:
        raise BuildError("embedded manifest is not a clean build of the pinned commit")
    if canonical_sha256(manifest) != engine["manifest_sha256"]:
        raise BuildError("embedded manifest_sha256 differs from pins.json")
    found = len(manifest.get("package_files", {}))
    if found != engine["package_file_count"]:
        raise BuildError(f"embedded package inventory size differs from pins.json: "
                         f"manifest lists {found} package files, pins.json says {engine['package_file_count']}")
    return hashlib.sha256(wheel.read_bytes()).hexdigest()


def check_build_python(build_python, pins, run=subprocess.run):
    result = run([build_python, "-I", "-c", VERSION_PROBE], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise BuildError("build interpreter lacks setuptools/wheel; prepare an approved build env first")
    found = json.loads(result.stdout)
    for requirement in pins["wheel_build"]["requirements"]:
        name, version = requirement.split("==")
        if found.get(name) != version:
            raise BuildError(f"build interpreter has {name} {found.get(name)}, pinned {version}")
    return found


def build_once(repo, pins, build_python, workdir, index):
    commit = pins["engine"]["commit"]
    checkout = workdir / f"src{index}"
    out = workdir / f"out{index}"
    out.mkdir()
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "--detach", str(checkout), commit],
                   check=True, capture_output=True)
    try:
        status = subprocess.run(["git", "-C", str(checkout), "status", "--porcelain"],
                                check=True, capture_output=True, text=True).stdout
        if status.strip():
            raise BuildError("fresh checkout of the pinned commit is not clean")
        env = dict(os.environ, SOURCE_DATE_EPOCH=str(pins["engine"]["commit_time_epoch"]),
                   PYTHONDONTWRITEBYTECODE="1", PIP_NO_INDEX="1", PIP_DISABLE_PIP_VERSION_CHECK="1")
        subprocess.run([build_python, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation",
                        "--no-index", "--wheel-dir", str(out), str(checkout)], check=True, env=env)
    finally:
        subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(checkout)],
                       check=False, capture_output=True)
    wheels = sorted(out.iterdir())
    if len(wheels) != 1:
        raise BuildError(f"build {index} produced {len(wheels)} files, expected one wheel")
    return wheels[0]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--build-python", required=True,
                        help="interpreter that already has the pinned setuptools/wheel")
    parser.add_argument("--repo", default=str(RECIPE.parents[1]), help="git repository holding the pinned commit")
    parser.add_argument("--dist", default=str(RECIPE / "dist"), help="destination (must be absent or empty)")
    parser.add_argument("--provenance-out", help="write a JSON provenance record here (keep outside Git)")
    parser.add_argument("--check-wheel", help="only verify an existing wheel against pins.json")
    args = parser.parse_args(argv)
    try:
        pins = load_pins()
        if args.check_wheel:
            print(check_wheel(Path(args.check_wheel), pins))
            return 0
        tools = check_build_python(args.build_python, pins)
        dist = Path(args.dist)
        if dist.exists() and (dist.is_symlink() or any(dist.iterdir())):
            raise BuildError(f"{dist} must be absent or an empty real directory")
        with tempfile.TemporaryDirectory(prefix="flock-ocr-wheel-") as tmp:
            work = Path(tmp)
            first = build_once(Path(args.repo), pins, args.build_python, work, 1)
            second = build_once(Path(args.repo), pins, args.build_python, work, 2)
            sha1, sha2 = check_wheel(first, pins), check_wheel(second, pins)
            if sha1 != sha2:
                raise BuildError(f"wheel is not reproducible: {sha1} != {sha2}")
            dist.mkdir(exist_ok=True)
            shutil.copy2(first, dist / first.name)
        record = {"engine_commit": pins["engine"]["commit"], "wheel": first.name, "wheel_sha256": sha1,
                  "manifest_sha256": pins["engine"]["manifest_sha256"], "build_tools": tools,
                  "source_date_epoch": pins["engine"]["commit_time_epoch"], "independent_builds_agree": True}
        if args.provenance_out:
            Path(args.provenance_out).write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    except (BuildError, subprocess.CalledProcessError, KeyError, OSError, ValueError) as error:
        print(f"build_wheel: FAIL: {error}", file=sys.stderr)
        return 1
    print(json.dumps(record, indent=2, sort_keys=True))
    print(f"WHEEL_SHA256={sha1}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
