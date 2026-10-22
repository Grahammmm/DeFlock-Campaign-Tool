"""Fresh-process no-effect probe; guards are installed before engine imports."""
from pathlib import Path
import socket
import subprocess
import sys
from unittest import mock


def forbidden(*args, **kwargs):
    raise RuntimeError("external effect forbidden in vision import/default probe")


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
with mock.patch.object(socket.socket, "connect", forbidden), \
        mock.patch.object(subprocess, "Popen", forbidden):
    from campaign_tool.records import ocr_stage
    from campaign_tool.records.extract import vision

    if ocr_stage.vision_backend({}) is not None:
        raise RuntimeError("vision unexpectedly enabled by default")
    if ocr_stage.build_stage("/nonexistent-synthetic-output", {}).fallback is not None:
        raise RuntimeError("stage default unexpectedly enables vision")
    if vision.needs_fallback({}):
        raise RuntimeError("empty receipt unexpectedly eligible")
    if ocr_stage.transcribe_png_main.__defaults__ != (ocr_stage.urllib_transport,):
        raise RuntimeError("unexpected bound transport default")
    if (vision.run_fallback.__kwdefaults__ or {}).get("runner") is not subprocess.run:
        raise RuntimeError("unexpected bound child runner default")
print("IMPORT_DEFAULTS_OFFLINE_OK")
