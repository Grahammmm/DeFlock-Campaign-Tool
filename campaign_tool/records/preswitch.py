"""Inventory-only pre-switch checks; no records or stage execution."""
import argparse
import ast
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import warnings
from types import SimpleNamespace

OWN_ROOT = Path(__file__).resolve().parents[2]
INVENTORY = Path(__file__).with_name("preswitch_inventory.json")
MODULE = re.compile(r"campaign_tool(?:\.[A-Za-z_]\w*)+")
TOKEN = re.compile(r"[A-Za-z_]\w*")


def source_info(path):
    source = path.read_text(encoding="utf-8")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)  # source snippets are not diagnostics
        tree = ast.parse(source)
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    functions = {"<module>": hashlib.sha256(source.encode()).hexdigest()[:16]}
    names, sites = {}, []
    lines = source.splitlines(keepends=True)
    function_types = (ast.FunctionDef, ast.AsyncFunctionDef)
    for node in ast.walk(tree):
        if not isinstance(node, function_types):
            continue
        scope, cur = [node.name], node
        while cur in parents:
            cur = parents[cur]
            if isinstance(cur, (*function_types, ast.ClassDef)):
                scope.append(cur.name)
        name = names[node] = ".".join(reversed(scope))
        functions[name] = hashlib.sha256("".join(lines[node.lineno - 1:node.end_lineno]).encode()).hexdigest()[:16]
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        cur, qualname = node, "<module>"
        while cur in parents:
            cur = parents[cur]
            if cur in names:
                qualname = names[cur]
                break
        sites.append((node, qualname, functions[qualname]))
    return sites, functions


def module_path(root, module):
    base = root.joinpath(*module.split("."))
    for path in (base.with_suffix(".py"), base / "__init__.py"):
        if path.is_file():
            # Never follow a source link outside the admitted engine tree.
            path.resolve().relative_to(root.resolve())
            return path
    return None


def check(status, rows):
    return {"status": status, "rows": rows}


def deps(root, modules=(), inventory=None, *, own=True):
    """Read source only. No guard inference, code import, or test execution."""
    inventory = json.loads(INVENTORY.read_text()) if inventory is None else inventory
    pending = ["campaign_tool.records.run_stages", *modules]
    seen, rows, functions, sites = set(), [], {}, []
    while pending:
        module = pending.pop()
        if module in seen:
            continue
        seen.add(module)
        path = module_path(root, module)
        if path is None:
            continue
        parsed, functions[module] = source_info(path)
        # Python executes parent package initializers too.
        parts = module.split(".")
        pending.extend(".".join(parts[:i]) for i in range(1, len(parts)))
        for node, qualname, fingerprint in parsed:
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            else:
                package = module if path.name == "__init__.py" else module.rpartition(".")[0]
                base = (importlib.util.resolve_name("." * node.level + (node.module or ""), package)
                        if node.level else node.module or "")
                names = [base]
                if base.startswith("campaign_tool"):
                    names += [base + "." + alias.name for alias in node.names]
            for name in names:
                top = name.split(".")[0]
                if top == "campaign_tool":
                    pending.append(name)
                elif top and top not in sys.stdlib_module_names:
                    sites.append(dict(module=module, line=node.lineno, qualname=qualname,
                                      top=top, fingerprint=fingerprint))
    stale = set()
    for entry in inventory:
        path = module_path(root, entry["module"])
        if entry["module"] not in functions and path:
            _, functions[entry["module"]] = source_info(path)
        actual = functions.get(entry["module"], {}).get(entry["qualname"])
        key = (entry["module"], entry["qualname"], entry["top"], entry["fingerprint"])
        if actual is None or "sha256:" + actual != entry["fingerprint"]:
            stale.add(key)
            if own:
                rows.append({k: entry[k] for k in ("module", "qualname", "top")} | {"status": "STALE"})
    allowed = {(e["module"], e["qualname"], e["top"], e["fingerprint"])
               for e in inventory if e.get("when_missing") and e.get("proof")}
    available = {}
    for site in sorted(sites, key=lambda s: (s["module"], s["line"], s["top"])):
        top = site["top"]
        if top not in available:
            try:
                available[top] = importlib.util.find_spec(top) is not None
            except (ImportError, ValueError):
                available[top] = False
        if available[top]:
            continue
        key = (site["module"], site["qualname"], top, "sha256:" + site.pop("fingerprint"))
        site["status"] = "CONTAINED" if key in allowed - stale else "UNREVIEWED_MISSING"
        rows.append(site)
    return check("FAIL" if any(r["status"] != "CONTAINED" for r in rows) else "PASS", rows)


def load_plan(path):
    plan = json.loads(path.read_text())
    if not isinstance(plan, dict) or plan.get("target_config_version") != 1:
        raise ValueError("PLAN_INVALID")
    stages = plan.get("stages")
    if not isinstance(stages, list) or not stages:
        raise ValueError("PLAN_INVALID")
    result = []
    for stage in stages:
        argv = stage.get("argv") if isinstance(stage, dict) else None
        if (not isinstance(argv, list) or not all(isinstance(t, str) for t in argv)
                or argv.count("-m") != 1 or not TOKEN.fullmatch(stage.get("name", ""))):
            raise ValueError("PLAN_INVALID")
        index = argv.index("-m") + 1
        if index >= len(argv) or not MODULE.fullmatch(argv[index]):
            raise ValueError("PLAN_INVALID")
        args = argv[index + 1:]
        verbs = []
        for arg in args:
            if arg.startswith("-"):
                break
            if not TOKEN.fullmatch(arg):
                raise ValueError("PLAN_INVALID")
            verbs.append(arg)
        flags = [t.split("=", 1)[0] for t in args if t.startswith("--")]
        if any(not re.fullmatch(r"--[A-Za-z0-9_-]+", f) for f in flags):
            raise ValueError("PLAN_INVALID")
        result.append((stage["name"], argv[index], verbs, flags))
    return result


