"""records campaign-readiness: read-only inventory of what keeps a campaign's records pipeline from running alone.

Reports states and fixed codes only (never record text, names, addresses, signatures, URLs or input paths). It is an
inventory, not permission to send, publish or approve; presence is not validity. Nothing is written.
"""
import argparse
import json
import os
import re
import stat
import sys

from .. import content, law
from . import agency_retention, analysis_config, campaign_config, followup_clock

SCHEMA = "records-campaign-readiness-v1"
FIELDS = ("schema_version", "campaign_id", "display_name", "law_package", "public_base_url",
          "records_mailbox_label", "timezone")
DEGRADED = {"READINESS_CONFIG_INVALID", "READINESS_SETTINGS_INVALID", "READINESS_ASKS_UNAVAILABLE",
            "READINESS_COVERAGE_UNAVAILABLE", "READINESS_PROFILE_INVALID", "READINESS_METHOD_UNAVAILABLE",
            "READINESS_RETENTION_INVALID", "READINESS_LAW_UNAVAILABLE", "READINESS_PORTAL_APPROVAL_UNAVAILABLE"}
REFUSALS = ("READINESS_PROJECT_UNAVAILABLE", "READINESS_CAMPAIGN_ROOT_UNAVAILABLE", "READINESS_AGENCIES_INVALID")
_SLUG = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
_EDIT = "a person edits P/%s after reading docs/RECORDS-FOLLOWUP-ENGINE.md (nothing here changes it)"
_JSON = "python3 -m json.tool P/%s"
_CLI = "python3 -m campaign_tool.records "


class _Missing(Exception):
    pass


class _Bad(Exception):
    pass


def _read(path, limit):
    try:
        return followup_clock._read_bounded(path, limit)
    except FileNotFoundError:
        raise _Missing from None
    except (OSError, ValueError):
        raise _Bad from None


def _json(raw):
    try:
        return followup_clock._strict(raw)
    except followup_clock.SettingsError:
        raise _Bad from None


def _probe(path, want):
    """present/missing/invalid by lstat only; no symlink at any level, the leaf is never opened."""
    if not isinstance(path, str) or not path or "\0" in path or len(path) > 4096 or not os.path.isabs(path):
        return "invalid"
    parts = os.path.abspath(path).split(os.sep)[1:]
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        mode = os.stat(parts[-1], dir_fd=fd, follow_symlinks=False).st_mode
        return "present" if (stat.S_ISDIR(mode) if want == "dir" else stat.S_ISREG(mode)) else "invalid"
    except (FileNotFoundError, NotADirectoryError):
        return "missing"
    except (OSError, IndexError):
        return "invalid"
    finally:
        os.close(fd)


def _issue(issues, code, field, kind, remediation, agency=None):
    row = {"code": code, "field": field, "kind": kind, "remediation": remediation}
    if agency:
        row["agency"] = agency
    issues.append(row)


def _config(project, issues):
    """campaign.json v1 field states and the validated config (or None)."""
    fields = dict.fromkeys(FIELDS, "missing")
    try:
        doc = _json(_read(os.path.join(project, "campaign.json"), 64 * 1024))
    except _Missing:
        _issue(issues, "READINESS_CONFIG_MISSING", "campaign.json", "human",
               "create P/campaign.json with records project-init (a new project) or by hand, then " + _JSON % "campaign.json")
        return fields, None
    except _Bad:
        doc = None
    try:
        config = campaign_config.load(project)
    except ValueError:
        config = None
    if config is None:
        for key in (doc if isinstance(doc, dict) else FIELDS):
            if key in fields:
                fields[key] = "invalid"
        _issue(issues, "READINESS_CONFIG_INVALID", "campaign.json", "human",
               "correct P/campaign.json to schema_version 1 and the documented keys: " + _JSON % "campaign.json")
        return fields, None
    for key in doc:
        fields[key] = "present"
    for key in FIELDS:
        if fields[key] == "missing":
            _issue(issues, "READINESS_CAMPAIGN_FIELD_MISSING", "campaign." + key, "human",
                   "add %s to P/campaign.json if this campaign needs it (docs/MULTI-CAMPAIGN.md), then " % key + _JSON % "campaign.json")
    return fields, config


