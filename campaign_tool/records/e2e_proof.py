"""records e2e-proof: replay ONE stored incoming message through every stage and write a proof report (REC-AUTO-11).

The message (an .eml already in a local mail store, attachments included) is copied into a fresh sandbox project
under ``--out``; the live project, mailbox and ledger are never opened for writing. Nothing is fetched, sent,
posted or published, and no credential or environment file is read. The stages run in order and each is timed:

  arrival_trigger -> save_mail -> extract -> digest_gates -> outputs (inventory, classify, policy comparison,
  gap proposals, follow-up drafts, next steps) -> next_steps check -> telegram_text (a file, never sent) ->
  console pages (Inbox, Documents, Findings, Deadlines, Drafts, Next Steps, Run health) -> deny one next step
  through the real panel POST (writes the decision to the queue journal and sends nothing).

The report lists every stage's output file and every manual step the run needed (there should be none). Output is
metadata only: counts, codes, relative file names. The pages and queue hold the private record text, so the sandbox
stays private on this host and is never to be committed.
"""
import argparse
import contextlib
import hashlib
import http.client
import io
import json
import os
import re
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

from . import approval_queue, arrival, arrival_summary, notify_text, run_orchestrator, run_stages

SCHEMA = "records-e2e-proof-v1"
MARKER = run_stages.SANDBOX_MARKER
PAGES = (("inbox", "/dashboard", "Inbox: mail received"), ("documents", "/documents", "Documents"),
         ("run_health", "/dashboard", "Stage status and run health"), ("findings", "/dashboard", "Findings"),
         ("deadlines", "/dashboard", "Deadlines"), ("drafts", "/dashboard", "Drafts"), ("next_steps", "/next-steps", "Next steps"))
NEXT_KINDS = ("next-social-post", "next-site-update", "next-followup-probe")
ARRIVAL_DONE = ("sync", "delta", "adoption", "portal")  # the live mailbox-side stages; a replay starts after them


class ProofError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _run_id(now):
    return "run_%s_%s" % (now.strftime("%Y%m%dT%H%M%SZ"), os.urandom(4).hex())


def pick_message(message, mail_store, nth):
    """The .eml to replay: ``message``, or the Nth newest .eml (1 = newest) under ``mail_store``."""
    if message:
        path = Path(message)
    else:
        if not mail_store:
            raise ProofError("MESSAGE_REQUIRED")
        found = sorted((p for p in Path(mail_store).rglob("*.eml") if p.is_file() and not p.is_symlink()),
                       key=lambda p: (p.stat().st_mtime_ns, str(p)), reverse=True)
        if not 1 <= nth <= len(found):
            raise ProofError("MESSAGE_NOT_FOUND")
        path = found[nth - 1]
    if path.suffix != ".eml" or not path.is_file() or path.is_symlink():
        raise ProofError("MESSAGE_NOT_FOUND")
    return path


class Timeline:
    def __init__(self, root):
        self.root, self.stages, self.failed = root, [], False

    def run(self, name, function, outputs=()):
        """Run one stage; an earlier failure marks the rest ``not_run``. Exception text is never kept."""
        entry = {"stage": name, "status": "not_run", "seconds": 0.0, "code": "EARLIER_STAGE_FAILED",
                 "detail": {}, "outputs": []}
        self.stages.append(entry)
        if self.failed:
            return entry
        started = time.monotonic()
        try:
            status, code, detail = function()
        except ProofError as error:
            status, code, detail = "fail", error.code, {}
        except Exception as error:  # noqa: BLE001 - content-free: only the exception class name is kept
            status, code, detail = "fail", "STAGE_" + type(error).__name__.upper(), {}
        entry.update(status=status, code=code, detail=detail, seconds=round(time.monotonic() - started, 3))
        entry["outputs"] = sorted(str(p.relative_to(self.root)) for p in (self.root / o for o in outputs)
                                  if p.exists())
        self.failed = status == "fail"
        return entry


