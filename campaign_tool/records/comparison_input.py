"""Compile a custodian-filled records-comparison-worksheet-v1 into records-agency-fact-sheets-v1 (private, review required).

A field is emitted only when attested, applicability-confirmed, cited by a fact that is checked in the store NOW, and every key the
typed WORKSHEET_FIELDS table requires is filled; otherwise it is omitted and stays unknown. Never derives a value. Writes only the
new --out file; refusals name a code and an agency/field/key position, never a value, source id or path.
"""
import argparse
import json
import os
from pathlib import Path
import sys

from . import comparison_worksheet as ws
from .outcomes import comparison as cmp

ROLES, EVERY_KEY = ("operator", "end_user", "public_agency"), "always"
FIELD_KEYS = {"state", "counterevidence", "custodian_attested", "applicability_confirmed", "citations", "value", "descriptors", "candidates"}
CITE_KEYS = {"fact_id", "source_id", "page", "locator"}
FLAGS = ("receipt_confirmed", "calendar_confirmed", "extension_notice_qualified", "applicability_confirmed")
DATES = ("received_on", "calendar_start", "calendar_end", "determination_on", "extension_notice_on")
REQUEST_KEYS = {*FLAGS, *DATES, "agency_id", "request_id", "holidays", "determination_state", "extension_claimed", "extension_days",
                "citations", "counterevidence", "register_source", "submitted_on", "submitted_note", "register_issues", "hint"}
EMIT_REQUEST_KEYS = REQUEST_KEYS - {"agency_id", "register_source", "submitted_on", "submitted_note", "register_issues", "hint"}


class Refused(Exception):
    def __init__(self, code, **where):
        super().__init__(code)
        self.code, self.where = code, where


def _typed(spec, key, value):
    """Refusal code for a filled value of the wrong type for its table entry, else None."""
    kind = spec["type"]
    if value is None:
        return None
    if kind == "bool":
        return None if type(value) is bool else "predicate_non_bool"
    if kind == "string":
        return None if type(value) is str and value.strip() else "blank_string"
    if kind == "enum":
        return None if type(value) is str and value in spec["values"] else {"policy_modality": "bad_modality", "event_type": "bad_event_type"}.get(key, "bad_enum")
    if type(value) is bool:
        return "days_bool"
    return "days_non_int" if type(value) is not int else "days_negative" if value < 0 else None


def _day(value, **where):
    try:
        return cmp._day(value).isoformat()
    except (ValueError, TypeError):
        raise Refused("bad_date", **where) from None


