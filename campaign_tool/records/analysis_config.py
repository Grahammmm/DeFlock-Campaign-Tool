"""Build the flock_review CONFIG for one agency from an operator profile (records-review-profile-v1)."""
import argparse
import datetime
import importlib.util
import errno
import json
import os
import re
import stat
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCHEMA = "records-review-profile-v1"
PAGE_RE = r"^\[\[PAGE (\d+)\]\]\s*$"
SLUG = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
# block -> (required keys, optional keys); an unknown key anywhere is PROFILE_INVALID
SHAPE = {
    None: ({"schema", "records_agency_id", "slug", "fact_agency", "agency_name", "portal", "review", "rules_intake", "compare_f3"},
           {"not_posted_analysis_only_ids", "log_doc_ids"}),
    "review": ({"method_dir", "method_python", "added_by"},
               {"roster_doc_id", "ocr_log_doc_ids", "training_join", "region_config", "verify_sample"}),
    "rules_intake": ({"law_specs", "policy_specs", "extra_specs", "f2_law_map", "sources"}, {"added_by", "added_at"}),
    "compare_f3": ({"applicability"}, {"golden"}),
}


def _validate(profile, slug):
    """Raise ValueError unless the profile has the frozen shape for this slug."""
    if not isinstance(profile, dict) or profile.get("schema") != SCHEMA or profile.get("slug") != slug:
        raise ValueError("schema or slug")
    for block, (need, may) in SHAPE.items():
        node = profile if block is None else profile.get(block)
        if not isinstance(node, dict) or not need <= set(node) <= need | may:
            raise ValueError(block or "profile")


# explicit required placeholders in a scaffolded candidate: dotted path -> expected leaf type (a null or blank leaf is incomplete)
REQUIRED_LEAVES = {"portal": str, "review.method_dir": str, "review.method_python": str, "review.added_by": str,
                   "rules_intake.law_specs": list, "rules_intake.policy_specs": list, "rules_intake.f2_law_map": str,
                   "compare_f3.applicability": str}
CANDIDATE = ".candidate"
MAX_BYTES = 1 << 20
AGENCY_ID = re.compile(r"[a-z][a-z0-9-]{0,63}")


def _incomplete_fields(profile):
    """Sorted dotted paths of required leaves that are still null or blank; ValueError for a wrong non-placeholder type."""
    missing = []
    for path, kind in REQUIRED_LEAVES.items():
        node = profile
        for key in path.split("."):
            node = node[key]
        if node is None or (kind is str and isinstance(node, str) and not node.strip()):
            missing.append(path)
        elif not isinstance(node, kind):
            raise ValueError(path)
    return sorted(missing)


def _strict_json(text):
    def pairs(items):
        keys = [k for k, _ in items]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate key")
        return dict(items)

    def refuse(_):
        raise ValueError("nonfinite")
    try:
        return json.loads(text, object_pairs_hook=pairs, parse_constant=refuse)
    except RecursionError as exc:
        raise ValueError("depth") from exc


