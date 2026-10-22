"""Versioned, source-pinned policy knowledge with explicit applicability gaps.

This module selects documentary requirements; it does not decide whether conduct
was lawful or whether an agency violated a rule.
"""
from datetime import date
import re

_DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")


def _day(value, field, errors):
    if value is None:
        return None
    if not isinstance(value, str) or not _DATE.fullmatch(value):
        errors.append({"code": "invalid_date", "field": field, "value": value})
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        errors.append({"code": "invalid_date", "field": field, "value": value})
        return None


def _source(v):
    return v.get("source") if isinstance(v.get("source"), dict) else {}


def _scope_key(v):
    return (v.get("family_id"), v.get("agency_id"), v.get("jurisdiction"))


def validate_versions(versions):
    """Validate a collection, reporting malformed data, overlaps, and gaps."""
    issues, gaps = [], []
    if not isinstance(versions, list):
        return {"schema": "policy-version-validation-v1", "valid": False,
                "versions": [], "issues": [{"code": "versions_must_be_list"}], "gaps": []}
    ids = set()
    parsed = []
    for i, v in enumerate(versions):
        if not isinstance(v, dict):
            issues.append({"code": "version_must_be_object", "index": i})
            continue
        vid = v.get("version_id")
        if not isinstance(vid, str) or not vid:
            issues.append({"code": "version_id_required", "index": i})
        elif vid in ids:
            issues.append({"code": "duplicate_version_id", "version_id": vid})
        ids.add(vid)
        for field in ("family_id", "jurisdiction"):
            if not isinstance(v.get(field), str) or not v[field]:
                issues.append({"code": "required_field_missing", "field": field, "version_id": vid})
        roles = v.get("regulated_roles")
        if not isinstance(roles, list) or not roles or any(not isinstance(x, str) or not x for x in roles):
            issues.append({"code": "regulated_roles_required", "version_id": vid})
        source = _source(v)
        for field in ("source_id", "url", "locator", "verification_status"):
            if not isinstance(source.get(field), str) or not source[field]:
                issues.append({"code": "source_field_missing", "field": field, "version_id": vid})
        digest = source.get("content_sha256")
        if digest is not None and (not isinstance(digest, str) or not _HASH.fullmatch(digest)):
            issues.append({"code": "invalid_source_hash", "version_id": vid})
        if not isinstance(v.get("requirements", []), list):
            issues.append({"code": "requirements_must_be_list", "version_id": vid})
        for field in ("exceptions", "incorporated_terms"):
            if field in v and not isinstance(v[field], list):
                issues.append({"code": "field_must_be_list", "field": field, "version_id": vid})
        dates = {f: _day(v.get(f), f, issues) for f in
                 ("issued_at", "retrieved_at", "effective_from", "effective_to", "superseded_at")}
        if v.get("effective_to") is not None and v.get("superseded_at") is not None:
            issues.append({"code": "multiple_end_dates", "version_id": vid})
        end = dates["effective_to"] if dates["effective_to"] is not None else dates["superseded_at"]
        start = dates["effective_from"]
        if start and end and end <= start:
            issues.append({"code": "empty_effective_interval", "version_id": vid})
        if start is None:
            gaps.append({"code": "effective_date_unknown", "version_id": vid,
                         "field": "effective_from"})
        if end is None and v.get("supersession_status") != "not_superseded_as_of_retrieval":
            gaps.append({"code": "supersession_date_unknown", "version_id": vid})
        if dates["issued_at"] is None:
            gaps.append({"code": "issued_date_unknown", "version_id": vid})
        if dates["retrieved_at"] is None:
            gaps.append({"code": "retrieved_date_unknown", "version_id": vid})
        parsed.append((v, dates, end))

    # Different captures of the same source locator are a change signal, not proof
    # that the source or governing rule changed on a particular date.
    captures = {}
    for v, _, _ in parsed:
        s = _source(v)
        key = (s.get("source_id"), s.get("url"), s.get("locator"))
        digest = s.get("content_sha256")
        if key in captures and digest and captures[key] and digest != captures[key]:
            issues.append({"code": "source_content_changed", "source_id": key[0],
                           "url": key[1], "locator": key[2]})
        elif digest:
            captures[key] = digest

    for i, (a, ad, ae) in enumerate(parsed):
        for b, bd, be in parsed[i + 1:]:
            # A wildcard agency version intersects every agency-specific version.
            if (a.get("family_id") != b.get("family_id") or
                    a.get("jurisdiction") != b.get("jurisdiction") or
                    (a.get("agency_id") not in (None, "*", b.get("agency_id")) and
                     b.get("agency_id") not in (None, "*", a.get("agency_id")))):
                continue
            common_roles = set(a.get("regulated_roles", [])) & set(b.get("regulated_roles", []))
            if not common_roles:
                continue
            ast, bst = ad["effective_from"], bd["effective_from"]
            # Only assert overlap when both start boundaries are known.
            if ast and bst and (ae is None or bst < ae) and (be is None or ast < be):
                issues.append({"code": "overlapping_versions", "version_ids":
                               [a.get("version_id"), b.get("version_id")],
                               "regulated_roles": sorted(common_roles)})
    return {"schema": "policy-version-validation-v1", "valid": not issues,
            "versions": len(parsed), "issues": issues, "gaps": gaps}


