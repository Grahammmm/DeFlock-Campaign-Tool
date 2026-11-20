"""Choose scratch space for fixtures that use the host's PrivateDir."""
import os
from pathlib import Path

from campaign_tool.records.container_host import PrivateDir, Rejected


def private_fixture_parent() -> Path:
    for key in ("TMPDIR", "XDG_RUNTIME_DIR"):
        parent = os.environ.get(key)
        if not parent:
            continue
        try:
            # The parent need not be 0700; TemporaryDirectory creates the private leaf.
            # PrivateDir still checks ownership, permissions and symlinks along the path.
            with PrivateDir(parent, private=False):
                if os.access(parent, os.W_OK | os.X_OK):
                    return Path(parent)
        except (Rejected, OSError):
            continue
    return Path.home()