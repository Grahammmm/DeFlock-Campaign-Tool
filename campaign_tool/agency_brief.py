"""agency-brief (DF1107-12): a private, printable meeting brief for one agency, built from published content only.

``agency-brief --agency ID --directory D [--replace]`` writes ``kit/briefs/<agency-slug>-<date>.html`` and its ``.txt`` twin
from the SAME ordered rows. Inputs are the organizer's validated ``content/`` model (``content/agencies.json`` must exist
as a real file; the kit's agency list is never a fallback). Every factual row names its source (a public URL and/or a
``content/<file>:<field>`` locator) and the date recorded with it; the generation date is never used as a checked date.
Nothing is fetched, sent or published, and no kit, draft or request text is read. ``published`` means the organizer's
content/ inputs, not proof that the website is deployed, and the loader does not authenticate private review receipts.

The pair is written one file after the other, not as a transaction: on an I/O failure the first file can already be new.
The command then says so and exits 1; running it again with ``--replace`` repairs the pair.
"""
import html
import os
import stat
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from . import labels
from .content import ContentError, MAX_JSON_BYTES, load_content, utc_moment
from .law import LOCAL_RULE_LABEL
from .legal_claims import legal_notes
from .privacy_patterns import refusal

BODY_KINDS = ("city_council", "county_board")
NOT_RECORDED = "Not recorded"
FOOTER = "Private meeting brief. Nothing was sent or published. Recorded facts may be incomplete; not legal advice."
CSP = "default-src 'none'; style-src 'unsafe-inline'"
STYLE = ("@page{margin:1in}body{margin:0;font-family:Georgia,'Times New Roman',serif;font-size:11pt;line-height:1.4;color:#000;"
         "background:#fff}h1{font-size:18pt}h2{font-size:13pt;margin:1.4em 0 .4em;border-bottom:1px solid #000}"
         "li{margin:0 0 .7em;white-space:pre-wrap}.src{display:block;font-size:9.5pt;color:#222}footer{margin-top:2em;"
         "font-size:9.5pt}")
SECTIONS = ("Agency", "Profile facts", "Requests made by the campaign", "Published findings", "Local rules on record",
            "Recorded agency actions", "Governing body", "Recorded votes", "Upcoming staged meetings",
            "Campaign asks", "Limitations")


class BriefError(ContentError):
    """The brief cannot be written; the message names the agency or a safe locator, never a private value."""


# --- rows ------------------------------------------------------------------------------------------------------

def _row(section, where, text, sources, when=""):
    if not sources:
        raise BriefError(f"{where}: a row without a source")
    return {"section": section, "where": where, "text": text, "sources": list(sources), "when": when}


def _loc(aid, field):
    return f"content/agencies.json:{aid}.{field}"


def _money(amount):
    return "$" + format(Decimal(amount), ",.2f")


def _link(base, path):
    return f"{base}{path}"


