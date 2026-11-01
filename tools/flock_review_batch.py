#!/usr/bin/env python3
"""Flock review batch: where every agency config in a list stands in the G1 pipeline, and what it waits for.
  status LIST [--run] [--roster ROSTER.csv] [--golden-config CFG --golden-expect FILE] [--out DIR] [--prev OLD.csv]
  | plan LIST [--max N] (N <= 2) [same options but --out, --prev]
status is read-only and writes only <out>/BATCH-STATUS.csv (and BATCH-CHANGES.csv with --prev: rows advanced, regressed, state, new, removed
since OLD.csv, which is read first). --run (needs the golden pair) advances every `ready` agency one flock_review
`run` at a time (exit 3 = reached a wait stage; exit 1 = failed, the batch goes on). plan prints at most N `[RUN]` card bodies for
agencies waiting on a model step. The Morro Bay golden must pass before --run or plan. Never runs a model, never touches kanban; no network.
Prints slugs, stage ids, states, counts and paths only. Exit 0 ok, 1 validation / gate / IO, 2 usage. done is not review or release."""
import argparse, contextlib, csv, importlib.util, io, os, re, subprocess, sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
def _tool(name):  # G1 and G5 are loaded by file path, never copied
    spec = importlib.util.spec_from_file_location("_batch_" + name, TOOLS / (name + ".py"))
    return spec.loader.exec_module(module := importlib.util.module_from_spec(spec)) or module
fr, gold = _tool("flock_review"), _tool("flock_review_golden")
HEADER = "slug,config,state,stages_done,stages_total,next,waiting_for,input_present,author_family,verifier_family,run_date".split(",")
ROSTER = "watch_agency,slug,in_scope,applicability,site_slug".split(",")
GATE = "golden gate: Morro Bay golden must pass first"
FACTS, RULES, COMPARE, VERIFY = ("sonnet-coder", "skills/agency-review/facts-intake step 5"), ("opus-coder-bopus", "skills/agency-review/rules-intake step 2"), \
    ("opus-coder-bopus", "skills/agency-review/compare steps 1-3, 6"), ("codex-reviewer", "G2 ## Verify + skills/flock-verify/PROMPT.md")
LANES = {"fact-drafts": FACTS, "rules-read": RULES, "assess": COMPARE, "f3-findings": COMPARE, "verify": VERIFY, "f4": VERIFY}
OTHER = {"anthropic": "openai", "openai": "anthropic"}
# flock_review's own failure lines (ids, codes, exception names, paths); no control characters, so no free text or escapes pass
REASON = re.compile(r"stage [a-z0-9][a-z0-9-]* failed rc=-?\d+ log=[^\x00-\x1f\x7f-\x9f]+|io: [A-Za-z_]\w*|locked: [^\x00-\x1f\x7f-\x9f]+", re.ASCII)
CHANGE, KINDS, BAD = "key,change,old_state,new_state,old_done,new_done".split(","), ("regressed", "advanced", "state", "new", "removed"), \
    {"failed", "invalid", "locked"}


