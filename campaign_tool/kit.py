"""Build an organizer kit: suggested agencies and drafted records requests.

``build_kit(root)`` reads ``<root>/campaign.json``, resolves the campaign's
county or city against the offline agency seed, and writes ``<root>/kit/``:

    agencies.json            organizer-owned list (selected: true by default);
                             never overwritten once it exists
    agencies.suggested.json  fresh suggestions on re-runs, for diffing
    requests/<agency_id>.md  one combined draft per selected law-enforcement agency
    governing-bodies.json    boards, councils and district attorneys
    law.json                 law-package summary or {"status": "missing"}
    summary.json             counts, location, package status, next steps
    README.md                what the kit is and that nothing was sent

Nothing is sent, no custodian is contacted, and no agency is asserted to use
ALPR. Drafts are built from the jurisdiction law package when one exists and
from templates/records-request.md with a banner when it does not.
"""
import csv
import hashlib
import io
import json
import math
import os
import re
import stat
import tempfile
import unicodedata
import urllib.parse
from datetime import date
from pathlib import Path

from . import discovery, law

TEMPLATE_PATH = law.TEMPLATE_PATH
LAW_ENFORCEMENT = discovery.LAW_ENFORCEMENT_KINDS
GOVERNING = ("county_board", "city_council", "district_attorney")
AGENCY_ID = re.compile(r"^[a-z]{2}-[a-z0-9-]+$")
DEFAULT_FEE_CAP = "[explicit approved cap, e.g. $50]"
DEFAULT_REQUESTER = "[requester-approved contact details]"
NEXT_STEPS = [
    "Review kit/agencies.json: deselect agencies that do not serve your area and add any missing ones.",
    "For each selected agency, find the records custodian email or portal on the agency's official website and record it in agencies.json (records_email, records_url, portal).",
    "Check each agency's fee policy and set an explicit fee cap in every draft before sending.",
    "Fill the date range and requester block in each kit/requests/*.md draft.",
    "Send nothing from this tool; sending happens only after an organizer approval outside it.",
]
SETTINGS_NAME = "request-settings.json"
SETTINGS_KEYS = ("schema_version", "requester", "fee_cap_usd", "date_from", "date_to")


def _next_steps(filled):
    """NEXT_STEPS without the fee/date/requester hand edits that request-settings.json already made."""
    missing = [word for key, word in (("date_range", "date range"), ("requester_block", "requester block"))
               if key not in filled]
    fill = [f"Fill the {' and '.join(missing)} in each kit/requests/*.md draft."] if missing else []
    return NEXT_STEPS[:2] + ([] if "fee_cap" in filled else NEXT_STEPS[2:3]) + fill + NEXT_STEPS[4:]


def _bad_setting(key, reason):
    return ValueError(f"kit/{SETTINGS_NAME}: {key}: {reason}")


def _settings_day(value, key, today):
    try:
        day = date.fromisoformat(value) if isinstance(value, str) and DATE.fullmatch(value) else None
    except ValueError:
        day = None
    if day is None or day > today:
        raise _bad_setting(key, "must be a real YYYY-MM-DD date, not after today")
    return day


