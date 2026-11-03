#!/usr/bin/env python3
"""Write the TEST, LIVE and OCR-enabled LIVE container stage plans from one argv table.

Generates, in ``deploy/records-runtime/targets/``:

- ``test-container.json.in`` (mode ``test``, T11i) and ``live-container.json.in``
  (mode ``live``, T16). The two plans differ in ``mode`` and in three
  placeholders: TEST uses ``@CONTAINER_PROJECT_DIR_TEST@``,
  ``@ADOPTION_CONFIG_TEST@`` and ``@CONTAINER_APPROVAL_FILE_TEST@`` (AUD-DF1), so
  a TEST plan never shares the LIVE project; argv shape and timeouts are equal.
- ``live-ocr-container.json.in`` and ``live-ocr-vision-container.json.in``
  (mode ``live``, Phase 2 / T6). Identical to ``live-container.json.in`` except
  the extract stage: it gains ``--ocr`` (per-page Tesseract) and, in the vision
  plan, ``--ocr-vision-config @CONTAINER_OCR_VISION_CONFIG@`` (local vision
  fallback for empty or low-confidence pages), with the limits in
  ``OCR_EXTRACT``.

Each stage is
``docker exec -u CONTAINER_USER CONTAINER env PYTHONPATH=ENGINE
CONTAINER_PYTHON -m campaign_tool.records.run_stages STAGE --project PROJECT
...``.
``CONTAINER_PYTHON`` is the image's parser environment
(``/opt/campaign-tool/venv/bin/python`` in the pinned flock-ocr image, which
carries the hash-locked pypdf/openpyxl bundle) when present, else the system
``python3``; without pypdf/openpyxl the extract stage codes PDF/XLSX documents
``PARSER_MISSING`` and does not attempt them. The
adapter enforces its own
container-side deadline (``--timeout``, 10 s inside the host timeout) because
killing the ``docker exec`` client does not stop work in the container.
``tests/test_records_run_stages.py`` checks all committed templates are
current.
"""
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
EXEC = ["@DOCKER@", "exec", "-u", "@CONTAINER_USER@", "@CONTAINER@",
        "env", "PYTHONPATH=@CONTAINER_ENGINE_DIR@",
        "@CONTAINER_PYTHON@", "-B", "-m", "campaign_tool.records.run_stages"]
STAGES = (
    # name, timeout, on_fail, extra adapter arguments
    ("sync", 120, "stop", []),
    ("delta", 600, "stop", []),
    # Adoption is fail-closed per identity; other stages work on already-ledgered data.
    ("adoption", 900, "continue", ["--config", "@ADOPTION_CONFIG@"]),
    ("portal", 300, "continue", ["--approval-file", "@CONTAINER_APPROVAL_FILE@"]),
    # Native-text extraction (T11k) works through the backlog under its own
    # deadline and per-run document cap; the rest is counted as deferred.
    ("extract", 600, "stop", ["--max-documents", "200", "--document-timeout", "120"]),
    ("digest_gates", 120, "stop", []),
    ("outputs", 120, "stop", []),
)

MODES = {"test", "live"}
# AUD-DF1: a TEST plan gets its own project, adoption config and approval file.
TEST_NAMES = {f"@{key}@": f"@{key}_TEST@"
              for key in ("CONTAINER_PROJECT_DIR", "ADOPTION_CONFIG", "CONTAINER_APPROVAL_FILE")}

# Phase 2 (T6) extract stage of the OCR-enabled LIVE plans, by variant:
# (timeout, on_fail, extra adapter arguments). --document-timeout is the
# per-document wall-clock budget (T6-timeout A); --ocr-page-timeout bounds each
# pdftoppm / tesseract child and --ocr-vision-timeout each vision child. A page
# without a published receipt starts only when its whole allowance fits the
# budget left (2 x 180 s, plus 300 s with vision), so 1200 s admits an uncached
# 27B vision page. 1200 <= 1490 - DEADLINE_MARGIN (15) leaves ~275 s of the
# stage for the validator's re-parses and ledger work. The run stays bounded by
# RUN_BUDGET_SECONDS and the 47 min unit (render.py check_budget); OCR publishes
# per-page immutable receipts, so a run cut short resumes on the next slot.
OCR_LIMITS = ["--max-documents", "20", "--document-timeout", "1200", "--ocr-page-timeout", "180"]
OCR_EXTRACT = {
    "ocr": (1500, "stop", [*OCR_LIMITS, "--ocr"]),
    "ocr-vision": (1500, "stop", [*OCR_LIMITS, "--ocr-vision-timeout", "300", "--ocr",
                                  "--ocr-vision-config", "@CONTAINER_OCR_VISION_CONFIG@"]),
}
# Committed template file name -> (mode, OCR variant or None).
TEMPLATES = {
    "test-container.json.in": ("test", None),
    "live-container.json.in": ("live", None),
    "live-ocr-container.json.in": ("live", "ocr"),
    "live-ocr-vision-container.json.in": ("live", "ocr-vision"),
}


def plan(mode="test", ocr=None):
    if mode not in MODES:
        raise ValueError(f"mode: expected one of {sorted(MODES)}")
    # DOC-1: every LIVE plan OCRs scans (tesseract receipts) on the run that
    # processes them; TEST stays Phase 1 (scans remain AWAITING_OCR there).
    if mode == "live" and ocr is None:
        ocr = "ocr"
    if ocr is not None and (ocr not in OCR_EXTRACT or mode != "live"):
        raise ValueError(f"ocr: expected one of {sorted(OCR_EXTRACT)} on a live plan")
    stages = []
    for name, timeout, on_fail, extra in STAGES:
        if name == "extract" and ocr is not None:
            timeout, on_fail, extra = OCR_EXTRACT[ocr]
        argv = EXEC + [name, "--project", "@CONTAINER_PROJECT_DIR@", "--timeout", str(timeout - 10),
                       "--run-id", "{run_id}", *extra]
        if mode == "test":
            argv = [TEST_NAMES.get(item, item) for item in argv]
        stages.append({"name": name, "argv": argv, "timeout_seconds": timeout, "on_fail": on_fail})
    return {"target_config_version": 1, "target": "@TARGET_NAME@", "mode": mode, "stages": stages}


def render_text(mode="test", ocr=None):
    return json.dumps(plan(mode, ocr), indent=1) + "\n"

# Opt-in LIVE mail refresh for T17. Existing Phase 1/Phase 2 plans stay pinned.
LIVE_MAIL_TEMPLATE = "live-mail-container.json.in"


def live_mail_plan():
    value = plan("live")
    value["stages"][0]["argv"] += ["--sync-script", "@CONTAINER_SYNC_SCRIPT@"]
    return value


if __name__ == "__main__":
    for filename, (mode, ocr) in TEMPLATES.items():
        (HERE / filename).write_text(render_text(mode, ocr), encoding="utf-8")
    (HERE / LIVE_MAIL_TEMPLATE).write_text(
        json.dumps(live_mail_plan(), indent=1) + "\n", encoding="utf-8")