def _read_regular(path, dir_fd=None):
    """Bounded read of one regular file without following a final symlink; OSError/ValueError on anything else."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("not a regular file")
        data = b""
        while len(data) <= MAX_BYTES:
            chunk = os.read(fd, MAX_BYTES + 1 - len(data))
            if not chunk:
                break
            data += chunk
    finally:
        os.close(fd)
    if len(data) > MAX_BYTES:
        raise ValueError("too large")
    return _strict_json(data.decode("utf-8"))


def _open_dir(path):
    """Open an absolute directory with no symlink in any component; returns an fd (OSError otherwise)."""
    path = Path(os.path.abspath(path))
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
        return fd
    except BaseException:
        os.close(fd)
        raise


def _campaign_root(root_fd, given):
    if given:
        return Path(given)
    try:
        fd = os.open(".campaign-root", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root_fd)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        raw = os.read(fd, 4097)
    finally:
        os.close(fd)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    line = text[:-1] if text.endswith("\n") else text
    if len(raw) > 4096 or not line or "\n" in line or "\x00" in line or not os.path.isabs(line):
        return None
    return Path(line)


def _agency_name(campaign, agency_id):
    """The exact campaign agency's validated name, or a fixed SCAFFOLD_* code (never names, paths or details)."""
    try:
        cfd = _open_dir(campaign)
    except OSError:
        return None, "SCAFFOLD_SOURCE_UNAVAILABLE"
    try:
        sub = os.open("content", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=cfd)
        try:
            agencies = _read_regular("agencies.json", dir_fd=sub)
        finally:
            os.close(sub)
    except OSError:
        return None, "SCAFFOLD_SOURCE_UNAVAILABLE"
    except ValueError:
        return None, "SCAFFOLD_AGENCIES_INVALID"
    finally:
        os.close(cfd)
    if not isinstance(agencies, list) or not all(isinstance(a, dict) and isinstance(a.get("agency_id"), str) for a in agencies):
        return None, "SCAFFOLD_AGENCIES_INVALID"
    ids = [a["agency_id"] for a in agencies]
    if len(ids) != len(set(ids)):
        return None, "SCAFFOLD_AGENCIES_INVALID"
    found = [a for a in agencies if a["agency_id"] == agency_id]
    if not found:
        return None, "SCAFFOLD_AGENCY_MISSING"
    name = found[0].get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > 200 or any(ord(c) < 32 or ord(c) == 127 for c in name):
        return None, "SCAFFOLD_AGENCIES_INVALID"
    return name, None


def _scaffold_object(slug, agency_id, name):
    return {"schema": SCHEMA, "records_agency_id": agency_id, "slug": slug, "fact_agency": name, "agency_name": name,
            "portal": None, "review": {"method_dir": None, "method_python": None, "added_by": None},
            "rules_intake": {"law_specs": None, "policy_specs": None, "extra_specs": [], "f2_law_map": None, "sources": []},
            "compare_f3": {"applicability": None}}


def scaffold_profile(root, slug, records_agency_id, *, campaign_root=None):
    """Exclusively create profiles/<slug>.json.candidate (0600) with every operator-only field a null placeholder.

    Returns one fixed outcome code. Never writes a live profile or a CONFIG and never overwrites any existing entry.
    """
    tool = _flock_review()
    if not isinstance(slug, str) or not SLUG.fullmatch(slug):
        return "SCAFFOLD_SLUG_INVALID"
    if not isinstance(records_agency_id, str) or not AGENCY_ID.fullmatch(records_agency_id):
        return "SCAFFOLD_AGENCY_INVALID"
    try:
        root_fd = _open_dir(root)
    except OSError:
        return "SCAFFOLD_ROOT_INVALID"
    try:
        real = Path(os.path.abspath(root)).resolve()
        if tool.under(real, REPO) or _store_like(real):
            return "ROOT_REFUSED"
        campaign = _campaign_root(root_fd, campaign_root)
        if campaign is None:
            return "SCAFFOLD_CAMPAIGN_REQUIRED"
        name, code = _agency_name(campaign, records_agency_id)
        if code:
            return code
        data = (json.dumps(_scaffold_object(slug, records_agency_id, name), indent=2) + "\n").encode("utf-8")
        return _create_candidate(root_fd, slug, data)
    finally:
        os.close(root_fd)