def help_internal(stderr, root):
    """Discard messages and source snippets; retain only exception and engine frame."""
    where = "unknown"
    for filename, function in re.findall(r'File "([^"]+)", line \d+, in ([\w.<>]+)', stderr):
        try:
            rel = Path(filename).resolve().relative_to(root / "campaign_tool")
        except ValueError:
            continue
        where = ".".join(("campaign_tool", *rel.with_suffix("").parts, function))
    kinds = re.findall(r"^([A-Za-z_]\w*(?:Error|Exception))(?::|$)", stderr, re.MULTILINE)
    return {"type": kinds[-1] if kinds else "HelpFailed", "where": where}


def stage_flags(root, stages, temp):
    rows = []
    for name, module, verbs, flags in stages:
        # Ignore the plan's docker/env/python prefix and every value. Never run a stage.
        env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
        env.update(TMPDIR=str(temp), HOME=str(temp))
        try:
            proc = subprocess.run([sys.executable, "-B", "-m", module, *verbs, "--help"],
                                  cwd=root, env=env, capture_output=True, text=True, timeout=30)
        except subprocess.TimeoutExpired:
            rows.append(dict(stage=name, module=module, status="STAGE_IMPORT_FAILED",
                             internal={"type": "TimeoutExpired", "where": "unknown"}))
            continue
        if proc.returncode:
            rows.append(dict(stage=name, module=module, status="STAGE_IMPORT_FAILED",
                             internal=help_internal(proc.stderr, root)))
            continue
        supported = set(re.findall(r"--[A-Za-z0-9_-]+", proc.stdout))
        unknown = sorted(set(flags) - supported)
        rows.extend(dict(stage=name, module=module, flag=f, status="STAGE_FLAG_UNKNOWN") for f in unknown)
        if not unknown:
            rows.append(dict(stage=name, module=module, status="PASS"))
    return check("FAIL" if any(r["status"] != "PASS" for r in rows) else "PASS", rows)


def probe_forms(temp):
    from . import extraction_routes, native_extract
    from .run_stages import internal_error

    forms = extraction_routes.SUPPORTED | extraction_routes.IMAGES | set(native_extract.FORM_PARSER) | {"xls"}
    rows = []
    missing = native_extract.missing_parsers()
    for form in sorted(forms):
        path = temp / ("synthetic." + form)
        path.write_bytes(b"synthetic\n")
        row = {"form": form, "status": "PASS"}
        for label, tools in (("ocr_off", None), ("ocr_on", {"probe": True})):
            pipeline = SimpleNamespace(ocr_tools=tools, _original=lambda _: {"storage_path": str(path)},
                                       _form=lambda *_: form, _alert=lambda *a, **k: None)
            try:
                detected, outcome = native_extract.classify_form(pipeline, "probe", missing=missing)
                if detected != form or (outcome is not None and outcome not in native_extract.OUTCOMES):
                    raise ValueError("FORM_OUTCOME_INVALID")
                row[label] = outcome or "PARSE"
            except Exception as error:  # report exactly what would escape the stage
                row[label] = "FORM_PROBE_RAISED"
                row["status"] = "FORM_PROBE_RAISED"
                row[label + "_internal"] = internal_error(error)
        rows.append(row)
    return check("FAIL" if any(r["status"] != "PASS" for r in rows) else "PASS", rows)


def run(root=OWN_ROOT, stages=None):
    root = root.resolve()
    own = root == OWN_ROOT
    with tempfile.TemporaryDirectory(prefix="preswitch-") as directory:
        temp = Path(directory)
        checks = {"A": deps(root, [s[1] for s in stages or []], own=own),
                  "B": stage_flags(root, stages, temp) if stages is not None else check("skipped", []),
                  "C": probe_forms(temp) if own else check("skipped", [{"status": "OTHER_TREE"}])}
    return {"schema_version": 1, "checks": checks,
            "overall": "FAIL" if any(c["status"] == "FAIL" for c in checks.values()) else "PASS"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--engine-root", type=Path, default=OWN_ROOT)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        if module_path(args.engine_root.resolve(), "campaign_tool.records.run_stages") is None:
            raise ValueError("ENGINE_INVALID")
        stages = load_plan(args.plan) if args.plan else None
    except (ValueError, OSError, TypeError, KeyError):
        parser.exit(2, "USAGE_INVALID\n")
    try:
        report = run(args.engine_root, stages)
    except Exception as error:
        # No source, paths, parser snippets or exception messages in diagnostics.
        report = {"schema_version": 1, "checks": {k: check("FAIL", [{"status": "CHECK_RAISED",
                  "internal": {"type": type(error).__name__, "where": "unknown"}}]) for k in "ABC"},
                  "overall": "FAIL"}
    if args.json:
        print(json.dumps(report, sort_keys=True))
    else:
        for name, result in report["checks"].items():
            print(name, result["status"], len(result["rows"]))
            for row in result["rows"]:
                if name == "A":
                    print(f"{row['module']}:{row.get('line', 0)} {row['qualname']} {row['top']} {row['status']}")
                else:
                    print(json.dumps(row, sort_keys=True))
        print(report["overall"])
    return 0 if report["overall"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
