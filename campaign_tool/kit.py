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
import hashlib
import json
import os
import re
import tempfile
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
        found = discovery.locate_online(query, cfg["state"])
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
                            fee_cap=DEFAULT_FEE_CAP, date_range="[date range, e.g. 2023-01-01 through today]"):
    """One draft covering every request scope in the law package."""
    name = _clean_agency_name(agency["name"])
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
    for scope in package["request_scopes"]:
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
    for scope in package["request_scopes"]:
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


def render_template_request(agency, template_path=TEMPLATE_PATH):
    """Fallback draft from templates/records-request.md when no law package exists."""
    name = _clean_agency_name(agency["name"])
    template = Path(template_path).read_text(encoding="utf-8")
    body = template.replace("[agency]", name)
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


def _write_readme(kit_dir, location, package_status):
    where = (f"{location.place_name} ({location.county_name} County)" if location.match_kind == "city"
             else f"{location.county_name} County")
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
  `kit` keeps drafts you edited and puts the fresh draft in `requests.suggested/`.
- `governing-bodies.json` lists boards, councils and district attorneys for
  meeting and oversight follow-up; they receive no records request draft.
- `law.json` summarizes the jurisdiction law package (status: {package_status}).
  A `draft` or `missing` package means no reviewed legal basis is attached.

Sending, fee payment and publication require an organizer approval outside
this tool. See docs/AGENCY-DISCOVERY.md.
"""
    _write_text(kit_dir / "README.md", text)


def build_kit(root, online=False, include=None, seed=None, law_base=None):
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

    kit_dir = root / "kit"
    requests_dir = kit_dir / "requests"
    suggested_dir = kit_dir / "requests.suggested"
    _kit_folder(kit_dir)
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
            return render_combined_request(package, entry) if package else render_template_request(entry)
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
        "next_steps": NEXT_STEPS,
    }
    _write_json(kit_dir / "summary.json", summary)
    _write_readme(kit_dir, location, package_status)

    cfg["location"] = {"county_fips": location.county_fips, "county_name": location.county_name,
                       "place_fips": location.place_fips, "place_name": location.place_name,
                       "match_kind": location.match_kind}
    cfg["jurisdiction"] = jurisdiction
    _write_json(root / "campaign.json", cfg)
    return summary


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