def _profile_rows(agency, labels_en):
    aid, rows = agency["agency_id"], []
    add = lambda where, text, sources, when="": rows.append(_row("Profile facts", where, text, sources, when))  # noqa: E731
    policy = agency.get("alpr_policy")
    if policy:
        add("alpr_policy", f"Posted ALPR usage policy: {policy['url']}", [policy["url"], policy["source_url"]],
            f"checked {policy['checked_on']}")
    else:
        add("alpr_policy", f"{NOT_RECORDED}: posted ALPR usage policy link", [_loc(aid, "profile.alpr_policy")])
    for item in (agency.get("policy_check") or {}).get("elements", []):
        page = f" (page {item['page']})" if "page" in item else ""
        add("policy_check", f"Policy checklist, organizer reading: {item['label']}: {item['status']}{page}",
            [agency["policy_check"]["policy_url"]], f"checked {agency['policy_check']['checked_on']}")
    contract = agency.get("contract")
    if contract:
        note = f" {contract['renewal_note']}" if contract["renewal_note"] else ""
        add("contract", f"Contract with {contract['vendor']}: {contract['start']} to {contract['end']}.{note}",
            [contract["source_url"]], f"checked {contract['checked_on']}")
        cost = contract.get("cost")
        if cost:
            add("contract.cost", f"Contract cost as stated in {cost['as_stated_in']}: {_money(cost['amount_usd'])} "
                f"({labels_en['agency.cost_' + cost['period']]})", [contract["source_url"]], f"checked {contract['checked_on']}")
    else:
        add("contract", f"{NOT_RECORDED}: contract term and cost", [_loc(aid, "profile.contract")])
    for item in (agency.get("contract_check") or {}).get("elements", []):
        page = f" (page {item['page']})" if "page" in item else ""
        note = f" - {item['note']}" if "note" in item else ""
        add("contract_check", f"Contract checklist, organizer reading: {item['label']}: {item['status']}{page}{note}",
            [agency["contract_check"]["contract_url"]], f"checked {agency['contract_check']['checked_on']}")
    funding = agency.get("funding") or []
    for entry in funding:
        parts = [labels_en["agency.funding_kind_" + entry["kind"]], entry["name"]]
        if "amount_usd" in entry:
            parts.append(f"{_money(entry['amount_usd'])} ({labels_en['agency.cost_' + entry['period']]})")
        if "fiscal_year" in entry:
            parts.append(f"fiscal year {entry['fiscal_year']}")
        add("funding", f"Funding as stated in {entry['as_stated_in']}: " + "; ".join(parts), [entry["source_url"]],
            f"checked {entry['checked_on']}")
    if not funding:
        add("funding", f"{NOT_RECORDED}: funding", [_loc(aid, "profile.funding")])
    snapshots = agency.get("transparency") or []
    if snapshots:
        newest = snapshots[0]
        names = (("cameras", "Cameras reported"), ("retention_days", "Retention days reported"),
                 ("searches_last_30_days", "Searches in the last 30 days reported"),
                 ("hotlist_alerts", "Hotlist alerts reported"),
                 ("shared_with_agencies", "Agencies reported as receiving shared data (portal number)"))
        for key, label in names:
            value = newest[key]
            add("transparency", f"{label}: {NOT_RECORDED.lower() if value is None else value}", [newest["source_url"]],
                f"checked {newest['checked_on']}")
    else:
        add("transparency", f"{NOT_RECORDED}: vendor transparency-portal numbers", [_loc(aid, "profile.transparency")])
    sharing = agency.get("sharing")
    if sharing:
        add("sharing", f"Recipients named in the transcribed vendor-portal sharing list: {len(sharing['recipients'])}",
            [sharing["source_url"]], f"checked {sharing['checked_on']}")
    else:
        add("sharing", f"{NOT_RECORDED}: transcribed sharing list", [_loc(aid, "profile.sharing")])
    return rows


def _asked_rows(agency):
    aid, rows = agency["agency_id"], []
    for index, item in enumerate(agency.get("asked", [])):
        updated = f"; updated {item['updated_on']}" if item.get("updated_on") else ""
        note = f" Note: {item['note']}" if item.get("note") else ""
        rows.append(_row("Requests made by the campaign", f"asked[{index}]",
                         f"Request: {item['topic']} (state: {item['state']}){note}", [_loc(aid, f"asked[{index}]")],
                         f"asked {item['asked_on']}{updated}"))
    return rows or [_row("Requests made by the campaign", "asked", f"{NOT_RECORDED}: requests", [_loc(aid, "asked")])]


