"""Records engine command line; services remain explicit and separately enabled."""
import argparse
import importlib
import os
import sys

COMMANDS = {
    "doctor": ("runtime", "main"),
    "validate-findings": "gates.validate_findings",
    "reconcile-coverage": "gates.reconcile_coverage",
    "collect-reviews": "gates.collect_reviews",
    "batch-report": "gates.batch_report",
    "version": "release_manifest",
    "scan-public": "public_scan",
    "run": ("run", "main"),
    "status": ("run", "status_main"),
    "arrival": ("arrival", "main"),
    "adopt-mail": ("mail_adoption", "main"),
    "mail-poll": ("mail_poll", "main"),
    "approve": ("publish", "approve_main"),
    "approvals": "approval_queue",
    "followups": "followup_clock",
    "mail-drafts": ("mail_drafts", "main"),
    "publish": ("publish", "publish_main"),
    "reopen": ("publish", "reopen_main"),
    "health": ("schedule", "health_main"),
    "pipeline-health": ("pipeline_health", "main"),
    "release-links": ("outcomes.release_links", "main"),
    "playbook": ("playbook", "main"),
    "items": ("item_tiers", "main"),
    "agency-policies": ("agency_policies", "main"),
    "first-request": ("first_request", "main"),
    "model-advice": ("model_advice", "main"),
    "panel": ("admin_panel", "main"),
    "schedule": ("schedule", "schedule_main"),
}


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=COMMANDS)
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.command == "run":
        from .runtime import platform_status, require
        # Windows cannot even import the Linux fcntl-based intake module.
        if not platform_status()["supported"]:
            require()
            return 2
    target = COMMANDS[args.command]
    module_name, function = target if isinstance(target, tuple) else (target, "main")
    module = importlib.import_module("." + module_name, __package__)
    sys.argv = [sys.argv[0] + " " + args.command, *args.arguments]
    return getattr(module, function)()
