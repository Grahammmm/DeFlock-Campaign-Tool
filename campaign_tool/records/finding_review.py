"""Human finding review before packet staging (DF2007-05, CON-FIND-1).

A model's PUBLIC-OK mark is not a human decision. `ROOT/.analysis/finding-reviews.jsonl` is a private, append-only,
hash-chained journal of human decisions about the *exact* finding (agency slug, finding_id and a sha256 over topic, claim,
fact_ids and rule_ids). An approve is an SSH signature over `finding_payload`, scope `finding-review-v1`, namespace
`deflock-approval`, checked by `approval.verify` (the protected trust anchor; nothing here copies or weakens it). A reject
carries a private reason. The analysis packet step (`analysis_bridge._packet_one`) holds a finding with FINDING_UNREVIEWED
until a bound approve verifies, FINDING_REJECTED after a reject and FINDING_REVIEW_DEFERRED while the journal or the trust
anchor is unavailable. A changed claim, topic, fact list or rule list is a different finding and needs a new review. The
later packet approval (`public_packets`, scope `public-packet-v1`) stays a separate decision; nothing here sends, publishes,
redacts or enables anything.

The chain is tamper-evident, not anchored: a complete rewrite or removal of a suffix cannot be detected from the file alone.

  records finding-review list --root R [--agency SLUG] [--finding-id ID] [--json | --payload]
  records finding-review approve --root R --agency SLUG --finding-id ID --signature-file F [--expected-hash H] [--entry-id E]
  records finding-review reject --root R --agency SLUG --finding-id ID --reason TEXT [--expected-hash H] [--entry-id E]

Exit 0 success or no-op, 1 refusal or deferral (one fixed code is printed), 2 usage.
"""
import argparse
import datetime
import fcntl
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path

from .. import approval
from . import analysis_config as ac

SCHEMA = SCOPE = "finding-review-v1"
JOURNAL = (".analysis", "finding-reviews.jsonl")
MAX = 64 << 20
MAX_REASON = 500
DEFERRED, UNAVAILABLE = "FINDING_REVIEW_DEFERRED", "finding_review_unavailable"
HOLDS = {"unreviewed": "FINDING_UNREVIEWED", "rejected": "FINDING_REJECTED", "deferred": DEFERRED}
_HEX, _AT, _ENTRY = re.compile(r"[0-9a-f]{64}"), re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z"), re.compile(r"[A-Za-z0-9._:-]{1,128}")
_COMMON = {"schema", "agency", "finding_id", "finding_sha256", "decision", "at", "prev_line_sha256"}
_APPROVE = _COMMON | {"principal", "signature", "topic", "claim", "fact_ids", "rule_ids"}
_REJECT = _COMMON | {"reason"}


