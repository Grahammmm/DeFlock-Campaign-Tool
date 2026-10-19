#!/usr/bin/env python3
"""Operator preflight for container/flock-ocr/Dockerfile (stdlib only).

Validates the build inputs, computes the Docker build context the same way
Docker's .dockerignore matcher does, requires that context to be exactly the
hash lock plus one wheel with the expected sha256, confirms the immutable base
image exists locally, and prints the docker build command. It never builds,
pulls, tags or runs anything; the operator runs the printed command.
"""
import argparse
import hashlib
import json
import os
import posixpath
import re
import subprocess
import sys
from pathlib import Path

RECIPE = Path(__file__).resolve().parent
WHEEL_RE = re.compile(r"deflock_campaign_tool-[^/]+-py3-none-any\.whl")
IMAGE_ID_RE = re.compile(r"sha256:[0-9a-f]{64}")
DIGEST_REF_RE = re.compile(r"[a-z0-9][a-z0-9._/:-]*@sha256:[0-9a-f]{64}")
COMMIT_RE = re.compile(r"[0-9a-f]{40}")
SHA256_RE = re.compile(r"[0-9a-f]{64}")


class PreflightError(Exception):
    pass


def validate_base_image(ref):
    """Accept only an image ID or a digest reference; mutable tags are rejected."""
    if IMAGE_ID_RE.fullmatch(ref) or DIGEST_REF_RE.fullmatch(ref):
        return ref
    raise PreflightError("BASE_IMAGE must be sha256:<64 hex> or name@sha256:<64 hex>, not a tag")


def _pattern_regex(pattern):
    """Translate one cleaned .dockerignore pattern like moby/patternmatcher."""
    out, i = "^", 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "*":
            if pattern[i + 1:i + 2] == "*":
                i += 1
                if pattern[i + 1:i + 2] == "/":
                    i += 1
                out += ".*" if i + 1 >= len(pattern) else "(.*/)?"
            else:
                out += "[^/]*"
        elif ch == "?":
            out += "[^/]"
        elif ch == "\\" and i + 1 < len(pattern):
            i += 1
            out += re.escape(pattern[i])
        elif ch == "[":
            end = pattern.find("]", i + 1)
            if end < 0:
                raise PreflightError(f"bad pattern {pattern!r}")
            out += pattern[i:end + 1].replace("[!", "[^", 1)
            i = end
        else:
            out += re.escape(ch)
        i += 1
    return re.compile(out + "$")


def read_dockerignore(path):
    rules = []
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        exclusion = line.startswith("!")
        if exclusion:
            line = line[1:].strip()
        line = posixpath.normpath(line).lstrip("/")
        rules.append((exclusion, _pattern_regex(line)))
    return rules


def is_ignored(relpath, rules):
    """Last matching rule wins; a rule also matches when it matches a parent dir."""
    parts = relpath.split("/")
    parents = ["/".join(parts[:n]) for n in range(1, len(parts))]
    ignored = False
    for exclusion, regex in rules:
        if exclusion != ignored:
            continue
        if regex.match(relpath) or any(regex.match(parent) for parent in parents):
            ignored = not exclusion
    return ignored


def context_files(context_dir, rules):
    """Every non-directory path Docker would transmit, relative and sorted."""
    sent = []
    for root, dirs, files in os.walk(context_dir, followlinks=False):
        dirs.sort()
        for name in sorted(files) + [d for d in dirs if os.path.islink(os.path.join(root, d))]:
            rel = os.path.relpath(os.path.join(root, name), context_dir).replace(os.sep, "/")
            if not is_ignored(rel, rules):
                sent.append(rel)
    return sorted(sent)


def check_context(context_dir, wheel_sha256):
    """Require the context to be exactly requirements.lock plus one hashed wheel."""
    context_dir = Path(context_dir)
    dist = context_dir / "dist"
    if not dist.is_dir() or dist.is_symlink():
        raise PreflightError("dist/ must be a real directory holding only the wheel")
    entries = sorted(p.name for p in dist.iterdir())
    if len(entries) != 1 or not WHEEL_RE.fullmatch(entries[0]):
        raise PreflightError(f"dist/ must hold exactly one engine wheel and nothing else (found {len(entries)} entries)")
    wheel = dist / entries[0]
    if wheel.is_symlink() or not wheel.is_file():
        raise PreflightError("the wheel must be a regular file")
    sent = context_files(context_dir, read_dockerignore(context_dir / ".dockerignore"))
    expected = ["dist/" + entries[0], "requirements.lock"]
    if sent != expected:
        raise PreflightError(f"build context would send {sent!r}, expected {expected!r}")
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    if digest != wheel_sha256:
        raise PreflightError("wheel sha256 does not match --wheel-sha256")
    return sent


def check_local_base(ref):
    """Confirm the immutable base is already local; never pulls."""
    result = subprocess.run(["docker", "image", "inspect", "--format", "{{json .}}", ref],
                            capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise PreflightError("base image is not present locally (preflight never pulls)")
    info = json.loads(result.stdout)
    if IMAGE_ID_RE.fullmatch(ref):
        if info.get("Id") != ref:
            raise PreflightError("local image ID does not equal BASE_IMAGE")
    elif ref not in (info.get("RepoDigests") or []):
        raise PreflightError("BASE_IMAGE digest is not among the local image's RepoDigests")


def build_command(args):
    return ["docker", "build", "--pull=false", "-f", str(RECIPE / "Dockerfile"),
            "--build-arg", f"BASE_IMAGE={args.base_image}",
            "--build-arg", f"ENGINE_COMMIT={args.engine_commit}",
            "--build-arg", f"WHEEL_SHA256={args.wheel_sha256}",
            "-t", args.tag, str(RECIPE)]


def main(argv=None):
    parser = argparse.ArgumentParser(description="Preflight for container/flock-ocr/Dockerfile")
    parser.add_argument("--base-image", required=True)
    parser.add_argument("--engine-commit", required=True)
    parser.add_argument("--wheel-sha256", required=True)
    parser.add_argument("--tag", default="<new tag>")
    parser.add_argument("--skip-docker", action="store_true", help="skip the local base image check")
    args = parser.parse_args(argv)
    try:
        validate_base_image(args.base_image)
        if not COMMIT_RE.fullmatch(args.engine_commit):
            raise PreflightError("--engine-commit must be 40 lowercase hex")
        if not SHA256_RE.fullmatch(args.wheel_sha256):
            raise PreflightError("--wheel-sha256 must be 64 lowercase hex")
        sent = check_context(RECIPE, args.wheel_sha256)
        if not args.skip_docker:
            check_local_base(args.base_image)
    except PreflightError as error:
        print(f"preflight: FAIL: {error}", file=sys.stderr)
        return 1
    print("preflight: OK; build context:", ", ".join(sent))
    print(" ".join(build_command(args)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