def _create_candidate(root_fd, slug, data):
    live, cand = slug + ".json", slug + ".json" + CANDIDATE
    try:
        os.mkdir("profiles", 0o700, dir_fd=root_fd)
    except FileExistsError:
        pass
    except OSError:
        return "SCAFFOLD_WRITE_FAILED"
    try:
        pfd = os.open("profiles", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
    except OSError:
        return "SCAFFOLD_ROOT_INVALID"
    tmp = ".scaffold-%d-%s.tmp" % (os.getpid(), os.urandom(6).hex())
    owned = None
    try:
        for name in (live, cand):
            try:
                os.lstat(name, dir_fd=pfd)
                return "SCAFFOLD_EXISTS"
            except FileNotFoundError:
                pass
            except OSError:
                return "SCAFFOLD_EXISTS"
        try:  # complete content first in a private temp name, then publish with link(), which cannot replace anything
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=pfd)
            try:
                owned = os.fstat(fd)
                view = memoryview(data)
                while view:
                    view = view[os.write(fd, view):]
            finally:
                os.close(fd)
            os.link(tmp, cand, src_dir_fd=pfd, dst_dir_fd=pfd, follow_symlinks=False)
        except FileExistsError:
            return "SCAFFOLD_EXISTS"
        except OSError as exc:
            return "SCAFFOLD_EXISTS" if exc.errno == errno.EEXIST else "SCAFFOLD_WRITE_FAILED"
        try:  # a live profile that appeared meanwhile wins: withdraw only the inode this call created
            os.lstat(live, dir_fd=pfd)
        except FileNotFoundError:
            return "PROFILE_SCAFFOLDED"
        except OSError:
            pass
        _unlink_owned(pfd, cand, owned)
        return "SCAFFOLD_EXISTS"
    finally:
        _unlink_owned(pfd, tmp, owned)
        os.close(pfd)


def _unlink_owned(pfd, name, owned):
    try:
        seen = os.lstat(name, dir_fd=pfd)
        if owned is not None and (seen.st_ino, seen.st_dev) == (owned.st_ino, owned.st_dev):
            os.unlink(name, dir_fd=pfd)
    except OSError:
        pass


def build_config(root, profile, *, created_date):
    """Pure (reads only the inputs listing): the complete flock_review CONFIG dict for a validated profile."""
    slug, rv, rules = profile["slug"], profile["review"], profile["rules_intake"]
    out = Path(root) / "agencies" / slug / "review"
    FO, RO, CO = out / "facts", out / "rules", out / "compare"
    records = out / "inputs" / "records"
    sources = [{"id": "REC-" + p.stem[:12], "title": "record " + p.stem[:12], "path": str(p), "kind": "record_text",
                "page_marker_regex": PAGE_RE} for p in sorted(records.glob("*.txt")) if p.is_file()]
    return {
        "not_posted_analysis_only_ids": profile.get("not_posted_analysis_only_ids", []),
        "review": {"schema": 1, "slug": slug, "method_dir": rv["method_dir"], "method_python": rv["method_python"], "out": str(out),
                   "run_date": created_date, "added_by": rv["added_by"], "roster_doc_id": rv.get("roster_doc_id"),
                   "ocr_log_doc_ids": rv.get("ocr_log_doc_ids", []), "training_join": rv.get("training_join"),
                   "region_config": rv.get("region_config"), "verify_sample": rv.get("verify_sample", 30),
                   "f3_findings": str(CO / "F3-FINDINGS.yaml")},
        "facts_intake": {"fact_agency": profile["fact_agency"], "portal": profile["portal"], "requests": [], "record_roots": [],
                         "ocr_text_dirs": [], "log_doc_ids": profile.get("log_doc_ids", []),
                         "source_manifest": str(records / "MANIFEST.csv"), "extra_sources": sources, "fact_store": str(FO / "store")},
        # the method's rules_build reads added_by/added_at from this block: profile values win, else the review author and run date
        "rules_intake": {"added_by": rv["added_by"], "added_at": created_date, **rules, "out": str(RO), "agency_name": profile["agency_name"]},
        "compare": {"out": str(CO), "facts": str(FO / "store" / "agencies" / slug / "FACTS.csv"), "facts_meta": str(FO / "FACTS-META.csv"),
                    "requirements": str(RO / "REQUIREMENTS.csv"), "version_diff": str(RO / "VERSION-DIFF.csv"),
                    "log_by_user": str(FO / "LOG-SUMMARY-by-user.csv"), "assessments": str(CO / "ASSESSMENTS.yaml"),
                    "f3": {"store": str(CO / "store"), "law": str(RO / "F2" / "LAW.csv"),
                           "policy": str(RO / "F2" / "agencies" / slug / "POLICY.csv"), "slug": slug,
                           "golden": profile["compare_f3"].get("golden"), "applicability": profile["compare_f3"]["applicability"]}},
    }


