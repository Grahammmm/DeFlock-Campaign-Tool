#!/usr/bin/env python3
"""Rehearse one agency (BF-df-e2e-rehearsal): walk one named agency through every stage of the records chain against the
private store and write one rehearsal receipt naming each stage's state and the human step still owed.
  python3 tools/rehearse_agency.py --store STORE --agency SLUG --out NEWDIR [--as-of YYYY-MM-DD]
Writes NEWDIR/REHEARSAL.md and NEWDIR/rehearsal.json (records-rehearsal-v1, review_required): twelve stage rows in a fixed
order (stage | state | source | next human step | gate) and a verdict naming the first row that is not done.
A state is decided from a recorded field (flock_state request and email-run states, CONTACT.csv, FACTS.csv cells, the
FINDINGS.csv / VERIFY.json / PUBLISH.json files and flock_publish's stage), never from prose or a title. OCR is `not-wired`
until df-ocr-live lands. Read-only over STORE and the repository: no send, no publish, no network, no write under STORE.
Next steps are printed as text for a human to run attended, never run here. Ids, slugs, dates, enums and counts only.
NEWDIR: absolute, normalised, absent, its parent an existing link-free folder, outside the repository and STORE; it is
created 0700 with 0600 files only after the receipt is built. A refusal prints one line and creates nothing.
Exit 0 every source loaded, 1 a source failed (receipt still written, its rows `unknown`), 2 usage or refusal."""
import argparse, csv, datetime, importlib.util, json, os, re, sys
from collections import Counter
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
REPO = TOOLS.parent