def load_request_settings(kit_dir, today=None):
    """Render keywords from kit/request-settings.json; {} when absent. Errors name the key, never the value."""
    path, today = Path(kit_dir) / SETTINGS_NAME, today or date.today()
    if not os.path.lexists(path):
        return {}
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 65536:
        raise _bad_setting("file", "must be a regular file (not a symlink) of at most 64 KiB")
    try:
        document = json.loads(path.read_bytes().decode("utf-8"))
    except (ValueError, RecursionError) as exc:
        reason = "must be UTF-8" if isinstance(exc, UnicodeDecodeError) else "must be valid JSON"
        raise _bad_setting("file", reason) from None
    if not isinstance(document, dict):
        raise _bad_setting("file", "must be a JSON object")
    for key in document:
        if key not in SETTINGS_KEYS:
            raise _bad_setting(key if re.fullmatch(r"[A-Za-z0-9_]{1,40}", key) else "(unnamed)", "unknown key")
    if type(document.get("schema_version")) is not int or document["schema_version"] != 1:
        raise _bad_setting("schema_version", "must be 1")
    settings, lines, cap = {}, document.get("requester"), document.get("fee_cap_usd")
    if "requester" in document:
        if not isinstance(lines, list) or not 1 <= len(lines) <= 8 or not all(
                isinstance(line, str) and 1 <= len(line) <= 120
                and not any(ord(c) < 32 or 127 <= ord(c) < 160 for c in line) for line in lines):
            raise _bad_setting("requester", "must be 1-8 lines of 1-120 characters without control characters")
        settings["requester_block"] = "\n".join(lines)
    if "fee_cap_usd" in document:
        # Range first: math.isfinite() overflows on a huge JSON integer such as 10**400.
        if type(cap) not in (int, float) or not 0 <= cap <= 10000 or not math.isfinite(cap):
            raise _bad_setting("fee_cap_usd", "must be a number from 0 to 10000")
        settings["fee_cap"] = "$" + format(cap, ",.2f")
    if "date_to" in document and "date_from" not in document:
        raise _bad_setting("date_to", "needs date_from")
    if "date_from" in document:
        start, end = _settings_day(document["date_from"], "date_from", today), document.get("date_to", "today")
        if end != "today" and _settings_day(end, "date_to", today) < start:
            raise _bad_setting("date_to", "must not be before date_from")
        settings["date_range"] = f"{document['date_from']} through {end}"
    return settings


def fee_cap_cents(kit_dir, today=None):
    """``fee_cap_usd`` in whole cents, as the drafts print it; None when no cap is set (same validation)."""
    cap = load_request_settings(kit_dir, today).get("fee_cap")
    return None if cap is None else int(cap.lstrip("$").replace(",", "").replace(".", ""))


def _read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _kit_folder(path, mode=0o777):
    if os.path.islink(path):
        raise ValueError(f"{path.name}/ must not be a symlink")
    if path.exists() and not path.is_dir():
        raise ValueError(f"{path.name}/ is not a folder")
    path.mkdir(mode=mode, exist_ok=True)


def _write_text(path, text):
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix="." + path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temp, path)
    except BaseException:
        os.unlink(temp)
        raise


def _write_json(path, document):
    _write_text(path, json.dumps(document, indent=2, ensure_ascii=False) + "\n")


def _load_record(path):
    try:
        document = _read_json(path) if os.path.lexists(path) else {"schema_version": 1, "drafts": {}}
    except (ValueError, OSError):
        document = None
    drafts = document.get("drafts") if isinstance(document, dict) and document.get("schema_version") == 1 else None
    if isinstance(drafts, dict) and all(isinstance(v, str) for v in drafts.values()):
        return drafts
    print("kit/drafts-written.json unreadable; treating differing drafts as edited")
    return {}


def _jurisdiction(cfg):
    return str(cfg.get("country", "US")).lower() + "-" + cfg["state"].lower()


def _location_query(cfg):
    query = cfg.get("location_query")
    if isinstance(query, str) and query.strip():
        return query.strip()
    county = cfg["county"].strip()
    if discovery.normalize(county).endswith(" county"):
        return county
    return county + " County"


def _resolve(cfg, seed, online):
    query = _location_query(cfg)
    try:
        return discovery.locate(query, cfg["state"], seed)
    except ValueError as offline_error:
        if not online:
            raise
        try:
            found = discovery.locate_online(query, cfg["state"])
        except ValueError as online_error:
            raise ValueError(f"{offline_error} Online lookup also failed: {online_error}") from None
        county = discovery.county_for(found, seed)
        if found.match_kind == "city":
            names = dict(discovery.cities_in(county))
            if found.place_name in names:
                return discovery.Location(found.county_fips, county["name"], found.state,
                                          found.place_fips or names[found.place_name],
                                          found.place_name, "city", query)
            raise ValueError(f"{offline_error}; the Census geocoder places {query!r} in "
                             f"{found.place_name!r}, which is not in the seed yet")
        return discovery.Location(found.county_fips, county["name"], found.state,
                                  None, None, "county", query)


def _clean_agency_name(name):
    if not isinstance(name, str) or not name.strip() or "<" in name or ">" in name:
        raise ValueError(f"Agency name must be plain text without angle brackets: {name!r}")
    return name.strip()


