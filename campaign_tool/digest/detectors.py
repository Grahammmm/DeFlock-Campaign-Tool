"""Deterministic, versioned detectors over extracted text units.

Input is a list of units ``{"locator": {...}, "text": "..."}`` (the intake
worker's ``units.jsonl`` shape, or ``units_from_text`` for plain text). Every
hit carries an exact locator (page/line, sheet/row, or line), the matched
excerpt, and the law-package rule it relates to when the package contains a
rule with that citation. Hits are leads for a reviewer and, when a model is
configured, inputs to the narrative. They are never findings, never
conclusions, and carry no confidence label of their own.

Run detectors on redacted units so that excerpts never hold personal data.
"""
import re

DETECTOR_VERSION = "digest-detectors-1"

# Citation fragments used to look up rule_ids in whatever package is loaded.
CITATIONS = {
    "retention": "2413",
    "sharing": "1798.90.55(b)",
    "immigration": "7284",
    "policy": "1798.90.51(b)",
    "end_user_policy": "1798.90.53",
    "access_records": "1798.90.52",
    "hearing": "1798.90.55(a)",
}
CHP_RETENTION_DAYS = 60

RETENTION_CONTEXT = re.compile(r"(?i)\b(retain|retention|retained|kept|keep|stored|store|purge[sd]?|delete[sd]?|destroy(?:ed)?|preserv\w*)\b")
DURATION = re.compile(r"(?i)\b(\d{1,4})\s*[- ]?\s*(day|days|month|months|year|years|week|weeks|hour|hours)\b")
SHARING = re.compile(r"(?i)\b(out[- ]of[- ]state|other states?|federal(?:\s+agenc\w+)?|nationwide|national\s+(?:lookup|search|network)|ICE|Immigration and Customs|Customs and Border|CBP|DHS|FBI|Homeland Security|U\.?S\.? Marshals?)\b")
IMMIGRATION = re.compile(r"(?i)\b(immigration|ICE|Immigration and Customs|CBP|Customs and Border)\b")
AUDIT = re.compile(r"(?i)\b(audit(?:s|ed|ing)?|audit\s+log|access\s+log|query\s+log|search\s+log|record\s+of\s+(?:each\s+)?access|log\s+of\s+(?:each\s+)?(?:access|query|search))\b")
HEARING = re.compile(r"(?i)\b(public\s+comment|public\s+hearing|regularly\s+scheduled\s+(?:public\s+)?meeting|board\s+(?:of\s+supervisors\s+)?meeting|city\s+council\s+meeting|open\s+meeting|agenda\s+item)\b")

# Civ. Code § 1798.90.51(b)/.53 usage-and-privacy-policy elements, as keyword families.
POLICY_ELEMENTS = {
    "authorized_purposes": re.compile(r"(?i)\b(authorized\s+purposes?|purpose\s+of\s+(?:the\s+)?(?:ALPR|system|program)|legitimate\s+law\s+enforcement\s+purpose)\b"),
    "authorized_users": re.compile(r"(?i)\b(authorized\s+(?:users?|personnel|employees?)|who\s+may\s+access|access\s+(?:is\s+)?(?:limited|restricted)\s+to)\b"),
    "training": re.compile(r"(?i)\btrain(?:ing|ed)\b"),
    "monitoring": re.compile(r"(?i)\b(monitor(?:ing|ed)?|audit(?:s|ed|ing)?|compliance\s+review)\b"),
    "sharing": re.compile(r"(?i)\b(shar(?:e|ed|ing)|disseminat\w+|disclos\w+|transfer(?:red|s)?)\b"),
    "security": re.compile(r"(?i)\b(security\s+(?:procedures?|practices?|safeguards?|measures?)|reasonable\s+security|unauthorized\s+access)\b"),
    "retention": re.compile(r"(?i)\b(retention|retain(?:ed)?|purge[sd]?|destroy(?:ed)?)\b"),
    "accuracy": re.compile(r"(?i)\b(accura\w+|correct(?:ion|ing)?|integrity)\b"),
    "posting": re.compile(r"(?i)\b(post(?:ed)?\s+(?:conspicuously\s+)?(?:on|to)\s+(?:(?:its|the|our|this)\s+)?(?:\w+\s+)?(?:internet\s+)?web\s*site|publicly\s+(?:available|posted)|available\s+(?:to\s+the\s+public|online))\b"),
    "policy_present": re.compile(r"(?i)\b(usage\s+and\s+privacy\s+policy|privacy\s+policy|usage\s+policy)\b"),
}


def rule_for(package, fragment):
    """Return the first rule in ``package`` whose citation contains ``fragment``."""
    for rule in (package or {}).get("rules", []):
        if fragment in rule.get("citation", ""):
            return rule
    return None