def _stage(argv):
    """Run one run_stages adapter in-process; return (exit_code, parsed result line)."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = run_stages.main(argv)
    lines = out.getvalue().strip().splitlines()
    if len(lines) != 1:
        raise ProofError("STAGE_OUTPUT_INVALID")
    return code, json.loads(lines[0])


def _adapter(name, project, run_id, *extra):
    code, line = _stage([name, "--project", str(project), "--timeout", "120", "--run-id", run_id,
                         "--require-sandbox", *extra])
    detail = {"counts": line.get("counts"), "outcomes": line.get("outcomes", [])}
    if code != 0 or line.get("status") == "fail":
        return "fail", line.get("error_code") or "STAGE_FAILED", detail
    return line["status"], line.get("error_code"), detail


def _get(port, path):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    try:
        conn.request("GET", path, headers={"Host": "127.0.0.1:%d" % port})
        response = conn.getresponse()
        return response.status, response.read().decode("utf-8", "replace")
    finally:
        conn.close()


def _post(port, path, fields):
    body = urlencode(fields)
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    try:
        conn.request("POST", path, body=body, headers={
            "Host": "127.0.0.1:%d" % port, "Origin": "http://127.0.0.1:%d" % port,
            "Content-Type": "application/x-www-form-urlencoded", "Content-Length": str(len(body))})
        response = conn.getresponse()
        response.read()
        return response.status
    finally:
        conn.close()


def replay(message, out, campaign_root, *, label="replay", as_of=None, now=None):
    """Replay one message; return the proof report dict. ``out/label`` must not exist yet."""
    now = now or datetime.now(timezone.utc)
    base = Path(os.path.abspath(out)) / label
    if base.exists():
        raise ProofError("OUT_EXISTS")
    os.umask(0o077)
    for directory in (base, base / "project", base / "runtime", base / "inbox", base / "console"):
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    project, runtime = base / "project", base / "runtime"
    marker = project / MARKER
    marker.write_text("e2e-proof sandbox\n", encoding="utf-8")
    os.chmod(marker, 0o600)
    raw = Path(message).read_bytes()
    (base / "inbox" / "00.eml").write_bytes(raw)
    run_id = _run_id(now)
    state = {}
    line = Timeline(base)
    manual = []
    env_old = os.environ.get(run_orchestrator.TRIGGER_ENV)
    os.environ[run_orchestrator.TRIGGER_ENV] = "arrival"

    def arrival_trigger():
        ref = hashlib.sha256(b"e2e-proof\0" + raw).hexdigest()
        arrival.stamp(str(runtime), "inbox-watch", ref)
        if not arrival.has_work(str(runtime), stamps_only=True):
            raise ProofError("ARRIVAL_NOT_SEEN")
        batch = arrival.claim(str(runtime), run_id)
        if len(batch) != 1:
            raise ProofError("ARRIVAL_BATCH_WRONG")
        state["arrivals"] = batch
        return "ok", None, {"stamps_claimed": len(batch)}

    def save_mail():
        from .run import Pipeline
        arrival_summary.capture_baseline(str(project), run_id)
        report = Pipeline(project / "processing-v1").ingest_inbox(base / "inbox")
        from .portal.store import Queue  # the portal stage already ran live: an empty queue stands in
        Queue(project / "portal-queue-v1").close()
        if report.get("failures"):
            raise ProofError("SAVE_MAIL_FAILED")
        return "ok", None, {"failures": 0, "messages": report.get("messages") or 1}

    def outputs():
        return _adapter("outputs", project, run_id, "--campaign-root", str(campaign_root))

    def next_steps_check():
        rows = [r for r in approval_queue.load_queue(str(project)) if r.get("kind") in NEXT_KINDS]
        kinds = {k: sum(1 for r in rows if r["kind"] == k) for k in NEXT_KINDS}
        state["next_rows"] = rows
        if not rows:
            # A message with no gap or misuse finding has nothing to recommend: say so; it is not a manual step.
            return "warn", "NO_NEXT_STEPS_FOR_THIS_MESSAGE", {"queued_by_kind": kinds}
        return "ok", None, {"queued_by_kind": kinds}

    def telegram_text():
        stages = [run_orchestrator.stage_entry(s["stage"], s["status"] if s["status"] != "not_run" else "skipped",
                                               _counts(s), s["seconds"], s["code"] if s["status"] != "ok" else None)
                  for s in line.stages if s["stage"] in ("extract", "digest_gates", "outputs")]
        stages = [run_orchestrator.stage_entry(name, "skipped", None, None, "REPLAY_STARTS_AFTER_MAILBOX_SYNC")
                  for name in ARRIVAL_DONE] + stages + [run_orchestrator.pending_notify()]
        started = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        summary = {"received_by_kind": None, "pending_drafts": None}
        with contextlib.suppress(Exception):
            summary = arrival_summary.summarize(str(project), run_id)
        report = run_orchestrator.build_report(run_id, "arrival", started, stages, {}, 0, [],
                                               trigger="arrival", arrivals=state.get("arrivals", []),
                                               arrival_summary=summary)
        text = notify_text.compose(report, "records", 0)
        (base / "telegram-summary.txt").write_text(text + "\n", encoding="utf-8")
        (project / "run-report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        state["report"] = str(project / "run-report.json")
        return "ok", None, {"lines": len(text.splitlines()), "sent": False}

    def console():
        server = _build_server(project, run_id, state["report"], as_of)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        try:
            fetched, missing, bodies = {}, [], {}
            for key, path, heading in PAGES:
                if path not in bodies:
                    status, bodies[path] = _get(port, path)
                    if status != 200:
                        raise ProofError("CONSOLE_PAGE_%d" % status)
                    (base / "console" / (re.sub(r"\W+", "_", path.strip("/")) or "dashboard")).with_suffix(".html") \
                        .write_text(bodies[path], encoding="utf-8")
                if heading not in bodies[path]:
                    missing.append(key)
                fetched[key] = path
            if missing:
                raise ProofError("CONSOLE_SECTION_MISSING")
            decision = {"denied": None}
            rows = state.get("next_rows") or []
            if rows:
                token = re.search(r'name="csrf_token" value="([^"]+)"', bodies["/next-steps"])
                if not token:
                    raise ProofError("CONSOLE_NO_CSRF")
                ident = rows[0]["id"]
                code = _post(port, "/approvals/reject", {"csrf_token": token.group(1), "id": ident,
                                                        "reason": "e2e-proof deny check", "return": "/next-steps"})
                after = {r["id"]: r["status"] for r in approval_queue.load_queue(str(project))}
                if code != 303 or after.get(ident) != "rejected":
                    raise ProofError("CONSOLE_DENY_FAILED")
                decision["denied"] = 1
            return "ok", None, {"pages": sorted(fetched), "decision": decision, "sent": False}
        finally:
            server.shutdown()
            server.server_close()

    try:
        line.run("arrival_trigger", arrival_trigger, ["runtime/arrival"])
        line.run("save_mail", save_mail, ["project/processing-v1/ledger.sqlite"])
        line.run("extract", lambda: _adapter("extract", project, run_id), ["project/processing-v1/extract"])
        line.run("digest_gates", lambda: _adapter("digest_gates", project, run_id))
        line.run("outputs", outputs, [f"project/run-outputs/{run_id}/outcomes/{name}" for name in (
            "received-items.json", "comparison.json", "gap-proposals.json", "response-items.json",
            "register.json", "release-links.json", "textstate.json")] + ["project/approvals"])
        line.run("next_steps", next_steps_check, ["project/approvals"])
        line.run("telegram_text", telegram_text, ["telegram-summary.txt", "project/run-report.json"])
        line.run("console", console, ["console"])
    finally:
        if env_old is None:
            os.environ.pop(run_orchestrator.TRIGGER_ENV, None)
        else:
            os.environ[run_orchestrator.TRIGGER_ENV] = env_old
    for stage in line.stages:
        if stage["status"] in ("fail", "not_run"):
            manual.append({"stage": stage["stage"], "why": stage["code"]})
    report = {"schema": SCHEMA, "run_id": run_id, "sandbox": str(base), "message_sha256": hashlib.sha256(raw).hexdigest(),
              "sent": False, "published": False, "stages": line.stages, "manual_steps": manual,
              "total_seconds": round(sum(s["seconds"] for s in line.stages), 3),
              "result": "PASS" if not manual else "FAIL"}
    (base / "proof-report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    (base / "proof-report.md").write_text(render_markdown(report), encoding="utf-8")
    return report


def _counts(stage):
    counts = (stage.get("detail") or {}).get("counts") or {}
    return {key: counts.get(key, 0) for key in ("input", "output", "failed", "skipped")}


def _build_server(project, run_id, report_path, as_of):
    from . import admin_panel
    return admin_panel.build_server(str(project), run_id, actor="e2e-proof", port=0, run_report_path=report_path,
                                    as_of=as_of, system=False)


def render_markdown(report):
    rows = ["| Stage | Status | Seconds | Output files |", "| --- | --- | ---: | --- |"]
    for s in report["stages"]:
        rows.append("| %s | %s%s | %.3f | %s |" % (s["stage"], s["status"], " (%s)" % s["code"] if s["code"] else "",
                                                 s["seconds"], ", ".join("`%s`" % o for o in s["outputs"]) or "-"))
    manual = ["- %s: %s" % (m["stage"], m["why"]) for m in report["manual_steps"]] or ["- none"]
    return "\n".join(["# Records e2e proof %s" % report["run_id"], "", "Result: **%s**. Sent: no. Published: no." %
                      report["result"], "", *rows, "", "Total seconds: %.3f" % report["total_seconds"], "",
                      "Manual steps needed:", *manual, ""])


def main(argv=None):
    parser = argparse.ArgumentParser(prog="records e2e-proof", description=__doc__.split("\n")[0])
    parser.add_argument("--message", help="one stored incoming message (.eml, attachments inside)")
    parser.add_argument("--mail-store", help="directory holding stored .eml files (read only)")
    parser.add_argument("--nth", type=int, default=1, help="with --mail-store: 1 = newest message, 2 = next, ...")
    parser.add_argument("--campaign-root", required=True)
    parser.add_argument("--out", required=True, help="private directory for the sandbox and proof files")
    parser.add_argument("--label", default="replay-1")
    parser.add_argument("--as-of", help="YYYY-MM-DD clock for the deadline and health views")
    args = parser.parse_args(argv)
    try:
        path = pick_message(args.message, args.mail_store, args.nth)
        report = replay(path, args.out, args.campaign_root, label=args.label, as_of=args.as_of)
    except ProofError as error:
        print("records e2e-proof: refused (%s)" % error.code, file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2))
    return 0 if report["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
