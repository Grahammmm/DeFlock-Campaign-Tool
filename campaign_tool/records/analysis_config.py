"""Build the flock_review CONFIG for one agency from an operator profile (records-review-profile-v1)."""
import argparse
import datetime
import importlib.util
import json
import re
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
    "rules_intake": ({"law_specs", "policy_specs", "extra_specs", "f2_law_map", "sources"}, set()),
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
        "rules_intake": {**rules, "out": str(RO), "agency_name": profile["agency_name"]},
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
    profile_path = root / "profiles" / (slug + ".json")
    if not SLUG.fullmatch(slug) or not profile_path.is_file():
        return "PROFILE_MISSING"
    try:
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
        _validate(profile, slug)
    except (OSError, ValueError):
        return "PROFILE_INVALID"
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
    parser.add_argument("--check-only", action="store_true", help="validate the CONFIG without writing it")
    args = parser.parse_args(argv)
    outcome = write_config(args.root, args.slug, check_only=args.check_only)
    print(outcome)
    return 0 if outcome in ("CONFIG_WRITTEN", "CONFIG_UNCHANGED", "CONFIG_PENDING") else 1
