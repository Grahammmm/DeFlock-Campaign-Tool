"""File-based child interpreter fixtures for tests.

Children run a named script under ``tests/child_fixtures/`` instead of inline
source, so a command guard sees an ordinary script path. Each helper keeps the
caller's interpreter flags; every other ``subprocess`` argument passes through
unchanged.
"""
from pathlib import Path
import subprocess
import sys

DIRECTORY = Path(__file__).resolve().parent

# Written ahead of dynamic source so ``sys.path[0]`` matches ``python -c``.
_PREAMBLE = ("import sys as _fixture_sys\n"
             "if not _fixture_sys.flags.isolated: _fixture_sys.path[0] = ''\n"
             "del _fixture_sys\n")


def fixture_path(name):
    """Absolute path of ``tests/child_fixtures/<name>.py``."""
    path = DIRECTORY / (name + ".py")
    if path.parent != DIRECTORY or name.startswith("test") or not path.is_file():
        raise ValueError("unknown child fixture: " + name)
    return path


def fixture_argv(name, *args, isolated=False):
    """Interpreter argv for callers that pass their own argv onward."""
    flags = ["-B", "-I"] if isolated else ["-B"]
    return [sys.executable, *flags, str(fixture_path(name)), *args]


def run_fixture(name, *args, **kwargs):
    """``subprocess.run`` of a fixture under ``python -B``."""
    return subprocess.run(fixture_argv(name, *args), **kwargs)


def popen_fixture(name, *args, **kwargs):
    """``subprocess.Popen`` of a fixture under ``python -B``."""
    return subprocess.Popen(fixture_argv(name, *args), **kwargs)


def write_source(directory, source, stem="child"):
    """Write genuinely dynamic child source to ``directory/stem.py``."""
    path = Path(directory) / (stem + ".py")
    path.write_text(_PREAMBLE + source, encoding="utf-8")
    return path