def _finding_rows(model, aid, base):
    from .content import finding_agency_ids
    rows = []
    for finding in model["findings"]:
        if aid not in finding_agency_ids(finding):
            continue
        parts = [f"{finding['title']} - {finding['summary']}",
                 f"Classification: {labels.CLASSIFICATION_LABELS.get(finding['classification'], finding['classification'])}",
                 f"confidence: {finding['confidence']}", f"event date {finding['event_date']}"]
        if finding.get("limitations"):
            parts.append("limitations: " + " ".join(finding["limitations"]))
        for correction in finding.get("corrections", []):
            parts.append(f"correction {correction['date']}: {correction['note']}")
        when = f"published {finding['published_at']}" + (f"; updated {finding['updated_at']}" if finding.get("updated_at") else "")
        rows.append(_row("Published findings", f"findings/{finding['slug']}", " | ".join(parts),
                         [_link(base, f"/findings/{finding['slug']}.html")], when))
    return rows or [_row("Published findings", "findings", f"{NOT_RECORDED}: no published finding names this agency",
                         ["content/findings"])]


def _local_rule_rows(model, aid, day):
    rows = []
    scoped = sorted((r for r in model.get("local_rules") or [] if aid in r["agency_ids"]),
                    key=lambda r: (r["rule_id"], r["effective_from"]))
    for rule in scoped:
        end = rule["effective_to"]
        state = ("past, not in force on the prepared date" if end is not None and end < day
                 else "not yet in force on the prepared date" if rule["effective_from"] > day else "in force on the prepared date")
        rows.append(_row("Local rules on record", f"local_rules/{rule['rule_id']}",
                         f"{rule['citation']} ({rule['rule_id']}) | duty as written: {rule['duty']} | effective "
                         f"{rule['effective_from']} to {end or 'open'}; {state} | {LOCAL_RULE_LABEL}; an organizer transcription, "
                         "not a determination about agency conduct.", [rule["source_url"]], f"checked {rule['checked_on']}"))
    return rows or [_row("Local rules on record", "local_rules", f"{NOT_RECORDED}: no local rule names this agency",
                         ["law/local-rules.json"])]


def _action_rows(agency, labels_en):
    aid = agency["agency_id"]
    actions = sorted(agency.get("actions", []), key=lambda a: a["on"], reverse=True)  # stable: equal days keep stored order
    rows = [_row("Recorded agency actions", "actions", f"{labels_en['agency.action_kind_' + a['kind']]}: {a['as_stated']} "
                 f"(effective {a['on']})", [a["source_url"]], f"checked {a['checked_on']}") for a in actions]
    return rows or [_row("Recorded agency actions", "actions", f"{NOT_RECORDED}: actions", ["content/agency-actions.json"])]


def _body(model, agency):
    """(body agency or None, source rows) - the governing body only when published content establishes it."""
    aid = agency["agency_id"]
    if agency["kind"] in BODY_KINDS:
        return agency, [_row("Governing body", "kind", f"This agency is the governing body: {agency['name']}",
                             [_loc(aid, "kind")])]
    pre = agency.get("pre_implementation_meeting") or {}
    matches = []
    if pre.get("status") == "found" and pre.get("body") and pre.get("source_url") and agency["jurisdiction_name"]:
        matches = [a for a in model["agencies"] if a["kind"] in BODY_KINDS and a["name"] == pre["body"]
                   and a["jurisdiction_name"] == agency["jurisdiction_name"]]
    if len(matches) == 1:
        return matches[0], [_row("Governing body", "pre_implementation_meeting",
                                 f"Governing body: {matches[0]['name']} (recorded as the body that met on {pre['meeting_on']} "
                                 "before the program)", [pre["source_url"], _loc(aid, "profile.pre_implementation_meeting")],
                                 f"checked {pre['checked_on']}")]
    return None, [_row("Governing body", "pre_implementation_meeting",
                       "Governing body not established by published content",
                       [_loc(aid, "profile.pre_implementation_meeting")])]


def _meeting_page(base, meeting):
    return _link(base, f"/meetings/{meeting['id']}.html")


def _vote_text(meeting):
    outcome = meeting["outcome"]
    parts = [f"{meeting['body']}, {meeting['starts_at']}: {outcome['result']}"]
    if outcome.get("votes"):
        parts.append("roll call as recorded: " + "; ".join(f"{v['name']}: {v['vote']}" for v in outcome["votes"]))
    elif outcome.get("vote"):
        parts.append(f"vote as stored (aggregate): {outcome['vote']}")
    else:
        parts.append("no vote count recorded")
    return ". ".join(parts)