def render_combined_request(package, agency, requester_block=DEFAULT_REQUESTER,
                            fee_cap=DEFAULT_FEE_CAP, date_range="[date range, e.g. 2023-01-01 through today]",
                            scopes=None):
    """One draft covering every request scope in the law package, or only ``scopes`` (package order)."""
    name = _clean_agency_name(agency["name"])
    chosen = [scope for scope in package["request_scopes"] if scopes is None or scope["scope_id"] in scopes]
    custodian = agency.get("records_email") or "[official records custodian - verify on the agency's website]"
    law_info = package["records_law"]
    rules = {rule["rule_id"]: rule for rule in package["rules"]}
    lines = [
        "# Draft records request: " + name,
        "",
        "DRAFT ONLY. Nothing has been sent. Verify the recipient on an official agency",
        "source and review the request process before sending. Law package status: "
        + package["status"] + ".",
        "",
        f"To: {custodian}",
        f"Agency: {name} ({agency.get('jurisdiction_name', '')})",
        f"Subject: Records concerning any automated license plate reader (ALPR) use by {name}, {date_range}",
        "",
        f"Under the {law_info['name']}, {law_info['citation']}, please provide existing",
        "records, if any, concerning the following. If your agency holds no responsive",
        "records for a section, please say so for that section.",
    ]
    for scope in chosen:
        lines += ["", f"## {scope['title']}", ""]
        lines += [f"- {item}" for item in scope["items"]]
    lines += [
        "",
        f"The date range for this request is {date_range}.",
        "",
        "Electronic native formats are preferred where maintained. Please identify omitted",
        "exhibits and explain any withholding with its stated legal basis. Please advise",
        f"before incurring fees beyond {fee_cap}. Rolling production is welcome.",
        "",
        requester_block,
        "",
        "## Statutory context (for the requester, not for sending)",
        "",
        f"- Determination period: {law_info['determination_days']} {law_info['day_type']} days from receipt, "
        f"with one extension of up to {law_info['determination_extension_days']} days in unusual circumstances.",
        f"- Fee basis: {law_info['fee_basis']}",
        f"- Enforcement: {law_info['appeal']}",
        "",
        "Rules the scopes relate to:",
    ]
    cited = []
    for scope in chosen:
        for rule_id in scope["rule_ids"]:
            if rule_id in rules and rule_id not in cited:
                cited.append(rule_id)
    lines += [f"- {rid}: {rules[rid]['citation']} (review: {rules[rid]['review']})" for rid in cited]
    lines += [
        "",
        "Operator checklist: record exact scope, recipient source, date, fee cap, send",
        "approval and delivery receipt. This draft does not determine a statutory deadline",
        "and does not assert that this agency operates ALPR.",
        "",
    ]
    return "\n".join(lines)


def render_template_request(agency, template_path=TEMPLATE_PATH, requester_block=None, fee_cap=None,
                            date_range=None):
    """Fallback draft from templates/records-request.md when no law package exists."""
    name = _clean_agency_name(agency["name"])
    template = Path(template_path).read_text(encoding="utf-8")
    body = template.replace("[agency]", name)
    for slot, value in (("[date range]", date_range), ("[narrow date range]", date_range),
                        ("[explicit approved cap]", fee_cap), ("[requester-approved contact details]", requester_block)):
        body = body.replace(slot, value) if value is not None else body
    banner = [
        "# Draft records request: " + name,
        "",
        "NO REVIEWED LAW PACKAGE: no jurisdiction law package was found, so this draft",
        "cites no statute and no deadline. Add a jurisdictions/[country]-[state]/package.json",
        "or have counsel supply the citation before sending. Nothing has been sent.",
        "",
        "Subject line and scope below come from templates/records-request.md. This draft",
        "does not assert that this agency operates ALPR.",
        "",
    ]
    return "\n".join(banner) + body.split("\n", 1)[1].lstrip("\n")


def _agency_entries(agencies):
    return [{**entry, "selected": True} for entry in agencies]