def _flock_review():
    spec = importlib.util.spec_from_file_location("_analysis_flock_review", REPO / "tools" / "flock_review.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _store_like(root):
    return (root / "LAW.csv").exists() or any(root.glob("agencies/*/FACTS.csv"))


def write_config(root, slug, *, created_date=None, check_only=False):
    """Return one outcome code; the config file is replaced only when the new CONFIG passes flock_review --check."""
    root, tool = Path(root).resolve(), _flock_review()
    if tool.under(root, REPO) or _store_like(root):
        return "ROOT_REFUSED"
    if not SLUG.fullmatch(slug):
        return "PROFILE_MISSING"
    live = root / "profiles" / (slug + ".json")
    candidate = live.with_name(live.name + CANDIDATE)
    # a live profile always wins; the candidate is read only by --check-only and only when no live entry exists
    use_candidate = check_only and not os.path.lexists(live) and os.path.lexists(candidate)
    profile_path = candidate if use_candidate else live
    if not use_candidate and not live.is_file():
        return "PROFILE_MISSING"
    try:
        profile = _read_regular(profile_path)
        _validate(profile, slug)
        incomplete = _incomplete_fields(profile)
    except (OSError, ValueError, TypeError):
        return "PROFILE_INVALID"
    if incomplete:
        return "PROFILE_INCOMPLETE " + ",".join(incomplete)
    if use_candidate:
        return "PROFILE_CANDIDATE"
    target = root / "agencies" / slug / "review-config.json"
    run_date = created_date or datetime.date.today().isoformat()
    old = target.read_bytes() if target.is_file() else None
    try:  # the run date is stable: kept from the existing config, new only when none exists
        kept = json.loads(old)["review"]["run_date"]
        run_date = kept if tool.facts_tool.valid_date(kept) else run_date
    except (TypeError, ValueError, KeyError):
        pass
    data = (json.dumps(build_config(root, profile, created_date=run_date), indent=2, sort_keys=True) + "\n").encode()
    with tempfile.TemporaryDirectory(prefix="analysis-config-") as tmp:
        candidate = Path(tmp) / "review-config.json"
        candidate.write_bytes(data)
        errors = tool.check(tool.context(candidate))
    if errors:
        return "CONFIG_INVALID " + errors[0].split()[1]
    if data == old:
        return "CONFIG_UNCHANGED"
    if check_only:
        return "CONFIG_PENDING"
    tool.put(target, data)
    return "CONFIG_WRITTEN"


def main(argv=None):
    parser = argparse.ArgumentParser(prog="records analysis-config", description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("--slug", required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true",
                      help="the default, named for runbooks: the flock_review --check gate runs first and the CONFIG is written only if it passes")
    mode.add_argument("--check-only", action="store_true", help="validate the CONFIG without writing it")
    mode.add_argument("--scaffold", action="store_true",
                      help="exclusively create profiles/SLUG.json.candidate from the campaign's agency; operator fields stay null placeholders")
    parser.add_argument("--records-agency-id", help="with --scaffold: the campaign agency_id")
    parser.add_argument("--campaign-root", help="with --scaffold: the campaign (else the absolute path in ROOT/.campaign-root)")
    args = parser.parse_args(argv)
    if args.scaffold:
        if not args.records_agency_id:
            parser.error("--scaffold needs --records-agency-id")
        outcome = scaffold_profile(args.root, args.slug, args.records_agency_id, campaign_root=args.campaign_root)
        print(outcome)
        return 0 if outcome == "PROFILE_SCAFFOLDED" else 1
    if args.records_agency_id or args.campaign_root:
        parser.error("--records-agency-id and --campaign-root need --scaffold")
    outcome = write_config(args.root, args.slug, check_only=args.check_only)
    print(outcome)
    return 0 if outcome in ("CONFIG_WRITTEN", "CONFIG_UNCHANGED", "CONFIG_PENDING") else 1