class Compiler:
    def __init__(self, tool, store, inventory_agencies, pairs):
        self.tool, self.store, self.agencies, self.pairs = tool, store, set(inventory_agencies), set(pairs)
        self._checked = {}

    def checked(self, agency_id):
        if agency_id not in self._checked:
            try:
                mapped, unmapped, _ = ws._candidates(self.tool, self.store, agency_id)
            except ws.WorksheetError as exc:
                raise Refused(str(exc)) from None
            self._checked[agency_id] = {x["fact_id"]: x["page"] for rows in (*mapped.values(), unmapped) for x in rows}
        return self._checked[agency_id]

    def cites(self, agency_id, items, **where):
        """Worksheet citations -> [{source_id: fact_id, locator: 'page N'}]; the page comes from the checked row read now."""
        if type(items) is not list:
            raise Refused("citation_shape", **where)
        result = []
        for item in items:
            fid = item.get("fact_id", item.get("source_id")) if type(item) is dict else None
            if type(fid) is not str or set(item) - CITE_KEYS:
                raise Refused("citation_shape", **where)
            checked = self.checked(agency_id)
            if fid not in checked or checked[fid] is None:
                raise Refused("citation_not_checked", **where)
            if item.get("page", checked[fid]) != checked[fid] or item.get("locator", "page %d" % checked[fid]) != "page %d" % checked[fid]:
                raise Refused("citation_page_mismatch", **where)
            result.append({"source_id": fid, "locator": "page %d" % checked[fid]})
        return result

    def field(self, agency_id, name, entry, where):
        """(emitted fact or None, incomplete) for one worksheet field entry."""
        spec = cmp.WORKSHEET_FIELDS[name]
        if type(entry) is not dict or set(entry) - FIELD_KEYS:
            raise Refused("unknown_field_key", **where)
        state, value = entry.get("state"), entry.get("value") or {}
        if state not in spec["fact"]["state"]["values"]:
            raise Refused("bad_state", **where, key="state")
        for key in ("custodian_attested", "applicability_confirmed"):
            if type(entry.get(key)) is not bool:
                raise Refused("flag_non_bool", **where, key=key)
        if type(value) is not dict:
            raise Refused("unknown_value_key", **where, key="value")
        for number, (key, item) in enumerate(value.items(), 1):
            if key not in spec["value"]:
                raise Refused("unknown_value_key", **where, key="#%d" % number)
            code = _typed(spec["value"][key], key, item)
            if code:
                raise Refused(code, **where, key=key)
        low, high = value.get("min_days"), value.get("max_days")
        if low is not None and high is not None and low > high:
            raise Refused("inverted_bounds", **where, key="min_days")
        if value.get("has_website") is False and value.get("conspicuous_online") is not None:
            raise Refused("online_without_website", **where, key="conspicuous_online")
        citations = self.cites(agency_id, entry.get("citations", []), **where, key="citations")
        counter = self.cites(agency_id, entry.get("counterevidence", []), **where, key="counterevidence")
        if entry["custodian_attested"] and not citations:
            raise Refused("attested_without_citation", **where, key="citations")
        observed = state == "observed"
        need = [k for k, s in spec["value"].items() if s["required"] == EVERY_KEY or (s["required"].startswith("only when") and value.get("has_website") is True)]
        missing = observed and (any(value.get(k) is None for k in need)
                                or ("min_days" in spec["value"] and low is None and high is None))
        if missing or not (entry["custodian_attested"] and entry["applicability_confirmed"] and citations):
            return None, bool(missing)
        fact = {"state": state, "applicability_confirmed": True, "citations": citations}
        if counter:
            fact["counterevidence"] = counter
        if observed:
            fact["value"] = {k: v for k, v in value.items() if v is not None}
        return fact, False

    def request(self, number, item, seen):
        where = {"request": number}
        if type(item) is not dict or set(item) - REQUEST_KEYS:
            raise Refused("unknown_request_key", **where)
        aid, rid = item.get("agency_id"), item.get("request_id")
        if type(aid) is not str or type(rid) is not str:
            raise Refused("unknown_request", **where)
        if rid in seen:
            raise Refused("duplicate_request", **where)
        seen.add(rid)
        if (aid, rid) not in self.pairs:
            raise Refused("request_wrong_agency" if rid in {r for _, r in self.pairs} else "unknown_request", **where)
        out = {"request_id": rid}
        for key in FLAGS:
            if item.get(key) is not None and type(item[key]) is not bool:
                raise Refused("flag_non_bool", **where, key=key)
        for key in DATES:
            if item.get(key) is not None:
                out[key] = _day(item[key], **where, key=key)
        if item.get("receipt_confirmed") is True and "received_on" not in out:
            raise Refused("receipt_without_date", **where, key="received_on")
        for key in ("calendar_start", "calendar_end"):
            if item.get("calendar_confirmed") is True and key not in out:
                raise Refused("calendar_without_" + key[9:], **where, key=key)
        if item.get("extension_notice_qualified") is True and "extension_notice_on" not in out:
            raise Refused("extension_without_notice_date", **where, key="extension_notice_on")
        state = item.get("determination_state")
        if state not in (None, "made", "pending") or (state == "made" and "determination_on" not in out):
            raise Refused("bad_determination", **where, key="determination_state")
        holidays = item.get("holidays")
        if holidays is not None:
            if type(holidays) is not list:
                raise Refused("bad_date", **where, key="holidays")
            out["holidays"] = [_day(d, **where, key="holidays") for d in holidays]
        if item.get("extension_claimed") is not None and type(item["extension_claimed"]) is not bool:
            raise Refused("flag_non_bool", **where, key="extension_claimed")
        days = item.get("extension_days")
        if days is not None and (type(days) is not int or days < 0):
            raise Refused("days_bool" if type(days) is bool else "days_non_int" if type(days) is not int else "days_negative", **where, key="extension_days")
        out.update({k: item[k] for k in (*FLAGS, "determination_state", "extension_claimed", "extension_days") if item.get(k) is not None})
        for key in ("citations", "counterevidence"):
            cited = self.cites(aid, item.get(key, []), **where, key=key)
            if cited:
                out[key] = cited
        return aid, out


