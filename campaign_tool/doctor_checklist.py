"""Read-only readiness checklist built from ``doctor_report`` output.

Every ``todo`` row names the one command or decision that clears it and who takes it. The checklist never runs a
fix, never marks anything reviewed and is not a legal status.
"""
import json
import shlex

ORGANIZER, LAW_REVIEWER, MAINTAINER = "organizer", "independent law reviewer", "maintainer"
TOOL = "python3 -m campaign_tool"


def _has_control(text):
    return any(ord(c) < 32 or 127 <= ord(c) < 160 or c in "\u2028\u2029" for c in text)


def _stale_contacts_next(report, d):
    found = report["stale_contacts"]
    count = len(found) + int(report.get("stale_contacts_more") or 0)
    shown = [str(entry.get("agency_id")) for entry in found[:3]]
    if any(_has_control(agency_id) for agency_id in shown):  # only the ids this advice would display
        raise ValueError("checklist stale contact agency_id contains a control character; repair kit/agencies.json")
    ids = ", ".join(shown) + (", …" if count > 3 else "")
    return (f"Re-check {count} contact(s): `{TOOL} kit-verify-agency --directory {d} --agency-id ID "
            f"--records-email EMAIL --source-url URL --checked-on YYYY-MM-DD` for {ids}; give the contact you "
            f"checked: --records-email EMAIL, --records-url URL or --portal URL (at least one is required)")


def _stale_kits_next(report, d):
    meetings = sorted({str(entry.get("meeting")) for entry in report["stale_comment_kits"]})
    # shlex.quote keeps shell word boundaries, not terminal display: a no-id meeting's filename stem can carry ESC or
    # a line break. Every distinct meeting is displayed, so every one is checked; never echo, strip or rewrite it.
    if any(_has_control(m) for m in meetings):
        raise ValueError("checklist stale meeting identifier contains a control character; repair content/meetings/")
    return "Re-stage with corrected findings: " + "; ".join(
        f"`{TOOL} meetings-stage --directory {d} --id {shlex.quote(m)} --replace`" for m in meetings)


def _select_agencies_next(report, d):
    # `kit` keeps an existing kit/agencies.json as it is, so rerunning it never selects anything there.
    if not report.get("agency_list_exists"):
        return f"Select agencies: `{TOOL} kit --directory {d}`"
    return (f"Select at least one agency in kit/agencies.json: `{TOOL} kit-agency select --directory {d} --id ID` "
            f"(ids are listed in that file; add one with `{TOOL} kit-agency add --directory {d} --id ID --name NAME "
            f"--kind KIND`), then rerun `{TOOL} kit --directory {d}`")


def _kit_ok(key):
    return lambda r: None if "kit_error" in r else bool(r[key])


# key, ok(report) -> True/False/None (None = info row), label, next(report, d) for a todo, owner
RULES = (
    ("config_readable", lambda r: r["config_readable"] is True, "Campaign config readable",
     lambda r, d: "Fix campaign.json so it is readable JSON", ORGANIZER),
    ("jurisdiction_verified", lambda r: r["jurisdiction_verified"] is True, "Jurisdiction verified",
     lambda r, d: "Check county and state against official sources, then set `jurisdiction_verified: true` in "
                  "campaign.json", ORGANIZER),
    ("reviewed_law_package", lambda r: r["reviewed_law_package"] is True, "Law package reviewed",
     lambda r, d: "A reviewer who did not write the package follows docs/LAW-PACKAGES.md 'Moving a package from "
                  "draft to reviewed'; the tool cannot do this step", LAW_REVIEWER),
    ("law_package_status", lambda r: None, lambda r: f"Law package status: {r['law_package_status']}", None, None),
    ("law_package_error", lambda r: False, "Law package readable",
     lambda r, d: f"Law package unreadable: {r['law_package_error']}; report it to the maintainer", MAINTAINER),
    ("kit_error", lambda r: False, "Kit agency list readable",
     lambda r, d: "Fix or recreate kit/agencies.json (not readable JSON)", ORGANIZER),
    ("agencies_selected", _kit_ok("agencies_selected"),
     lambda r: ("Agencies selected: unknown until kit/agencies.json is readable" if "kit_error" in r
                else f"Agencies selected ({r['agencies_selected']})"),
     _select_agencies_next, ORGANIZER),
    ("kit_built", lambda r: bool(r["kit_built"]), "Kit built",
     lambda r, d: f"Build the kit: `{TOOL} kit --directory {d}`", ORGANIZER),
    ("stale_contacts", lambda r: not r["stale_contacts"], "Agency contacts recently checked",
     _stale_contacts_next, ORGANIZER),
    ("stale_comment_kits", lambda r: not r["stale_comment_kits"], "Meeting comment kits cite current corrections",
     _stale_kits_next, ORGANIZER),
    ("public_deployment_checked", lambda r: r["public_deployment_checked"] is True, "Public deployment checked",
     lambda r, d: "Follow docs/CLOUDFLARE.md; publishing stays a human decision", ORGANIZER),
    ("newsletter_checked", lambda r: r["newsletter_checked"] is True, "Newsletter checked",
     lambda r, d: "Follow docs/NEWSLETTER.md; sending stays a human decision", ORGANIZER),
    ("safe_to_send_automatically", lambda r: None,
     "Automatic sending: stays off by design: every send is approved by a person.", None, None),
)
KNOWN = {rule[0] for rule in RULES} | {"next_actions", "stale_contacts_more", "production_ready"}