def _tool(name):  # siblings are loaded by file path, never copied
    spec = importlib.util.spec_from_file_location("_rehearse_" + name, TOOLS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


state_tool, facts_tool, email_tool = _tool("flock_state"), _tool("flock_facts"), _tool("flock_email")
publish_tool, privacy = _tool("flock_publish"), _tool("flock_privacy")
SCHEMA = "records-rehearsal-v1"
STAGES = ("agency-verified", "request-drafted", "request-approved", "request-sent", "response-received", "preserved",
          "extracted", "ocr", "catalogued", "detected", "reviewed", "published")
STATES = ("done", "waiting-on-gate", "waiting-on-agency", "not-wired", "absent", "inconsistent", "unknown")
GATES = ("send", "publish", "law", "none")
SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
HEX = re.compile(r"[0-9a-f]{64}")
OCR = ("not-wired", "docs/STATUS.md (OCR live wiring T6 not done)", "df-ocr-live is not wired; scanned pages are read by hand")
COMPLETE = "chain complete for %s; machine-valid, not semantic acceptance"
NOTE = "review-required; read-only rehearsal: nothing was sent, published or written to the store"
ERRORS = (OSError, UnicodeDecodeError, csv.Error, ValueError, KeyError, TypeError, state_tool.Refuse)


class Refused(Exception):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Refused("usage: " + message.split(":")[0])


def cmd(*words):
    return "python3 tools/" + " ".join(words)


def row(stage, state, source, step, gate="none"):
    return {"stage": stage, "state": state, "source": source, "next": step, "gate": gate}


def guard(text):  # every generated sentence passes the shared privacy patterns and the legal-claim guard, or is withheld
    return "(withheld by guard)" if privacy.categories(text) or privacy.legal_claims(text) else text


def contact_row(folder, slug):
    path, src = folder / "CONTACT.csv", "CONTACT.csv via flock_email.contact"
    step = cmd("flock_email.py check STORE") + " after recording the agency's records contact from its official page"
    if not os.path.lexists(path):
        return row("agency-verified", "absent", src, step)
    if path.is_symlink():
        return row("agency-verified", "inconsistent", src + " (link refused)", step)
    _, errs, _ = email_tool.contact(path, slug)
    return row("agency-verified", "inconsistent" if errs else "done", src + " (errors=%d)" % len(errs),
               step if errs else "none")


def request_rows(runs, requests):
    """Rows 2-5 from the latest email run's flock-state-v1 state and the REQUESTS.csv request states."""
    last = runs[-1] if runs else None
    run_state = last[1] if last else None
    src = "flock_state runs (latest %s: %s)" % (last[0], run_state) if last else "flock_state runs (none)"
    rsrc = "flock_state requests (%s)" % (" ".join("%s=%d" % kv for kv in sorted(Counter(r[1] for r in requests).items())) or "none")
    bad_run = run_state in ("run-malformed", "link-refused", "run-inconsistent", "approval-unverified", "sent-unverified")
    status = cmd("flock_state.py state STORE --agency SLUG")
    draft = cmd("flock_email.py draft RUNDIR --drafts DIR") + " (draft only), then flock_email.py lint"
    if run_state in (None, "follow-up-drafted"):
        drafted = row("request-drafted", "absent", src, draft)
    elif run_state in ("run-malformed", "link-refused"):
        drafted = row("request-drafted", "inconsistent", src, status)
    else:
        drafted = row("request-drafted", "done", src, "none")
    approve = "G reads and signs: " + cmd("flock_approve.py payload RUNDIR") + "; then flock_approve.py approve RUNDIR --signature SIG"
    if run_state in ("approved-not-sent", "sent-recorded"):
        approved = row("request-approved", "done", src, "none", "send")
    elif run_state == "email-drafted":
        approved = row("request-approved", "waiting-on-gate", src, approve, "send")
    elif bad_run and drafted["state"] == "done":
        approved = row("request-approved", "inconsistent", src, cmd("flock_approve.py status STORE"), "send")
    else:
        approved = row("request-approved", drafted["state"] if drafted["state"] == "inconsistent" else "absent", src,
                       approve, "send")
    errs = any(r[1] == "record-error" for r in requests)
    record = "record the request: REQUESTS.csv row (see skills/flock-followup), then " + status
    if errs:
        sent = row("request-sent", "inconsistent", rsrc, cmd("flock_requests.py --check STORE"), "send")
    elif run_state == "sent-recorded":
        sent = row("request-sent", "done" if requests else "inconsistent", src + "; " + rsrc, "none" if requests else record, "send")
    elif requests or run_state in ("sent-unverified", "run-inconsistent"):  # a send with no signed, recorded run behind it
        sent = row("request-sent", "inconsistent", src + "; " + rsrc, status, "send")
    elif run_state == "approved-not-sent":
        sent = row("request-sent", "waiting-on-gate", src, "operator sends attended under G's signed approval; " + record, "send")
    else:
        sent = row("request-sent", "absent", src, "follows request-approved", "send")
    states = {r[1] for r in requests}
    answered = states & {"determined", "produced", "closed"}
    if errs:
        got = row("response-received", "inconsistent", rsrc, cmd("flock_requests.py --check STORE"))
    elif answered:
        got = row("response-received", "done", rsrc, "none")
    elif any(s.startswith("open-") for s in states):
        got = row("response-received", "waiting-on-agency", rsrc, cmd("flock_requests.py due STORE --agency SLUG"))
    else:
        got = row("response-received", "absent", rsrc, "follows request-sent")
    return [drafted, approved, sent, got]


def fact_rows(folder, slug, errors):
    """Rows preserved, extracted, ocr and catalogued from FACTS.csv cells (flock_facts.load decides row errors)."""
    path, src = folder / "FACTS.csv", "FACTS.csv via flock_facts.load"
    ocr = row("ocr", *OCR)
    imp = cmd("flock_facts.py import ...") + " then flock_facts.py --check STORE"
    review = cmd("flock_facts.py status --store STORE --agency SLUG --list unreviewed")
    keep = "preserve the produced records in the records engine (canonical ledger), then cite their digests"
    try:
        if path.is_symlink():
            raise ValueError("link refused")
        rows, errs = facts_tool.load(path, slug)[1:] if os.path.lexists(path) else ([], [])
        if any(e.startswith("row 1:") for e in errs):
            raise ValueError("header is not FACTS v2")
    except ERRORS as exc:
        errors.append({"source": "FACTS.csv", "error": type(exc).__name__})
        return [row(s, "unknown", src + " (failed to load)", imp) for s in ("preserved", "extracted")] + [
            ocr, row("catalogued", "unknown", src + " (failed to load)", imp)]
    cells = [r for _, r in rows]
    digests = sum(bool(HEX.fullmatch(r["source_sha256"].strip())) for r in cells)
    unreviewed = sum(r["status"] == "unreviewed" for r in cells)
    counts = " (rows=%d with_source=%d errors=%d)" % (len(cells), digests, len(errs))
    if not cells:
        return [row("preserved", "absent", src + counts, keep), row("extracted", "absent", src + counts, imp), ocr,
                row("catalogued", "absent", src + counts, review)]
    return [row("preserved", "done" if digests == len(cells) else "inconsistent", src + counts,
                "none" if digests == len(cells) else keep),
            row("extracted", "inconsistent" if errs else "done", src + counts, imp if errs else "none"), ocr,
            row("catalogued", "inconsistent" if errs else "absent" if unreviewed else "done",
                src + " (unreviewed=%d)" % unreviewed, review if errs or unreviewed else "none")]


def review_rows(folder, errors):
    """Rows detected, reviewed, published from the latest run holding FINDINGS.csv, its VERIFY.json and PUBLISH.json."""
    runs = folder / "runs"
    found = [] if runs.is_symlink() or not runs.is_dir() else sorted(
        p for p in runs.iterdir() if DATE.fullmatch(p.name) and not p.is_symlink() and (p / "FINDINGS.csv").is_file())
    detect = cmd("flock_review.py run CONFIG") + " (agency-review method; compare writes FINDINGS.csv)"
    if not found:
        return [row("detected", "absent", "runs/*/FINDINGS.csv (none)", detect),
                row("reviewed", "absent", "runs/*/VERIFY.json (none)", "follows detected", "law"),
                row("published", "absent", "runs/*/PUBLISH.json (none)", "follows reviewed", "publish")]
    run = found[-1]
    src = "runs/%s/FINDINGS.csv" % run.name
    try:
        with open(run / "FINDINGS.csv", encoding="utf-8-sig", newline="") as fh:
            table = list(csv.reader(fh))
        if not table or table[0] != publish_tool.compare.HEADER:
            raise ValueError("header is not FINDINGS.csv's")
        findings = [dict(zip(table[0], r)) for r in table[1:] if len(r) == len(table[0])]
        flagged = sum(bool(privacy.legal_claims(f["claim"] + "\n" + f["next_step"])) for f in findings)
        detected = row("detected", "done", src + " (findings=%d legal_claim_flags=%d)" % (len(findings), flagged),
                       "a human reads the %d flagged findings before review" % flagged if flagged else "none")
    except ERRORS as exc:
        errors.append({"source": "FINDINGS.csv", "error": type(exc).__name__})
        detected = row("detected", "unknown", src + " (failed to load)", detect)
    vsrc, psrc = "runs/%s/VERIFY.json" % run.name, "flock_publish.stage runs/%s" % run.name
    if not (run / "VERIFY.json").is_file() or (run / "VERIFY.json").is_symlink():
        return [detected, row("reviewed", "waiting-on-gate", vsrc + " (absent)",
                              "a different-family verifier checks each finding (skills/flock-verify)", "law"),
                row("published", "absent", psrc, "follows reviewed", "publish")]
    reviewed = row("reviewed", "done", vsrc, "none", "law")
    status = cmd("flock_publish.py status STORE")
    try:
        stage, _ = publish_tool.stage(run)
    except ERRORS as exc:
        errors.append({"source": "PUBLISH.json", "error": type(exc).__name__})
        return [detected, reviewed, row("published", "unknown", psrc + " (failed to load)", status, "publish")]
    state = {"approved": "done", "pending": "waiting-on-gate", "partly-approved": "waiting-on-gate",
             "no-export": "absent"}.get(stage, "inconsistent")
    step = {"done": "none", "absent": cmd("flock_publish.py export RUNDIR SITE_SLUG") + " (every entry pending)",
            "waiting-on-gate": "G approves or declines each entry (skills/flock-publish); " + status}.get(state, status)
    return [detected, reviewed, row("published", state, psrc + " (%s)" % stage, step, "publish")]


def rehearse(store, slug, as_of):
    folder, errors, got = store / "agencies" / slug, [], {}
    for name, load in (("REQUESTS.csv", lambda: state_tool.folder_requests(folder)),
                       ("runs", lambda: state_tool.folder_runs(folder, state_tool.stage_fn()))):
        try:
            got[name] = load()
        except ERRORS as exc:
            errors.append({"source": name, "error": type(exc).__name__})
    requests, runs = got.get("REQUESTS.csv"), got.get("runs")
    stages = [contact_row(folder, slug)]
    if requests is None or runs is None:
        stages += [row(s, "unknown", "flock_state (failed to load)", cmd("flock_state.py state STORE --agency SLUG"), g)
                   for s, g in (("request-drafted", "none"), ("request-approved", "send"), ("request-sent", "send"),
                                ("response-received", "none"))]
    else:
        stages += request_rows(runs, requests)
    stages += fact_rows(folder, slug, errors) + review_rows(folder, errors)
    for r in stages:  # every generated sentence passes the guard after the slug is filled in
        r.update({k: guard(r[k].replace("SLUG", slug)) for k in ("source", "next")})
    assert [r["stage"] for r in stages] == list(STAGES) and all(r["state"] in STATES and r["gate"] in GATES for r in stages)
    first = next((r for r in stages if r["state"] != "done"), None)
    verdict = ({"first_break": first["stage"], "state": first["state"], "next": first["next"],
                "text": "first break: %s — %s — %s" % (first["stage"], first["state"], first["next"])} if first else
               {"first_break": None, "state": "done", "next": "none", "text": COMPLETE % slug})
    return {"schema": SCHEMA, "review_required": True, "agency": slug, "as_of": as_of, "stages": stages,
            "verdict": verdict, "source_errors": sorted(errors, key=lambda e: (e["source"], e["error"])), "note": NOTE}


def markdown(doc):
    lines = ["# Rehearsal receipt: %s" % doc["agency"], "", "As of %s. %s." % (doc["as_of"], doc["note"].capitalize()), "",
             "| stage | state | source | next human step | gate |", "| --- | --- | --- | --- | --- |"]
    lines += ["| %s | %s | %s | %s | %s |" % tuple(r[k].replace("|", "/") for k in ("stage", "state", "source", "next", "gate"))
              for r in doc["stages"]]
    lines += ["", "Verdict: " + doc["verdict"]["text"], ""]
    lines += ["Source errors: " + (", ".join("%s (%s)" % (e["source"], e["error"]) for e in doc["source_errors"]) or "none"), ""]
    return "\n".join(lines)


def inside(path, root):
    return path == root or root in path.parents


def checked(argv):
    parser = Parser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    for name in ("--store", "--agency", "--out"):
        parser.add_argument(name, required=True)
    parser.add_argument("--as-of", default=datetime.date.today().isoformat())
    a = parser.parse_args(argv)
    if not DATE.fullmatch(a.as_of) or not facts_tool.valid_date(a.as_of) or not SLUG.fullmatch(a.agency) or len(a.agency) > 64:
        raise Refused("usage: --as-of must be YYYY-MM-DD and --agency a plain slug")
    store, out = Path(a.store), Path(a.out)
    if not store.is_absolute() or not out.is_absolute() or Path(os.path.normpath(out)) != out:
        raise Refused("absolute, normalised paths required")
    store = store.resolve()
    if inside(store, REPO) or store.is_symlink() or not store.is_dir():
        raise Refused("store must be a folder outside the repository")
    base, folder = store / "agencies", store / "agencies" / a.agency
    if base.is_symlink() or not base.is_dir() or folder.is_symlink() or not folder.is_dir():
        raise Refused("unknown agency")
    if os.path.lexists(out):
        raise Refused("out exists")
    if any(p.is_symlink() for p in out.parents) or not out.parent.is_dir():
        raise Refused("out parent unavailable")
    real = out.parent.resolve() / out.name
    if inside(real, REPO) or inside(real, store):
        raise Refused("out must lie outside the repository and the store")
    return store, a.agency, out, a.as_of


def write(out, doc):
    body = {"REHEARSAL.md": markdown(doc), "rehearsal.json": json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n"}
    out.mkdir(mode=0o700)
    for name, text in body.items():
        fd = os.open(out / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)


def main(argv=None):
    try:
        store, slug, out, as_of = checked(sys.argv[1:] if argv is None else argv)
    except Refused as exc:
        print("rehearse | refused | %s" % exc)
        return 2
    doc = rehearse(store, slug, as_of)
    try:
        write(out, doc)
    except OSError:
        print("rehearse | refused | out not written")
        return 2
    print("rehearse | %s | %s | %s" % (slug, doc["verdict"]["text"], "source_errors=%d" % len(doc["source_errors"])))
    return 1 if doc["source_errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
