#!/usr/bin/env python3
"""Flock page: records-page story data builder + lint for the site rules (G4).
  build --cards C --sources S --location L --thread T --base-sources N --out F [--replace] [--not-posted ID..] [--not-posted-title T..]
  lint  --story S --location L --thread T --base-sources N [--not-posted ID..] [--not-posted-title T..]
build turns a model-written CARDS.json plus G3's SOURCES.json into <slug>-story.json (the keys of the site's morrobay-story.mjs),
numbering cites after the page's N base sources; it lints its own result first and writes nothing on any error. lint checks story,
thread and location against site rules 1, 2, 3, 4, 7, 8, 9 and the draft tone; it prints `<area> <index>: <rule> <reason>` errors
and counts (sources = numbered extraSources), never card text. No network. Exit 0 ok, 1 validation/lint/IO, 2 usage. Output is
private (0600); site adoption and publication are a separate operator + G card."""
import argparse, contextlib, importlib.util, json, os, re, sys, tempfile
from datetime import date, datetime, timezone
from pathlib import Path
TOOLS, REPO = Path(__file__).resolve().parent, Path(__file__).resolve().parents[1]
def _tool(name):  # F3 is loaded by file path for the wording regexes, never copied
    spec = importlib.util.spec_from_file_location("_page_" + name, TOOLS / (name + ".py"))
    return spec.loader.exec_module(module := importlib.util.module_from_spec(spec)) or module
compare = _tool("flock_compare")
KEYS = ("title eyebrow headline dek firstRequest asOf programStart programNote programEvents keyDates overview admissions findings "
        "retention retentionCaption milestones extraSources").split()
META = "title eyebrow headline dek firstRequest asOf programStart programNote overview retentionCaption".split()
CITED = ("admissions", "findings", "retention", "milestones")
EMAIL, PHONE = re.compile(r"\S+@\S+\.\w+"), re.compile(r"\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}")
URL, ACCESS = re.compile(r"https?://[^\s\"'<>]+", re.I), re.compile(r"[?&](token|key|sig|signature|auth|access|code)=", re.I)
CUT = re.compile(r"^\d[\d,]*\s+(cut|dropped|removed|fewer|more|less|added|lost|gained|down|up)\b", re.I)
OUTREACH, INVDOC = re.compile(r"\boutreach\b", re.I), re.compile(r"inventory documents? (\d+)", re.I)
SENTENCE, CITE = re.compile(r"[.!?](?=\s|[\"'”’]|$)"), re.compile(r"\[[\d,\s]*\]")
COUNTS = ("cards", "admissions", "sources", "records_messages", "dropped_outreach")
class Fail(Exception): pass
def strings(v):  # every scalar under v, as text
    if isinstance(v, (dict, list)): yield from (s for x in (v.values() if isinstance(v, dict) else v) for s in strings(x))
    elif isinstance(v, (str, int, float)) and not isinstance(v, bool): yield str(v)
def walk(obj, prefix=""):  # -> (area, index, string) for every string value; index is the list position (0 for a scalar)
    for key, value in obj.items():
        yield from ((prefix + key, i, s) for i, item in enumerate(value if isinstance(value, list) else [value]) for s in strings(item))
def day(value):
    try:
        return date.fromisoformat(value) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) else None
    except (TypeError, ValueError):
        return None
