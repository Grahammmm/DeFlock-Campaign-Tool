"""Test-only: run `python -m campaign_tool.outbox` the way -m does, trusting a throwaway key.

Usage: python3 tests/outbox_entry_driver.py ALLOWED_SIGNERS OUTBOX-ARGS...
runpy executes the module as __main__ (not as campaign_tool.outbox), exactly as the
documented entry point runs; only the in-process approval trust anchor is patched.
"""
import runpy
import shutil
import sys
from pathlib import Path
from unittest import mock


def main(argv):
    from campaign_tool import approval
    signers, *arguments = argv
    with mock.patch.object(approval, "TRUSTED_SIGNERS", Path(signers)), \
            mock.patch.object(approval, "SSH_KEYGEN", Path(shutil.which("ssh-keygen") or "/usr/bin/ssh-keygen")), \
            mock.patch.object(approval, "_require_root", lambda path: None):
        sys.argv = ["campaign_tool.outbox", *arguments]
        runpy.run_module("campaign_tool.outbox", run_name="__main__", alter_sys=True)


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    main(sys.argv[1:])