def _law(config, issues):
    if config is None:
        return "invalid" if any(i["code"] == "READINESS_CONFIG_INVALID" for i in issues) else "missing"
    try:
        status = law.load_package(config.law_package)["status"]
    except (OSError, ValueError, KeyError, RecursionError):
        status = "invalid" if law.package_path(config.law_package).is_file() else "missing"
    if status in ("invalid", "missing"):
        _issue(issues, "READINESS_LAW_UNAVAILABLE", "law_package", "command",
               _CLI + "law-package check jurisdictions/<package>")
    elif status != "reviewed":
        _issue(issues, "READINESS_LAW_DRAFT", "law_package", "command",
               _CLI + "law-package check jurisdictions/<package> (a human reviewer still attests it)")
    return status


def _agencies(root):
    try:
        doc = _json(_read(os.path.join(root, "content", "agencies.json"), content.MAX_JSON_BYTES))
    except (_Missing, _Bad):
        raise _Refusal("READINESS_AGENCIES_INVALID") from None
    if not isinstance(doc, list) or len(doc) > 500:
        raise _Refusal("READINESS_AGENCIES_INVALID")
    ids = []
    for entry in doc:
        if (not isinstance(entry, dict) or not isinstance(entry.get("agency_id"), str)
                or not content.AGENCY_ID.fullmatch(entry["agency_id"]) or not isinstance(entry.get("name"), str)
                or entry["agency_id"] in ids):
            raise _Refusal("READINESS_AGENCIES_INVALID")
        if entry.get("selected", True) is True:
            ids.append(entry["agency_id"])
    return sorted(ids)


class _Refusal(Exception):
    pass


def _table(project, folder, agency):
    base = os.path.join(project, folder, agency)
    state = _probe(base + ".tsv", "file")
    if state == "missing":
        state = {"present": "proposed", "missing": "missing"}.get(_probe(base + ".proposed.tsv", "file"), "invalid")
    return state


def _retention(project, ids, issues):
    try:
        _read(os.path.join(project, "agency-retention.csv"), 5 * 1024 * 1024)
    except _Missing:
        return dict.fromkeys(ids, "missing")
    except _Bad:
        _issue(issues, "READINESS_RETENTION_INVALID", "retention", "command", _CLI + "agency-retention P/agency-retention.csv")
        return dict.fromkeys(ids, "invalid")
    rows, problems = agency_retention.load(os.path.join(project, "agency-retention.csv"))
    if problems:
        _issue(issues, "READINESS_RETENTION_INVALID", "retention", "command", _CLI + "agency-retention P/agency-retention.csv")
    have = {row["agency"] for row in rows}
    return {i: "present" if i in have else ("invalid" if problems else "missing") for i in ids}