def units_from_text(text):
    """Plain text -> line units with ``{"line": n}`` locators."""
    return [{"locator": {"line": n}, "text": line} for n, line in enumerate(text.splitlines(), 1)]


def _lines(unit):
    """Yield (locator, line_text) with a line number added to page/row units."""
    locator = dict(unit.get("locator") or {})
    text = unit.get("text") or ""
    if "line" in locator or "\n" not in text:
        yield locator, text
        return
    for n, line in enumerate(text.splitlines(), 1):
        yield {**locator, "line": n}, line


def _hit(detector, rule, locator, excerpt, kind, detail=None):
    return {
        "detector": detector,
        "detector_version": DETECTOR_VERSION,
        "rule_id": rule["rule_id"] if rule else None,
        "citation": rule["citation"] if rule else None,
        "locator": locator,
        "excerpt": excerpt[:240],
        "kind": kind,
        "detail": detail or {},
    }


def _days(value, unit):
    n = int(value)
    unit = unit.lower().rstrip("s")
    return {"hour": n / 24, "day": n, "week": n * 7, "month": n * 30, "year": n * 365}[unit]


def detect_retention(units, package):
    rule = rule_for(package, CITATIONS["retention"])
    hits = []
    for unit in units:
        for locator, line in _lines(unit):
            if not RETENTION_CONTEXT.search(line):
                continue
            for match in DURATION.finditer(line):
                days = _days(match.group(1), match.group(2))
                comparison = "exceeds_chp_60_days" if days > CHP_RETENTION_DAYS else "within_chp_60_days"
                hits.append(_hit("retention_period", rule, locator, line.strip(), "comparison",
                                 {"stated": match.group(0), "stated_days": days, "reference_days": CHP_RETENTION_DAYS,
                                  "comparison": comparison,
                                  "note": "Veh. Code 2413 binds CHP; for other agencies this is a reference point, not the governing period."}))
    return hits


def detect_sharing(units, package):
    sharing_rule = rule_for(package, CITATIONS["sharing"])
    immigration_rule = rule_for(package, CITATIONS["immigration"])
    hits = []
    for unit in units:
        for locator, line in _lines(unit):
            if SHARING.search(line):
                hits.append(_hit("out_of_state_or_federal_sharing", sharing_rule, locator, line.strip(), "mention",
                                 {"terms": sorted({m.group(0).lower() for m in SHARING.finditer(line)})}))
            if IMMIGRATION.search(line) and immigration_rule:
                hits.append(_hit("immigration_enforcement_mention", immigration_rule, locator, line.strip(), "mention"))
    return hits


def detect_policy_elements(units, package):
    rule = rule_for(package, CITATIONS["policy"])
    end_user = rule_for(package, CITATIONS["end_user_policy"])
    found = {}
    for unit in units:
        for locator, line in _lines(unit):
            for element, pattern in POLICY_ELEMENTS.items():
                if element not in found and pattern.search(line):
                    found[element] = (locator, line.strip())
    hits = []
    for element, (locator, line) in found.items():
        hits.append(_hit("policy_element_present", rule, locator, line, "mention", {"element": element}))
    for element in POLICY_ELEMENTS:
        if element not in found:
            hits.append(_hit("policy_element_absent", rule, {"scope": "document"}, "", "absence",
                             {"element": element, "end_user_rule_id": end_user["rule_id"] if end_user else None,
                              "note": "Absence in extracted text is not proof the element is absent from the record; check unextracted pages."}))
    return hits


def detect_audit_log(units, package):
    rule = rule_for(package, CITATIONS["access_records"])
    hits = []
    for unit in units:
        for locator, line in _lines(unit):
            if AUDIT.search(line):
                hits.append(_hit("audit_log_mention", rule, locator, line.strip(), "mention"))
    if not hits:
        hits.append(_hit("audit_log_absent", rule, {"scope": "document"}, "", "absence",
                         {"note": "No audit/access-log language found in extracted text."}))
    return hits


def detect_public_hearing(units, package):
    rule = rule_for(package, CITATIONS["hearing"])
    hits = []
    for unit in units:
        for locator, line in _lines(unit):
            if HEARING.search(line):
                hits.append(_hit("public_hearing_mention", rule, locator, line.strip(), "mention"))
    if not hits:
        hits.append(_hit("public_hearing_absent", rule, {"scope": "document"}, "", "absence",
                         {"note": "No public-comment or hearing language found in extracted text."}))
    return hits


DETECTORS = [detect_retention, detect_sharing, detect_policy_elements, detect_audit_log, detect_public_hearing]


def run_detectors(units, package):
    """Run every detector; returns a flat list of hits in detector order."""
    hits = []
    for detector in DETECTORS:
        hits.extend(detector(units, package))
    return hits