def _load_existing(path):
    existing = _read_json(path)
    if not isinstance(existing, list):
        raise ValueError(f"{path} must be a JSON list of agencies")
    for entry in existing:
        if not isinstance(entry, dict) or not AGENCY_ID.match(str(entry.get("agency_id", ""))):
            raise ValueError(f"{path}: every entry needs an agency_id like 'ca-example-police'")
        if not isinstance(entry.get("name"), str):
            raise ValueError(f"{path}: entry {entry.get('agency_id')} needs a name")
        entry.setdefault("selected", True)
        entry.setdefault("kind", "other")
    return existing


def _write_readme(kit_dir, location, package_status, scopes=None):
    where = (f"{location.place_name} ({location.county_name} County)" if location.match_kind == "city"
             else f"{location.county_name} County")
    chosen = f"\n- Drafts cover only the request scopes chosen with `--scope`: {', '.join(scopes)}." if scopes else ""
    text = f"""# Organizer kit for {where}

Generated offline by `campaign_tool kit`. Nothing in this folder has been sent
to any agency, and nothing here claims that any agency uses automated license
plate readers. Every agency entry is a suggestion from the shared seed
(`verified: false`) until you confirm it on the agency's official website.

- `agencies.json` is yours: set `selected` to false for agencies to skip, fill
  `records_email`, `records_url` and `portal` from official pages, and add
  agencies the seed missed. Re-running `kit` keeps this file and writes the
  fresh suggestions to `agencies.suggested.json`.
- `requests/` holds one draft per selected law-enforcement agency. Fill the
  date range, fee cap and requester block before anyone sends it. Re-running
  `kit` keeps drafts you edited and puts the fresh draft in `requests.suggested/`.{chosen}
- `request-settings.json` (optional, private) fills every draft, e.g. `{{"schema_version": 1, "requester":
  ["A. Organizer", "a@example.org"], "fee_cap_usd": 50, "date_from": "2023-01-01", "date_to": "today"}}`.
- `governing-bodies.json` lists boards, councils and district attorneys for
  meeting and oversight follow-up; they receive no records request draft.
- `law.json` summarizes the jurisdiction law package (status: {package_status}).
  A `draft` or `missing` package means no reviewed legal basis is attached.

Sending, fee payment and publication require an organizer approval outside
this tool. See docs/AGENCY-DISCOVERY.md.
"""
    _write_text(kit_dir / "README.md", text)


