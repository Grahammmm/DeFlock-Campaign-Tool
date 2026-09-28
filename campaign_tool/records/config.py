"""Explicit, private JSON settings for records intake.

Relative JSON paths are relative to the config file; CLI paths are relative to
the working directory. Paths are literal: no home, environment or macro expansion.
No settings are discovered from the host or retained between invocations.
"""
import json
import os
from pathlib import Path
import re


def _strings(value, name):
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() or "\x00" in item
        for item in value
    ):
        raise ValueError(name + " must be an array of nonempty strings")
    return list(value)


def _path(value, base):
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError("paths must be nonempty strings")
    return os.path.abspath(base / value)


def load_config(path=None, *, roots=None, output=None, snapshot=None):
    """Load only an explicit file or an internal worker snapshot; CLI paths win."""
    if path is not None and snapshot is not None:
        raise ValueError("--config and --worker-config cannot be combined")
    base = Path.cwd()
    data = {}
    if path is not None:
        config_path = Path(path).absolute()
        with config_path.open(encoding="utf-8") as source:
            data = json.load(source)
        base = config_path.parent
    elif snapshot is not None:
        data = json.loads(snapshot)
    keys = {"roots", "output", "agency_patterns", "excluded_path_fragments"}
    if not isinstance(data, dict):
        raise ValueError("config must be a JSON object")
    unknown = set(data) - keys
    if unknown:
        raise ValueError("unknown config keys: " + ", ".join(sorted(unknown)))
    configured_roots = [_path(item, base) for item in _strings(data.get("roots", []), "roots")]
    configured_output = _path(data["output"], base) if "output" in data else None
    patterns = data.get("agency_patterns", {})
    if not isinstance(patterns, dict):
        raise ValueError("agency_patterns must be an object mapping names to regex strings")
    for name, pattern in patterns.items():
        if not isinstance(name, str) or not name.strip() or not isinstance(pattern, str) or not pattern.strip():
            raise ValueError("agency_patterns requires nonempty names and regex strings")
        try:
            re.compile(pattern, re.I)
        except re.error as error:
            raise ValueError("invalid agency pattern for " + name + ": " + str(error)) from error
    fragments = _strings(data.get("excluded_path_fragments", []), "excluded_path_fragments")
    if roots is not None:
        configured_roots = [_path(item, Path.cwd()) for item in _strings(roots, "--root")]
    if output is not None:
        configured_output = _path(output, Path.cwd())
    return {
        "roots": configured_roots,
        "output": configured_output,
        "agency_patterns": dict(patterns),
        "excluded_path_fragments": [fragment.replace("\\", "/") for fragment in fragments],
    }


def validate_config(config, command):
    """Reject incomplete or unsafe invocation paths before any output mutation."""
    if not config["output"]:
        raise ValueError("output is required via --output or --config")
    out = Path(config["output"])
    if out.resolve() != out:
        raise ValueError("output must not contain symlinks")
    if out.exists() and not out.is_dir():
        raise ValueError("output must be a directory")
    if command in {"inventory", "run"}:
        if not config["roots"]:
            raise ValueError("roots are required via --root or --config")
        for root in config["roots"]:
            p = Path(root)
            if p.resolve() != p or not p.is_dir():
                raise ValueError("missing root or symlink in root path: " + root)
