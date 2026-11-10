"""Findings law check: every rule a published finding cites is in the campaign's law package and in force on its date.

A finding cites a rule through ``rule_ids`` or any non-null ``sources[].rule_id``. ``load_content`` (build) refuses
the first problem and ``content-check`` names them all, with the same text. Nothing is read when no rule is cited and
the campaign has no ``law/local-rules.json``, and a tree with no ``campaign.json`` entry at all is not checked (a
present but unusable entry, such as a directory, FIFO or dangling link, is refused, not treated as absent). A present local
overlay (organizer-transcribed city or county rules, DF1952-01) is validated even when nothing cites it; a finding may
cite its ``local-`` ids. This is not a legal-sufficiency review.
"""
import os
import stat
from pathlib import Path

from . import law
from .content import ContentError


UNUSABLE_CONFIG = "campaign.json: must resolve to a readable regular file"


def _config_present(root):
    """False only when ``root/campaign.json`` genuinely does not exist; a present entry must resolve to a regular file.

    Inspects without opening: a directory, FIFO, dangling or looping link, or link to a non-regular object is present
    but unusable and is refused with a fixed message that names no path or system error.
    """
    path = root / "campaign.json"
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError:
        raise ContentError(UNUSABLE_CONFIG) from None
    try:
        regular = stat.S_ISREG(os.stat(path).st_mode)
    except (OSError, ValueError):
        regular = False
    if not regular:
        raise ContentError(UNUSABLE_CONFIG) from None
    return True


def jurisdiction(root):
    """``country-state`` from ``root/campaign.json`` (lowercase); None only when campaign.json is genuinely absent.

    A present entry that is not (or does not resolve to) a regular file raises ``ContentError``, never None.
    """
    root = Path(root)
    if not _config_present(root):
        return None
    from .cli import read_config
    try:
        cfg = read_config(root)
    except (OSError, ValueError) as exc:
        raise ContentError(f"campaign.json: {exc}") from None
    return str(cfg.get("country", "US")).lower() + "-" + cfg["state"].lower()


def cited(finding):
    """Sorted rule ids a loaded finding cites (``rule_ids`` and every non-null ``sources[].rule_id``)."""
    from_sources = {source["rule_id"] for source in finding["sources"] if source.get("rule_id") is not None}
    return sorted(set(finding.get("rule_ids", [])) | from_sources)


LOAD = object()  # check(): read the campaign's agencies only when an overlay needs them


def _local_problem(where, rule_id, finding, versions):
    """The refusal for a cited local rule that has no in-force, in-scope version for the finding, else None."""
    agency, day = finding.get("agency_id"), finding["event_date"]
    if not versions:
        return f"{where}: local rule {rule_id} is not in {law.LOCAL_RULES_FILE}"
    if not any(agency in version["agency_ids"] for version in versions):
        return f"{where}: local rule {rule_id} does not apply to agency {agency}"
    if not any(rule["rule_id"] == rule_id
               for rule in law.rules_in_force({"rules": []}, day, local_rules=versions, agency_id=agency)):
        windows = ", ".join(f"{v['effective_from']} to {v['effective_to'] or 'open'}"
                            for v in versions if agency in v["agency_ids"])
        return f"{where}: local rule {rule_id} is not in force on {day} (effective {windows})"
    return None


def law_problems(findings, package, jurisdiction, local_rules=(), overlay_refused=False):
    """One line per cited rule that the package lacks or that is not in force on the finding's event_date.

    A cited id the state package does not hold but that starts ``local-`` is looked up in ``local_rules`` and must have
    a version naming the finding's agency whose window holds the event date. With ``overlay_refused`` the overlay's own
    refusal is already listed, so its ids are not listed again.
    """
    problems = []
    rules = {rule["rule_id"]: rule for rule in package["rules"]} if package is not None else {}
    for finding in findings:
        where, day = f"findings/{finding['slug']}", finding["event_date"]
        in_force = {rule["rule_id"] for rule in law.rules_in_force(package, day)} if package is not None else set()
        for rule_id in cited(finding):
            if rule_id not in rules and rule_id.startswith("local-"):
                if not overlay_refused:
                    line = _local_problem(where, rule_id, finding, [r for r in local_rules if r["rule_id"] == rule_id])
                    if line:
                        problems.append(line)
            elif package is None:
                problems.append(f"{where}: cites rule {rule_id} but there is no usable {jurisdiction} law package")
            elif rule_id not in rules:
                problems.append(f"{where}: rule {rule_id} is not in the {jurisdiction} law package")
            elif rule_id not in in_force:
                rule = rules[rule_id]
                problems.append(f"{where}: rule {rule_id} is not in force on {day} "
                                f"(effective {rule['effective_from']} to {rule['effective_to'] or 'open'})")
    return problems


def check(root, findings, agencies=LOAD):
    """``(problems, local_rules)`` for ``findings`` under ``root``; nothing is read when no rule is cited and no overlay exists.

    ``local_rules`` are all validated versions of ``root/law/local-rules.json`` ([] without one). A present overlay is
    validated even when no finding cites it, against ``agencies`` (default: the campaign's loaded agencies; None skips
    the agency-scope check for a caller whose agency file is itself refused and already named). An invalid overlay is
    one problem line naming its field.
    """
    present = law.local_rules_present(root)
    if not present and not any(cited(finding) for finding in findings):
        return [], []
    name = jurisdiction(root)
    if name is None:
        return ([f"{law.LOCAL_RULES_FILE}: needs a campaign.json to name the campaign's jurisdiction"]
                if present else []), []
    try:
        package = law.load_package(name)
    except (OSError, ValueError):
        package = None
    problems, local_rules = [], []
    if present:
        try:
            if agencies is LOAD:
                from .content import load_campaign_agencies  # local: this module imports content
                agencies = load_campaign_agencies(root)
            local_rules = law.load_local_rules(root, package, agencies)
        except ValueError as exc:
            problems.append(str(exc))
    return problems + law_problems(findings, package, name, local_rules, bool(problems)), local_rules


def check_findings(root, findings):
    """Law problems for ``findings`` under ``root``; [] without reading anything when no rule is cited and no overlay exists."""
    return check(root, findings)[0]


def package_line(jurisdiction):
    """Informational ``law package <jur>: draft|reviewed|missing`` line (never a problem; unreadable is missing)."""
    try:
        status = law.package_status(jurisdiction)[0]
    except OSError:
        status = "missing"
    return f"law package {jurisdiction}: {status}"