def build_kit(root, online=False, include=None, seed=None, law_base=None, scopes=None):
    """Build ``<root>/kit``; returns the summary dict. Raises ValueError on bad input."""
    from .cli import read_config  # local import: cli imports this module lazily too
    root = Path(root)
    cfg = read_config(root)
    include = tuple(include) if include else discovery.DEFAULT_INCLUDE
    jurisdiction = _jurisdiction(cfg)
    seed = seed or discovery.load_seed(jurisdiction)
    location = _resolve(cfg, seed, online)
    suggested = _agency_entries(discovery.agencies_for(location, seed, include))

    try:
        package = law.load_package(jurisdiction, law_base)
        package_status = package["status"]
    except ValueError as exc:
        package, package_status = None, "missing"
        package_error = str(exc)
    if scopes:  # validated before any write; package order, deduped
        if package is None:
            raise ValueError(f"--scope needs a law package; none found for {jurisdiction}")
        wanted = {law.request_scope(package, scope_id)["scope_id"] for scope_id in scopes}
        scopes = [s["scope_id"] for s in package["request_scopes"] if s["scope_id"] in wanted]

    kit_dir = root / "kit"
    requests_dir = kit_dir / "requests"
    suggested_dir = kit_dir / "requests.suggested"
    _kit_folder(kit_dir)
    settings = load_request_settings(kit_dir)
    _kit_folder(requests_dir)
    _kit_folder(suggested_dir)

    agencies_path = kit_dir / "agencies.json"
    suggested_path = kit_dir / "agencies.suggested.json"
    if agencies_path.exists():
        agencies = _load_existing(agencies_path)
        _write_json(suggested_path, suggested)
        have = {a["agency_id"] for a in agencies}
        fresh = {a["agency_id"] for a in suggested}
        diff_count = len(have ^ fresh)
        agencies_preserved = True
    else:
        agencies = suggested
        _write_json(agencies_path, agencies)
        if suggested_path.exists():
            suggested_path.unlink()
        diff_count = 0
        agencies_preserved = False

    selected = [a for a in agencies if a.get("selected") is True]
    targets = [a for a in selected if a.get("kind") in LAW_ENFORCEMENT]
    record_path = kit_dir / "drafts-written.json"
    record, new_record, lines, edited = _load_record(record_path), {}, {}, set()
    by_name = {a["agency_id"] + ".md": a for a in agencies if a.get("kind") in LAW_ENFORCEMENT}

    def render(entry, quiet=False):
        try:
            return (render_combined_request(package, entry, scopes=scopes or None, **settings) if package
                    else render_template_request(entry, **settings))
        except ValueError:
            if not quiet:
                raise

    def is_edited(path, fresh=None):
        data = path.read_bytes()
        unchanged = fresh is not None and data == fresh.encode("utf-8")
        return not unchanged and (hashlib.sha256(data).hexdigest() != record[path.name]
                                  if path.name in record else fresh is not None)

    def keep(name, line):
        edited.add(name)
        new_record.update({name: record[name]} if name in record else {})
        lines[name] = line
    for entry in targets:
        text = render(entry)
        path = requests_dir / (entry["agency_id"] + ".md")
        if not path.is_symlink() and path.is_file() and is_edited(path, text):
            _write_text(suggested_dir / path.name, text)
            keep(path.name, f"kept edited draft requests/{path.name}; fresh draft in requests.suggested/{path.name}")
            continue
        _write_text(path, text)
        new_record[path.name] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    for stale in requests_dir.glob("*.md"):
        name = stale.name
        if name in new_record or name in edited or not (stale.is_symlink() or stale.is_file()):
            continue
        fresh = render(by_name[name], quiet=True) if name in by_name and name not in record else None
        if stale.is_symlink():
            lines[name] = f"kept linked file requests/{name}"
        elif (name in record or fresh is not None) and not is_edited(stale, fresh):
            stale.unlink()
        elif name in record:
            keep(name, f"kept edited draft requests/{name} (agency not selected)")
        else:
            lines[name] = f"kept requests/{name} (not written by kit)"
    for old in suggested_dir.glob("*.md"):
        if old.name not in edited and old.is_file() and not old.is_symlink():
            old.unlink()
    for name in sorted(lines):
        print(lines[name])
    _write_json(record_path, {"schema_version": 1, "drafts": dict(sorted(new_record.items()))})

    governing = [a for a in agencies if a.get("kind") in GOVERNING]
    _write_json(kit_dir / "governing-bodies.json", governing)

    if package:
        law_doc = law.summary(package)
        law_doc["deadline_example"] = {
            "sent_date": date.today().isoformat(),
            "determination_due": law.deadline(package, date.today()).isoformat(),
            "with_extension": law.deadline(package, date.today(), extension=True).isoformat(),
            "note": "Illustrative only; the period runs from the agency's receipt. " + law.HOLIDAY_NOTE,
        }
    else:
        law_doc = {"status": "missing", "jurisdiction": jurisdiction, "error": package_error,
                   "note": "Requests were drafted from templates/records-request.md without a statute."}
    _write_json(kit_dir / "law.json", law_doc)

    counts = {}
    for entry in agencies:
        counts[entry.get("kind", "other")] = counts.get(entry.get("kind", "other"), 0) + 1
    summary = {
        "generated_offline": not online,
        "sent": False,
        "location": location.to_dict(),
        "jurisdiction": jurisdiction,
        "law_package_status": package_status,
        "reviewed_law_package": package_status == "reviewed",
        "agencies_total": len(agencies),
        "agencies_selected": len(selected),
        "agencies_by_kind": dict(sorted(counts.items())),
        "requests_drafted": len(targets),
        "drafts_kept_edited": len(edited),
        "governing_bodies": len(governing),
        "agencies_preserved": agencies_preserved,
        "suggestion_diff_count": diff_count,
        "include": list(include),
        "scopes": scopes or "all",
        "request_settings": {"requester": "requester_block" in settings, "fee_cap": "fee_cap" in settings,
                             "date_range": "date_range" in settings},
        "next_steps": _next_steps(settings),
    }
    _write_json(kit_dir / "summary.json", summary)
    _write_readme(kit_dir, location, package_status, scopes)

    cfg["location"] = {"county_fips": location.county_fips, "county_name": location.county_name,
                       "place_fips": location.place_fips, "place_name": location.place_name,
                       "match_kind": location.match_kind}
    cfg["jurisdiction"] = jurisdiction
    _write_json(root / "campaign.json", cfg)
    return summary