def words(text): return set(re.findall(r"[a-z0-9]+", CITE.sub(" ", text).lower()))
def norm(text): return " ".join(str(text).lower().split())
def load(path): return json.loads(Path(os.path.expanduser(path)).read_text(encoding="utf-8"))
def lint(story, thread, location, base, hidden_ids=(), hidden_titles=()):
    errs, loc_reqs = [], {r["id"] for r in location["requests"]}
    loc_docs = {d["document_id"] for d in location["documents"]}
    verified = {d["document_id"] for d in thread["documents"]
                if isinstance(v := d.get("upload_verification"), dict) and v.get("public_get_sha_ok") is True}
    n = dict(cards=len(story.get("findings") or ()), admissions=len(story.get("admissions") or ()),
             sources=len(story.get("extraSources") or ()), records_messages=0, dropped_outreach=0)
    if list(story) != KEYS:
        errs.append("story 0: schema keys missing or out of order")
    for i, m in enumerate(thread["messages"]):  # R1 records correspondence only
        reqs = m.get("requests")
        if not isinstance(reqs, list) or not reqs or any(r not in loc_reqs and r != "outreach" for r in reqs):
            errs.append("thread.messages %d: R1 request not in location requests" % i)
        elif all(r == "outreach" for r in reqs):
            n["dropped_outreach"] += 1
        else:
            n["records_messages"] += 1
    texts = list(walk(story))
    for area, i, s in texts:
        bad = [("R1", "outreach in story", OUTREACH.search(s)), ("R9", "email-like string", EMAIL.search(s)),
               ("R9", "phone-like string", PHONE.search(s)), ("R9", "access-link parameter",
                                                              any(ACCESS.search(u) for u in URL.findall(s))),
               ("tone", "always-banned wording", compare.ALWAYS.search(s)), ("tone", "quote-gated wording", compare.GATED.search(s))]
        errs += ["%s %d: %s %s" % (area, i, rule, why) for rule, why, hit in bad if hit]
        errs += ["%s %d: R8 inventory document not posted" % (area, i) for doc in INVDOC.findall(s)
                 if doc not in loc_docs or doc not in verified]
    deny = [v.lower() for v in list(hidden_ids) + list(hidden_titles) if v.strip()]
    for area, i, s in texts + list(walk(thread, "thread.")) + list(walk(location, "location.")):  # R8 not-posted invisible
        if any(d in s.lower() for d in deny):
            errs.append("%s %d: R8 not-posted record named" % (area, i))
    start = day(story.get("programStart"))
    if "Day 1" not in str(story.get("programNote", "")):
        errs.append("programNote 0: R2 missing Day 1")
    for key in ("firstRequest", "asOf", "programStart"):
        errs += ["%s 0: R2 invalid date" % key] * (day(story.get(key)) is None)
    events = set()
    for area in ("programEvents", "keyDates", "admissions", "milestones"):
        for i, item in enumerate(story.get(area) or ()):
            when = day(item.get("date"))
            errs += ["%s %d: R2 invalid date" % (area, i)] * (when is None)
            if area == "programEvents" and when:
                events.add(when)
                errs += ["%s %d: R2 event before programStart" % (area, i)] * bool(start and when < start)
            if area == "keyDates":  # R3
                errs += ["%s %d: R3 date not in programEvents" % (area, i)] * bool(when not in events)
                errs += ["%s %d: R3 short over 40 chars" % (area, i)] * (len(str(item.get("short", ""))) > 40)
    cards = story.get("findings") or ()
    for j, card in enumerate(cards):  # R4 one fact per card
        figure = norm(card.get("figure", ""))
        for i in range(j):
            other = cards[i]
            a, b = words(str(card.get("text", ""))), words(str(other.get("text", "")))
            for why, hit in (("same figure", figure == norm(other.get("figure", ""))),
                             ("same label", norm(card.get("label", "")) == norm(other.get("label", ""))),
                             ("near-duplicate text", bool(a | b) and len(a & b) / len(a | b) >= 0.5)):
                errs += ["findings %d: R4 %s as findings %d" % (j, why, i)] * hit
        errs += ["findings %d: R4 use a number chain (a → b)" % j] * bool(CUT.search(figure))
        errs += ["findings %d: R4 number chain needs two numbers" % j] * ("→" in figure and len(re.findall(r"\d[\d,.]*", figure)) < 2)
        count = len(SENTENCE.findall(CITE.sub("", str(card.get("text", ""))).strip()))
        errs += ["findings %d: R4 text is not 2-4 sentences" % j] * (not 2 <= count <= 4)
    declared, cited = {}, set()  # R7 cite everything
    for i, line in enumerate(story.get("extraSources") or ()):
        m = re.match(r"\[(\d+)\] ", str(line))
        if not m:
            errs.append("extraSources %d: R7 not numbered" % i)
        elif int(m.group(1)) in declared:
            errs.append("extraSources %d: R7 number declared twice" % i)
        else:
            declared[int(m.group(1))] = i
    if sorted(declared) != list(range(base + 1, base + 1 + len(declared))):
        errs.append("extraSources 0: R7 numbers not contiguous from %d" % (base + 1))
    for area in CITED:
        for i, item in enumerate(story.get(area) or ()):
            cite = str(item.get("cite", "")).strip()
            if not cite and area == "milestones":
                continue
            if not re.fullmatch(r"\[\s*[0-9]+(?:\s*,\s*[0-9]+)*\s*\](?:\s*\[\s*[0-9]+(?:\s*,\s*[0-9]+)*\s*\])*", cite):
                errs.append("%s %d: R7 invalid or empty cite" % (area, i))
                continue
            for k in map(int, re.findall(r"[0-9]+", cite)):
                cited.add(k)
                errs += ["%s %d: R7 cite has no source" % (area, i)] * (k > base and k not in declared or k < 1)
    errs += ["extraSources %d: R7 never cited" % i for k, i in declared.items() if k not in cited]
    return list(dict.fromkeys(errs)), n
