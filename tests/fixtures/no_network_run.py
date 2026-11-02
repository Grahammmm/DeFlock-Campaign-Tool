"""Run the offline CLI under a fail-closed socket-connect guard (no sitecustomize)."""
import runpy
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
attempted = False


class NetworkForbidden(BaseException):
    """Not an OSError: application fallback handling must not hide a connect."""


def deny_connect(*args, **kwargs):
    global attempted
    attempted = True
    raise NetworkForbidden("NETWORK_FORBIDDEN: offline CLI attempted a connect")


socket.socket.connect = deny_connect
socket.socket.connect_ex = deny_connect
try:
    if sys.argv[1:] == ["--guard-negative-control"]:
        with socket.socket() as connection:
            connection.connect(("127.0.0.1", 9))
    else:
        sys.argv = ["campaign_tool", *sys.argv[1:]]
        runpy.run_module("campaign_tool", run_name="__main__")
finally:
    if attempted:
        print("NETWORK_FORBIDDEN: offline CLI attempted a connect", file=sys.stderr)
        raise SystemExit(97)