EMAIL = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]{1,63}(\.[A-Za-z0-9-]{1,63})*\.[A-Za-z]{2,24}")
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _https_url(value, field):
    parts = urllib.parse.urlsplit(value) if isinstance(value, str) else None
    if not parts or parts.scheme != "https" or not parts.hostname or any(ord(c) < 33 for c in value):
        raise ValueError(f"{field} must be an https:// URL with a host")


POSTAL_LINES, POSTAL_WIDTH = (2, 5), 80
POSTAL_KEYS = ("mailing_address", "mailing_verification")


def _postal_chars(text):
    """True when ``text`` has none of: angle brackets, control, format or surrogate characters, line separators."""
    return not any(c in "<>" or unicodedata.category(c) in ("Cc", "Cf", "Cs", "Zl", "Zp") for c in text)


def parse_mailing_address(value):
    """The lines of ``LINE|LINE`` (2-5 trimmed lines of 1-80 characters), or ValueError naming the rule, not the text."""
    if not isinstance(value, str) or not _postal_chars(value.replace("|", "")):
        raise ValueError("mailing_address must be plain text lines without angle brackets or control characters")
    lines = [part.strip() for part in value.split("|")]
    if not POSTAL_LINES[0] <= len(lines) <= POSTAL_LINES[1] or any(not 1 <= len(line) <= POSTAL_WIDTH for line in lines):
        raise ValueError("mailing_address must be 2-5 lines separated by |, each 1-80 characters")
    return lines


def _real_day(value, today=None):
    try:
        day = date.fromisoformat(value) if isinstance(value, str) and DATE.fullmatch(value) else None
    except ValueError:
        day = None
    return day is not None and day <= (today or date.today())


def postal_proof(entry, today=None):
    """The verified ``mailing_address`` lines of one kit/agencies.json entry, or ValueError (never echoes the address)."""
    lines, proof = entry.get("mailing_address"), entry.get("mailing_verification")
    if entry.get("verified") is not True or lines is None or proof is None:
        raise ValueError("no verified mailing_address for this agency; run kit-verify-agency --mailing-address first")
    try:
        plain = (isinstance(lines, list) and all(isinstance(line, str) and "|" not in line for line in lines)
                 and parse_mailing_address("|".join(lines)) == lines)
    except ValueError:
        plain = False
    if not plain:
        raise ValueError("mailing_address in kit/agencies.json is not 2-5 plain lines of 1-80 characters")
    if (not isinstance(proof, dict) or set(proof) != {"source_url", "checked_on"}
            or not _real_day(proof["checked_on"], today)):
        raise ValueError("mailing_verification in kit/agencies.json needs a source_url and a real checked_on date")
    try:
        _https_url(proof["source_url"], "source_url")
    except ValueError:
        raise ValueError("mailing_verification in kit/agencies.json needs an https:// source_url") from None
    return list(lines)


