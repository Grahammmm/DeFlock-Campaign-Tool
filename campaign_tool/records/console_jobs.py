"""Read-only Jobs screen (``/jobs``) for the private loopback console.

Lists every scheduled job the deploy/ unit files define, with what systemd reports about it. Only
``systemctl --user show`` is run; nothing is started, stopped, enabled or changed. Missing, skipped,
held or unknown is never shown as passed: only an installed, enabled unit whose last run reported
``Result=success`` with exit status 0 is "ok".

The job list is discovered from ``deploy/**/*.timer|*.path`` (``.in`` templates included), so another
geography that ships its own units needs no code change. Extra units may be added with the console's
``--timer-unit`` option. Schedules are read from systemd at run time, never from this module.

The optional GitHub Actions section needs a read-only token from ``DEFLOCK_GITHUB_READ_TOKEN`` or an
owner-only file named by ``DEFLOCK_GITHUB_READ_TOKEN_FILE``, and the repository slug from
``DEFLOCK_GITHUB_REPO`` (``owner/name``). Without them it says "not configured". The token is never shown.
"""
import json
import os
import re
import stat
import subprocess
import urllib.request
from pathlib import Path

from . import console_layout
from .outcomes.reading import esc

DEPLOY_ROOT = Path(__file__).resolve().parents[2] / "deploy"
UNIT_SUFFIXES = (".timer", ".path")
UNIT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,100}\.(timer|path)")
SERVICE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,100}\.service")
PROPS = ("LoadState", "UnitFileState", "ActiveState", "Unit", "LastTriggerUSec", "NextElapseUSecRealtime")
SERVICE_PROPS = ("LoadState", "ActiveState", "Result", "ExecMainStatus")
ENABLED_OK = frozenset({"enabled", "enabled-runtime", "static", "generated", "alias"})
TOKEN_ENV, TOKEN_FILE_ENV, REPO_ENV, FLOWS_ENV = (
    "DEFLOCK_GITHUB_READ_TOKEN", "DEFLOCK_GITHUB_READ_TOKEN_FILE", "DEFLOCK_GITHUB_REPO",
    "DEFLOCK_GITHUB_WORKFLOWS")
DEFAULT_WORKFLOWS = ("check", "camera-source-review", "production")
REPO_SLUG = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}")
MAX_TOKEN = 4096
SHOWN = {"ok": "ok", "failed": "FAILED", "skipped": "SKIPPED (not a pass)", "disabled": "DISABLED",
         "never": "installed, never run", "not_installed": "NOT INSTALLED", "unknown": "UNKNOWN (not a pass)"}


def discover(deploy_root=DEPLOY_ROOT, extra=()):
    """Sorted unique trigger-unit names from unit files under deploy/, plus any explicit extras."""
    names = set()
    try:
        for path in Path(deploy_root).rglob("*"):
            name = path.name[:-3] if path.name.endswith(".in") else path.name
            if name.endswith(UNIT_SUFFIXES) and UNIT_NAME.fullmatch(name) and path.is_file():
                names.add(name)
    except OSError:
        pass
    names.update(unit for unit in extra if UNIT_NAME.fullmatch(unit))
    return sorted(names)


def system_runner(args):
    """Run a read-only systemctl command; returns (returncode, stdout). Raises OSError if unavailable."""
    done = subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=5)
    return done.returncode, done.stdout


def _show(runner, unit, props):
    """Parsed property dict, or None when systemctl is unavailable or its output is not usable."""
    try:
        _, out = runner(["show", unit, "--no-pager", "--property=" + ",".join(props)])
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if not isinstance(out, str):
        return None
    found = {}
    for line in out.splitlines():
        key, sep, value = line.partition("=")
        if sep and key in props:
            found[key] = value.strip()
    return found if "LoadState" in found else None


def _stamp(value):
    return "never" if value in ("", "n/a", "0") else value


def inspect(unit, runner=system_runner):
    """One job row. ``state`` is a key of SHOWN; only ``ok`` may be rendered as passing."""
    row = {"unit": unit, "state": "unknown", "installed": "unknown", "enabled": "unknown", "last_trigger": "unknown",
           "last_result": "unknown", "next_run": "unknown", "detail": "systemctl unavailable or output not understood"}
    props = _show(runner, unit, PROPS)
    if props is None:
        return row
    if props["LoadState"] == "not-found":
        return dict(row, state="not_installed", installed="no", enabled="no", last_trigger="n/a", last_result="n/a",
                    next_run="n/a", detail="unit file is not installed for this user")
    if props["LoadState"] != "loaded":
        return dict(row, detail="load state %s" % props["LoadState"][:40])
    row.update(installed="yes", enabled=props.get("UnitFileState") or "unknown",
               last_trigger=_stamp(props["LastTriggerUSec"]) if "LastTriggerUSec" in props else "n/a (path unit)",
               next_run=_stamp(props["NextElapseUSecRealtime"]) if "NextElapseUSecRealtime" in props
               else "n/a (event driven)")
    service = props.get("Unit") or ""
    if not SERVICE_NAME.fullmatch(service):
        service = unit.rsplit(".", 1)[0] + ".service"
    svc = _show(runner, service, SERVICE_PROPS)
    if svc is None or svc["LoadState"] != "loaded":
        return dict(row, detail="triggered service state not readable")
    result, status = svc.get("Result", ""), svc.get("ExecMainStatus", "")
    row["last_result"] = "result=%s exit=%s" % (result or "unknown", status or "unknown")
    if row["enabled"] not in ENABLED_OK:
        return dict(row, state="disabled", detail="installed but not enabled; it will not run on its own")
    if status == "73":
        return dict(row, state="skipped", detail="exit 73: a held lock skipped the slot")
    if "failed" in (props.get("ActiveState"), svc.get("ActiveState")) or result not in ("", "success"):
        return dict(row, state="failed", detail="last run failed (result %s, exit %s)" % (result[:40] or "unknown", status[:10] or "unknown"))
    if result != "success" or status != "0":
        if row["last_trigger"] == "never" and result in ("", "success"):
            return dict(row, state="never", detail="no run recorded yet")
        return dict(row, detail="no complete result recorded")
    if row["last_trigger"] == "never":
        return dict(row, state="never", detail="no run recorded yet")
    return dict(row, state="ok", detail="last run succeeded")