class FindingReviewError(Exception):
    """A refusal with one fixed, input-free code."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _canon(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _sha(data):
    return hashlib.sha256(data if isinstance(data, bytes) else data.encode("utf-8")).hexdigest()


def _ids(value):
    """Ordered ID tokens of a semicolon-separated CSV cell; nothing is deduplicated, sorted or repaired."""
    if not isinstance(value, str):
        raise FindingReviewError("finding_malformed")
    tokens = value.split(";") if value else []
    if any(not t.strip() or t != t.strip() for t in tokens):
        raise FindingReviewError("finding_malformed")
    return tokens


def _fields(finding):
    """-> {topic, claim, fact_ids[], rule_ids[]} from a finding row (strings) or raise finding_malformed."""
    try:
        topic, claim = finding["topic"], finding["claim"]
        facts, rules = _ids(finding["fact_ids"]), _ids(finding["rule_ids"])
    except (KeyError, TypeError):
        raise FindingReviewError("finding_malformed") from None
    if not isinstance(topic, str) or not isinstance(claim, str) or not facts:
        raise FindingReviewError("finding_malformed")
    return {"topic": topic, "claim": claim, "fact_ids": facts, "rule_ids": rules}


def finding_hash(finding):
    """Lowercase SHA-256 over canonical JSON of exactly topic, claim, fact_ids, rule_ids (no clock, verdict or journal state)."""
    return _sha(_canon(_fields(finding)))


def finding_payload(agency, finding):
    """The exact text a reviewer signs (scope finding-review-v1, round 0): the full finding plus its binding."""
    fid = finding.get("finding_id") if isinstance(finding, dict) else None
    if not isinstance(agency, str) or not isinstance(fid, str):
        raise FindingReviewError("finding_malformed")
    fields = _fields(finding)
    content = _canon(dict(fields, agency=agency, finding_id=fid, finding_sha256=finding_hash(finding)))
    try:
        return approval.payload(SCOPE, agency + ":" + fid, content).decode("utf-8")
    except approval.ApprovalError:
        raise FindingReviewError("finding_malformed") from None


def _row(entry):
    """The finding row an approve entry retained, so its signed payload can be rebuilt exactly."""
    return {"finding_id": entry["finding_id"], "topic": entry["topic"], "claim": entry["claim"],
            "fact_ids": ";".join(entry["fact_ids"]), "rule_ids": ";".join(entry["rule_ids"])}


# ---- the journal -----------------------------------------------------------------------------------------------------

def _path(root):
    return Path(root).joinpath(*JOURNAL)


def _check_parent(root):
    """The analysis directory must be a real owner-only directory of this user (never a symlink)."""
    st = os.lstat(_path(root).parent)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o077:
        raise FindingReviewError(UNAVAILABLE)


def _check_leaf(st):
    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid != os.geteuid() or st.st_mode & 0o177 or st.st_size > MAX:
        raise FindingReviewError(UNAVAILABLE)


def _line_entry(line, prev):
    """-> the strictly validated entry of one canonical journal line or raise finding_review_unavailable."""
    entry = None
    try:
        entry = json.loads(line.decode("utf-8"), parse_constant=_no_constant)
        ok = isinstance(entry, dict) and _canon(entry).encode("utf-8") == line and entry.get("prev_line_sha256") == prev
        kind = entry.get("decision") if ok else None
        keys = set(entry) - {"entry_id"} if ok else set()
        ok = ok and keys == (_APPROVE if kind == "approve" else _REJECT if kind == "reject" else None) and entry["schema"] == SCHEMA
        ok = ok and ac.SLUG.fullmatch(entry["agency"]) and _text(entry["finding_id"], 200) and _HEX.fullmatch(entry["finding_sha256"])
        ok = ok and _AT.fullmatch(entry["at"]) and (entry.get("entry_id") is None or _ENTRY.fullmatch(entry["entry_id"]))
        if ok and kind == "approve":
            row = _row(entry)
            ok = (_text(entry["principal"], 200) and _text(entry["signature"], approval.MAX_SIGNATURE, multiline=True)
                  and finding_hash(row) == entry["finding_sha256"] and _fields(row) == {k: entry[k] for k in ("topic", "claim", "fact_ids", "rule_ids")})
        elif ok:
            ok = _reason(entry["reason"])
    except (ValueError, TypeError, KeyError, AttributeError, FindingReviewError):
        ok = False
    if not ok or entry is None:
        raise FindingReviewError(UNAVAILABLE)
    return entry


def _no_constant(_):
    raise ValueError("NaN or Infinity")


def _text(value, limit, multiline=False):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= limit and (multiline or value.isprintable())


def _reason(value):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= MAX_REASON and len(value.strip()) <= MAX_REASON and value.isprintable()


def _parse(data):
    """-> (entries, sha256 of the last complete line or None); the whole chain is validated or finding_review_unavailable."""
    if not data:
        return [], None
    if not data.endswith(b"\n"):
        raise FindingReviewError(UNAVAILABLE)
    entries, prev = [], None
    for line in data[:-1].split(b"\n"):
        entries.append(_line_entry(line, prev))
        prev = _sha(line)
    return entries, prev


def _read(root):
    """-> (entries, tail digest). An absent journal is empty and nothing is created, locked, cached or written."""
    try:
        _check_parent(root)
        fd = os.open(_path(root), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return [], None
    except (OSError, FindingReviewError):
        raise FindingReviewError(UNAVAILABLE) from None
    try:
        st = os.fstat(fd)
        _check_leaf(st)
        return _parse(os.pread(fd, st.st_size, 0))
    except OSError:
        raise FindingReviewError(UNAVAILABLE) from None
    finally:
        os.close(fd)


def _locked_append(root, decide):
    """Exclusive lock over re-read, validation and one fsynced O_APPEND line. decide(entries, tail) -> (entry dict | None, result)."""
    try:
        _check_parent(root)
        fd = os.open(_path(root), os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    except (OSError, FindingReviewError):
        raise FindingReviewError(UNAVAILABLE) from None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        st = os.fstat(fd)
        _check_leaf(st)
        entries, tail = _parse(os.pread(fd, st.st_size, 0))
        entry, result = decide(entries, tail)
        if entry is not None:
            line = _canon(dict(entry, prev_line_sha256=tail)).encode("utf-8")
            if st.st_size + len(line) + 1 > MAX:
                raise FindingReviewError(UNAVAILABLE)
            data, done = line + b"\n", 0
            while done < len(data):
                done += os.write(fd, data[done:])
            os.fsync(fd)
        return result
    except OSError:
        raise FindingReviewError(UNAVAILABLE) from None
    finally:
        os.close(fd)


def _latest(entries, agency, finding_id, digest):
    return next((e for e in reversed(entries) if (e["agency"], e["finding_id"], e["finding_sha256"]) == (agency, finding_id, digest)), None)


# ---- current review state --------------------------------------------------------------------------------------------

class Reviews:
    """The review state of findings of one root, read once; a signed approve is re-verified (and cached) when it is used."""

    def __init__(self, root):
        self.cache = {}
        try:
            self.entries, self.error = _read(root)[0], None
        except FindingReviewError as exc:
            self.entries, self.error = [], exc.code

    def state(self, agency, finding, ambiguous=False):
        """-> {state: unreviewed|approved|rejected|deferred, reason, principal, code} for the exact current finding."""
        try:
            digest = finding_hash(finding)
            finding_id = finding["finding_id"]
        except (FindingReviewError, KeyError, TypeError):
            return {"state": "unreviewed", "code": "finding_malformed"}
        if ambiguous:
            return {"state": "unreviewed", "code": "finding_ambiguous"}
        if self.error:
            return {"state": "deferred", "code": self.error}
        key = (agency, finding_id, digest)
        if key not in self.cache:
            self.cache[key] = self._decide(_latest(self.entries, *key), agency)
        return self.cache[key]

    @staticmethod
    def _decide(latest, agency):
        if latest is None:
            return {"state": "unreviewed"}
        if latest["decision"] == "reject":
            return {"state": "rejected", "reason": latest["reason"]}
        try:
            principal = approval.verify(finding_payload(agency, _row(latest)).encode("utf-8"), latest["signature"])
        except approval.TrustUnavailable:
            return {"state": "deferred", "code": DEFERRED}
        except (approval.ApprovalError, FindingReviewError):
            return {"state": "unreviewed", "code": "finding_signature_refused"}
        if principal != latest["principal"]:
            return {"state": "unreviewed", "code": "finding_signature_refused"}
        return {"state": "approved", "principal": principal}

    def gate(self, agency, finding, ambiguous=False):
        """-> None when a bound, verified approve exists; else (hold code, private reason or None)."""
        state = self.state(agency, finding, ambiguous)
        if state["state"] == "approved":
            return None
        return HOLDS[state["state"]], state.get("reason")


# ---- the API ---------------------------------------------------------------------------------------------------------

def _admit(root):
    root = Path(root).resolve()
    if not root.is_dir() or ac._flock_review().under(root, ac.REPO) or ac._store_like(root):
        raise FindingReviewError("finding_root_refused")
    return root


def _evaluate(root, agency, reviews):
    from . import analysis_bridge as bridge  # lazy: the bridge imports this module
    if not isinstance(agency, str) or not ac.SLUG.fullmatch(agency):
        raise FindingReviewError("finding_unknown")
    try:
        config = json.loads((root / "agencies" / agency / "review-config.json").read_text(encoding="utf-8"))
        gate = reviews and (lambda fid, found, dup: reviews.gate(agency, dict(found, finding_id=fid), dup))
        return bridge._evaluate(root, agency, config, gate or None)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise FindingReviewError("finding_inputs_unavailable") from None


def _verdicts(ev):
    from . import analysis_bridge as bridge
    return {r.get("finding_id"): r.get("verdict") for r in bridge._table(ev["run"] / "VERIFY.csv")}


def list_findings(root, *, agency=None):
    """Read-only rows for the current PUBLIC-OK findings, in agency / finding_id order; each carries the exact payload to sign."""
    root = _admit(root)
    reviews = Reviews(root)
    names = sorted(p.parent.name for p in root.glob("agencies/*/review-config.json"))
    rows = []
    for slug in [agency] if agency else names:
        try:
            ev = _evaluate(root, slug, reviews)
        except FindingReviewError:
            if agency:
                raise
            continue
        verdicts = _verdicts(ev)
        for row, found, _, hold in ev["results"]:
            row_out = {"agency": slug, "finding_id": row["finding_id"], "review_state": "unreviewed", "reason": None,
                       "hold_code": hold["code"] if hold else None, "f4_verdict": verdicts.get(row["finding_id"]) or "unavailable"}
            if found is not None:
                finding = dict(found, finding_id=row["finding_id"])
                state = reviews.state(slug, finding, row["finding_id"] in ev["dups"])
                try:
                    digest, payload = finding_hash(finding), finding_payload(slug, finding)
                except FindingReviewError:
                    digest = payload = None
                row_out.update(topic=found["topic"], claim=found["claim"], fact_ids=found["fact_ids"], rule_ids=found["rule_ids"],
                               finding_sha256=digest, payload=payload, review_state=state["state"], reason=state.get("reason"),
                               facts=[{"fact_id": i, "source_sha256": ev["facts"].get(i, {}).get("source_sha256"), "page": ev["facts"].get(i, {}).get("page")}
                                      for i in found["fact_ids"].split(";") if i])
            rows.append(row_out)
    return sorted(rows, key=lambda r: (r["agency"], r["finding_id"]))


def _target(root, agency, finding_id, expected_hash):
    """The one current finding to decide on -> (finding row, its hash); refuses unknown, ambiguous, malformed or stale."""
    ev = _evaluate(root, agency, None)
    matches = [(r, f) for r, f, _, _ in ev["results"] if r["finding_id"] == finding_id]
    if not matches or matches[0][1] is None:
        raise FindingReviewError("finding_unknown")
    if len(matches) > 1 or finding_id in ev["dups"]:
        raise FindingReviewError("finding_ambiguous")
    finding = dict(matches[0][1], finding_id=finding_id)
    digest = finding_hash(finding)
    if expected_hash is not None and expected_hash != digest:
        raise FindingReviewError("finding_stale")
    return finding, digest


def _entry(kind, agency, finding_id, digest, entry_id, **extra):
    if entry_id is not None and not (isinstance(entry_id, str) and _ENTRY.fullmatch(entry_id)):
        raise FindingReviewError("finding_entry_id_invalid")
    entry = {"schema": SCHEMA, "agency": agency, "finding_id": finding_id, "finding_sha256": digest, "decision": kind,
             "at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), **extra}
    return dict(entry, entry_id=entry_id) if entry_id else entry


def _decide(root, agency, finding_id, expected_hash, entry_id, kind, same, extra):
    """Shared writer: re-check the binding under the journal lock, then append unless the exact decision is already the latest."""
    root = _admit(root)
    finding, digest = _target(root, agency, finding_id, expected_hash)
    extra = extra(finding, digest)  # a signature is verified before anything is opened or created

    def decide(entries, tail):
        _, now = _target(root, agency, finding_id, digest)  # the same finding still, inside the lock
        latest = _latest(entries, agency, finding_id, now)
        if latest is not None and latest["decision"] == kind and same(latest):
            return None, dict(latest, idempotent=True)
        entry = _entry(kind, agency, finding_id, now, entry_id, **extra)
        return entry, dict(entry, prev_line_sha256=tail, idempotent=False)
    return _locked_append(root, decide)


def approve(root, agency, finding_id, signature, *, expected_hash=None, entry_id=None):
    """Record a human approval of the exact current finding; the signature must verify against the protected trust anchor.

    Trust or verifier unavailable -> FindingReviewError(FINDING_REVIEW_DEFERRED) with nothing appended (retry later).
    """
    def extra(finding, digest):
        try:
            principal = approval.verify(finding_payload(agency, finding).encode("utf-8"), signature)
        except approval.TrustUnavailable:
            raise FindingReviewError(DEFERRED) from None
        except approval.ApprovalError:
            raise FindingReviewError("finding_signature_refused") from None
        return dict(principal=principal, signature=signature, **_fields(finding))
    return _decide(root, agency, finding_id, expected_hash, entry_id, "approve",
                   lambda latest: latest["signature"] == signature, extra)


def reject(root, agency, finding_id, reason, *, expected_hash=None, entry_id=None):
    """Record a human rejection (private reason, no signature) of the exact current finding."""
    if not _reason(reason):
        raise FindingReviewError("finding_reason_invalid")
    return _decide(root, agency, finding_id, expected_hash, entry_id, "reject",
                   lambda latest: latest["reason"] == reason, lambda finding, digest: {"reason": reason})


# ---- command line ----------------------------------------------------------------------------------------------------

def _signature(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        raise FindingReviewError("finding_signature_unreadable") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise FindingReviewError("finding_signature_unreadable")
        data = os.read(fd, approval.MAX_SIGNATURE + 1)
        if len(data) > approval.MAX_SIGNATURE:
            raise FindingReviewError("finding_signature_unreadable")
        return data.decode("utf-8")
    except (OSError, UnicodeError):
        raise FindingReviewError("finding_signature_unreadable") from None
    finally:
        os.close(fd)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="records finding-review", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    verbs = parser.add_subparsers(dest="verb", required=True)
    for name in ("list", "approve", "reject"):
        sub = verbs.add_parser(name)
        sub.add_argument("--root", required=True)
        sub.add_argument("--agency", required=name != "list")
        sub.add_argument("--finding-id", required=name != "list")
        if name != "list":
            sub.add_argument("--expected-hash")
            sub.add_argument("--entry-id")
    lister = verbs.choices["list"]
    lister.add_argument("--json", action="store_true")
    lister.add_argument("--payload", action="store_true", help="print only the exact payload of the one --agency/--finding-id finding")
    verbs.choices["approve"].add_argument("--signature-file", required=True)
    verbs.choices["reject"].add_argument("--reason", required=True)
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    if args.verb == "list" and args.payload and not (args.agency and args.finding_id and not args.json):
        parser.error("--payload needs --agency and --finding-id and excludes --json")
    os.umask(0o077)
    try:
        if args.verb == "list":
            rows = [r for r in list_findings(args.root, agency=args.agency) if not args.finding_id or r["finding_id"] == args.finding_id]
            if args.payload:
                if len(rows) != 1 or not rows[0].get("payload"):
                    raise FindingReviewError("finding_unknown")
                sys.stdout.write(rows[0]["payload"])
            elif args.json:
                print(json.dumps(rows, indent=1, sort_keys=True))
            else:
                for r in rows:
                    print("%s %s %s %s%s" % (r["agency"], r["finding_id"], (r.get("finding_sha256") or "-")[:12], r["review_state"],
                                             " hold=" + r["hold_code"] if r["hold_code"] else ""))
            return 0
        if args.verb == "approve":
            done = approve(args.root, args.agency, args.finding_id, _signature(args.signature_file), expected_hash=args.expected_hash, entry_id=args.entry_id)
        else:
            done = reject(args.root, args.agency, args.finding_id, args.reason, expected_hash=args.expected_hash, entry_id=args.entry_id)
    except FindingReviewError as exc:
        print(exc.code)
        return 1
    print("%s %s %s %s%s" % ("FINDING_REVIEW_UNCHANGED" if done["idempotent"] else "FINDING_APPROVED" if args.verb == "approve" else "FINDING_REJECTED",
                              done["agency"], done["finding_id"], done["finding_sha256"][:12], " " + done["principal"] if args.verb == "approve" else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