def read_roster(path):
    try:
        with open(path, encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames != ROSTER:
                raise fr.Fail("roster: header must be " + ",".join(ROSTER))
            roster, rows = {}, {}
            for n, r in enumerate(reader, 2):  # the header is row 1
                slug = (r.get("slug") or "").strip()
                if not slug or slug in rows:
                    raise fr.Fail("roster row %d: " % n + ("slug already used on row %d" % rows[slug] if slug else "slug is empty"))
                roster[slug], rows[slug] = r, n
            return roster
    except (OSError, ValueError, csv.Error):
        raise fr.Fail("roster: ROSTER cannot be read") from None


def out_dir(raw):  # checked before anything runs: an existing real folder, or a new one in a real folder; never a symlink
    out = Path(os.path.abspath(os.path.expanduser(raw)))
    if fr.under(out.resolve(), fr.REPO):
        raise fr.Fail("out: --out is inside the repository")
    if out.is_symlink() or not (out.is_dir() or (not os.path.lexists(out) and out.parent.is_dir() and not out.parent.is_symlink())):
        raise fr.Fail("out: --out must be a real folder")
    return out


def read_list(path):  # -> [context, ...] in file order; an unreadable config stays in the list (it will be `invalid`)
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except (OSError, ValueError):
        raise fr.Fail("list: LIST cannot be read") from None
    entries, paths, slugs = [], {}, {}
    for n, line in enumerate((x.strip() for x in lines), 1):
        if not line or line.startswith("#"):
            continue
        real = Path(os.path.expanduser(line)).resolve()
        if real in paths:
            raise fr.Fail("list line %d: config already listed on line %d" % (n, paths[real]))
        try:
            c = fr.context(real)
        except fr.Fail:
            c = {"path": real, "slug": ""}
        if c["slug"] and c["slug"] in slugs:
            raise fr.Fail("list line %d: slug already used on line %d" % (n, slugs[c["slug"]]))
        paths[real], slugs[c["slug"]] = n, n
        entries.append(c)
    if not entries:
        raise fr.Fail("list: no configs")
    return entries


def marks(c):  # provider of each stage mark, from REVIEW-STATE.json
    stages = fr.load_state(c)["stages"]
    return {sid: ((stages.get(sid) or {}).get("mark") or {}).get("provider") or "" for sid in fr.IDS}


def worked(p):  # real model work: a non-empty file; a CSV needs a row past its header (f4-start leaves a header-only one)
    try:
        lines = [x for x in p.read_text(encoding="utf-8", errors="replace").splitlines() if x.strip()] if p.is_file() else []
    except OSError:
        return False
    return len(lines) > (1 if p.suffix == ".csv" else 0)



def agency(c, roster):  # read-only; -> row dict (HEADER keys) plus 'files' (the wait stage's input files)
    row = dict.fromkeys(HEADER, "")
    row.update(slug=c["slug"], config=str(c["path"]), state="invalid", files=[])
    if "cfg" not in c or fr.check(c):
        return row
    try:
        state, dig, made = fr.load_state(c), {}, marks(c)
        words = [fr.evaluate(c, state, dig, i)[0] for i in range(len(fr.STAGES))]
        todo = [i for i, w in enumerate(words) if w not in ("done", "skip")]
        bad = [i for i, w in enumerate(words) if w == "failed"]
        first = bad[0] if bad else todo[0] if todo else None
        sid, kind = (fr.IDS[first], fr.STAGES[first][1]) if first is not None else ("", "")
        files = fr.wait_files(c, sid) if kind == "wait" else []
        present = bool(files) and all(p.is_file() for p in files)  # file presence only
        marked = ((state["stages"].get(sid) or {}).get("mark") or {}).get("files", {})
        # Shared rules inputs do not establish agency-specific activity: only its explicit marker does.
        active = (c["out"] / ".review" / "in-flight" / sid).is_file() or (sid != "rules-read" and any(
            worked(p) and (str(p) not in marked or fr.fhash(p) != marked[str(p)]) for p in files))
    except (fr.Fail, OSError, KeyError, TypeError, ValueError):
        return row
    slug, scope = c["slug"], (roster or {}).get(c["slug"]) or {}
    held = roster is not None and ((scope.get("in_scope") or "").strip() != "yes" or (scope.get("applicability") or "") != c["appl"])
    word = "held" if held else "locked" if fr.lock_state(c)[0] == "held" else "failed" if bad else "done" if first is None \
        else ("in-flight" if active else "waiting") if kind == "wait" else "ready"
    row.update(state=word, stages_done=len(words) - len(todo), stages_total=len(words), next=sid, files=files, run_date=c["date"],
               input_present=("yes" if present else "no") if kind == "wait" else "", author_family=made.get("assess", ""),
               verifier_family=made.get("verify", ""), waiting_for=sid if word in ("waiting", "in-flight") else "")
    return row


def gate(args):  # the Morro Bay golden, in-process: `golden ok` or nothing moves
    if not (args.golden_config and args.golden_expect):
        raise fr.Fail(GATE)
    shown, code = io.StringIO(), 1
    try:
        c = gold.fr.context(args.golden_config)
        if not gold.fr.check(c):
            with contextlib.redirect_stdout(shown):
                code = gold.compare(gold.counts(c), args.golden_expect)
    except gold.fr.Fail as exc:
        shown.write("%s\n" % exc)
    except OSError as exc:
        shown.write("io: %s\n" % type(exc).__name__)
    print(shown.getvalue(), end="")
    if code:
        raise fr.Fail(GATE)


def advance(entries, roster):  # one flock_review run at a time, in list order; no stage is forced, re-marked or skipped
    rows = [agency(c, roster) for c in entries]
    for i, (c, row) in enumerate(zip(entries, rows)):
        if row["state"] == "ready":
            done = subprocess.run([sys.executable, "-B", str(TOOLS / "flock_review.py"), "run", str(c["path"])], stdin=subprocess.DEVNULL,
                                  capture_output=True, check=False)
            rows[i] = agency(c, roster)
            failed = done.returncode not in (0, 3)
            rows[i]["state"] = "failed" if failed else rows[i]["state"]
            print("run %s rc=%d state=%s" % (c["slug"], done.returncode, rows[i]["state"]))
            if failed:  # the child's own failure line from stdout only (never stderr or free text), else a fixed form
                why = [x for x in (done.stdout or b"").decode("utf-8", "replace").splitlines() if REASON.fullmatch(x)]
                print("run %s reason: %s" % (c["slug"], why[-1][:300] if why else "unknown (see flock_review status %s)" % c["path"]))
    return rows


def key(row):  # the slug; an invalid row has none, so its config path
    return (row.get("slug") or "") or "config:" + str(row.get("config") or "")


def read_prev(raw):  # read before anything runs or is written, so --prev may be the BATCH-STATUS.csv this call replaces
    path = Path(os.path.expanduser(raw))
    if path.is_symlink():
        raise fr.Fail("prev: is a link; refused")
    try:
        with open(path, encoding="utf-8", newline="", opener=lambda p, f: os.open(p, f | os.O_NOFOLLOW)) as handle:
            reader, prev = csv.DictReader(handle), {}
            if reader.fieldnames != HEADER:
                raise fr.Fail("prev: header differs")
            for n, r in enumerate(reader, 2):  # the header is row 1
                if key(r) in prev:
                    raise fr.Fail("prev row %d: duplicate" % n)
                prev[key(r)] = r
            return prev
    except (OSError, ValueError, csv.Error):
        raise fr.Fail("prev: cannot be read") from None


def changes(prev, rows):  # -> [(kind, key, old_state, new_state, old_done, new_done)] sorted; unchanged ones carry kind ""
    def done(r):
        try:
            return int(r["stages_done"])
        except (KeyError, TypeError, ValueError):
            return None  # unknown: only the state is compared
    cell = lambda r, f: "" if r is None or r.get(f) is None else str(r[f])  # noqa: E731
    now, found = {key(r): r for r in rows}, []
    for k in now.keys() | prev.keys():
        old, new = prev.get(k), now.get(k)
        before, after = done(old), done(new)
        moved = after - before if before is not None and after is not None else 0  # stages_done delta; 0 when unknown
        kind = "new" if old is None else "removed" if new is None else "regressed" if moved < 0 or (
            new["state"] in BAD and old["state"] not in BAD) else "advanced" if moved > 0 else \
            "state" if new["state"] != old["state"] else ""
        found.append((kind, k, cell(old, "state"), cell(new, "state"), cell(old, "stages_done"), cell(new, "stages_done")))
    return sorted(found, key=lambda c: (KINDS.index(c[0]) if c[0] else len(KINDS), c[1]))


def plan(entries, rows, limit):  # at most `limit` model cards in flight, counting the agencies whose card is already working
    print("CARDS")
    for c, row in [(c, r) for c, r in zip(entries, rows) if r["state"] == "waiting"][:max(limit - sum(r["state"] == "in-flight" for r in rows), 0)]:
        sid, (lane, skill) = row["next"], LANES[row["next"]]
        made = marks(c)
        authors = {made["assess"], made["f3-findings"]} - {""}  # the families a verifier must differ from
        files = [str(p) for p in row["files"]] or ["%s/drafts/<name>.yaml" % c["FO"]]
        print("\n".join(["[RUN] %s %s %s" % (row["slug"], sid, row["run_date"]), "lane: %s (the dispatcher confirms the actual runtime)" % lane,
                         "method: " + skill, "write:"] + ["  " + f for f in files] + [
                         "mark: python3 tools/flock_review.py mark %s %s --provider <actual runtime> --model <actual runtime>" % (row["config"], sid),
                         "caps: 40 turns / 2 h; chunk by document; write REMAINING.md on cap"]
                        + (["family: " + (OTHER.get(next(iter(authors)), "operator picks") if len(authors) == 1 else "operator picks")] if sid in ("verify", "f4") else [])),
              end="\n\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("status", "plan"))
    parser.add_argument("list", metavar="LIST")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--max", type=int, default=2, metavar="N")
    for name in ("--roster", "--golden-config", "--golden-expect", "--out", "--prev"):
        parser.add_argument(name)
    args = parser.parse_args(argv)
    if (args.run and args.command != "status") or not 1 <= args.max <= 2:
        parser.error("usage: status LIST [--run] | plan LIST [--max 1|2]")
    if args.out and args.command == "plan":
        parser.error("plan prints cards only; --out is for status")
    if args.prev and args.command == "plan":
        parser.error("plan prints cards only; --prev is for status")
    try:
        prev = read_prev(args.prev) if args.prev else None
        out = out_dir(args.out) if args.out else None
        roster, entries = read_roster(args.roster) if args.roster else None, read_list(args.list)
        if args.run or args.command == "plan":
            gate(args)
        rows = advance(entries, roster) if args.run else [agency(c, roster) for c in entries]
        if args.command == "plan":
            return plan(entries, rows, args.max) or 0
        for row in rows:
            print("%s %s %s/%s next=%s" % (row["slug"] or "-", row["state"], row["stages_done"], row["stages_total"], row["next"] or "-"))
        if out:
            text = io.StringIO()
            writer = csv.DictWriter(text, fieldnames=HEADER, extrasaction="ignore", lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
            fr.put(out / "BATCH-STATUS.csv", text.getvalue().encode())
        if prev is not None:  # what moved since the last status (unchanged rows are counted, not listed)
            found = changes(prev, rows)
            for c in (c for c in found if c[0]):
                print("change %s %s %s->%s %s->%s" % (c[1], c[0], c[2] or "-", c[3] or "-", c[4] or "-", c[5] or "-"))
            print("changes " + " ".join("%s=%d" % (k, sum(c[0] == k for c in found)) for k in ("advanced", "regressed", "state", "new",
                  "removed")) + " unchanged=%d" % sum(not c[0] for c in found))
            if out:
                text = io.StringIO()
                csv.writer(text, lineterminator="\n").writerows([CHANGE] + [[c[1], c[0], *c[2:]] for c in found if c[0]])
                fr.put(out / "BATCH-CHANGES.csv", text.getvalue().encode())
        count = lambda word: sum(r["state"] == word for r in rows)  # noqa: E731
        print("agencies=%d done=%d waiting=%d in_flight=%d failed=%d invalid=%d held=%d locked=%d ready=%d" % (
            len(rows), count("done"), count("waiting"), count("in-flight"), count("failed"), count("invalid"), count("held"), count("locked"),
            count("ready")))
        return 0
    except fr.Fail as exc:
        return print(exc) or exc.code
    except OSError as exc:
        return print("io: %s" % type(exc).__name__) or 1


if __name__ == "__main__":
    sys.exit(main())