def _check_verification(agencies, agency_id, *, source_url, checked_on, records_email=None, records_url=None,
                        portal=None, today=None, portal_vendor=None, mailing_address=None):
    """Every kit-verify-agency rule for one agency in ``agencies``: (entry, given, verification), or ValueError.

    Changes nothing; ``portal_vendor`` (import only) needs ``portal`` and must be a known vendor."""
    matches = [entry for entry in agencies if entry["agency_id"] == agency_id]
    if len(matches) != 1:
        raise ValueError(f"agency_id {agency_id!r} must appear exactly once in kit/agencies.json (found {len(matches)})")
    given = {name: value for name, value in (("records_email", records_email), ("records_url", records_url),
                                             ("portal", portal)) if value is not None}
    if portal_vendor is not None:
        from .content import PORTAL_VENDORS
        if portal_vendor not in PORTAL_VENDORS:
            raise ValueError(f"portal_vendor must be one of {', '.join(PORTAL_VENDORS)}")
        if portal is None:
            raise ValueError("portal_vendor needs a portal_url")
    if not given and mailing_address is None:
        raise ValueError("give at least one of records_email, records_url, portal")
    if mailing_address is not None:
        parse_mailing_address(mailing_address)
    _https_url(source_url, "source_url")
    for field in ("records_url", "portal"):
        if field in given:
            _https_url(given[field], field)
    if "records_email" in given and not (isinstance(records_email, str) and EMAIL.fullmatch(records_email)):
        raise ValueError("records_email must be a plain address like records@agency.example")
    try:
        checked = date.fromisoformat(checked_on) if DATE.fullmatch(str(checked_on)) else None
    except ValueError:
        checked = None
    if checked is None:
        raise ValueError("checked_on must be a real YYYY-MM-DD date")
    if checked > (today or date.today()):
        raise ValueError(f"checked_on {checked_on} is after today")
    entry = matches[0]
    if "portal" in given:
        base = entry["portal"] if isinstance(entry.get("portal"), dict) else {"vendor": "unknown"}
        given["portal"] = {**base, **({"vendor": portal_vendor} if portal_vendor else {}), "url": given["portal"]}
    return entry, given, {"source_url": source_url, "checked_on": checked_on, "fields": sorted(given)}


def _apply_verification(entry, given, verification):
    entry.update(given)
    entry["verified"] = True
    entry["verification"] = verification


def _agencies_for_verify(root):
    path = Path(root) / "kit" / "agencies.json"
    if not path.is_file() or path.is_symlink():
        raise ValueError("run kit first: kit/agencies.json is missing")
    _load_existing(path)
    return path, _read_json(path)


def verify_agency(root, agency_id, *, source_url, checked_on, records_email=None, records_url=None,
                  portal=None, today=None, mailing_address=None):
    """Record an organizer-checked contact and/or postal address, its official source and date; never fetches it.

    ``mailing_address`` (``LINE|LINE``) is stored with its own ``mailing_verification`` (this call's source and
    date) in the same write; ``verification.fields`` stays electronic-only and later electronic checks keep it."""
    path, agencies = _agencies_for_verify(root)
    entry, given, verification = _check_verification(
        agencies, agency_id, source_url=source_url, checked_on=checked_on, records_email=records_email,
        records_url=records_url, portal=portal, today=today, mailing_address=mailing_address)
    _apply_verification(entry, given, verification)
    result = {"agency_id": agency_id, "fields": sorted(given), "checked_on": checked_on, "verified": True}
    if mailing_address is not None:
        entry["mailing_address"] = parse_mailing_address(mailing_address)
        entry["mailing_verification"] = {"source_url": source_url, "checked_on": checked_on}
        result["mailing_address"] = "recorded"
    _write_json(path, agencies)
    return result


class VerifyImportError(ValueError):
    exit_code = 2


VERIFY_COLUMNS = ("agency_id", "source_url", "checked_on", "records_email", "records_url", "portal_vendor", "portal_url")
VERIFY_MAX_BYTES, VERIFY_MAX_ROWS, VERIFY_SHOWN = 512 * 1024, 500, 20