def source_line(entry, docs):
    if entry.get("kind") != "record":
        return str(entry["label"])
    doc, req = docs.get(entry.get("document_id")), entry.get("request")
    if not doc:  # site rule 8: no id, no title for a record that is not posted
        return "%sCity records held by DeFlock SLO, not posted here." % ("Request %s, " % req if req else "")
    req, pages = req or doc.get("request"), [str(p) for p in entry.get("pages") or ()]
    where = ", page%s %s" % ("s" * (len(pages) > 1), ", ".join(pages)) if pages else ""
    return "%s%s%s; inventory document %s." % ("Request %s, " % req if req else "", doc["title"], where, doc["document_id"])
def assemble(cards, sources, location, base):
    if cards.get("schema") != 1 or not re.fullmatch(r"[a-z0-9-]+", str(cards.get("slug", ""))):
        raise Fail("cards 0: schema must be 1 with a lowercase slug")
    meta, by_n = cards["meta"], {e["n"]: e for e in sources}
    extra, docs = {x["key"]: x["text"] for x in cards.get("extra_sources") or ()}, {d["document_id"]: d for d in location["documents"]}
    missing = [k for k in META if k not in meta]
    if missing:
        raise Fail("meta 0: missing %d key(s)" % len(missing))
    numbers, lines, out, errs = {}, [], {}, []
    for area in CITED:
        out[area] = []
        for i, item in enumerate(cards.get(area) or ()):
            got = []
            for ref in item.get("sources") or ():
                key = ("s", ref) if isinstance(ref, int) and not isinstance(ref, bool) and ref in by_n else \
                      ("x", ref[2:]) if isinstance(ref, str) and ref.startswith("x:") and ref[2:] in extra else None
                if key is None:
                    errs.append("%s %d: R7 unknown source reference" % (area, i))
                    continue
                if key not in numbers:
                    numbers[key] = base + 1 + len(numbers)
                    lines.append("[%d] %s" % (numbers[key], source_line(by_n[key[1]], docs) if key[0] == "s" else extra[key[1]]))
                got.append(numbers[key])
            row = {k: v for k, v in item.items() if k != "sources"}
            row["cite"] = "[%s]" % ", ".join(map(str, sorted(set(got)))) if got else ""
            out[area].append(row)
    story = {k: meta[k] for k in META}
    story.update(programEvents=cards.get("programEvents") or [], keyDates=cards.get("keyDates") or [], extraSources=lines, **out)
    return {k: story[k] for k in KEYS}, errs
def report(errs, n):
    print("\n".join(errs + [" ".join("%s=%d" % (k, n[k]) for k in COUNTS) + " errors=%d" % len(errs)]))
    return 1 if errs else 0
def build(args):
    n, errs, tmp = dict.fromkeys(COUNTS, 0), [], None
    try:
        out = Path(os.path.expanduser(args.out)).resolve()
        if out == REPO or REPO in out.parents:
            raise Fail("out 0: refused inside the repository")
        location, thread = load(args.location), load(args.thread)
        story, errs = assemble(load(args.cards), load(args.sources), location, args.base_sources)
        more, n = lint(story, thread, location, args.base_sources, args.not_posted, args.not_posted_title)
        errs = list(dict.fromkeys(errs + more))
        if not errs and out.exists() and not args.replace:
            errs.append("out 0: exists; pass --replace to supersede it")
        if not errs:
            fd, tmp = tempfile.mkstemp(prefix="." + out.name + ".", suffix=".tmp", dir=out.parent)  # 0600, exclusive
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(story, indent=2, ensure_ascii=False) + "\n")
            if out.exists():
                old = out.with_name(out.name + ".superseded-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
                os.link(out, old)  # keep the old file aside; never deleted
            os.replace(tmp, out)
            tmp = None
    except Fail as exc:
        errs.append(str(exc))
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        errs.append("io 0: %s" % type(exc).__name__)
    finally:
        if tmp is not None:
            with contextlib.suppress(OSError):
                os.unlink(tmp)  # a failed build leaves no temp file
    return report(errs, n)
def run_lint(args):
    try:
        errs, n = lint(load(args.story), load(args.thread), load(args.location), args.base_sources, args.not_posted,
                       args.not_posted_title)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        errs, n = ["io 0: %s" % type(exc).__name__], dict.fromkeys(COUNTS, 0)
    return report(errs, n)
def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("build", "lint"):
        p = sub.add_parser(name)
        for flag in (("--cards", "--sources", "--out") if name == "build" else ("--story",)) + ("--location", "--thread"):
            p.add_argument(flag, required=True)
        p.add_argument("--base-sources", type=int, required=True)
        p.add_argument("--not-posted", nargs="*", default=[], action="extend")
        p.add_argument("--not-posted-title", nargs="*", default=[], action="extend")
        if name == "build":
            p.add_argument("--replace", action="store_true")
    args = parser.parse_args(argv)
    return build(args) if args.command == "build" else run_lint(args)
if __name__ == "__main__":
    sys.exit(main())