def _profiles(analysis_root, ids, issues):
    """Per agency (profile, method) states from root/profiles/*.json; exact records_agency_id, never a guessed slug."""
    if analysis_root is None:
        return {i: ("not_configured", "not_configured") for i in ids}
    unknown = dict.fromkeys(ids, ("invalid", "invalid"))
    pdir = os.path.join(os.path.abspath(analysis_root), "profiles")
    if _probe(pdir, "dir") != "present":
        if _probe(os.path.abspath(analysis_root), "dir") != "present":
            _issue(issues, "READINESS_PROFILE_INVALID", "analysis_profile", "human",
                   _CLI + "campaign-readiness --project P --analysis-root R")
            return unknown
        return dict.fromkeys(ids, ("missing", "missing"))
    try:
        names = sorted(n for n in os.listdir(pdir) if n.endswith(".json"))
    except OSError:
        names = None
    if names is None or len(names) > 1000:
        _issue(issues, "READINESS_PROFILE_INVALID", "analysis_profile", "human", _CLI + "analysis-config --root R --slug S --check-only")
        return unknown
    found, unreadable = {}, False
    for name in names:
        proposed = name.endswith(".proposed.json")
        slug = name[:-len(".proposed.json" if proposed else ".json")]
        try:
            profile = _json(_read(os.path.join(pdir, name), 1024 * 1024))
            agency = profile["records_agency_id"]
            if not isinstance(agency, str) or not _SLUG.fullmatch(slug):
                raise _Bad
        except (_Bad, _Missing, KeyError, TypeError):
            unreadable = True
            continue
        try:
            analysis_config._validate(profile, slug)
            shaped = True
        except ValueError:
            shaped = False
        found.setdefault(agency, []).append((proposed, shaped, profile))
    if unreadable:
        _issue(issues, "READINESS_PROFILE_INVALID", "analysis_profile", "command", _CLI + "analysis-config --root R --slug S --check-only")
    out = {}
    for agency in ids:
        entries = found.get(agency, [])
        confirmed = [e for e in entries if not e[0]]
        candidates = [e for e in entries if e[0]]
        if len(confirmed) > 1 or any(not e[1] for e in confirmed):
            out[agency] = ("invalid", "missing")
            _issue(issues, "READINESS_PROFILE_INVALID", "analysis_profile", "command",
                   _CLI + "analysis-config --root R --slug S --check-only", agency)
        elif confirmed:
            out[agency] = ("present", _method(confirmed[0][2], agency, issues))
        elif candidates:
            out[agency] = ("proposed", "missing")
            if not all(e[1] for e in candidates):
                _issue(issues, "READINESS_PROFILE_INCOMPLETE", "analysis_profile", "human",
                       "a person completes the proposed profile, then " + _CLI + "analysis-config --root R --slug S --check-only", agency)
        else:
            out[agency] = ("invalid", "missing") if unreadable else ("missing", "missing")
    return out


def _method(profile, agency, issues):
    review = profile["review"]
    states = (_probe(review["method_dir"], "dir"), _probe(review["method_python"], "file"))
    if "invalid" in states:
        _issue(issues, "READINESS_METHOD_UNAVAILABLE", "analysis_method", "command",
               _CLI + "analysis-config --root R --slug S --check-only", agency)
        return "invalid"
    return "present" if states == ("present", "present") else "missing"