def _meeting_rows(model, agency, body, base, now):
    aid = agency["agency_id"]
    ids = {aid} | ({body["agency_id"]} if body else set())
    chosen = [m for m in model["meetings"] if m.get("agency_id") in ids and m["status"] != "cancelled"]
    tag = lambda m: "" if body and m["agency_id"] == body["agency_id"] else " [meeting tagged directly with this agency]"  # noqa: E731
    past = sorted((m for m in chosen if m.get("outcome") and utc_moment(m["starts_at"]) < now),
                  key=lambda m: m["id"])
    past.sort(key=lambda m: utc_moment(m["starts_at"]), reverse=True)
    votes = []
    for m in past:
        out = m["outcome"]
        sources = [out["source_url"]] + [out[k] for k in ("minutes_url", "recording_url") if out.get(k)]
        votes.append(_row("Recorded votes", f"meetings/{m['id']}", _vote_text(m) + tag(m), sources + [_meeting_page(base, m)],
                          f"outcome date {out['on']}; verified {out['verified_at']}"))
    if not votes:
        votes = [_row("Recorded votes", "meetings", f"{NOT_RECORDED}: no past outcome for this body in staged content",
                      ["content/meetings"])]
    upcoming = sorted((m for m in chosen if utc_moment(m["starts_at"]) >= now), key=lambda m: m["id"])
    upcoming.sort(key=lambda m: utc_moment(m["starts_at"]))
    ahead = []
    for m in upcoming:
        detail = [f"{m['body']}, starts {m['starts_at']}"] + ([f"agenda item: {m['agenda_item']}"] if m["agenda_item"] else []) \
            + ([f"location: {m['location']}"] if m["location"] else [])
        sources = ([m["agenda_url"]] if m["agenda_url"] else []) + [_meeting_page(base, m)]
        ahead.append(_row("Upcoming staged meetings", f"meetings/{m['id']}", "; ".join(detail) + tag(m), sources,
                          f"starts {m['starts_at']}"))
    if not ahead:
        ahead = [_row("Upcoming staged meetings", "meetings", f"{NOT_RECORDED}: no upcoming staged meeting for this body",
                      ["content/meetings"])]
    return votes, ahead


def _ask_rows(model):
    asks = model["site"].get("comment_asks")
    if not asks:
        return [_row("Campaign asks", "comment_asks", "Campaign asks not recorded", ["content/site.json:comment_asks"])]
    return [_row("Campaign asks", f"comment_asks[{n}]", f"Campaign asks, not agency statements: {ask}",
                 [f"content/site.json:comment_asks[{n}]"]) for n, ask in enumerate(asks, 1)]


def build_rows(model, agency, now):
    """The ordered semantic rows both formats render. Pure: reads only the validated model and the injected clock."""
    labels_en, day = labels.DEFAULTS, now.astimezone(timezone.utc).date().isoformat()
    base = (model["site"].get("base_url") or "").rstrip("/")
    aid = agency["agency_id"]
    verification = agency["verification"]
    rows = [_row("Agency", "identity", f"{agency['name']} ({labels.KIND_LABELS.get(agency['kind'], agency['kind'])})"
                 + (f", {agency['jurisdiction_name']}" if agency["jurisdiction_name"] else "") + f"; id {aid}",
                 [verification["source_url"], _loc(aid, "name")], f"verified {verification['checked_on']}")]
    rows += _profile_rows(agency, labels_en) + _asked_rows(agency) + _finding_rows(model, aid, base)
    rows += _local_rule_rows(model, aid, day) + _action_rows(agency, labels_en)
    body, body_rows = _body(model, agency)
    votes, ahead = _meeting_rows(model, agency, body, base, now)
    rows += body_rows + votes + ahead + _ask_rows(model)
    rows += [_row("Limitations", "limitations", text, ["campaign_tool agency-brief (fixed text)"], "") for text in (
        f"Prepared {day} (UTC); sources retain their own dates. The preparation date is not a checked date.",
        "Built only from the organizer's published content inputs; it does not show that the website is deployed.",
        "Campaign asks are the campaign's, not agency statements. Local rules are organizer transcriptions.")]
    return rows