def _short(value, limit=80):
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _control_free_directory(directory):
    """Return the directory as text, refusing terminal controls and line separators.

    Shell quoting keeps shell argument boundaries but not terminal display: an ESC sequence or a line break in the
    path would reach the terminal or forge an extra checklist line. Same policy as kit_agencies._has_control; the
    message never echoes the path."""
    raw = str(directory)
    if _has_control(raw):
        raise ValueError("checklist directory contains a control character; use a control-free path to this campaign")
    return raw


def checklist(report, directory, agency_list_exists=False, *, git_row=None):
    """Rows in RULES order, then unknown keys as info, then the derived production_ready row.

    agency_list_exists: kit/agencies.json is present, so the agencies_selected fix is a kit-agency selection."""
    raw = _control_free_directory(directory)
    # Shell quoting alone does not keep argparse from treating a leading '-' as an option.
    d = shlex.quote("./" + raw if raw.startswith("-") else raw)
    facts = dict(report, agency_list_exists=agency_list_exists)
    rows = []
    for key, ok, label, todo, owner in RULES:
        if key not in report:
            continue
        result = ok(facts)
        state = "info" if result is None else "ok" if result else "todo"
        rows.append({"key": key, "state": state, "label": label(facts) if callable(label) else label,
                     "next": todo(facts, d) if state == "todo" else None,
                     "owner": owner if state == "todo" else None})
    for key in report:
        if key not in KNOWN:
            rows.append({"key": key, "state": "info", "label": f"Other: {key} = {_short(report[key])}",
                         "next": None, "owner": None})
    if git_row is not None:  # from git_exposure.git_exposure_row; absent outside any git work tree
        rows.append(git_row)
    todos = sum(row["state"] == "todo" for row in rows)
    rows.append({"key": "production_ready", "state": "info", "owner": None, "next": None,
                 "label": (f"Production ready: not yet, {todos} to do; ready only when every todo above is done"
                           if todos else "Production ready: no todo remains above; a person still decides")})
    return rows


def render(report, directory, as_json=False, agency_list_exists=False):
    from .git_exposure import git_exposure_row
    _control_free_directory(directory)  # before the Git lookup, in text and JSON alike
    rows = checklist(report, directory, agency_list_exists, git_row=git_exposure_row(directory))
    if as_json:
        return json.dumps(rows, indent=2, ensure_ascii=False)
    lines = []
    for row in rows:
        lines.append(f"[{row['state']}]".ljust(7) + row["label"])
        if row["state"] == "todo":
            lines.append(f"       → {row['next']} ({row['owner']})")
    todos = sum(row["state"] == "todo" for row in rows)
    oks = sum(row["state"] == "ok" for row in rows)
    lines.append(f"{todos} to do · {oks} ok · not a legal status")
    return "\n".join(lines)