def load_token(environ=None):
    """(token or None, reason). The token is never rendered."""
    environ = os.environ if environ is None else environ
    token = environ.get(TOKEN_ENV, "").strip()
    if token:
        return token, ""
    path = environ.get(TOKEN_FILE_ENV, "").strip()
    if not path:
        return None, "not configured"
    try:
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > MAX_TOKEN:
            return None, "not configured (token file must be a regular owner-only file)"
        with open(path, encoding="utf-8") as handle:
            token = handle.read(MAX_TOKEN).strip()
    except (OSError, UnicodeError):
        return None, "not configured (token file unreadable)"
    return (token, "") if token else (None, "not configured (token file empty)")


def default_fetch(repo, token):
    """Latest workflow runs as parsed JSON. The only network call; tests inject a fake."""
    request = urllib.request.Request(
        "https://api.github.com/repos/%s/actions/runs?per_page=50" % repo,
        headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "deflock-console-jobs"})
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read(2 * 1024 * 1024))


def github_runs(environ=None, fetch=default_fetch):
    """{'state': 'not_configured'|'unavailable'|'ok', 'reason': str, 'rows': [...]}."""
    environ = os.environ if environ is None else environ
    token, reason = load_token(environ)
    repo = environ.get(REPO_ENV, "").strip()
    if token is None:
        return {"state": "not_configured", "rows": [], "reason": reason}
    if not REPO_SLUG.fullmatch(repo):
        return {"state": "not_configured", "rows": [], "reason": "not configured (set %s to owner/name)" % REPO_ENV}
    flows = tuple(w.strip() for w in environ.get(FLOWS_ENV, "").split(",") if w.strip()) or DEFAULT_WORKFLOWS
    try:
        latest = {}
        for run in fetch(repo, token)["workflow_runs"]:
            latest.setdefault(str(run.get("name", "")), run)  # newest first
    except Exception:  # network, auth, shape: a category only, never the detail
        return {"state": "unavailable", "rows": [], "reason": "GitHub unavailable or response not understood"}
    rows = []
    for flow in flows:
        run = latest.get(flow)
        if run is None:
            rows.append({"workflow": flow, "state": "unknown", "text": "no run found", "when": ""})
            continue
        concluded = str(run.get("conclusion") or run.get("status") or "unknown")
        rows.append({"workflow": flow, "state": "ok" if concluded == "success" else
                     "failed" if concluded == "failure" else "unknown",
                     "text": concluded, "when": str(run.get("updated_at") or "unknown")})
    return {"state": "ok", "rows": rows, "reason": ""}


def render(units, runner=system_runner, github=None):
    out = ["<h1>Jobs</h1><p>Read-only. Shows what systemd reports for each scheduled job; nothing here starts, "
           "stops or changes a job. Missing, skipped, held or unknown is never shown as passed.</p>"]
    if not units:
        out.append('<p class="diag" role="status">No unit files found under deploy/: job list unknown.</p>')
    else:
        out.append("<table><thead><tr><th>Job</th><th>State</th><th>Installed</th><th>Enabled</th>"
                   "<th>Last trigger</th><th>Last result</th><th>Next run</th></tr></thead><tbody>")
        for unit in units:
            row = inspect(unit, runner)
            cls = "" if row["state"] == "ok" else ' class="overdue"' if row["state"] == "failed" else ' class="muted"'
            out.append("<tr><td><code>%s</code></td><td%s>%s<br><small>%s</small></td><td>%s</td><td>%s</td>"
                       "<td>%s</td><td>%s</td><td>%s</td></tr>" % (
                           esc(unit), cls, esc(SHOWN[row["state"]]), esc(row["detail"]), esc(row["installed"]),
                           esc(row["enabled"]), esc(row["last_trigger"]), esc(row["last_result"]), esc(row["next_run"])))
        out.append("</tbody></table>")
    out.append("<h2>GitHub Actions</h2>")
    github = github or {"state": "not_configured", "rows": [], "reason": "not configured"}
    if github["state"] != "ok":
        out.append("<p>GitHub Actions: %s.</p>" % esc(github["reason"]))
    else:
        out.append("<table><thead><tr><th>Workflow</th><th>Latest run</th><th>Updated</th></tr></thead><tbody>")
        for row in github["rows"]:
            text = "passed" if row["state"] == "ok" else ("FAILED" if row["state"] == "failed" else
                                                          "UNKNOWN (not a pass): " + row["text"])
            out.append("<tr><td>%s</td><td>%s</td><td>%s</td></tr>" % (esc(row["workflow"]), esc(text), esc(row["when"])))
        out.append("</tbody></table>")
    return console_layout.shell("Jobs", "\n".join(out), active=None)


def _disabled(args):
    raise OSError("system checks disabled")


def serve(handler):
    if handler.path != "/jobs":
        return False
    hooks = getattr(handler.server, "jobs_hooks", {})
    options = handler.server.options
    runner = hooks.get("runner") or (system_runner if options.get("system", True) else _disabled)
    github = github_runs(hooks.get("environ"), hooks.get("github_fetch", default_fetch))
    handler._send(200, render(discover(extra=options.get("timers", ())), runner, github))
    return True