# --- rendering -------------------------------------------------------------------------------------------------

def _anchor(source):
    shown = html.escape(source, quote=True)
    if source.startswith(("https://", "/")):
        return f'<a href="{shown}" rel="noopener noreferrer">{shown}</a>'
    return f"<code>{shown}</code>"


def render_html(title, rows):
    e = lambda text: html.escape(text, quote=True)  # noqa: E731
    body = [f"<h1>{e(title)}</h1>"]
    for section in SECTIONS:
        items = [r for r in rows if r["section"] == section]
        if not items:
            continue
        body.append(f"<h2>{e(section)}</h2>\n<ul>")
        for r in items:
            sources = "".join(f'<span class="src">Source: {_anchor(s)}</span>' for s in r["sources"])
            when = f'<span class="src">Date: {e(r["when"])}</span>' if r["when"] else ""
            body.append(f"<li>{e(r['text'])}{sources}{when}</li>")
        body.append("</ul>")
    page = (f'<!DOCTYPE html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
            f'<meta http-equiv="Content-Security-Policy" content="{CSP}">\n<meta name="robots" content="noindex, nofollow">\n'
            f'<title>{e(title)}</title>\n<style>{STYLE}</style>\n</head>\n<body>\n' + "\n".join(body)
            + f"\n<footer>{e(FOOTER)}</footer>\n</body>\n</html>\n")
    return page


def render_text(title, rows):
    lines = [title.upper(), ""]
    for section in SECTIONS:
        items = [r for r in rows if r["section"] == section]
        if not items:
            continue
        lines += [f"== {section} =="]
        for r in items:
            lines.append("- " + r["text"].replace("\n", "\n  "))
            lines += [f"  Source: {s}" for s in r["sources"]]
            lines += [f"  Date: {r['when']}"] if r["when"] else []
        lines.append("")
    return "\n".join(lines + [FOOTER]) + "\n"


# --- gates -----------------------------------------------------------------------------------------------------

def gate(rows, page, text):
    """Raise BriefError naming a safe locator and category for a private-looking value; else the alert-only legal notes."""
    for r in rows:
        for part in [r["text"], r["when"], *r["sources"]]:
            problem = refusal(f"brief row {r['section']}/{r['where']}", part)
            if problem:
                raise BriefError(problem)
    for name, data in (("brief.html", page), ("brief.txt", text)):
        problem = refusal(name, data)
        if problem:
            raise BriefError(problem)
    return legal_notes((f"brief row {r['section']}/{r['where']}", r["text"]) for r in rows)


# --- input and output paths ------------------------------------------------------------------------------------

def _lstat(path):
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None


def _check_inputs(root):
    content = root / "content"
    info = _lstat(content)
    if info is None or not stat.S_ISDIR(info.st_mode):
        raise BriefError("content/ must be a real directory (not a symlink); the brief reads published content only")
    agencies = _lstat(content / "agencies.json")
    if agencies is None or not stat.S_ISREG(agencies.st_mode):
        raise BriefError("content/agencies.json is required as a regular file (not a symlink); the kit's agency list is "
                         "never used for a brief")
    if agencies.st_size > MAX_JSON_BYTES:
        raise BriefError("content/agencies.json is larger than the 2 MiB limit")


def _preflight(root, names, replace):
    """Identity of each target, or None; refuses unsafe directories and targets before anything is written."""
    for rel in ("kit", "kit/briefs"):
        info = _lstat(root / rel)
        if info is not None and not stat.S_ISDIR(info.st_mode):
            raise BriefError(f"{rel} must be a real directory (not a symlink)")
    found = {}
    for name in names:
        info = _lstat(root / "kit" / "briefs" / name)
        if info is None:
            found[name] = None
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise BriefError(f"kit/briefs/{name} exists and is not a regular single-link file; it was left alone")
        if not replace:
            raise BriefError(f"kit/briefs/{name} already exists; both files were kept unchanged")
        found[name] = (info.st_dev, info.st_ino)
    return found