def collect(project, *, analysis_root=None, portal_approval_file=None):
    project = os.path.abspath(project)
    if _probe(project, "dir") != "present":
        raise _Refusal("READINESS_PROJECT_UNAVAILABLE")
    try:
        pointer = _read(os.path.join(project, ".campaign-root"), 4096).decode("utf-8")
    except (_Missing, _Bad, UnicodeDecodeError):
        raise _Refusal("READINESS_CAMPAIGN_ROOT_UNAVAILABLE") from None
    root = pointer.rstrip("\n")
    if not root or "\n" in root or "\0" in root or not os.path.isabs(root) or _probe(root, "dir") != "present":
        raise _Refusal("READINESS_CAMPAIGN_ROOT_UNAVAILABLE")
    ids = _agencies(root)
    issues = []
    fields, config = _config(project, issues)
    law_status = _law(config, issues)
    settings = followup_clock.load_settings(project)
    if settings["invalid"]:
        _issue(issues, "READINESS_SETTINGS_INVALID", "followup_settings", "command", _JSON % "followup-settings.json")
    retention = _retention(project, ids, issues)
    profiles = _profiles(analysis_root, ids, issues)
    signature = "unknown" if settings["invalid"] else ("present" if settings["default"].get("signature") else "missing")
    if signature == "missing":
        _issue(issues, "READINESS_SIGNATURE_MISSING", "signature", "human", _EDIT % "followup-settings.json")
    rows = []
    for agency in ids:
        autonomy = "invalid" if settings["invalid"] else followup_clock._setting(settings, agency)["autonomy"]
        row = {"agency": agency, "autonomy": autonomy, "signature": signature,
               "asks": _table(project, "agency-asks", agency), "coverage": _table(project, "followup-coverage", agency),
               "analysis_profile": profiles[agency][0], "analysis_method": profiles[agency][1], "retention": retention[agency]}
        rows.append(row)
        if autonomy == "off":
            _issue(issues, "READINESS_AUTONOMY_OFF", "autonomy", "human", _EDIT % "followup-settings.json", agency)
        for key, folder, code in (("asks", "agency-asks", "READINESS_ASKS_UNAVAILABLE"),
                                  ("coverage", "followup-coverage", "READINESS_COVERAGE_UNAVAILABLE")):
            if row[key] == "invalid":
                _issue(issues, code, key, "human", "replace the unreadable P/%s/ID.tsv with a regular file" % folder, agency)
            elif row[key] != "present":
                _issue(issues, "READINESS_%s_%s" % (key.upper(), row[key].upper()), key, "human",
                       "a person writes and confirms P/%s/ID.tsv (a proposal is not confirmed)" % folder, agency)
        if row["retention"] == "missing":
            _issue(issues, "READINESS_RETENTION_MISSING", "retention", "command",
                   "add a cited row for ID to P/agency-retention.csv, then " + _CLI + "agency-retention P/agency-retention.csv", agency)
        if analysis_root is not None and row["analysis_profile"] in ("missing", "proposed"):
            _issue(issues, "READINESS_PROFILE_%s" % row["analysis_profile"].upper(), "analysis_profile", "command",
                   _CLI + "analysis-config --root R --slug S --check-only", agency)
    if analysis_root is None:
        _issue(issues, "READINESS_ANALYSIS_ROOT_UNSPECIFIED", "analysis_root", "command",
               _CLI + "campaign-readiness --project P --analysis-root R")
    approval = "not_configured"
    if portal_approval_file is not None:
        approval = _probe(os.path.abspath(portal_approval_file), "file")
        if approval == "invalid":
            _issue(issues, "READINESS_PORTAL_APPROVAL_UNAVAILABLE", "portal_approval", "human",
                   "check that the supplied approval file is a regular file with no symlink in its path")
        elif approval == "missing":
            _issue(issues, "READINESS_PORTAL_APPROVAL_MISSING", "portal_approval", "human",
                   "approval is a human step; see docs/RECORDS-PORTAL.md (presence is not validity)")
    else:
        _issue(issues, "READINESS_PORTAL_APPROVAL_UNSPECIFIED", "portal_approval", "command",
               _CLI + "campaign-readiness --project P --portal-approval-file F")
    return {"schema": SCHEMA, "campaign_fields": fields, "law_package": law_status, "portal_approval": approval,
            "agencies": rows, "issues": issues}


def render(report):
    lines = [report["schema"] + " (inventory only; not permission to send, publish or approve)"]
    lines.append("campaign_fields: " + " ".join("%s=%s" % kv for kv in report["campaign_fields"].items()))
    lines.append("law_package: " + report["law_package"])
    lines.append("portal_approval: %s (presence only)" % report["portal_approval"])
    for row in report["agencies"]:
        lines.append("agency %s" % row["agency"] + "".join(" %s=%s" % (k, v) for k, v in row.items() if k != "agency"))
    for item in report["issues"]:
        lines.append("%s %s%s: %s %s" % (item["code"], item.get("agency", "-") + " ", item["field"], item["kind"] + ":", item["remediation"]))
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(prog="records campaign-readiness", description=__doc__.split("\n")[0])
    parser.add_argument("--project", required=True)
    parser.add_argument("--analysis-root", help="read-only: the host-owned analysis root holding profiles/*.json")
    parser.add_argument("--portal-approval-file", help="read-only: presence of this file is reported, never its contents")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = collect(args.project, analysis_root=args.analysis_root, portal_approval_file=args.portal_approval_file)
    except _Refusal as refusal:
        print(refusal.args[0], file=sys.stderr)
        return 2
    sys.stdout.write(json.dumps(report, indent=2) + "\n" if args.json else render(report))
    return 1 if any(i["code"] in DEGRADED for i in report["issues"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