def _pointer(v):
    s = _source(v)
    return {k: s.get(k) for k in ("source_id", "url", "locator", "content_sha256",
                                   "verification_status")}


def select_version(versions, *, agency_id, jurisdiction, regulated_role, event_at):
    """Select one version only when scope and temporal applicability are bounded."""
    checked = validate_versions(versions)
    base = {"schema": "policy-version-selection-v1", "version": None,
            "candidates": [], "gaps": [], "validation": checked}
    if not checked["valid"]:
        base.update(status="invalid_versions", gaps=checked["issues"] + checked["gaps"])
        return base
    err = []
    event = _day(event_at, "event_at", err)
    if err or event is None:
        base.update(status="event_date_unknown", gaps=err or [{"code": "event_date_required"}])
        return base
    scoped = []
    for v in versions:
        if v.get("agency_id") not in (None, "*", agency_id):
            continue
        if v.get("jurisdiction") != jurisdiction:
            continue
        if regulated_role not in v.get("regulated_roles", []):
            continue
        scoped.append(v)
    if not scoped:
        jurisdiction_match = any(v.get("agency_id") in (None, "*", agency_id) and
                                 v.get("jurisdiction") == jurisdiction for v in versions)
        role_match = any(v.get("agency_id") in (None, "*", agency_id) and
                         regulated_role in v.get("regulated_roles", []) for v in versions)
        code = "regulated_role_gap" if jurisdiction_match else ("jurisdiction_gap" if role_match else "applicability_gap")
        base.update(status=code, gaps=[{"code": code, "agency_id": agency_id,
                                        "jurisdiction": jurisdiction,
                                        "regulated_role": regulated_role}])
        return base
    candidates, unknown, outside = [], [], []
    for v in scoped:
        start = _day(v.get("effective_from"), "effective_from", [])
        end_value = v.get("effective_to") if v.get("effective_to") is not None else v.get("superseded_at")
        end = _day(end_value, "effective_to", [])
        if start is None:
            unknown.append(v)
        elif event < start:
            outside.append(v)
        elif end is not None and event >= end:
            outside.append(v)
        elif end is None and v.get("supersession_status") != "not_superseded_as_of_retrieval":
            unknown.append(v)
        elif end is None and v.get("retrieved_at") and event > date.fromisoformat(v["retrieved_at"]):
            # Open interval is only attested through retrieval, never extrapolated.
            unknown.append(v)
        else:
            candidates.append(v)
    if len(candidates) > 1:
        base.update(status="overlapping_versions", candidates=[v["version_id"] for v in candidates],
                    gaps=[{"code": "overlapping_versions"}])
    elif len(candidates) == 1:
        v = candidates[0]
        base.update(status="selected", version=v, candidates=[v["version_id"]],
                    source_pointer=_pointer(v))
    elif unknown:
        base.update(status="effective_date_unknown", candidates=[v["version_id"] for v in unknown],
                    gaps=[{"code": "applicability_date_unknown", "version_ids":
                           [v["version_id"] for v in unknown]}])
    else:
        base.update(status="not_in_force", candidates=[v["version_id"] for v in outside],
                    gaps=[{"code": "no_version_in_force", "event_at": event_at}])
    return base


def requirements_for(versions, *, agency_id, jurisdiction, regulated_role, event_at):
    """Return cited documentary requirements, exceptions and terms, not a verdict."""
    selection = select_version(versions, agency_id=agency_id, jurisdiction=jurisdiction,
                               regulated_role=regulated_role, event_at=event_at)
    result = {"schema": "policy-requirements-v1", "status": selection["status"],
              "selection": selection, "requirements": [], "exceptions": [],
              "incorporated_terms": [], "gaps": list(selection.get("gaps", [])),
              "legal_conclusion": False}
    v = selection.get("version")
    if v is None:
        return result
    pointer = selection.get("source_pointer", _pointer(v))
    for field in ("requirements", "exceptions", "incorporated_terms"):
        for item in v.get(field, []):
            result[field].append({**item, "version_id": v["version_id"],
                                  "source_pointer": item.get("source_pointer") or pointer})
    if not result["requirements"]:
        result["gaps"].append({"code": "requirements_not_transcribed", "version_id": v["version_id"]})
    return result