def _subdir(parent, name):
    """A directory descriptor for parent/name, created 0700 when missing, never following a symlink."""
    try:
        os.mkdir(name, 0o700, dir_fd=parent)
    except FileExistsError:
        pass
    return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)


def _write_file(directory, name, data, replace, expected):
    """Write ``name`` 0600 through a same-directory exclusive temporary; recheck the target at commit."""
    temporary = f".{name}.{os.getpid()}.tmp"
    handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
    try:
        with os.fdopen(handle, "wb") as stream:
            os.fchmod(handle, 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(handle)
        try:
            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            info = None
        if info is None and expected is not None:
            raise BriefError(f"kit/briefs/{name} changed while the brief was prepared")
        if info is not None:
            if (not replace or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or (info.st_dev, info.st_ino) != expected):
                raise BriefError(f"kit/briefs/{name} appeared or changed while the brief was prepared; it was left alone")
            os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        else:
            os.link(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)  # exclusive: fails if a file arrived
            os.unlink(temporary, dir_fd=directory)
        os.fsync(directory)
    except BaseException:
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass
        raise


# --- entry points ----------------------------------------------------------------------------------------------

def build_brief(root, agency_id, *, replace=False, now=None):
    """Write the brief pair for ``agency_id``; -> {"written": [html, txt], "flags": [review-required notes]}."""
    root = Path(root)
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise BriefError("the clock must be timezone-aware")
    day = now.astimezone(timezone.utc).date()
    _check_inputs(root)
    model = load_content(root, today=day)
    agency = next((a for a in model["agencies"] if a["agency_id"] == agency_id), None)
    if agency is None:
        raise BriefError(f"agency {str(agency_id)[:80]!r} is not a selected agency in content/agencies.json")
    if not agency["verified"] or not agency.get("verification"):
        raise BriefError(f"agency {agency_id} is not organizer-verified in content/agencies.json; no brief was written")
    rows = build_rows(model, agency, now)
    title = f"Meeting brief: {agency['name']}"
    page, text = render_html(title, rows), render_text(title, rows)
    flags = gate(rows, page, text)
    names = (f"{agency['slug']}-{day.isoformat()}.html", f"{agency['slug']}-{day.isoformat()}.txt")
    expected = _preflight(root, names, replace)
    written = []
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        kit_fd = _subdir(root_fd, "kit")
        try:
            briefs_fd = _subdir(kit_fd, "briefs")
            try:
                for name, data in zip(names, (page.encode("utf-8"), text.encode("utf-8"))):
                    try:
                        _write_file(briefs_fd, name, data, replace, expected[name])
                    except BriefError:
                        raise
                    except OSError as exc:
                        window = (f"; kit/briefs/{written[0]} was already written, so the pair is incomplete: run the same "
                                  "command again with --replace" if written else "")
                        raise BriefError(f"kit/briefs/{name} could not be written ({exc.strerror or 'I/O error'}){window}") from None
                    written.append(name)
            finally:
                os.close(briefs_fd)
        finally:
            os.close(kit_fd)
    except BriefError as exc:
        if written and "incomplete" not in str(exc):
            raise BriefError(f"{exc}; kit/briefs/{written[0]} was already written, so the pair is incomplete: run the same "
                             "command again with --replace") from None
        raise
    finally:
        os.close(root_fd)
    return {"written": [f"kit/briefs/{name}" for name in written], "flags": flags}


def run_cli(root, agency_id, replace=False):
    result = build_brief(root, agency_id, replace=replace)
    for path in result["written"]:
        print(path)
    for note in result["flags"]:
        print(note, file=sys.stderr)
    print("Private brief written. Nothing was sent or published.")
