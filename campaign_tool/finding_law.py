"""Findings law check: every rule a published finding cites is in the campaign's law package and in force on its date.

A finding cites a rule through ``rule_ids`` or any non-null ``sources[].rule_id``. ``load_content`` (build) refuses
the first problem and ``content-check`` names them all, with the same text. Nothing is read when no rule is cited,
and a tree without ``campaign.json`` is not checked. This is not a legal-sufficiency review.
"""
from pathlib import Path

from . import law
from .content import ContentError


def jurisdiction(root):
    """``country-state`` from ``root/campaign.json`` (lowercase), or None when there is no campaign.json."""
    root = Path(root)
    if not (root / "campaign.json").is_file():
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


def law_problems(findings, package, jurisdiction):
    """One line per cited rule that the package lacks or that is not in force on the finding's event_date."""
    problems = []
    rules = {rule["rule_id"]: rule for rule in package["rules"]} if package is not None else {}
    for finding in findings:
        where, day = f"findings/{finding['slug']}", finding["event_date"]
        in_force = {rule["rule_id"] for rule in law.rules_in_force(package, day)} if package is not None else set()
        for rule_id in cited(finding):
            if package is None:
                problems.append(f"{where}: cites rule {rule_id} but there is no usable {jurisdiction} law package")
            elif rule_id not in rules:
                problems.append(f"{where}: rule {rule_id} is not in the {jurisdiction} law package")
            elif rule_id not in in_force:
                rule = rules[rule_id]
                problems.append(f"{where}: rule {rule_id} is not in force on {day} "
                                f"(effective {rule['effective_from']} to {rule['effective_to'] or 'open'})")
    return problems


def check_findings(root, findings):
    """Law problems for ``findings`` under ``root``; [] without reading anything when no rule is cited."""
    if not any(cited(finding) for finding in findings):
        return []
    name = jurisdiction(root)
    if name is None:
        return []
    try:
        package = law.load_package(name)
    except (OSError, ValueError):
        package = None
    return law_problems(findings, package, name)


def package_line(jurisdiction):
    """Informational ``law package <jur>: draft|reviewed|missing`` line (never a problem)."""
    return f"law package {jurisdiction}: {law.package_status(jurisdiction)[0]}"