def _read_verify_csv(csv_path):
    """[(line, cells)] for the non-blank data rows of a small regular UTF-8 CSV with the seven-column header."""
    try:
        fd = os.open(csv_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:  # O_NOFOLLOW: a symlink fails here with ELOOP
        raise VerifyImportError(f"csv cannot be opened as a regular file (symlinks are refused): {exc.strerror}") from None
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise VerifyImportError("csv must be a regular file")
    with os.fdopen(fd, "rb") as handle:
        data = handle.read(VERIFY_MAX_BYTES + 1)
    if len(data) > VERIFY_MAX_BYTES:
        raise VerifyImportError("csv is larger than 512 KiB")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise VerifyImportError("csv is not UTF-8") from None
    reader, rows, start = csv.reader(io.StringIO(text, newline="")), [], 1
    try:
        for cells in reader:
            if cells:
                rows.append((start, [cell.strip() for cell in cells]))
            start = reader.line_num + 1
    except csv.Error as exc:
        raise VerifyImportError(f"csv line {start}: {exc}") from None
    if not rows or rows[0][1] != list(VERIFY_COLUMNS):
        raise VerifyImportError("csv header must be exactly " + ",".join(VERIFY_COLUMNS))
    return rows[1:]


def verify_agencies_csv(root, csv_path, *, check=False, today=None):
    """Check every CSV row with the kit-verify-agency rules, then apply them all in one write, or write nothing.

    Never fetches a URL or sends to an address; errors name the line and never echo an e-mail address."""
    path, agencies = _agencies_for_verify(root)
    rows, errors, first_line, applied = _read_verify_csv(csv_path), [], {}, []
    if not 1 <= len(rows) <= VERIFY_MAX_ROWS:
        raise VerifyImportError(f"csv must have 1-{VERIFY_MAX_ROWS} data rows (found {len(rows)})")
    for line, cells in rows:
        try:
            if len(cells) != len(VERIFY_COLUMNS):
                raise ValueError(f"expected {len(VERIFY_COLUMNS)} cells, found {len(cells)}")
            agency_id, source_url, checked_on, email, url, vendor, portal_url = (cell or None for cell in cells)
            if agency_id in first_line:
                raise ValueError(f"same agency as line {first_line[agency_id]}")
            applied.append(_check_verification(
                agencies, agency_id, source_url=source_url, checked_on=checked_on, records_email=email,
                records_url=url, portal=portal_url, today=today, portal_vendor=vendor))
        except ValueError as exc:
            errors.append(f"line {line}: {EMAIL.sub('[address]', str(exc))}")
        first_line.setdefault(cells[0] or None, line)
    if errors:
        more = f"\nand {len(errors) - VERIFY_SHOWN} more" if len(errors) > VERIFY_SHOWN else ""
        raise VerifyImportError(f"{len(errors)} bad row(s); kit/agencies.json was not changed:\n"
                                + "\n".join(errors[:VERIFY_SHOWN]) + more)
    for entry, given, verification in applied:
        _apply_verification(entry, given, verification)
    if not check:
        _write_json(path, agencies)
    return {"verified": [entry["agency_id"] for entry, _, _ in applied], "count": len(applied), "written": not check}


def kit_status(root):
    """Doctor fields: agencies_selected and kit_built."""
    root = Path(root)
    report = {"agencies_selected": 0, "kit_built": (root / "kit" / "summary.json").is_file()}
    path = root / "kit" / "agencies.json"
    if path.is_file():
        try:
            report["agencies_selected"] = sum(
                1 for entry in _read_json(path)
                if isinstance(entry, dict) and entry.get("selected") is True)
        except (ValueError, OSError):
            report["kit_error"] = "kit/agencies.json is not readable JSON"
    return report


def stale_contacts(root, max_age_days, today=None):
    """Selected, verified kit agencies whose verification.checked_on is more than max_age_days old (read-only).

    Missing kit/agencies.json -> []; unreadable -> None (doctor already reports kit_error). A verified entry with a
    missing or unparseable checked_on is listed with nulls (first): it cannot be shown fresh. doctor caps the list."""
    path = Path(root) / "kit" / "agencies.json"
    if not path.is_file():
        return []
    try:
        agencies = _read_json(path)
    except (ValueError, OSError):
        return None
    if not isinstance(agencies, list):
        return []
    today = today or date.today()
    stale = []
    for entry in agencies:
        if not (isinstance(entry, dict) and entry.get("selected") is True and entry.get("verified") is True):
            continue
        verification = entry.get("verification")
        raw = verification.get("checked_on") if isinstance(verification, dict) else None
        try:
            checked = date.fromisoformat(raw) if isinstance(raw, str) and DATE.fullmatch(raw) else None
        except ValueError:
            checked = None
        age = (today - checked).days if checked else None
        if age is None or age > max_age_days:
            stale.append({"agency_id": str(entry.get("agency_id")), "checked_on": checked and raw, "age_days": age})
    stale.sort(key=lambda item: (item["age_days"] is not None, -(item["age_days"] or 0), item["agency_id"]))
    return stale