def compile_sheet(doc, run_id, as_of, tool, store, inventory_agencies, pairs):
    """(fact-sheets document, counts {agency_id: (emitted, incomplete)}); raises Refused."""
    if doc.get("run_id") != run_id or doc.get("as_of") != as_of:
        raise Refused("run_mismatch")
    if type(doc.get("reviewer_role")) is not str or not doc["reviewer_role"].strip():
        raise Refused("missing_reviewer_role")
    if not doc.get("review_date"):
        raise Refused("missing_review_date")
    _day(doc["review_date"], key="review_date")
    build = Compiler(tool, store, inventory_agencies, pairs)
    sheets, counts = {}, {}
    for number, agency in enumerate(doc["agencies"], 1):
        where = {"agency": number}
        aid = agency.get("agency_id") if type(agency) is dict else None
        if aid not in build.agencies:
            raise Refused("unknown_agency", **where)
        if aid in sheets:
            raise Refused("duplicate_agency", **where)
        roles = agency.get("regulated_roles")
        if type(roles) is not list or any(r not in ROLES for r in roles):
            raise Refused("bad_role", **where, key="regulated_roles")
        if agency.get("jurisdiction") not in (None, "us-ca"):
            raise Refused("wrong_jurisdiction", **where, key="jurisdiction")
        if type(agency.get("facts")) is not dict:
            raise Refused("unknown_field", **where)
        facts, counts[aid] = {}, [0, 0]
        for number, (name, entry) in enumerate(sorted(agency["facts"].items()), 1):
            if name not in cmp.WORKSHEET_FIELDS:
                raise Refused("unknown_field", **where, field="#%d" % number)
            fact, incomplete = build.field(aid, name, entry, {**where, "field": name})
            counts[aid][0] += fact is not None
            counts[aid][1] += incomplete
            if fact:
                facts[name] = fact
        sheets[aid] = {"agency_id": aid, "jurisdiction": agency.get("jurisdiction"), "regulated_roles": roles, "facts": facts, "requests": []}
    items, seen = (doc.get("requests") or {}).get("items", []), set()
    if type(items) is not list:
        raise Refused("unknown_request_key")
    for number, item in enumerate(items, 1):
        aid, out = build.request(number, item, seen)
        sheets.setdefault(aid, {"agency_id": aid, "requests": []})["requests"].append(out)
    for sheet in sheets.values():
        sheet["requests"].sort(key=lambda r: r["request_id"])
    return {"schema": cmp.INPUT_SCHEMA, "agencies": [sheets[a] for a in sorted(sheets)]}, counts


def _stubs(sheets, agencies, pairs):
    """Validation copy: ID-only stubs for inventory agencies/requests the worksheet did not supply."""
    by_id = {a["agency_id"]: {**a, "requests": [dict(r) for r in a["requests"]]} for a in sheets["agencies"]}
    for aid in agencies:
        by_id.setdefault(aid, {"agency_id": aid, "requests": []})
    for aid, rid in pairs:
        if rid not in {r["request_id"] for r in by_id[aid]["requests"]}:
            by_id[aid]["requests"].append({"request_id": rid})
    return list(by_id.values())


def build(worksheet, facts_store, comparison):
    """(output bytes, counts); raises Refused / ws.WorksheetError. Reads only."""
    run_id, as_of, agencies, pairs = ws._read_comparison(comparison)
    try:
        doc = ws._load(worksheet, ws.SCHEMA, "agencies")
        store = cmp._checked(Path(facts_store), directory=True).resolve()
    except (OSError, ValueError, UnicodeError, RecursionError):
        raise Refused("worksheet_unreadable") from None
    if store == ws.REPO or ws.REPO in store.parents:
        raise Refused("fact_store_in_checkout")
    sheets, counts = compile_sheet(doc, run_id, as_of, ws._tool(), store, agencies, pairs)
    data = (json.dumps(sheets, sort_keys=True, indent=2) + "\n").encode("utf-8")
    try:
        loaded, _ = cmp._load_bytes(data)
        cmp.compare(_stubs(loaded, agencies, pairs), run_id=run_id, as_of=as_of)
    except (OSError, ValueError, KeyError, TypeError):
        raise Refused("compiled_input_invalid") from None
    return data, counts


def main(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(prog="records comparison-input", description=__doc__)
    for name in ("worksheet", "facts-store", "comparison", "out"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args(argv)
    try:
        if os.path.lexists(args.out):
            raise ws.WorksheetError("output_exists")
        data, counts = build(args.worksheet, args.facts_store, args.comparison)
        ws._write(args.out, args.comparison, args.facts_store, data)
    except Refused as exc:
        print("comparison_input_" + exc.code + "".join(f" {k}={v}" for k, v in exc.where.items()), file=sys.stderr)
        return 1
    except ws.WorksheetError as exc:
        print("comparison_input_" + str(exc), file=sys.stderr)
        return 1
    for aid in sorted(counts):
        print(f"agency={aid} emitted={counts[aid][0]} incomplete={counts[aid][1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
